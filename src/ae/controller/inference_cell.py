"""InferenceCell reconcile lane (experimental)."""

from __future__ import annotations

import hashlib
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - Python < 3.11 compatibility
    class StrEnum(str, Enum):
        """Backport-compatible StrEnum shim."""
from typing import Protocol

import requests

from ae._utc import UTC
from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    InferenceCellManifest,
    InferenceCellSetManifest,
    InferenceCellSpec,
    InferenceExecutorSpec,
    InferenceMemberSpec,
    LinkMetricSample,
    app_key,
)
from ae.controller.state import InferenceCellRecord, InferenceCellSetRecord, SQLiteStateStore
from ae.runtime import RemoteRuntime, RuntimeAdapter, StubRuntime

_NO_UPDATE = object()


class CellPhase(StrEnum):
    PENDING = "PENDING"
    ADMITTING = "ADMITTING"
    RESERVING = "RESERVING"
    FABRIC = "FABRIC"
    STARTING_WORKERS = "STARTING_WORKERS"
    STARTING_LEADER = "STARTING_LEADER"
    JOINING = "JOINING"
    READY = "READY"
    RESTARTING = "RESTARTING"
    FAILED = "FAILED"


@dataclass(slots=True, frozen=True)
class StagePlacement:
    stage: int
    site_id: str
    node_id: str
    gpu_indices: list[int]

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "site_id": self.site_id,
            "node_id": self.node_id,
            "gpu_indices": list(self.gpu_indices),
        }

    @classmethod
    def from_dict(cls, data: dict) -> StagePlacement:
        return cls(
            stage=int(data.get("stage", 0)),
            site_id=str(data.get("site_id") or ""),
            node_id=str(data.get("node_id") or ""),
            gpu_indices=[int(v) for v in list(data.get("gpu_indices") or [])],
        )


@dataclass(slots=True, frozen=True)
class EdgeRequirement:
    from_stage: int
    to_stage: int
    from_site: str
    to_site: str
    return_path: bool = False

    def to_dict(self) -> dict:
        return {
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "from_site": self.from_site,
            "to_site": self.to_site,
            "return_path": self.return_path,
        }


@dataclass(slots=True)
class AdmissionReport:
    admitted: bool
    reasons: list[str]
    b_boundaries: int
    r_return: int
    net_cost_p95_ms: float
    jitter_p95_ms: float
    loss_pct: float
    required_edges: list[dict]

    def to_dict(self) -> dict:
        return {
            "admitted": bool(self.admitted),
            "reasons": list(self.reasons),
            "b_boundaries": int(self.b_boundaries),
            "r_return": int(self.r_return),
            "net_cost_p95_ms": round(float(self.net_cost_p95_ms), 4),
            "jitter_p95_ms": round(float(self.jitter_p95_ms), 4),
            "loss_pct": round(float(self.loss_pct), 6),
            "required_edges": list(self.required_edges),
        }


@dataclass(slots=True)
class LeaseBundle:
    lease_id: str
    slots: list[tuple[str, int]]
    master_port: int | None
    api_port: int
    locked_nodes: list[str]


@dataclass(slots=True)
class FabricSessionInfo:
    session_id: str
    ifname: str
    member_ips: dict[str, str]
    expires_at: datetime
    policy_mode: str
    allowed_rules: list[dict]
    mode: str = "lan_direct"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "ifname": self.ifname,
            "member_ips": dict(self.member_ips),
            "expires_at": self.expires_at.isoformat(),
            "policy_mode": self.policy_mode,
            "allowed_rules": list(self.allowed_rules),
            "mode": self.mode,
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _truthy_env(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _site_order(members: list[InferenceMemberSpec]) -> list[str]:
    sites: list[str] = []
    for member in members:
        if member.site_id not in sites:
            sites.append(member.site_id)
    return sites


def _members_by_site(members: list[InferenceMemberSpec]) -> dict[str, list[InferenceMemberSpec]]:
    out: dict[str, list[InferenceMemberSpec]] = {}
    for member in members:
        out.setdefault(member.site_id, []).append(member)
    return out


class StagePlanner:
    """Deterministic stage placement helper."""

    @staticmethod
    def plan(spec: InferenceCellSpec) -> list[StagePlacement]:
        tp = int(spec.parallelism.tp)
        pp = int(spec.parallelism.pp)
        if tp <= 0 or pp <= 0:
            raise ValueError("parallelism tp/pp must be > 0")
        eligible = [m for m in spec.members if int(m.gpu_count) >= tp]
        if len(eligible) < pp:
            raise ValueError(f"need {pp} members with gpu_count >= tp={tp}, found {len(eligible)}")

        sites = _site_order(eligible)
        if not sites:
            raise ValueError("no eligible sites for placement")

        # v0 topology defaults: PP=2 split across two sites, PP=4 grouped 2+2 when enabled.
        stage_sites: list[str]
        if pp == 2 and len(sites) >= 2:
            stage_sites = [sites[0], sites[1]]
        elif pp == 4 and spec.placement_policy.pack_stages_by_site and len(sites) >= 2:
            stage_sites = [sites[0], sites[0], sites[1], sites[1]]
        else:
            stage_sites = [sites[i % len(sites)] for i in range(pp)]

        by_site = _members_by_site(eligible)
        used_nodes: set[str] = set()
        placements: list[StagePlacement] = []
        for stage, site_id in enumerate(stage_sites):
            candidates = by_site.get(site_id, [])
            chosen = None
            for candidate in candidates:
                if candidate.node_id in used_nodes:
                    continue
                chosen = candidate
                break
            if chosen is None:
                raise ValueError(f"insufficient unique nodes in site {site_id!r} for stage {stage}")
            used_nodes.add(chosen.node_id)
            placements.append(
                StagePlacement(
                    stage=stage,
                    site_id=chosen.site_id,
                    node_id=chosen.node_id,
                    gpu_indices=list(range(tp)),
                )
            )
        return placements

    @staticmethod
    def gpu_slots(placements: list[StagePlacement]) -> list[tuple[str, int]]:
        slots: list[tuple[str, int]] = []
        for placement in placements:
            for gpu in placement.gpu_indices:
                slots.append((placement.node_id, int(gpu)))
        return slots


class BoundaryBudgetAdmission:
    """Boundary-based admission evaluator for cross-site PP."""

    @staticmethod
    def _metric_lookup(
        metrics: list[LinkMetricSample], from_site: str, to_site: str
    ) -> LinkMetricSample | None:
        exact = [m for m in metrics if m.from_site == from_site and m.to_site == to_site]
        rev = [m for m in metrics if m.from_site == to_site and m.to_site == from_site]
        candidates = exact or rev
        if not candidates:
            return None
        # Use the worst sample for conservative admission.
        return sorted(
            candidates,
            key=lambda m: (float(m.rtt_p95_ms), float(m.jitter_p95_ms), float(m.loss_pct)),
            reverse=True,
        )[0]

    @classmethod
    def evaluate(cls, spec: InferenceCellSpec, placements: list[StagePlacement]) -> AdmissionReport:
        reasons: list[str] = []
        ordered = sorted(placements, key=lambda p: p.stage)
        if len(ordered) < 2:
            return AdmissionReport(
                admitted=True,
                reasons=[],
                b_boundaries=0,
                r_return=0,
                net_cost_p95_ms=0.0,
                jitter_p95_ms=0.0,
                loss_pct=0.0,
                required_edges=[],
            )

        required: list[EdgeRequirement] = []
        b = 0
        for idx in range(len(ordered) - 1):
            left = ordered[idx]
            right = ordered[idx + 1]
            if left.site_id == right.site_id:
                continue
            b += 1
            required.append(
                EdgeRequirement(
                    from_stage=left.stage,
                    to_stage=right.stage,
                    from_site=left.site_id,
                    to_site=right.site_id,
                    return_path=False,
                )
            )

        r = 1 if ordered[-1].site_id != ordered[0].site_id else 0
        if r:
            required.append(
                EdgeRequirement(
                    from_stage=ordered[-1].stage,
                    to_stage=ordered[0].stage,
                    from_site=ordered[-1].site_id,
                    to_site=ordered[0].site_id,
                    return_path=True,
                )
            )

        net_cost = 0.0
        max_jitter = 0.0
        max_loss = 0.0
        required_rows: list[dict] = []
        metrics = list(spec.link_metrics or [])
        for edge in required:
            metric = cls._metric_lookup(metrics, edge.from_site, edge.to_site)
            if metric is None:
                reasons.append(
                    f"missing link metric {edge.from_site}->{edge.to_site} for required edge "
                    f"{edge.from_stage}->{edge.to_stage}"
                )
                continue
            rtt = float(metric.rtt_p95_ms)
            jitter = float(metric.jitter_p95_ms)
            loss = float(metric.loss_pct)
            net_cost += rtt / 2.0
            max_jitter = max(max_jitter, jitter)
            max_loss = max(max_loss, loss)
            required_rows.append(
                {
                    **edge.to_dict(),
                    "rtt_p95_ms": rtt,
                    "jitter_p95_ms": jitter,
                    "loss_pct": loss,
                }
            )
            if rtt > float(spec.link_budget.rtt_p95_ms_max):
                reasons.append(
                    f"edge {edge.from_site}->{edge.to_site} rtt_p95={rtt}ms exceeds "
                    f"{spec.link_budget.rtt_p95_ms_max}ms"
                )
            if jitter > float(spec.link_budget.jitter_p95_ms_max):
                reasons.append(
                    f"edge {edge.from_site}->{edge.to_site} jitter_p95={jitter}ms exceeds "
                    f"{spec.link_budget.jitter_p95_ms_max}ms"
                )
            if loss > float(spec.link_budget.loss_pct_max):
                reasons.append(
                    f"edge {edge.from_site}->{edge.to_site} loss={loss}% exceeds "
                    f"{spec.link_budget.loss_pct_max}%"
                )

        perf = spec.perf_budget
        if perf.compute_token_ms_p50 is not None and perf.compute_token_ms_p50 > 0:
            compute_ms = float(perf.compute_token_ms_p50)
            if net_cost > float(perf.alpha_net) * compute_ms:
                reasons.append(
                    f"net_cost_p95={net_cost:.3f}ms exceeds "
                    f"alpha_net({perf.alpha_net})*compute({compute_ms}ms)"
                )
            if max_jitter > float(perf.beta_jitter) * compute_ms:
                reasons.append(
                    f"jitter_p95={max_jitter:.3f}ms exceeds "
                    f"beta_jitter({perf.beta_jitter})*compute({compute_ms}ms)"
                )
            if max_loss > float(perf.loss_pct_max):
                reasons.append(f"loss={max_loss:.6f}% exceeds perf loss cap {perf.loss_pct_max}%")

        return AdmissionReport(
            admitted=len(reasons) == 0,
            reasons=reasons,
            b_boundaries=b,
            r_return=r,
            net_cost_p95_ms=net_cost,
            jitter_p95_ms=max_jitter,
            loss_pct=max_loss,
            required_edges=required_rows,
        )


class FabricBroker(Protocol):
    def create_session(
        self,
        *,
        cell_key: str,
        policy_mode: str,
        mode: str,
        ttl_seconds: int,
        placements: list[StagePlacement],
        allowed_rules: list[dict],
    ) -> FabricSessionInfo: ...

    def teardown_session(self, session_id: str) -> None: ...


class FabricAgentClient(Protocol):
    def ensure_session(self, node_id: str, session: FabricSessionInfo) -> bool: ...

    def teardown_session(self, node_id: str, session_id: str) -> None: ...


class NoopFabricAgentClient:
    """Plan-time fabric agent that reports success."""

    def ensure_session(self, node_id: str, session: FabricSessionInfo) -> bool:
        _ = (node_id, session)
        return True

    def teardown_session(self, node_id: str, session_id: str) -> None:
        _ = (node_id, session_id)


class HttpFabricAgentClient:
    """Node-agent-backed fabric session client."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self._store = store
        self._token = (
            os.getenv("AE_AGENT_TOKEN")
            or os.getenv("AE_AGENT_API_TOKEN")
            or os.getenv("AE_INFERENCE_AGENT_TOKEN")
        )
        self._timeout = float(os.getenv("AE_INFERENCE_AGENT_TIMEOUT", "5") or 5)

    def _endpoint_for_node(self, node_id: str) -> str | None:
        rec = self._store.get_node(node_id)
        if rec is None:
            return None
        node, _status = rec
        endpoint = str(node.endpoint or "").strip()
        if not endpoint:
            return None
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint.rstrip("/")
        return f"http://{endpoint.rstrip('/')}"

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"X-Agent-Token": self._token}

    def ensure_session(self, node_id: str, session: FabricSessionInfo) -> bool:
        endpoint = self._endpoint_for_node(node_id)
        if not endpoint:
            return False
        try:
            resp = requests.post(
                endpoint + "/v1/fabric/ensure_session",
                json={"node_id": node_id, "session": session.to_dict()},
                headers=self._headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return False
            payload = resp.json() if resp.content else {}
            return bool(payload.get("ok", False))
        except Exception:
            return False

    def teardown_session(self, node_id: str, session_id: str) -> None:
        endpoint = self._endpoint_for_node(node_id)
        if not endpoint:
            return
        try:
            requests.post(
                endpoint + "/v1/fabric/teardown_session",
                json={"node_id": node_id, "session_id": session_id},
                headers=self._headers(),
                timeout=self._timeout,
            )
        except Exception:
            return


class LocalFabricBroker:
    """State-store-backed fabric broker stub for v0 control-plane bring-up."""

    def __init__(self, store: SQLiteStateStore):
        self._store = store

    def create_session(
        self,
        *,
        cell_key: str,
        policy_mode: str,
        mode: str,
        ttl_seconds: int,
        placements: list[StagePlacement],
        allowed_rules: list[dict],
    ) -> FabricSessionInfo:
        ttl = max(30, int(ttl_seconds))
        session_id = f"s-{uuid.uuid4().hex[:12]}"
        ifname = f"wg-cell-{hashlib.sha256(cell_key.encode('utf-8')).hexdigest()[:8]}"
        digest = hashlib.sha256(cell_key.encode("utf-8")).hexdigest()
        octet3 = int(digest[:2], 16) % 200 + 20
        member_ips: dict[str, str] = {}
        members: list[dict] = []
        for idx, placement in enumerate(sorted(placements, key=lambda p: p.node_id), start=1):
            ip = f"10.250.{octet3}.{idx}"
            member_ips[placement.node_id] = ip
            members.append(
                {
                    "node_id": placement.node_id,
                    "site_id": placement.site_id,
                    "fabric_ip": ip,
                    "stage": placement.stage,
                }
            )
        expires_at = _now() + timedelta(seconds=ttl)
        self._store.upsert_fabric_session(
            session_id=session_id,
            cell_key=cell_key,
            policy_mode=policy_mode,
            members=members,
            allowed_rules=allowed_rules,
            status="active",
            expires_at=expires_at,
        )
        return FabricSessionInfo(
            session_id=session_id,
            ifname=ifname,
            member_ips=member_ips,
            expires_at=expires_at,
            policy_mode=policy_mode,
            allowed_rules=list(allowed_rules),
            mode=mode,
        )

    def teardown_session(self, session_id: str) -> None:
        self._store.delete_fabric_session(session_id)


class InferenceLeaseManager:
    """Lease operations for GPU/port/node resources."""

    def __init__(self, store: SQLiteStateStore):
        self._store = store

    def reserve(
        self,
        *,
        cell_key: str,
        spec: InferenceCellSpec,
        placements: list[StagePlacement],
    ) -> LeaseBundle | None:
        lease_id = f"l-{uuid.uuid4().hex[:12]}"
        ttl = max(60, int(spec.fabric.ttl_seconds))
        slots = StagePlanner.gpu_slots(placements)
        if not self._store.acquire_inference_gpu_leases(
            lease_id=lease_id,
            cell_key=cell_key,
            slots=slots,
            ttl_seconds=ttl,
        ):
            return None

        master_stage = int(spec.rendezvous.master_stage)
        stage0 = next((p for p in placements if p.stage == master_stage), placements[0])
        master_port = None
        fallback_mode = str(getattr(spec.executor, "fallback_mode", "none") or "none")
        reserve_mp_port = spec.executor.type == "mp" or fallback_mode == "mp_on_failure"
        if reserve_mp_port:
            master_port = self._store.reserve_inference_port_from_range(
                lease_id=lease_id,
                cell_key=cell_key,
                node_id=stage0.node_id,
                start=int(spec.rendezvous.master_port_min),
                end=int(spec.rendezvous.master_port_max),
                ttl_seconds=ttl,
            )
            if master_port is None:
                self.release(LeaseBundle(lease_id, slots, None, 0, []))
                return None

        api_port = self._store.reserve_inference_port_from_range(
            lease_id=lease_id,
            cell_key=cell_key,
            node_id=stage0.node_id,
            start=int(spec.rendezvous.api_port_min),
            end=int(spec.rendezvous.api_port_max),
            ttl_seconds=ttl,
        )
        if api_port is None:
            self.release(LeaseBundle(lease_id, slots, master_port, 0, []))
            return None

        locked_nodes: list[str] = []
        if spec.fabric.policy_mode == "strict_ports":
            node_ids = sorted({p.node_id for p in placements})
            if not self._store.acquire_inference_node_locks(
                lease_id=lease_id,
                cell_key=cell_key,
                node_ids=node_ids,
                ttl_seconds=ttl,
            ):
                self.release(LeaseBundle(lease_id, slots, master_port, api_port, []))
                return None
            locked_nodes = node_ids

        return LeaseBundle(
            lease_id=lease_id,
            slots=slots,
            master_port=master_port,
            api_port=int(api_port),
            locked_nodes=locked_nodes,
        )

    def release(self, lease: LeaseBundle) -> None:
        if not lease or not lease.lease_id:
            return
        self._store.release_inference_port_leases(lease.lease_id)
        self._store.release_inference_gpu_leases(lease.lease_id)
        self._store.release_inference_node_locks(lease.lease_id)


def _build_allowed_rules(spec: InferenceExecutorSpec, lease: LeaseBundle) -> list[dict]:
    rules: list[dict] = [{"proto": "tcp", "port": int(lease.api_port)}]
    if spec.type == "mp":
        if lease.master_port is not None:
            rules.append({"proto": "tcp", "port": int(lease.master_port)})
        return rules
    if lease.master_port is not None:
        # Reserve mp rendezvous even for Ray when fallback is enabled.
        rules.append({"proto": "tcp", "port": int(lease.master_port)})
    rp = spec.ray_ports
    rules.extend(
        [
            {"proto": "tcp", "port": int(rp.head_port)},
            {"proto": "tcp", "port": int(rp.node_manager_port)},
            {"proto": "tcp", "port": int(rp.object_manager_port)},
            {"proto": "tcp", "port": int(rp.runtime_env_agent_port)},
            {"proto": "tcp", "range": [int(rp.min_worker_port), int(rp.max_worker_port)]},
        ]
    )
    if rp.ray_client_server_port is not None:
        rules.append({"proto": "tcp", "port": int(rp.ray_client_server_port)})
    if rp.metrics_export_port is not None:
        rules.append({"proto": "tcp", "port": int(rp.metrics_export_port)})
    if rp.include_dashboard:
        rules.append({"proto": "tcp", "port": int(rp.dashboard_port)})
    return rules


def _make_condition(existing: dict, name: str, ok: bool, message: str) -> dict:
    out = dict(existing)
    out[name] = {
        "status": bool(ok),
        "message": str(message),
        "last_transition_at": _now().isoformat(),
    }
    return out


class InferenceCellController:
    """Experimental reconcile loop for inference cells."""

    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        broker: FabricBroker | None = None,
        agent: FabricAgentClient | None = None,
        runtime: RuntimeAdapter | None = None,
    ) -> None:
        self._store = store
        self._broker = broker or LocalFabricBroker(store)
        self._execution_enabled = _truthy_env("AE_INFERENCE_EXPERIMENTAL", "0")
        if agent is None:
            self._agent = (
                HttpFabricAgentClient(store) if self._execution_enabled else NoopFabricAgentClient()
            )
        else:
            self._agent = agent
        self._local_runtime = runtime or StubRuntime()
        self._node_runtimes: dict[str, RemoteRuntime] = {}
        self._leases = InferenceLeaseManager(store)

    def _lease_from_allocations(self, alloc: dict) -> LeaseBundle | None:
        lease_id = str(alloc.get("lease_id") or "")
        if not lease_id:
            return None
        slots = []
        for item in list(alloc.get("gpu_slots") or []):
            if not isinstance(item, dict):
                continue
            slots.append((str(item.get("node_id") or ""), int(item.get("gpu_index") or 0)))
        locked_nodes = [str(v) for v in list(alloc.get("locked_nodes") or [])]
        return LeaseBundle(
            lease_id=lease_id,
            slots=slots,
            master_port=(
                int(alloc.get("master_port")) if alloc.get("master_port") is not None else None
            ),
            api_port=int(alloc.get("api_port") or 0),
            locked_nodes=locked_nodes,
        )

    def _record(self, manifest: InferenceCellManifest) -> InferenceCellRecord:
        rec = self._store.get_inference_cell(
            manifest.metadata.name, namespace=manifest.metadata.namespace
        )
        if rec is None:
            raise RuntimeError(f"inference cell {manifest.metadata.name} not found after register")
        return rec

    def _log_event(self, manifest: InferenceCellManifest, event_type: str, message: str) -> None:
        self._store.record_inference_cell_event(
            manifest.metadata.name,
            manifest.metadata.namespace,
            event_type=event_type,
            message=message,
        )

    def _update(
        self,
        manifest: InferenceCellManifest,
        *,
        phase: CellPhase | None = None,
        allocations: dict | None = None,
        admission: dict | None = None,
        conditions: dict | None = None,
        restarts: int | None = None,
        last_error: str | None | object = _NO_UPDATE,
    ) -> InferenceCellRecord:
        kwargs = {
            "phase": phase.value if phase else None,
            "allocations": allocations,
            "admission": admission,
            "conditions": conditions,
            "restarts": restarts,
        }
        if last_error is _NO_UPDATE:
            self._store.update_inference_cell_status(
                manifest.metadata.name,
                namespace=manifest.metadata.namespace,
                **kwargs,
            )
        else:
            self._store.update_inference_cell_status(
                manifest.metadata.name,
                namespace=manifest.metadata.namespace,
                last_error=last_error,
                **kwargs,
            )
        return self._record(manifest)

    def _render_workers(
        self,
        manifest: InferenceCellManifest,
        placements: list[StagePlacement],
        *,
        executor_type: str | None = None,
    ) -> list[dict]:
        run_type = str(executor_type or manifest.spec.executor.type)
        out: list[dict] = []
        for placement in placements:
            if int(placement.stage) == int(manifest.spec.rendezvous.master_stage):
                continue
            if run_type == "ray":
                out.append(
                    {
                        "name": f"{manifest.metadata.name}-ray-worker-{placement.node_id}",
                        "node_id": placement.node_id,
                        "site_id": placement.site_id,
                        "type": "ray-worker",
                        "stage": placement.stage,
                    }
                )
            else:
                out.append(
                    {
                        "name": f"{manifest.metadata.name}-mp-stage{placement.stage}",
                        "node_id": placement.node_id,
                        "site_id": placement.site_id,
                        "type": "mp-stage",
                        "stage": placement.stage,
                        "headless": True,
                    }
                )
        return out

    def _render_leader(
        self,
        manifest: InferenceCellManifest,
        placements: list[StagePlacement],
        alloc: dict,
        *,
        executor_type: str | None = None,
    ) -> dict:
        run_type = str(executor_type or manifest.spec.executor.type)
        master_stage = int(manifest.spec.rendezvous.master_stage)
        leader = next((p for p in placements if p.stage == master_stage), placements[0])
        if run_type == "ray":
            return {
                "name": f"{manifest.metadata.name}-ray-head",
                "node_id": leader.node_id,
                "site_id": leader.site_id,
                "type": "ray-head",
                "stage": leader.stage,
                "api_port": int(alloc.get("api_port") or 0),
            }
        return {
            "name": f"{manifest.metadata.name}-mp-stage{leader.stage}",
            "node_id": leader.node_id,
            "site_id": leader.site_id,
            "type": "mp-stage",
            "stage": leader.stage,
            "headless": False,
            "master_port": int(alloc.get("master_port") or 0),
            "api_port": int(alloc.get("api_port") or 0),
        }

    def _alloc_stage_placements(self, alloc: dict) -> list[StagePlacement]:
        out: list[StagePlacement] = []
        for item in list(alloc.get("placements") or []):
            if not isinstance(item, dict):
                continue
            out.append(StagePlacement.from_dict(item))
        return sorted(out, key=lambda p: p.stage)

    def _runtime_for_node(self, node_id: str) -> tuple[RemoteRuntime, str]:
        rec = self._store.get_node(node_id)
        if rec is None:
            raise RuntimeError(f"node {node_id} not registered")
        node, _status = rec
        endpoint = str(node.endpoint or "").strip()
        if not endpoint:
            raise RuntimeError(f"node {node_id} has no agent endpoint")
        if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
            endpoint = f"http://{endpoint}"
        cached = self._node_runtimes.get(endpoint)
        if cached is not None:
            return cached, endpoint
        runtime = RemoteRuntime(endpoint, self._local_runtime)
        self._node_runtimes[endpoint] = runtime
        return runtime, endpoint

    def _gpu_count_from_labels(self, labels: dict | None) -> int | None:
        if not labels:
            return None
        for key in ("gpu.count", "nvidia.gpu.count", "gpu_count"):
            raw = labels.get(key)
            if raw in (None, ""):
                continue
            if isinstance(raw, int):
                return raw
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return None

    def _validate_members_for_execution(self, spec: InferenceCellSpec) -> list[str]:
        errors: list[str] = []
        for member in list(spec.members or []):
            rec = self._store.get_node(member.node_id)
            if rec is None:
                errors.append(f"member {member.node_id} is not registered")
                continue
            node, status = rec
            if status is None or str(status.status or "").lower() != "ready":
                errors.append(f"member {member.node_id} is not ready")
            endpoint = str(node.endpoint or "").strip()
            if not endpoint:
                errors.append(f"member {member.node_id} has no agent endpoint")
            gpu_count = self._gpu_count_from_labels(node.labels)
            if gpu_count is not None and gpu_count < int(member.gpu_count):
                errors.append(
                    "member "
                    f"{member.node_id} gpu.count={gpu_count} < "
                    f"manifest gpuCount={member.gpu_count}"
                )
        return errors

    def _active_executor(self, manifest: InferenceCellManifest, alloc: dict) -> str:
        raw = (
            str(alloc.get("active_executor") or manifest.spec.executor.type or "ray")
            .strip()
            .lower()
        )
        return "mp" if raw == "mp" else "ray"

    def _supports_mp_fallback(self, manifest: InferenceCellManifest) -> bool:
        mode = (
            str(getattr(manifest.spec.executor, "fallback_mode", "none") or "none").strip().lower()
        )
        return mode == "mp_on_failure"

    def _env_list(self, values: dict[str, str | int]) -> list[dict[str, str]]:
        return [{"name": str(k), "value": str(v)} for k, v in values.items()]

    def _workload_manifest(
        self,
        *,
        name: str,
        namespace: str,
        image: str,
        command: list[str],
        args: list[str],
        env: dict[str, str | int],
        api_port: int | None = None,
        runtime_class_name: str | None = None,
    ) -> AppManifest:
        payload: dict = {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "image": image,
                "replicas": 1,
                "command": list(command),
                "args": list(args),
                "env": self._env_list(env),
            },
        }
        if api_port:
            payload["spec"]["ports"] = [{"name": "http", "containerPort": int(api_port)}]
            payload["spec"]["health"] = {
                "readiness": {"httpGet": {"path": "/health", "port": int(api_port)}},
                "liveness": {"httpGet": {"path": "/health", "port": int(api_port)}},
            }
        if runtime_class_name:
            payload["spec"]["runtimeClassName"] = str(runtime_class_name)
        return AppManifest.model_validate(payload)

    @staticmethod
    def _executor_runtime_class(manifest: InferenceCellManifest) -> str | None:
        raw = str(getattr(manifest.spec.executor, "runtime_class_name", "") or "").strip()
        if raw:
            return raw
        env = str(os.getenv("AE_INFERENCE_RUNTIME_CLASS", "") or "").strip()
        return env or None

    def _mp_stage_manifest(
        self,
        manifest: InferenceCellManifest,
        placement: StagePlacement,
        alloc: dict,
    ) -> AppManifest:
        namespace = manifest.metadata.namespace or DEFAULT_NAMESPACE
        name = f"{manifest.metadata.name}-mp-stage{placement.stage}"
        model_path = str(manifest.spec.model.local_path)
        tp = int(manifest.spec.parallelism.tp)
        pp = int(manifest.spec.parallelism.pp)
        api_port = int(alloc.get("api_port") or 0)
        master_port = int(alloc.get("master_port") or 0)
        is_leader = int(placement.stage) == int(manifest.spec.rendezvous.master_stage)
        args = [
            (
                "python -m vllm.entrypoints.openai.api_server "
                '--model "$MODEL_PATH" '
                "--distributed-executor-backend mp "
                '--tensor-parallel-size "$TP" '
                '--pipeline-parallel-size "$PP" '
                '--nnodes "$PP" '
                '--node-rank "$STAGE" '
                '--master-addr "$MASTER_ADDR" '
                '--master-port "$MASTER_PORT" '
                '--port "$API_PORT" '
                "--host 0.0.0.0 " + (" " if is_leader else "--headless ") + "|| sleep infinity"
            )
        ]
        env = {
            "MODEL_PATH": model_path,
            "TP": tp,
            "PP": pp,
            "STAGE": int(placement.stage),
            "MASTER_ADDR": str(alloc.get("master_addr") or ""),
            "MASTER_PORT": master_port,
            "API_PORT": api_port,
            "CUDA_VISIBLE_DEVICES": ",".join(str(i) for i in placement.gpu_indices),
            "NCCL_SOCKET_IFNAME": str(alloc.get("fabric_ifname") or "eth0"),
        }
        runtime_class_name = self._executor_runtime_class(manifest)
        return self._workload_manifest(
            name=name,
            namespace=namespace,
            image=str(manifest.spec.executor.mp_image),
            command=["/bin/sh", "-lc"],
            args=args,
            env=env,
            api_port=(api_port if is_leader else None),
            runtime_class_name=runtime_class_name,
        )

    def _ray_worker_manifest(
        self,
        manifest: InferenceCellManifest,
        placement: StagePlacement,
        alloc: dict,
        *,
        leader: bool,
    ) -> AppManifest:
        namespace = manifest.metadata.namespace or DEFAULT_NAMESPACE
        role = "head" if leader else "worker"
        if leader:
            name = f"{manifest.metadata.name}-ray-head"
        else:
            name = f"{manifest.metadata.name}-ray-worker-{placement.node_id}"
        rp = manifest.spec.executor.ray_ports
        base_cmd = (
            "ray start "
            + (
                "--head " f"--port {int(rp.head_port)} "
                if leader
                else f'--address "$MASTER_ADDR:{int(rp.head_port)}" '
            )
            + '--node-ip-address "$FABRIC_IP" '
            + f"--node-manager-port {int(rp.node_manager_port)} "
            + f"--object-manager-port {int(rp.object_manager_port)} "
            + f"--runtime-env-agent-port {int(rp.runtime_env_agent_port)} "
            + f"--min-worker-port {int(rp.min_worker_port)} "
            + f"--max-worker-port {int(rp.max_worker_port)} "
            + ("--include-dashboard=true " if bool(rp.include_dashboard and leader) else "")
            + ("--dashboard-host 0.0.0.0 " if bool(rp.include_dashboard and leader) else "")
            + (
                "--dashboard-port " + str(int(rp.dashboard_port)) + " "
                if bool(rp.include_dashboard and leader)
                else ""
            )
            + "--block"
        )
        env = {
            "MASTER_ADDR": str(alloc.get("master_addr") or ""),
            "FABRIC_IP": str((alloc.get("member_fabric_ips") or {}).get(placement.node_id, "")),
            "RAY_ROLE": role,
        }
        runtime_class_name = self._executor_runtime_class(manifest)
        return self._workload_manifest(
            name=name,
            namespace=namespace,
            image=str(manifest.spec.executor.ray_image),
            command=["/bin/sh", "-lc"],
            args=[base_cmd],
            env=env,
            api_port=(int(alloc.get("api_port") or 0) if leader else None),
            runtime_class_name=runtime_class_name,
        )

    def _ray_launcher_manifest(
        self,
        manifest: InferenceCellManifest,
        placement: StagePlacement,
        alloc: dict,
    ) -> AppManifest:
        namespace = manifest.metadata.namespace or DEFAULT_NAMESPACE
        name = f"{manifest.metadata.name}-ray-launcher"
        tp = int(manifest.spec.parallelism.tp)
        pp = int(manifest.spec.parallelism.pp)
        api_port = int(alloc.get("api_port") or 0)
        cmd = (
            "python -m vllm.entrypoints.openai.api_server "
            '--model "$MODEL_PATH" '
            "--distributed-executor-backend ray "
            '--tensor-parallel-size "$TP" '
            '--pipeline-parallel-size "$PP" '
            "--host 0.0.0.0 "
            '--port "$API_PORT" '
            "|| sleep infinity"
        )
        env = {
            "MODEL_PATH": str(manifest.spec.model.local_path),
            "TP": tp,
            "PP": pp,
            "API_PORT": api_port,
            "CUDA_VISIBLE_DEVICES": ",".join(str(i) for i in placement.gpu_indices),
        }
        runtime_class_name = self._executor_runtime_class(manifest)
        return self._workload_manifest(
            name=name,
            namespace=namespace,
            image=str(manifest.spec.executor.launcher_image),
            command=["/bin/sh", "-lc"],
            args=[cmd],
            env=env,
            api_port=api_port,
            runtime_class_name=runtime_class_name,
        )

    def _apply_manifest_to_node(
        self,
        node_id: str,
        manifest: AppManifest,
        *,
        revision: int = 1,
    ) -> dict:
        runtime, endpoint = self._runtime_for_node(node_id)
        result = runtime.ensure_app(manifest, revision, keep_old=True)
        app_name = app_key(manifest.metadata.name, manifest.metadata.namespace)
        return {
            "node_id": node_id,
            "endpoint": endpoint,
            "app_name": app_name,
            "pod_states": [
                {
                    "pod_name": pod.pod_name,
                    "ready": bool(pod.ready),
                    "status": str(pod.status),
                    "endpoint": pod.endpoint,
                }
                for pod in list(result.pod_states or [])
            ],
        }

    def _remove_execution_workloads(self, alloc: dict) -> None:
        for item in list((alloc.get("execution") or {}).get("workloads") or []):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id") or "")
            app_name = str(item.get("app_name") or "")
            if not node_id or not app_name:
                continue
            with suppress(Exception):
                runtime, _endpoint = self._runtime_for_node(node_id)
                runtime.remove_app(app_name)

    def _teardown_fabric(self, alloc: dict) -> None:
        session_id = str(alloc.get("fabric_session_id") or "")
        if not session_id:
            return
        for placement in self._alloc_stage_placements(alloc):
            with suppress(Exception):
                self._agent.teardown_session(placement.node_id, session_id)
        self._broker.teardown_session(session_id)

    def _run_once(self, manifest: InferenceCellManifest) -> InferenceCellRecord:
        rec = self._record(manifest)
        alloc = dict(rec.allocations or {})
        cond = dict(rec.conditions or {})
        phase = CellPhase(rec.phase)
        cell_key = rec.cell_key

        if phase == CellPhase.PENDING:
            self._log_event(manifest, "PhaseChange", "PENDING -> ADMITTING")
            return self._update(manifest, phase=CellPhase.ADMITTING, conditions=cond)

        if phase == CellPhase.ADMITTING:
            if self._execution_enabled:
                member_errors = self._validate_members_for_execution(manifest.spec)
                if member_errors:
                    message = "; ".join(member_errors)
                    cond = _make_condition(cond, "AdmissionReady", False, message)
                    self._log_event(manifest, "AdmissionFailed", message)
                    return self._update(
                        manifest,
                        phase=CellPhase.FAILED,
                        conditions=cond,
                        admission={"admitted": False, "reasons": list(member_errors)},
                        last_error="ADMISSION_MEMBER_INVALID",
                    )
            try:
                placements = StagePlanner.plan(manifest.spec)
            except Exception as exc:  # noqa: BLE001
                cond = _make_condition(cond, "AdmissionReady", False, str(exc))
                self._log_event(manifest, "AdmissionFailed", str(exc))
                return self._update(
                    manifest,
                    phase=CellPhase.FAILED,
                    conditions=cond,
                    admission={"admitted": False, "reasons": [str(exc)]},
                    last_error="ADMISSION_PLACEMENT_FAILED",
                )

            report = BoundaryBudgetAdmission.evaluate(manifest.spec, placements)
            alloc["placements"] = [p.to_dict() for p in placements]
            alloc["gpu_slots"] = [
                {"node_id": node_id, "gpu_index": gpu_idx}
                for node_id, gpu_idx in StagePlanner.gpu_slots(placements)
            ]
            alloc["active_executor"] = str(manifest.spec.executor.type)
            cond = _make_condition(
                cond,
                "AdmissionReady",
                report.admitted,
                "ok" if report.admitted else "; ".join(report.reasons),
            )
            if not report.admitted:
                self._log_event(manifest, "AdmissionFailed", "; ".join(report.reasons))
                return self._update(
                    manifest,
                    phase=CellPhase.FAILED,
                    allocations=alloc,
                    admission=report.to_dict(),
                    conditions=cond,
                    last_error="ADMISSION_REJECTED",
                )
            self._log_event(manifest, "AdmissionPassed", "placement and boundary budget accepted")
            return self._update(
                manifest,
                phase=CellPhase.RESERVING,
                allocations=alloc,
                admission=report.to_dict(),
                conditions=cond,
                last_error=None,
            )

        if phase == CellPhase.RESERVING:
            placements = self._alloc_stage_placements(alloc)
            lease = self._leases.reserve(
                cell_key=cell_key, spec=manifest.spec, placements=placements
            )
            if lease is None:
                cond = _make_condition(
                    cond, "ResourcesReserved", False, "resource reservation failed"
                )
                self._log_event(manifest, "ReserveFailed", "resource reservation failed")
                return self._update(
                    manifest,
                    phase=CellPhase.FAILED,
                    conditions=cond,
                    last_error="RESOURCE_RESERVE_FAILED",
                )
            alloc["lease_id"] = lease.lease_id
            alloc["master_port"] = lease.master_port
            alloc["api_port"] = lease.api_port
            alloc["locked_nodes"] = list(lease.locked_nodes)
            cond = _make_condition(cond, "ResourcesReserved", True, "ok")
            self._log_event(manifest, "Reserved", f"lease={lease.lease_id}")
            return self._update(
                manifest,
                phase=CellPhase.FABRIC,
                allocations=alloc,
                conditions=cond,
                last_error=None,
            )

        if phase == CellPhase.FABRIC:
            placements = self._alloc_stage_placements(alloc)
            lease = self._lease_from_allocations(alloc)
            if lease is None:
                cond = _make_condition(cond, "FabricReady", False, "missing lease information")
                return self._update(
                    manifest,
                    phase=CellPhase.FAILED,
                    conditions=cond,
                    last_error="FABRIC_MISSING_LEASE",
                )
            rules = _build_allowed_rules(manifest.spec.executor, lease)
            session = self._broker.create_session(
                cell_key=cell_key,
                policy_mode=manifest.spec.fabric.policy_mode,
                mode=str(getattr(manifest.spec.fabric, "mode", "lan_direct") or "lan_direct"),
                ttl_seconds=manifest.spec.fabric.ttl_seconds,
                placements=placements,
                allowed_rules=rules,
            )
            ensured_nodes: list[str] = []
            for placement in placements:
                if not self._agent.ensure_session(placement.node_id, session):
                    cond = _make_condition(
                        cond,
                        "FabricReady",
                        False,
                        f"agent ensure failed on {placement.node_id}",
                    )
                    for ensured in ensured_nodes:
                        with suppress(Exception):
                            self._agent.teardown_session(ensured, session.session_id)
                    self._broker.teardown_session(session.session_id)
                    self._log_event(
                        manifest,
                        "FabricFailed",
                        f"ensure_session failed on {placement.node_id}",
                    )
                    return self._update(
                        manifest,
                        phase=CellPhase.FAILED,
                        allocations=alloc,
                        conditions=cond,
                        last_error="FABRIC_AGENT_FAILED",
                    )
                ensured_nodes.append(placement.node_id)
            alloc["fabric_session_id"] = session.session_id
            alloc["fabric_ifname"] = session.ifname
            alloc["member_fabric_ips"] = dict(session.member_ips)
            alloc["fabric_allowed_rules"] = list(session.allowed_rules)
            alloc["fabric_mode"] = str(session.mode)
            master_stage = int(manifest.spec.rendezvous.master_stage)
            stage0 = next((p for p in placements if p.stage == master_stage), placements[0])
            alloc["master_addr"] = session.member_ips.get(stage0.node_id)
            cond = _make_condition(cond, "FabricReady", True, "ok")
            self._log_event(manifest, "FabricReady", f"session={session.session_id}")
            return self._update(
                manifest,
                phase=CellPhase.STARTING_WORKERS,
                allocations=alloc,
                conditions=cond,
                last_error=None,
            )

        if phase == CellPhase.STARTING_WORKERS:
            placements = self._alloc_stage_placements(alloc)
            alloc.setdefault("workloads", {})
            run_type = self._active_executor(manifest, alloc)
            alloc["workloads"]["workers"] = self._render_workers(
                manifest, placements, executor_type=run_type
            )
            if not self._execution_enabled:
                cond = _make_condition(cond, "WorkersStarted", True, "planned")
                self._log_event(manifest, "WorkersPlanned", "worker workloads rendered")
                return self._update(
                    manifest,
                    phase=CellPhase.STARTING_LEADER,
                    allocations=alloc,
                    conditions=cond,
                    last_error=None,
                )

            execution = dict(alloc.get("execution") or {})
            execution.setdefault("workloads", [])

            def _apply_worker_set(exec_type: str) -> tuple[list[dict], list[dict]]:
                workloads: list[dict] = []
                applied: list[dict] = []
                master_stage = int(manifest.spec.rendezvous.master_stage)
                for placement in placements:
                    if int(placement.stage) == master_stage:
                        continue
                    if exec_type == "ray":
                        wf = self._ray_worker_manifest(manifest, placement, alloc, leader=False)
                        role = "ray-worker"
                    else:
                        wf = self._mp_stage_manifest(manifest, placement, alloc)
                        role = "mp-worker"
                    applied_rec = self._apply_manifest_to_node(placement.node_id, wf, revision=1)
                    applied_rec["role"] = role
                    workloads.append(
                        {
                            "name": wf.metadata.name,
                            "node_id": placement.node_id,
                            "stage": int(placement.stage),
                            "role": role,
                        }
                    )
                    applied.append(applied_rec)
                return workloads, applied

            worker_message = "runtime applied"
            try:
                workloads, applied = _apply_worker_set(run_type)
            except Exception as exc:  # noqa: BLE001
                if run_type == "ray" and self._supports_mp_fallback(manifest):
                    self._log_event(
                        manifest,
                        "ExecutorFallback",
                        f"ray worker start failed, switching to mp: {exc}",
                    )
                    alloc["active_executor"] = "mp"
                    self._remove_execution_workloads(alloc)
                    execution = {"workloads": []}
                    run_type = "mp"
                    alloc["workloads"]["workers"] = self._render_workers(
                        manifest, placements, executor_type="mp"
                    )
                    try:
                        workloads, applied = _apply_worker_set("mp")
                        worker_message = "runtime applied (fallback=mp)"
                        cond = _make_condition(cond, "ExecutorFallback", True, str(exc))
                    except Exception as mp_exc:  # noqa: BLE001
                        cond = _make_condition(cond, "WorkersStarted", False, str(mp_exc))
                        self._log_event(manifest, "WorkersFailed", str(mp_exc))
                        return self._update(
                            manifest,
                            phase=CellPhase.FAILED,
                            allocations=alloc,
                            conditions=cond,
                            last_error="WORKER_START_FAILED",
                        )
                else:
                    cond = _make_condition(cond, "WorkersStarted", False, str(exc))
                    self._log_event(manifest, "WorkersFailed", str(exc))
                    return self._update(
                        manifest,
                        phase=CellPhase.FAILED,
                        allocations=alloc,
                        conditions=cond,
                        last_error="WORKER_START_FAILED",
                    )

            execution["workloads"] = list(execution.get("workloads") or [])
            execution["workloads"].extend(applied)
            alloc["workloads"]["workers"] = workloads
            alloc["execution"] = execution
            cond = _make_condition(cond, "WorkersStarted", True, worker_message)
            self._log_event(manifest, "WorkersStarted", worker_message)
            return self._update(
                manifest,
                phase=CellPhase.STARTING_LEADER,
                allocations=alloc,
                conditions=cond,
                last_error=None,
            )

        if phase == CellPhase.STARTING_LEADER:
            placements = self._alloc_stage_placements(alloc)
            alloc.setdefault("workloads", {})
            run_type = self._active_executor(manifest, alloc)
            alloc["workloads"]["leader"] = self._render_leader(
                manifest,
                placements,
                alloc,
                executor_type=run_type,
            )
            if not self._execution_enabled:
                cond = _make_condition(cond, "LeaderStarted", True, "planned")
                self._log_event(manifest, "LeaderPlanned", "leader workload rendered")
                return self._update(
                    manifest,
                    phase=CellPhase.JOINING,
                    allocations=alloc,
                    conditions=cond,
                    last_error=None,
                )

            master_stage = int(manifest.spec.rendezvous.master_stage)
            leader = next((p for p in placements if p.stage == master_stage), placements[0])
            execution = dict(alloc.get("execution") or {})
            execution.setdefault("workloads", [])
            try:
                if run_type == "ray":
                    head = self._ray_worker_manifest(manifest, leader, alloc, leader=True)
                    head_rec = self._apply_manifest_to_node(leader.node_id, head, revision=1)
                    head_rec["role"] = "ray-head"
                    execution["workloads"].append(head_rec)

                    launcher = self._ray_launcher_manifest(manifest, leader, alloc)
                    launcher_rec = self._apply_manifest_to_node(
                        leader.node_id, launcher, revision=1
                    )
                    launcher_rec["role"] = "ray-launcher"
                    execution["workloads"].append(launcher_rec)
                    api_endpoint = ""
                    if launcher_rec.get("pod_states"):
                        first = list(launcher_rec.get("pod_states") or [])[0]
                        api_endpoint = str(first.get("endpoint") or "")
                    alloc["api_endpoint"] = (
                        api_endpoint or f"{alloc.get('master_addr')}:{alloc.get('api_port')}"
                    )
                    leader_msg = "runtime applied (ray)"
                else:
                    mp_leader = self._mp_stage_manifest(manifest, leader, alloc)
                    mp_rec = self._apply_manifest_to_node(leader.node_id, mp_leader, revision=1)
                    mp_rec["role"] = "mp-leader"
                    execution["workloads"].append(mp_rec)
                    api_endpoint = ""
                    if mp_rec.get("pod_states"):
                        first = list(mp_rec.get("pod_states") or [])[0]
                        api_endpoint = str(first.get("endpoint") or "")
                    alloc["api_endpoint"] = (
                        api_endpoint or f"{alloc.get('master_addr')}:{alloc.get('api_port')}"
                    )
                    leader_msg = "runtime applied (mp)"
            except Exception as exc:  # noqa: BLE001
                if run_type == "ray" and self._supports_mp_fallback(manifest):
                    self._log_event(
                        manifest,
                        "ExecutorFallback",
                        f"ray leader start failed, restarting in mp mode: {exc}",
                    )
                    alloc["active_executor"] = "mp"
                    self._remove_execution_workloads(alloc)
                    alloc["execution"] = {"workloads": []}
                    cond = _make_condition(cond, "ExecutorFallback", True, str(exc))
                    return self._update(
                        manifest,
                        phase=CellPhase.STARTING_WORKERS,
                        allocations=alloc,
                        conditions=cond,
                        last_error=None,
                    )
                cond = _make_condition(cond, "LeaderStarted", False, str(exc))
                self._log_event(manifest, "LeaderFailed", str(exc))
                return self._update(
                    manifest,
                    phase=CellPhase.FAILED,
                    allocations=alloc,
                    conditions=cond,
                    last_error="LEADER_START_FAILED",
                )

            alloc["execution"] = execution
            cond = _make_condition(cond, "LeaderStarted", True, leader_msg)
            self._log_event(manifest, "LeaderStarted", leader_msg)
            return self._update(
                manifest,
                phase=CellPhase.JOINING,
                allocations=alloc,
                conditions=cond,
                last_error=None,
            )

        if phase == CellPhase.JOINING:
            if not self._execution_enabled:
                cond = _make_condition(cond, "ApiReady", True, "planned")
                self._log_event(manifest, "CellReady", "cell reached READY")
                return self._update(
                    manifest,
                    phase=CellPhase.READY,
                    conditions=cond,
                    last_error=None,
                )

            missing: list[str] = []
            for item in list((alloc.get("execution") or {}).get("workloads") or []):
                if not isinstance(item, dict):
                    continue
                node_id = str(item.get("node_id") or "")
                app_name = str(item.get("app_name") or "")
                if not node_id or not app_name:
                    continue
                try:
                    runtime, _endpoint = self._runtime_for_node(node_id)
                    infos = list(runtime.list_containers_info() or [])
                except Exception:
                    missing.append(f"{node_id}:{app_name}")
                    continue
                labels_match = [
                    info
                    for info in infos
                    if str((info.get("labels") or {}).get("ae.app") or "") == app_name
                ]
                if not labels_match:
                    missing.append(f"{node_id}:{app_name}")
            if missing:
                retries = int(rec.restarts or 0)
                max_restarts = int(manifest.spec.health.max_restarts)
                message = f"missing runtime containers: {', '.join(missing)}"
                if retries < max_restarts:
                    cond = _make_condition(cond, "ApiReady", False, message)
                    self._log_event(manifest, "JoinPending", message)
                    return self._update(
                        manifest,
                        phase=CellPhase.RESTARTING,
                        allocations=alloc,
                        conditions=cond,
                        last_error="JOIN_FAILED",
                    )
                cond = _make_condition(cond, "ApiReady", False, message)
                self._log_event(manifest, "JoinFailed", message)
                return self._update(
                    manifest,
                    phase=CellPhase.FAILED,
                    allocations=alloc,
                    conditions=cond,
                    last_error="JOIN_FAILED",
                )

            api_ep = str(
                alloc.get("api_endpoint") or f"{alloc.get('master_addr')}:{alloc.get('api_port')}"
            )
            cond = _make_condition(cond, "ApiReady", True, api_ep)
            self._log_event(manifest, "CellReady", f"cell reached READY api={api_ep}")
            return self._update(
                manifest,
                phase=CellPhase.READY,
                allocations=alloc,
                conditions=cond,
                last_error=None,
            )

        if phase == CellPhase.RESTARTING:
            self._remove_execution_workloads(alloc)
            lease = self._lease_from_allocations(alloc)
            if lease is not None:
                self._leases.release(lease)
            self._teardown_fabric(alloc)
            alloc = {}
            restarts = int(rec.restarts) + 1
            cond = _make_condition(cond, "Restarting", True, "scheduled")
            self._log_event(manifest, "Restarting", f"restart={restarts}")
            return self._update(
                manifest,
                phase=CellPhase.ADMITTING,
                allocations=alloc,
                conditions=cond,
                restarts=restarts,
                last_error=None,
            )

        return rec

    def reconcile_manifest(
        self,
        manifest: InferenceCellManifest,
        *,
        source: str = "manual",
        max_steps: int = 16,
    ) -> InferenceCellRecord:
        self._store.register_inference_cell(manifest, source=source)
        for _ in range(max_steps):
            rec = self._record(manifest)
            if rec.phase in {CellPhase.READY.value, CellPhase.FAILED.value}:
                return rec
            updated = self._run_once(manifest)
            if updated.phase == rec.phase:
                return updated
        return self._record(manifest)

    def delete_cell(self, name: str, namespace: str | None = None) -> None:
        rec = self._store.get_inference_cell(name, namespace=namespace)
        if rec is None:
            return
        alloc = dict(rec.allocations or {})
        self._remove_execution_workloads(alloc)
        lease = self._lease_from_allocations(alloc)
        if lease is not None:
            self._leases.release(lease)
        self._teardown_fabric(alloc)
        self._store.delete_inference_cell(name, namespace=namespace)


class InferenceCellSetController:
    """Replica-set style reconcile for inference cells."""

    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        cell_controller: InferenceCellController | None = None,
    ) -> None:
        self._store = store
        self._cells = cell_controller or InferenceCellController(store)

    def _cell_manifest_from_template(
        self, manifest: InferenceCellSetManifest, cell_name: str
    ) -> InferenceCellManifest:
        md_labels = dict(manifest.metadata.labels or {})
        md_labels["k1s.cellset"] = manifest.metadata.name
        payload = {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCell",
            "metadata": {
                "name": cell_name,
                "namespace": manifest.metadata.namespace or DEFAULT_NAMESPACE,
                "labels": md_labels,
            },
            "spec": manifest.spec.template.model_dump(by_alias=True),
        }
        return InferenceCellManifest.model_validate(payload)

    def _expected_names(self, manifest: InferenceCellSetManifest) -> list[str]:
        fmt = manifest.spec.name_format or "{set}-{i:03d}"
        return [
            fmt.format(set=manifest.metadata.name, i=i) for i in range(int(manifest.spec.replicas))
        ]

    def reconcile_manifest(
        self,
        manifest: InferenceCellSetManifest,
        *,
        source: str = "manual",
    ) -> InferenceCellSetRecord:
        self._store.register_inference_cellset(manifest, source=source)
        namespace = manifest.metadata.namespace or DEFAULT_NAMESPACE
        expected = self._expected_names(manifest)
        expected_set = set(expected)
        existing = [
            cell
            for cell in self._store.list_inference_cells(namespace=namespace)
            if (cell.manifest.metadata.labels or {}).get("k1s.cellset") == manifest.metadata.name
        ]

        for cell_name in expected:
            cell_manifest = self._cell_manifest_from_template(manifest, cell_name)
            self._cells.reconcile_manifest(
                cell_manifest, source=f"cellset:{manifest.metadata.name}"
            )

        for cell in existing:
            if cell.cell_id in expected_set:
                continue
            self._cells.delete_cell(cell.cell_id, namespace=namespace)

        current_cells = [
            cell
            for cell in self._store.list_inference_cells(namespace=namespace)
            if (cell.manifest.metadata.labels or {}).get("k1s.cellset") == manifest.metadata.name
        ]
        ready = sum(1 for cell in current_cells if cell.phase == CellPhase.READY.value)
        self._store.update_inference_cellset_status(
            manifest.metadata.name,
            namespace=namespace,
            desired=int(manifest.spec.replicas),
            current=len(current_cells),
            ready=ready,
            last_error=None,
        )
        rec = self._store.get_inference_cellset(manifest.metadata.name, namespace=namespace)
        if rec is None:
            raise RuntimeError(
                f"inference cellset {manifest.metadata.name} not found after reconcile"
            )
        return rec

    def scale(
        self, name: str, replicas: int, namespace: str | None = None
    ) -> InferenceCellSetRecord | None:
        rec = self._store.get_inference_cellset(name, namespace=namespace)
        if rec is None:
            return None
        payload = rec.manifest.model_dump(by_alias=True)
        payload.setdefault("spec", {})
        payload["spec"]["replicas"] = int(max(0, replicas))
        updated = InferenceCellSetManifest.model_validate(payload)
        return self.reconcile_manifest(updated, source="scale")
