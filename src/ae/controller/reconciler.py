# ruff: noqa: E501,I001,E402,S110,S112,SIM102,SIM105,SIM108,SIM114,SIM118,UP034,UP038
"""Reconcile loop coordinating manifests, runtime operations, and health."""

from __future__ import annotations

import hashlib
import logging
import json
from dataclasses import dataclass
from pathlib import Path
import os
import socket
from typing import Any

from ae.controller.health import HealthManager, HealthReport, PodHealth
from ae.controller.spec import AppManifest, VolumeSpec, app_key_for_manifest, load_manifest
from ae.runtime import RuntimeAdapter, RuntimeResult
from ae.storage.config import DEFAULT_CLASS_ANNOTATIONS


def _record_event_metric_safe(name: str) -> None:
    """Best-effort metric hook used by reconciler events."""
    try:
        from ae.observability.http_api import record_event_metric  # type: ignore

        record_event_metric(name)
    except Exception:
        pass


def _truthy_env(name: str, default: str = "0") -> bool:
    raw = os.getenv(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


from .state import SQLiteStateStore
from ae.ingress.service import IngressService
from ae.secrets import SecretManager
from ae.config.manager import ConfigManager
from ae.controller.scheduler import Scheduler

SC_GROUP = "storage.k8s.io"
SC_VERSION = "v1"
SC_RESOURCE = "storageclasses"
CORE_GROUP = ""
CORE_VERSION = "v1"
PVC_RESOURCE = "persistentvolumeclaims"
PV_RESOURCE = "persistentvolumes"
LOCAL_PATH_PROVISIONER = "k1s.io/local-path"
WAIT_FOR_FIRST_CONSUMER = "WaitForFirstConsumer"
SELECTED_NODE_ANNOTATION = "volume.kubernetes.io/selected-node"


@dataclass(slots=True)
class ReconcileReport:
    """Summary of a reconcile run."""

    app_name: str
    created: int
    updated: int
    removed: int
    ready_replicas: int
    live_replicas: int
    revision: int
    revision_status: str


class Reconciler:
    """Coordinates manifest application across runtime, health, and state store."""

    def __init__(
        self,
        runtime: RuntimeAdapter,
        state_store: SQLiteStateStore,
        health_manager: HealthManager | None = None,
        ingress_service: IngressService | None = None,
        secret_manager: SecretManager | None = None,
        config_manager: ConfigManager | None = None,
        service_controller=None,
    ) -> None:
        self._runtime = runtime
        self._state_store = state_store
        self._health_manager = health_manager or HealthManager()
        self._ingress_service = ingress_service
        self._secret_manager = secret_manager
        self._config_manager = config_manager or ConfigManager()
        self._service_controller = service_controller
        self._scheduler = Scheduler(self._state_store)
        self._runtime_cache: dict[str, RuntimeAdapter] = {}
        self._base_runtime = getattr(runtime, "_local", runtime)
        # Inject exec callback for exec probes
        try:
            self._health_manager.set_exec_callback(self._exec_across_runtimes)
        except Exception:
            pass
        try:
            self._health_manager.set_event_callback(self._on_probe_event)
        except Exception:
            pass
        # Track last seen restart counts per container/app to detect crash loops
        self._last_restart_counts: dict[str, int] = {}
        self._last_restart_ts: dict[str, float] = {}
        # Create cooldowns: app -> unix timestamp until which we avoid creating new replicas
        self._create_cooldown_until: dict[str, float] = {}
        self._apishim_store = None
        self._apishim_store_checked = False
        self._default_sc_name: str | None = None
        self._register_local_node = _truthy_env("AE_REGISTER_LOCAL_NODE")

    def _runtime_for_agent(self, agent_url: str | None) -> RuntimeAdapter:
        """Return a runtime bound to the target agent URL (cached)."""
        if not agent_url:
            return self._runtime
        cached = self._runtime_cache.get(agent_url)
        if cached:
            return cached
        try:
            from ae.runtime import RemoteRuntime

            base = getattr(self._runtime, "_local", self._base_runtime)
            rt = RemoteRuntime(agent_url, base)
            self._runtime_cache[agent_url] = rt
            return rt
        except Exception:
            return self._runtime

    def _exec_across_runtimes(self, pod_name: str, command: list[str], timeout: int | None) -> int:
        """Try exec across known runtimes (local + cached remote)."""
        runtimes = [self._runtime] + list(self._runtime_cache.values())
        for rt in runtimes:
            try:
                return int(rt.exec(pod_name, command, timeout=timeout))  # type: ignore[arg-type]
            except Exception:
                continue
        return 127

    def _runtime_backend_name(self) -> str:
        env_backend = os.getenv("AE_RUNTIME_BACKEND")
        if env_backend:
            return str(env_backend).strip().lower()
        return self._runtime.__class__.__name__.lower()

    def _ensure_local_node(self) -> None:
        if not self._register_local_node:
            return
        try:
            node_id = os.getenv("AE_NODE_ID") or socket.gethostname()
            name = os.getenv("AE_NODE_NAME") or node_id
            existing = None
            try:
                existing = self._state_store.get_node(node_id)
            except Exception:
                existing = None
            if existing is None:
                try:
                    if self._state_store.list_nodes():
                        return
                except Exception:
                    pass
                self._state_store.upsert_node(
                    node_id,
                    name=name,
                    labels={"role": "controller"},
                    taints=[],
                    backend=self._runtime_backend_name(),
                    endpoint=None,
                    pod_cidr=None,
                    wg_pubkey=None,
                )
                self._state_store.record_heartbeat(node_id, "Ready")
                return
            node, _status = existing
            # Refresh heartbeat only for local/controller nodes (no endpoint or role label).
            labels = getattr(node, "labels", {}) or {}
            if getattr(node, "endpoint", None) is None or labels.get("role") == "controller":
                self._state_store.record_heartbeat(node_id, "Ready")
        except Exception:
            pass

    def _ensure_on_runtime(
        self,
        runtime: RuntimeAdapter,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool,
        limit_create: int | None,
        pod_names: list[str] | None,
        node_id: str | None,
    ) -> RuntimeResult:
        """Call ensure_app with backward-compatible fallbacks for older runtimes."""
        try:
            return runtime.ensure_app(  # type: ignore[arg-type]
                manifest,
                revision,
                keep_old=keep_old,
                limit_create=limit_create,
                pod_names=pod_names,
                node_id=node_id,
            )
        except TypeError:
            try:
                return runtime.ensure_app(  # type: ignore[arg-type]
                    manifest,
                    revision,
                    keep_old=keep_old,
                    limit_create=limit_create,
                    replica_ids=pod_names,
                )
            except TypeError:
                return runtime.ensure_app(manifest, revision)  # type: ignore[arg-type]

    def reconcile_manifest_path(self, path: Path) -> ReconcileReport:
        """Load a manifest from disk and reconcile it."""

        manifest = load_manifest(path)
        return self.reconcile(manifest)

    def reconcile(self, manifest: AppManifest) -> ReconcileReport:
        """Reconcile the runtime to match the manifest."""

        spec_hash = self._compute_spec_hash(manifest)
        revision, _ = self._state_store.prepare_revision(manifest, spec_hash)
        app_name = app_key_for_manifest(manifest)

        self._state_store.record_event(
            app_name,
            revision,
            "ApplyStarted",
            f"Reconciling revision {revision}",
        )
        _record_event_metric_safe("apply_start")
        _record_event_metric_safe("apply_start")

        # Apply config/secret projections. Be resilient to secret errors so a
        # single bad SOPS file does not crash the controller under load.
        try:
            manifest_with_env = self._apply_configs_and_secrets(manifest)
        except Exception as exc:  # noqa: BLE001
            try:
                self._state_store.record_event(
                    app_name,
                    revision,
                    "SecretError",
                    f"secrets/config application failed: {exc}",
                )
            except Exception:
                pass
            # Fallback: continue without injecting secret/config env
            manifest_with_env = manifest
        # Prepare file projections and add a read-only volume mount if any files were written
        projection_root = self._prepare_file_projections(manifest, revision)
        manifest_for_runtime = manifest_with_env
        if projection_root is not None:
            vols = list(manifest_for_runtime.spec.volumes)
            mount_root = f"/var/run/ae/config/{app_name}"
            # Append a typed VolumeSpec so downstream code sees attributes, not dicts
            vols.append(
                VolumeSpec(host_path=str(projection_root), mount_path=mount_root, read_only=True)
            )
            updated_spec = manifest_for_runtime.spec.model_copy(update={"volumes": vols})
            manifest_for_runtime = manifest_for_runtime.model_copy(update={"spec": updated_spec})

        # Run init containers if the runtime supports it
        try:
            inits = getattr(manifest_for_runtime.spec, "init_containers", []) or []
            if inits and callable(getattr(self._runtime, "run_init_containers", None)):
                # Emit start events
                for c in inits:
                    try:
                        name = (
                            getattr(c, "name", None) if not isinstance(c, dict) else c.get("name")
                        )
                        if name:
                            self._state_store.record_event(
                                app_name, revision, "InitStart", f"container={name}"
                            )
                            _record_event_metric_safe("init_start")
                    except Exception:
                        pass
                results = self._runtime.run_init_containers(manifest_for_runtime)  # type: ignore[attr-defined]
                for name, rc, msg in results:
                    try:
                        self._state_store.record_event(
                            app_name,
                            revision,
                            "InitDone",
                            f"container={name} rc={rc} {msg or ''}",
                        )
                        _record_event_metric_safe("init_done")
                    except Exception:
                        pass
        except Exception:
            # Do not block rollout on init container orchestration errors
            pass

        # Rollout policy
        rollout = getattr(manifest.spec, "rollout", {}) or {}
        strategy = str(rollout.get("strategy", "parallel")).lower()
        max_unavail = int(rollout.get("maxUnavailable", 0))

        # Pause: record snapshot with current status and skip runtime/ingress changes
        if bool(rollout.get("pause", False)):
            # Build a health report from last known status (if any)
            prev = self._state_store.get_status(app_name)
            pods = self._state_store.list_pods(app_name)
            hr = HealthReport(
                ready_replicas=prev.ready_replicas if prev else 0,
                live_replicas=prev.live_replicas if prev else 0,
                pods=[
                    PodHealth(
                        pod_name=r.pod_name,
                        ready=r.ready,
                        live=r.live,
                        readiness_message=r.readiness_message,
                        liveness_message=r.liveness_message,
                    )
                    for r in pods
                ],
            )
            result = RuntimeResult(
                revision=revision, created=0, updated=0, removed=0, pod_states=[]
            )
            revision_status = "paused"
            self._state_store.record_snapshot(
                manifest=manifest_for_runtime,
                runtime_result=result,
                health_report=hr,
                revision=revision,
                revision_status=revision_status,
            )
            self._state_store.record_event(
                app_name,
                revision,
                "RolloutPaused",
                "Rollout paused by manifest; runtime unchanged",
            )
            return ReconcileReport(
                app_name=app_name,
                created=0,
                updated=0,
                removed=0,
                ready_replicas=hr.ready_replicas,
                live_replicas=hr.live_replicas,
                revision=revision,
                revision_status=revision_status,
            )

        self._ensure_local_node()
        import time as _t

        now_ts = float(_t.time())
        limit_create = 1 if strategy == "ordered" else None
        # Apply create cooldown when active
        until = float(self._create_cooldown_until.get(app_name, 0.0) or 0.0)
        if until > now_ts:
            limit_create = 0

        placements, schedule_warnings = self._scheduler.plan(manifest_for_runtime, revision)
        for w in schedule_warnings:
            try:
                self._state_store.record_event(app_name, revision, "ScheduleWarning", w)
            except Exception:
                pass
        self._apply_selected_node_annotations(manifest_for_runtime, placements, revision)
        # Persist placement hints before reconcile (so dashboard can render)
        try:
            node_rows: list[tuple[str, str]] = []
            for pl in placements:
                node_id = getattr(getattr(pl, "node", None), "node_id", None)
                if not node_id:
                    continue
                for pod_name in getattr(pl, "pod_names", []) or []:
                    node_rows.append((pod_name, node_id))
            if node_rows:
                self._state_store.set_pod_nodes(app_name, node_rows)
        except Exception:
            pass

        # Keep old pods during rollout to respect surge/unavailable; we'll remove them after readiness check
        aggregate_states: list = []
        created = updated = removed = 0
        runtimes_used: list[RuntimeAdapter] = []
        remaining_limit = limit_create
        for placement in placements:
            # Ensure pod_names are unique per app/revision (avoid duplicate scheduling across nodes)
            pod_names = list(dict.fromkeys(getattr(placement, "pod_names", []) or []))
            runtime = self._runtime_for_agent(getattr(placement, "agent_url", None))
            if runtime not in runtimes_used:
                runtimes_used.append(runtime)
            per_limit = None
            if remaining_limit is not None:
                per_limit = max(0, remaining_limit)
            # Record storage binding to the chosen node (best-effort)
            if getattr(manifest_for_runtime.spec, "storage", None) and getattr(
                placement, "node", None
            ):
                try:
                    for s in manifest_for_runtime.spec.storage:
                        self._state_store.upsert_volume_attachment(
                            app_name,
                            getattr(s, "name", ""),
                            placement.node.node_id,  # type: ignore[union-attr]
                            getattr(s, "retention", None),
                        )
                except Exception:
                    pass
            res = self._ensure_on_runtime(
                runtime,
                manifest_for_runtime,
                revision,
                keep_old=True,
                limit_create=per_limit,
                pod_names=pod_names,
                node_id=getattr(getattr(placement, "node", None), "node_id", None),
            )
            created += res.created
            updated += res.updated
            removed += res.removed
            aggregate_states.extend(res.pod_states)
            if remaining_limit is not None:
                remaining_limit = max(0, remaining_limit - res.created)

        result = RuntimeResult(
            revision=revision,
            created=created,
            updated=updated,
            removed=removed,
            pod_states=aggregate_states,
        )
        health_report = self._health_manager.evaluate(manifest, result)
        # Service/IPAM: prefer Service VIPs when controller is available
        if self._service_controller:
            try:
                self._service_controller.reconcile(manifest_for_runtime, result, health_report)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "service reconcile failed for %s: %s", app_name, exc
                )
        # Record probe backoff metrics from messages
        try:
            from ae.observability.http_api import record_probe_backoff  # type: ignore

            def _parse_bo(msg: str | None) -> int:
                try:
                    import re

                    m = re.search(r"backoff \((\d+)s\)", str(msg or ""))
                    return int(m.group(1)) if m else 0
                except Exception:
                    return 0

            for pod in getattr(health_report, "pods", []) or []:
                record_probe_backoff(
                    app_name,
                    pod.pod_name,
                    "readiness",
                    _parse_bo(getattr(pod, "readiness_message", "")),
                )
                record_probe_backoff(
                    app_name,
                    pod.pod_name,
                    "liveness",
                    _parse_bo(getattr(pod, "liveness_message", "")),
                )
        except Exception:
            pass
        # Pre-switch rollout hook (optional)
        hook_ok = True
        hook_msg = None
        try:
            ro = getattr(manifest.spec, "rollout", {}) or {}
            hooks = ro.get("hooks") if isinstance(ro, dict) else None
            pre = (hooks or {}).get("preSwitch") or (hooks or {}).get("pre_switch")
            if pre:
                hook_ok, hook_msg = self._run_rollout_hook(manifest, result, pre)
                if not hook_ok:
                    try:
                        self._state_store.record_event(
                            app_name,
                            revision,
                            "HookFailed",
                            f"preSwitch: {hook_msg or 'failed'}",
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        if manifest.spec.ingress and self._ingress_service and hook_ok:
            upstreams = self._select_upstreams(manifest, result, health_report)
            if upstreams:
                upstream_param = upstreams[0] if len(upstreams) == 1 else upstreams
                self._ingress_service.apply(manifest, upstream_param)
                self._ingress_service.reload()
                self._state_store.record_event(
                    app_name,
                    revision,
                    "IngressConfigured",
                    f"Ingress upstreams set to {', '.join(upstreams)}",
                )
        elif self._ingress_service and not manifest.spec.ingress:
            self._ingress_service.remove(app_name)
            self._ingress_service.reload()
            self._state_store.record_event(
                app_name,
                revision,
                "IngressRemoved",
                "Ingress configuration removed",
            )
        # Post-switch rollout hook (optional, best-effort)
        try:
            if hook_ok:
                ro = getattr(manifest.spec, "rollout", {}) or {}
                hooks = ro.get("hooks") if isinstance(ro, dict) else None
                post = (hooks or {}).get("postSwitch") or (hooks or {}).get("post_switch")
                if post:
                    ok, msg = self._run_rollout_hook(manifest, result, post)
                    ev = "HookPassed" if ok else "HookFailed"
                    self._state_store.record_event(
                        app_name,
                        revision,
                        ev,
                        f"postSwitch: {msg or ('ok' if ok else 'failed')}",
                    )
        except Exception:
            pass
        revision_status = self._calculate_revision_status(manifest, health_report, result)

        # Crashloop detection: emit events when container restart counts surge in a window
        try:
            self._detect_crashloops(manifest)
        except Exception:
            pass

        # Remove old revisions if availability is satisfied, except while canary is active
        desired = manifest.spec.replicas
        ro_now = getattr(manifest.spec, "rollout", {}) or {}
        strat_now = str(ro_now.get("strategy", "parallel")).lower()
        w_now = None
        try:
            w_now = int(ro_now.get("weight")) if ro_now.get("weight") is not None else None
        except Exception:
            w_now = None
        canary_active = strat_now == "canary" and (w_now or 0) > 0 and (w_now or 0) < 100
        if (not canary_active) and health_report.ready_replicas >= max(0, desired - max_unavail):
            try:
                # Run preStop exec for old replicas before removal (best-effort)
                try:
                    self._run_prestop_on_old(manifest, keep_revision=revision)
                except Exception:
                    pass
                removed_old = self._remove_old_revisions_all(app_name, revision, runtimes_used)
                if removed_old > 0:
                    self._state_store.record_event(
                        app_name,
                        revision,
                        "RolloutOldRemoved",
                        f"Removed {removed_old} old revision container(s)",
                    )
            except Exception:
                pass

        # Emit rollout change events (e.g., canary enabled/updated/disabled)
        try:
            prev = self._state_store.get_status(app_name)
            prev_ro = None
            if prev is not None:
                try:
                    prev_man = self._state_store.get_revision_manifest(prev.app_name, prev.revision)
                    prev_ro = getattr(prev_man.spec, "rollout", {}) or {}
                except Exception:
                    prev_ro = None
            new_ro = getattr(manifest.spec, "rollout", {}) or {}

            def _ro_view(ro: dict | None) -> tuple[str, int | None, bool]:
                try:
                    strat = str((ro or {}).get("strategy", "parallel")).lower()
                    weight = (ro or {}).get("weight")
                    w = int(weight) if weight is not None else None
                    paused = bool((ro or {}).get("pause", False))
                    return strat, w, paused
                except Exception:
                    return "parallel", None, False

            p_strat, p_weight, _p_paused = _ro_view(prev_ro if isinstance(prev_ro, dict) else {})
            n_strat, n_weight, _n_paused = _ro_view(new_ro if isinstance(new_ro, dict) else {})
            ev_type = None
            msg = None
            if p_strat != "canary" and n_strat == "canary" and (n_weight or 0) > 0:
                ev_type = "CanaryEnabled"
                msg = (
                    f"canary enabled: weight {int(n_weight)}%"
                    if n_weight is not None
                    else "canary enabled"
                )
            elif p_strat == "canary" and (n_strat != "canary" or (n_weight or 0) == 0):
                ev_type = "CanaryDisabled"
                msg = "canary disabled"
            elif (
                p_strat == "canary"
                and n_strat == "canary"
                and (p_weight != n_weight)
                and (n_weight is not None)
            ):
                ev_type = "CanaryUpdated"
                msg = f"canary weight {int(p_weight or 0)}% -> {int(n_weight)}%"
            if ev_type and msg:
                self._state_store.record_event(app_name, revision, ev_type, msg)
        except Exception:
            pass

        self._state_store.record_snapshot(
            manifest=manifest_for_runtime,
            runtime_result=result,
            health_report=health_report,
            revision=revision,
            revision_status=revision_status,
        )
        self._state_store.record_event(
            app_name,
            revision,
            "ApplyCompleted",
            f"Revision {revision} status {revision_status}",
        )
        if health_report.ready_replicas < manifest.spec.replicas:
            try:
                self._state_store.record_event(
                    app_name,
                    revision,
                    "RolloutProgressing",
                    f"{health_report.ready_replicas}/{manifest.spec.replicas} replicas ready",
                )
            except Exception:
                pass
            _record_event_metric_safe("rollout_progressing")
        else:
            _record_event_metric_safe("rollout_complete")
        return ReconcileReport(
            app_name=app_name,
            created=result.created,
            updated=result.updated,
            removed=result.removed,
            ready_replicas=health_report.ready_replicas,
            live_replicas=health_report.live_replicas,
            revision=revision,
            revision_status=revision_status,
        )

    def _run_prestop_on_old(self, manifest: AppManifest, *, keep_revision: int) -> None:
        """Execute lifecycle.preStop on old replicas before removal.

        Best-effort: supports exec/http/tcp handlers. Uses terminationGracePeriodSeconds as
        timeout. Emits PreStop* events with outcome per replica.
        """
        lc = getattr(manifest.spec, "lifecycle", None)
        handler = getattr(lc, "pre_stop", None) if lc else None
        if handler is None:
            return
        app_name = app_key_for_manifest(manifest)
        timeout = 10
        try:
            timeout = int(
                (
                    getattr(handler, "timeout_seconds", None)
                    or getattr(manifest.spec, "termination_grace_period_seconds", 10)
                    or 10
                )
            )
        except Exception:
            timeout = 10
        # Discover old replicas via runtime list
        runtime = self._runtime
        items: list[dict] = []
        try:
            items = list(getattr(runtime, "list_containers_info", lambda: [])() or [])  # type: ignore[misc]
        except Exception:
            items = []
        for it in items:
            labels = (it or {}).get("labels") or {}
            if (labels.get("ae.app") or "") != app_name:
                continue
            if str(labels.get("ae.revision")) == str(keep_revision):
                continue
            rid = (
                labels.get("ae.pod_name")
                or labels.get("ae.replica_id")
                or labels.get("ae.replica")
            )
            if not rid:
                continue
            rc = None
            # Exec handler
            if getattr(handler, "exec", None) is not None:
                cmd = list(getattr(handler.exec, "command", []) or [])  # type: ignore[union-attr]
                if not cmd:
                    continue
                try:
                    if callable(getattr(runtime, "exec", None)):
                        rc = int(runtime.exec(str(rid), cmd, timeout=timeout))  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    rc = None
                try:
                    self._state_store.record_event(
                        app_name,
                        keep_revision,
                        "PreStopExec",
                        f"pod={rid} rc={rc if rc is not None else 'n/a'}",
                    )
                except Exception:
                    pass
            # HTTP handler
            elif getattr(handler, "http_get", None) is not None:
                try:
                    import requests as _req

                    path = str(handler.http_get.path or "/")  # type: ignore[union-attr]
                    target = self._endpoint_from_container_info(it, handler.http_get.port)  # type: ignore[union-attr]
                    if target:
                        host, port = target
                        url = f"http://{host}:{int(port)}{path}"
                        _req.get(url, timeout=max(1, int(timeout)))
                        outcome = "ok"
                    else:
                        outcome = "skipped(no-endpoint)"
                except Exception as exc:  # noqa: BLE001
                    outcome = f"error({exc.__class__.__name__})"
                try:
                    self._state_store.record_event(
                        app_name,
                        keep_revision,
                        "PreStopHTTP",
                        f"pod={rid} {outcome}",
                    )
                except Exception:
                    pass
            # TCP handler
            elif getattr(handler, "tcp_socket", None) is not None:
                import socket as _sock

                try:
                    target = self._endpoint_from_container_info(it, handler.tcp_socket.port)  # type: ignore[union-attr]
                    if target:
                        host, port = target
                        with _sock.create_connection((host, int(port)), timeout=max(1, int(timeout))):
                            outcome = "ok"
                    else:
                        outcome = "skipped(no-endpoint)"
                except OSError:
                    outcome = "error"
                try:
                    self._state_store.record_event(
                        app_name,
                        keep_revision,
                        "PreStopTCP",
                        f"pod={rid} {outcome}",
                    )
                except Exception:
                    pass

    def _remove_old_revisions_all(
        self, app_name: str, revision: int, runtimes: list[RuntimeAdapter] | None
    ) -> int:
        """Remove old revision containers across all runtimes used in this reconcile."""
        total = 0
        seen: set[int] = set()
        for rt in list(runtimes or []) + [self._runtime]:
            if id(rt) in seen:
                continue
            seen.add(id(rt))
            try:
                total += int(rt.remove_old_revisions(app_name, revision))
            except Exception:
                pass
        return total

    def _run_rollout_hook(self, manifest, runtime_result, hook) -> tuple[bool, str | None]:  # noqa: ANN001
        """Execute a rollout hook against the new revision.

        Supports:
          - exec: list[str] executed in the first ready replica (fallback to first replica)
          - tcp: { port } TCP connect to replica endpoint host:port
          - timeoutSeconds: optional per-hook timeout (default 5)
        """
        timeout = 5
        try:
            timeout = int(hook.get("timeoutSeconds", 5))
        except Exception:
            timeout = 5
        pods = list(runtime_result.pod_states or [])
        target = None
        # prefer ready
        for r in pods:
            if getattr(r, "ready", False):
                target = r
                break
        if target is None and pods:
            target = pods[0]
        if target is None:
            return False, "no pods available for hook"
        # exec hook
        if "exec" in hook:
            cmd = hook.get("exec") or []
            if not isinstance(cmd, (list, tuple)) or not cmd:
                return False, "exec hook missing/invalid command"
            try:
                code = self._runtime.exec(target.pod_name, [str(x) for x in cmd], timeout=timeout)
                return (code == 0), (f"exec rc={code}")
            except Exception as exc:  # noqa: BLE001
                return False, f"exec error: {exc}"
        # tcp hook
        if "tcp" in hook:
            port = None
            try:
                port = int((hook.get("tcp") or {}).get("port", 0))
            except Exception:
                port = 0
            if port <= 0:
                return False, "tcp.port must be set"
            # Build target host: use resolved endpoint host or localhost
            host = "127.0.0.1"
            try:
                ep = str(getattr(target, "endpoint", "") or "")
                if ":" in ep:
                    host = ep.split(":", 1)[0]
            except Exception:
                pass
            import socket as _s

            try:
                with _s.create_connection((host, int(port)), timeout=max(1, int(timeout))):
                    return True, "tcp ok"
            except OSError as exc:
                return False, f"tcp error: {exc}"
        return False, "unsupported hook"

    def _detect_crashloops(self, manifest: AppManifest) -> None:
        import time as _t

        app = app_key_for_manifest(manifest)
        try:
            infos = self._runtime.list_containers_info()  # type: ignore[attr-defined]
        except Exception:
            infos = []
        # thresholds
        try:
            wnd = float(os.getenv("AE_RESTART_WINDOW_SEC", "60"))
            thr = int(os.getenv("AE_RESTART_THRESHOLD", "3"))
        except Exception:
            wnd, thr = 60.0, 3
        now = float(_t.time())
        surges = 0
        for c in infos or []:
            labels = c.get("labels") or {}
            if (labels.get("ae.app") or "") != app:
                continue
            name = str(c.get("name", ""))
            rc = int(c.get("restart_count", 0) or 0)
            key = f"{app}|{name}"
            last_rc = self._last_restart_counts.get(key, rc)
            last_ts = self._last_restart_ts.get(key, now)
            if rc > last_rc:
                # increased since last observe; if within window, count surge
                if (now - last_ts) <= wnd and (rc - last_rc) >= 1:
                    surges += rc - last_rc
                # update trackers
                self._last_restart_counts[key] = rc
                self._last_restart_ts[key] = now
        if surges >= thr:
            try:
                # record event and mark crashloop in API for a short TTL
                self._state_store.record_event(
                    app,
                    0,
                    "CrashLoopDetected",
                    f"container restarts surged: {surges} in {int(wnd)}s (>= {thr})",
                )
            except Exception:
                pass
            try:
                from ae.observability.http_api import set_app_crashloop  # type: ignore

                set_app_crashloop(app, ttl_seconds=float(os.getenv("AE_CRASHLOOP_TTL", "300")))
            except Exception:
                pass
            # Apply create cooldown
            try:
                cd = float(os.getenv("AE_RECREATE_COOLDOWN_SEC", "30"))
            except Exception:
                cd = 30.0
            try:
                self._create_cooldown_until[app] = float(_t.time()) + float(cd)
                self._state_store.record_event(
                    app, 0, "RecreateCooldown", f"suppressing new replica creation for {int(cd)}s"
                )
            except Exception:
                pass

    def _select_upstreams(
        self,
        manifest: AppManifest,
        result: RuntimeResult,
        health_report: HealthReport,
    ) -> list[str]:
        app_name = app_key_for_manifest(manifest)
        # Prefer Service VIP when recorded and ready backends exist
        try:
            svc = self._state_store.get_service(app_name)
        except Exception:
            svc = None

        if svc and manifest.spec.service:
            svc_port = None
            try:
                if manifest.spec.service.ports:
                    svc_port = int(manifest.spec.service.ports[0].port)
                elif manifest.spec.service.port:
                    svc_port = int(manifest.spec.service.port)
            except Exception:
                svc_port = None
            if svc_port:
                try:
                    eps = self._state_store.list_service_endpoints(app_name)
                except Exception:
                    eps = []
                if any(ep.ready for ep in eps):
                    return [f"{svc.cluster_ip}:{svc_port}"]

        states_by_id = {state.pod_name: state for state in result.pod_states}

        # Prefer only ready endpoints; defer ingress changes until at least one
        # replica is ready to avoid transient 502s during warm-up.
        ready_eps: list[str] = []
        for pod in health_report.pods:
            if not pod.ready:
                continue
            state = states_by_id.get(pod.pod_name)
            if state and state.endpoint:
                host, port = self._split_host_port(state.endpoint)
                if host and port:
                    ready_eps.append(f"{host}:{port}")
        # When canary is enabled, include previous revision endpoints to split traffic
        try:
            ro = getattr(manifest.spec, "rollout", {}) or {}
            strat = str(ro.get("strategy", "parallel")).lower()
            w = int(ro.get("weight")) if ro.get("weight") is not None else 0
        except Exception:
            strat, w = "parallel", 0
        if ready_eps and strat == "canary" and w > 0:
            try:
                items = self._runtime.list_containers_info()  # type: ignore[attr-defined]
            except Exception:
                items = []
            preferred_port = self._preferred_container_port(manifest)
            prev_eps: list[str] = []
            cur_rev = str(result.revision)
            for it in items or []:
                labs = it.get("labels") or {}
                if (labs.get("ae.app") or "") != app_name:
                    continue
                if (labs.get("ae.revision") or "") == cur_rev:
                    continue
                target = self._endpoint_from_container_info(it, preferred_port)
                if target:
                    host, port = target
                    prev_eps.append(f"{host}:{int(port)}")
            # Merge, de-duplicating, keeping new first to honor prefer-first policy
            seen = set()
            merged: list[str] = []
            for ep in ready_eps + prev_eps:
                if ep in seen:
                    continue
                seen.add(ep)
                merged.append(ep)
            if merged:
                return merged
        if ready_eps:
            return ready_eps

        # Fallback: allow loopback endpoints when nothing else is ready (useful for local/stub runtimes)
        for pod in health_report.pods:
            if not pod.ready:
                continue
            state = states_by_id.get(pod.pod_name)
            if state and state.endpoint:
                host, port = self._split_host_port(state.endpoint)
                if host and port:
                    return [f"{host}:{port}"]

        # No ready endpoints yet: return empty to keep previous ingress configuration intact.
        return []

    def _split_host_port(self, endpoint: str) -> tuple[str | None, int | None]:
        """Parse host:port strings, tolerating IPv6 bracket notation."""
        try:
            if endpoint.startswith("["):
                host, port = endpoint.rsplit("]:", 1)
                return host.lstrip("["), int(port)
            host, port = endpoint.rsplit(":", 1)
            return host, int(port)
        except Exception:
            return None, None

    def _preferred_container_port(self, manifest: AppManifest) -> int | None:
        try:
            if manifest.spec.health and manifest.spec.health.readiness:
                r = manifest.spec.health.readiness
                if getattr(r, "http_get", None) is not None:
                    return int(r.http_get.port)
                if getattr(r, "tcp_socket", None) is not None:
                    return int(r.tcp_socket.port)
        except Exception:
            pass
        try:
            if manifest.spec.service and getattr(manifest.spec.service, "target_port", None):
                return int(manifest.spec.service.target_port)
        except Exception:
            pass
        try:
            if manifest.spec.ports:
                return int(manifest.spec.ports[0].container_port)
        except Exception:
            pass
        return None

    def _endpoint_from_container_info(
        self, info: dict, port_hint: int | None
    ) -> tuple[str, int] | None:
        pod_ip = (info or {}).get("pod_ip")
        host_ip = (info or {}).get("host_ip") or "127.0.0.1"
        port_map = (info or {}).get("port_map") or {}
        host_ports = list((info or {}).get("host_ports") or [])
        if pod_ip and port_hint:
            return str(pod_ip), int(port_hint)
        if port_hint is not None and port_map:
            try:
                if port_hint in port_map:
                    return str(host_ip), int(port_map[port_hint])
                if str(port_hint) in port_map:
                    return str(host_ip), int(port_map[str(port_hint)])
            except Exception:
                pass
        if host_ports:
            try:
                return str(host_ip), int(host_ports[0])
            except Exception:
                return None
        return None

    def _compute_spec_hash(self, manifest: AppManifest) -> str:
        payload = json.dumps(
            manifest.model_dump(by_alias=True, exclude_none=True),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _calculate_revision_status(
        self, manifest: AppManifest, report: HealthReport, runtime_result: RuntimeResult
    ) -> str:
        """Classify status with stable, non-flappy semantics.

        - ready:       ready_replicas >= desired
        - progressing: replicas for this revision exist (even if liveness fails yet),
                       or runtime reported creates/updates but replicas not observed yet (race)
        - degraded:    no replicas exist and no recent create/update for this revision

        Rationale: right after scheduling/creation, HTTP liveness probes can fail
        briefly until the container publishes ports and starts serving. Treating
        that period as "progressing" avoids misleading degraded states during
        normal warm-up while still surfacing real outages (no replicas present).
        """
        is_job = str(getattr(manifest.spec, "workload", "service")).lower() == "job"
        if is_job:
            desired = max(1, int(manifest.spec.replicas))
            succeeded = 0
            failed = 0
            running = 0
            for rs in runtime_result.pod_states:
                if getattr(rs, "status", "") == "running":
                    running += 1
                rc = getattr(rs, "exit_code", None)
                if rc is None:
                    continue
                if rc == 0:
                    succeeded += 1
                else:
                    failed += 1
            if succeeded >= desired:
                return "ready"
            if failed > 0 and running == 0:
                return "degraded"
            if len(report.pods) > 0 or (runtime_result.created + runtime_result.updated) > 0:
                return "progressing"
            return "degraded"
        desired = max(1, int(manifest.spec.replicas))
        if report.ready_replicas >= desired:
            return "ready"
        # Consider any recorded replica (regardless of liveness) as progressing.
        if len(report.pods) > 0:
            return "progressing"
        # Race: runtime created/updated containers but list didn't return them yet
        if (runtime_result.created + runtime_result.updated) > 0:
            return "progressing"
        return "degraded"

    def _apply_configs_and_secrets(self, manifest: AppManifest) -> AppManifest:
        env_map: dict[str, str] = {}

        # Configs first
        if getattr(manifest.spec, "config_refs", None):
            cfg_env = self._config_manager.load_env(manifest.spec.config_refs)
            env_map.update(cfg_env)

        # Secrets override configs
        if manifest.spec.secret_refs:
            if self._secret_manager:
                sec_env = self._secret_manager.load_env(manifest.spec.secret_refs)
                env_map.update(sec_env)
            else:
                import json, yaml
                from pathlib import Path as _P

                for ref in manifest.spec.secret_refs:
                    try:
                        content = _P(ref.path).read_text(encoding="utf-8")
                        try:
                            data = json.loads(content)
                        except json.JSONDecodeError:
                            data = yaml.safe_load(content)
                        if isinstance(data, dict):
                            for m in ref.env:
                                if m.key in data:
                                    env_map[m.name] = str(data[m.key])
                    except Exception:
                        pass

        # Manifest env wins last
        for item in manifest.spec.env:
            if "name" in item and "value" in item:
                env_map[item["name"]] = item["value"]

        if not env_map:
            return manifest

        merged_env = [{"name": k, "value": v} for k, v in sorted(env_map.items())]
        updated_spec = manifest.spec.model_copy(update={"env": merged_env})
        return manifest.model_copy(update={"spec": updated_spec})

    def _on_probe_event(
        self, pod_name: str, probe_type: str, success: bool, message: str
    ) -> None:
        """Hook from HealthManager when probe effective status changes."""
        app_name = pod_name
        try:
            import re

            m = re.match(r"^(?P<app>.+)-rev\d+-\d+$", str(pod_name))
            if m:
                app_name = m.group("app")
            elif "-" in pod_name:
                app_name = pod_name.split("-", 1)[0]
        except Exception:
            app_name = pod_name.split("-", 1)[0] if "-" in pod_name else pod_name
        try:
            self._state_store.record_event(
                app_name,
                0,
                f"{probe_type.capitalize()}{'OK' if success else 'Fail'}",
                f"{pod_name}: {message}",
            )
            _record_event_metric_safe(f"{probe_type}_{'ok' if success else 'fail'}")
        except Exception:
            pass

    def _prepare_file_projections(self, manifest: AppManifest, revision: int) -> Path | None:
        """Write config and secret key/value pairs into files for the app.

        Writes two folders under state/projections/<app>-rev<rev>/{config,secret} with one
        file per key containing the string value. Returns the projection root if any files
        were written, else None.
        """
        app = app_key_for_manifest(manifest)
        root = Path("state/projections") / f"{app}-rev{revision}"
        wrote = False

        # Config files
        if getattr(manifest.spec, "config_refs", None):
            cfg_dir = root / "config"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            for ref in manifest.spec.config_refs:
                try:
                    data = self._config_manager._load(Path(ref.path))  # type: ignore[arg-type]
                except Exception:
                    continue
                if getattr(ref, "files", None):
                    for mapping in ref.files:
                        k = str(mapping.get("key", ""))
                        fn = str(mapping.get("file", ""))
                        if not k or not fn:
                            continue
                        if k not in data:
                            continue
                        path = (root / fn) if fn.startswith("/") else (cfg_dir / fn)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            path.write_text(str(data[k]), encoding="utf-8")
                            wrote = True
                        except Exception:
                            pass
                else:
                    for k, v in data.items():
                        try:
                            (cfg_dir / str(k)).write_text(str(v), encoding="utf-8")
                            wrote = True
                        except Exception:
                            pass

        # Secret files
        if manifest.spec.secret_refs:
            sec_dir = root / "secret"
            sec_dir.mkdir(parents=True, exist_ok=True)
            for ref in manifest.spec.secret_refs:
                # load via manager if available, else plaintext YAML/JSON
                data = None
                if self._secret_manager:
                    try:
                        data = self._secret_manager._decrypt(Path(ref.path))  # type: ignore[arg-type]
                    except Exception:
                        data = None
                if data is None:
                    try:
                        content = Path(ref.path).read_text(encoding="utf-8")
                        import json, yaml

                        try:
                            data = json.loads(content)
                        except json.JSONDecodeError:
                            data = yaml.safe_load(content)
                    except Exception:
                        data = None
                if not isinstance(data, dict):
                    continue
                if getattr(ref, "files", None):
                    for mapping in ref.files:
                        k = str(mapping.get("key", ""))
                        fn = str(mapping.get("file", ""))
                        if not k or not fn:
                            continue
                        if k not in data:
                            continue
                        path = (root / fn) if fn.startswith("/") else (sec_dir / fn)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            path.write_text(str(data[k]), encoding="utf-8")
                            wrote = True
                        except Exception:
                            pass
                else:
                    for k, v in data.items():
                        try:
                            (sec_dir / str(k)).write_text(str(v), encoding="utf-8")
                            wrote = True
                        except Exception:
                            pass

        # Prefer absolute path for runtime volume mounts to avoid Docker/Podman
        # treating relative paths as named volumes (which can fail validation).
        try:
            abs_root = root.resolve()
        except Exception:
            abs_root = root
        return abs_root if wrote else None

    def _get_apishim_store(self):
        if self._apishim_store_checked:
            return self._apishim_store
        self._apishim_store_checked = True
        try:
            from ae.apishim.store import ObjectStore
        except Exception:
            self._apishim_store = None
            return None
        dsn = os.getenv("AE_APISHIM_DSN")
        db_env = os.getenv("AE_APISHIM_DB")
        db_path = Path(db_env or "state/apishim.db")
        if not dsn and not db_path.exists():
            self._apishim_store = None
            return None
        try:
            self._apishim_store = ObjectStore(dsn=dsn) if dsn else ObjectStore(db_path=db_path)
        except Exception:
            self._apishim_store = None
        return self._apishim_store

    def _apply_selected_node_annotations(
        self, manifest: AppManifest, placements: list, revision: int
    ) -> None:
        pvc_mounts = list(getattr(manifest.spec, "pvc_mounts", []) or [])
        if not pvc_mounts:
            return
        store = self._get_apishim_store()
        if store is None:
            return
        nodes = [
            getattr(getattr(pl, "node", None), "node_id", None)
            for pl in placements
            if getattr(getattr(pl, "node", None), "node_id", None)
        ]
        if not nodes:
            return
        target_node = nodes[0]
        namespace = getattr(getattr(manifest, "metadata", None), "namespace", None) or "default"
        pvcs = {
            (str(pm.claim_name), str(getattr(pm, "namespace", None) or namespace))
            for pm in pvc_mounts
        }
        warnings: list[str] = []
        for claim_name, ns in pvcs:
            pvc = store.get(CORE_GROUP, CORE_VERSION, PVC_RESOURCE, ns, claim_name)
            if pvc is None:
                continue
            if not self._pvc_needs_selected_node(store, pvc):
                continue
            selected = self._pvc_selected_node(pvc)
            if selected and selected != target_node:
                warnings.append(
                    f"PVC {ns}/{claim_name} already selected node {selected}; scheduled node is {target_node}"
                )
                continue
            if selected == target_node:
                continue
            meta = dict(getattr(pvc, "metadata", None) or {})
            annotations = dict(meta.get("annotations") or {})
            annotations[SELECTED_NODE_ANNOTATION] = target_node
            meta["annotations"] = annotations
            try:
                store.upsert(
                    CORE_GROUP,
                    CORE_VERSION,
                    PVC_RESOURCE,
                    ns,
                    claim_name,
                    meta,
                    dict(getattr(pvc, "spec", None) or {}),
                    status=dict(getattr(pvc, "status", None) or {}),
                )
            except Exception as exc:
                warnings.append(f"failed to set selected node on PVC {ns}/{claim_name}: {exc}")

        if len(set(nodes)) > 1:
            warnings.append(
                "multiple nodes scheduled while single-writer PVCs require a single selected node"
            )
        if not warnings:
            return
        app_name = app_key_for_manifest(manifest)
        for w in warnings:
            try:
                self._state_store.record_event(app_name, revision, "ScheduleWarning", w)
            except Exception:
                continue

    def _pvc_needs_selected_node(self, store, pvc) -> bool:
        pv = self._bound_pv(store, pvc)
        if pv is not None:
            pv_spec = self._obj_spec(pv)
            csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
            if isinstance(csi, dict) and self._is_single_writer(pv_spec):
                return True
        sc_name = self._pvc_storage_class_name(pvc) or self._default_storage_class_name(store)
        if not sc_name:
            return False
        sc = store.get(SC_GROUP, SC_VERSION, SC_RESOURCE, None, sc_name)
        if sc is None:
            return False
        sc_spec = self._obj_spec(sc)
        provisioner = str(sc_spec.get("provisioner") or "")
        binding_mode = str(sc_spec.get("volumeBindingMode") or "")
        return provisioner == LOCAL_PATH_PROVISIONER and binding_mode == WAIT_FOR_FIRST_CONSUMER

    def _default_storage_class_name(self, store) -> str | None:
        if self._default_sc_name:
            return self._default_sc_name
        try:
            classes = store.list_all(SC_GROUP, SC_VERSION, SC_RESOURCE)
        except Exception:
            return None
        for sc in classes:
            meta = getattr(sc, "metadata", None)
            annotations = meta.get("annotations") if isinstance(meta, dict) else {}
            if not isinstance(annotations, dict):
                continue
            for key in DEFAULT_CLASS_ANNOTATIONS:
                raw = annotations.get(key)
                if raw is not None and str(raw).lower() in {"true", "1", "yes"}:
                    self._default_sc_name = sc.name
                    return self._default_sc_name
        if classes:
            self._default_sc_name = classes[0].name
            return self._default_sc_name
        return None

    def _bound_pv(self, store, pvc):
        spec = getattr(pvc, "spec", None)
        pv_name = spec.get("volumeName") if isinstance(spec, dict) else None
        if not pv_name:
            return None
        try:
            return store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, str(pv_name))
        except Exception:
            return None

    @staticmethod
    def _obj_spec(obj: Any) -> dict[str, Any]:
        spec = getattr(obj, "spec", None)
        return spec if isinstance(spec, dict) else {}

    @staticmethod
    def _pvc_storage_class_name(pvc) -> str | None:
        spec = getattr(pvc, "spec", None)
        if not isinstance(spec, dict):
            return None
        name = spec.get("storageClassName")
        return str(name) if name else None

    @staticmethod
    def _pvc_selected_node(pvc) -> str | None:
        meta = getattr(pvc, "metadata", None)
        annotations = meta.get("annotations") if isinstance(meta, dict) else {}
        if not isinstance(annotations, dict):
            return None
        node = annotations.get(SELECTED_NODE_ANNOTATION)
        return str(node) if node else None

    @staticmethod
    def _pvc_is_bound(pvc) -> bool:
        spec = getattr(pvc, "spec", None)
        status = getattr(pvc, "status", None)
        vol = spec.get("volumeName") if isinstance(spec, dict) else None
        phase = status.get("phase") if isinstance(status, dict) else None
        return bool(vol) or phase == "Bound"

    @staticmethod
    def _is_single_writer(spec: dict[str, Any]) -> bool:
        modes = set(spec.get("accessModes") or []) if isinstance(spec, dict) else set()
        return not bool(modes & {"ReadWriteMany", "ReadOnlyMany"})


# ruff: noqa
