from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from ae.controller.health import HealthReport
from ae.controller.spec import AppManifest, app_key_for_manifest
from ae.controller.state import (
    AuthorityObjectEntry,
    AppEvent,
    AppStatus,
    EdgeIngressPolicyRecord,
    EdgeIngressRouteRecord,
    NodeLease,
    NodeRecord,
    NodeStatus,
    PodStatus,
    ProbeHistoryEntry,
    RegistryConflictError,
    RegistryEntry,
    RevisionInfo,
    ServiceEndpoint,
    ServiceListItem,
    ServiceRecord,
    SiteIngressEndpoint,
    SiteIngressListItem,
    VolumeAttachment,
    WorkloadMetricsSnapshot,
    WorkLedgerEntry,
    WorkOutboxEntry,
    WorkQueueLease,
    SQLiteStateStore,
    _outbox_publish_msg_id,
    _outbox_publish_subject,
)
from ae.ha.fencing import parse_envelope, work_operation
from ae.runtime import RuntimeResult


def _b64encode(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return base64.b64encode(raw).decode("ascii")


def _b64decode(value: str | None) -> bytes:
    if not value:
        return b""
    return base64.b64decode(value.encode("ascii"))


def _prefix_end(prefix: bytes) -> bytes:
    if not prefix:
        return b"\x00"
    buf = bytearray(prefix)
    for idx in range(len(buf) - 1, -1, -1):
        if buf[idx] < 0xFF:
            buf[idx] += 1
            return bytes(buf[: idx + 1])
    return b"\x00"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _dt_from_iso(value: str | None, *, default: datetime | None = None) -> datetime | None:
    if not value:
        return default
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return default


def _parse_duration_seconds(value: str | None, default: float) -> float:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        pass
    for suffix, mult in ("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0):
        if raw.endswith(suffix):
            try:
                return float(raw[: -len(suffix)]) * mult
            except ValueError:
                return default
    return default


class EtcdHttpClient:
    def __init__(
        self,
        endpoints: list[str],
        *,
        api_prefix: str | None = None,
        timeout_s: float = 3.0,
        ca_cert: str | None = None,
        cert: str | None = None,
        key: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self._endpoints = [e.rstrip("/") for e in endpoints if e and e.strip()]
        if not self._endpoints:
            raise ValueError("etcd endpoints required")
        self._api_prefixes = []
        if api_prefix:
            self._api_prefixes.append(api_prefix.strip("/"))
        else:
            self._api_prefixes.extend(["v3", "v3alpha"])
        self._active_prefix: str | None = None
        self._timeout_s = float(timeout_s)
        self._retry_max = max(1, int(os.getenv("AE_ETCD_RETRY_MAX", "3") or "3"))
        self._retry_backoff_s = _parse_duration_seconds(
            os.getenv("AE_ETCD_RETRY_BACKOFF", "100ms"),
            0.1,
        )
        self._retry_jitter = max(0.0, min(1.0, float(os.getenv("AE_ETCD_RETRY_JITTER", "0.2") or "0.2")))
        self._verify: bool | str = True
        if ca_cert:
            self._verify = ca_cert
        self._cert = (cert, key) if cert and key else cert
        self._user = user
        self._password = password
        self._token: str | None = None
        if self._user and self._password:
            self._authenticate()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = self._token
        return headers

    def _authenticate(self) -> None:
        if not self._user or not self._password:
            return
        payload = {"name": self._user, "password": self._password}
        resp = self._post("/auth/authenticate", payload, allow_auth=False)
        token = resp.get("token") if isinstance(resp, dict) else None
        if not token:
            raise RuntimeError("etcd auth failed: missing token")
        self._token = str(token)

    def _retry_sleep(self, attempt: int) -> None:
        base = max(0.0, self._retry_backoff_s)
        if base <= 0:
            return
        delay = base * (2**max(0, attempt - 1))
        if self._retry_jitter > 0:
            jitter = (random.random() * 2 - 1) * self._retry_jitter
            delay *= max(0.0, 1.0 + jitter)
        time.sleep(max(0.0, delay))

    @staticmethod
    def _is_transient_status(status_code: int) -> bool:
        return status_code in {408, 425, 429, 500, 502, 503, 504}

    @staticmethod
    def _response_summary(resp: requests.Response) -> str:
        text = (resp.text or "").strip().replace("\n", " ")
        if len(text) > 160:
            text = text[:157] + "..."
        return text

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_s: float | None = None,
        allow_auth: bool = True,
    ) -> dict[str, Any]:
        last_non_404_exc: Exception | None = None
        last_404_exc: Exception | None = None
        prefixes: list[str] = []
        if self._active_prefix:
            prefixes.append(self._active_prefix)
        prefixes.extend([p for p in self._api_prefixes if p not in prefixes])
        for endpoint in self._endpoints:
            endpoint_non_404_exc: Exception | None = None
            endpoint_404_exc: Exception | None = None
            for prefix in prefixes:
                # Fallback probing is only for API discovery. If we already saw a
                # non-404 error on this endpoint, keep that signal and avoid
                # masking it with prefix fallback noise.
                if endpoint_non_404_exc is not None and prefix != self._active_prefix:
                    continue
                url = f"{endpoint}/{prefix}{path}"
                for attempt in range(1, self._retry_max + 1):
                    try:
                        resp = requests.post(
                            url,
                            json=payload,
                            timeout=timeout_s or self._timeout_s,
                            verify=self._verify,
                            cert=self._cert,
                            headers=self._headers(),
                        )
                        if resp.status_code in {401, 403} and allow_auth and self._user:
                            self._authenticate()
                            resp = requests.post(
                                url,
                                json=payload,
                                timeout=timeout_s or self._timeout_s,
                                verify=self._verify,
                                cert=self._cert,
                                headers=self._headers(),
                            )
                        if resp.status_code == 404 and prefix != self._active_prefix:
                            endpoint_404_exc = RuntimeError(f"etcd api not found at {url}")
                            break
                        if self._is_transient_status(resp.status_code):
                            err = RuntimeError(
                                f"etcd status {resp.status_code} from {url}: {self._response_summary(resp)}"
                            )
                            if attempt < self._retry_max:
                                self._retry_sleep(attempt)
                                continue
                            endpoint_non_404_exc = err
                            break
                        resp.raise_for_status()
                        try:
                            data = resp.json()
                        except ValueError as exc:
                            raise RuntimeError(f"etcd non-json response from {url}") from exc
                        self._active_prefix = prefix
                        return data
                    except (requests.Timeout, requests.ConnectionError) as exc:
                        if attempt < self._retry_max:
                            self._retry_sleep(attempt)
                            continue
                        endpoint_non_404_exc = exc
                        break
                    except Exception as exc:  # noqa: BLE001
                        endpoint_non_404_exc = exc
                        break
                if endpoint_non_404_exc is not None:
                    break
            if endpoint_non_404_exc is not None:
                last_non_404_exc = endpoint_non_404_exc
                continue
            if endpoint_404_exc is not None:
                last_404_exc = endpoint_404_exc
                continue
        final_exc = last_non_404_exc or last_404_exc or RuntimeError("unknown etcd error")
        final_msg = f"etcd request failed: {final_exc}"
        if "mvcc: database space exceeded" in str(final_exc).lower():
            final_msg = (
                f"{final_msg}. etcd quota is exhausted; run "
                "scripts/dev/etcd_maintenance.sh compact-defrag "
                "or reset dev state (make dev-state-clean CONFIRM=1)."
            )
        raise RuntimeError(final_msg)

    def range(
        self,
        key: str,
        *,
        range_end: str | bytes | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"key": _b64encode(key)}
        if range_end is not None:
            payload["range_end"] = _b64encode(range_end)
        if limit is not None:
            payload["limit"] = int(limit)
        return self._post("/kv/range", payload)

    def put(self, key: str, value: str, *, lease: int | None = None) -> None:
        payload: dict[str, Any] = {"key": _b64encode(key), "value": _b64encode(value)}
        if lease:
            payload["lease"] = int(lease)
        self._post("/kv/put", payload)

    def delete(self, key: str) -> None:
        payload = {"key": _b64encode(key)}
        self._post("/kv/deleterange", payload)

    def delete_prefix(self, prefix: str) -> None:
        rng = _prefix_end(prefix.encode("utf-8"))
        payload = {
            "key": _b64encode(prefix),
            "range_end": _b64encode(rng),
        }
        self._post("/kv/deleterange", payload)

    def txn(self, compare: list[dict], success: list[dict], failure: list[dict]) -> dict:
        payload = {"compare": compare, "success": success, "failure": failure}
        return self._post("/kv/txn", payload)

    def grant_lease(self, ttl_seconds: int) -> int:
        payload = {"TTL": int(ttl_seconds)}
        resp = self._post("/lease/grant", payload)
        lease_id = resp.get("ID") if isinstance(resp, dict) else None
        if lease_id is None:
            raise RuntimeError("etcd lease grant failed")
        return int(lease_id)

    def revoke_lease(self, lease_id: int) -> None:
        payload = {"ID": int(lease_id)}
        self._post("/lease/revoke", payload)

    def maintenance_status(self) -> dict[str, Any]:
        return self._post("/maintenance/status", {})

    def maintenance_alarms(self, *, action: str = "GET", member_id: str | None = None, alarm: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": str(action).upper()}
        if member_id is not None:
            payload["memberID"] = str(member_id)
        if alarm is not None:
            payload["alarm"] = str(alarm).upper()
        return self._post("/maintenance/alarm", payload)

    def compact(self, revision: int, *, physical: bool = True) -> None:
        payload = {"revision": str(int(revision)), "physical": bool(physical)}
        self._post("/kv/compaction", payload)

    def defragment(self) -> None:
        self._post("/maintenance/defragment", {})


class EtcdStateStore(SQLiteStateStore):
    """Etcd-backed state store (dev-etcd)."""

    def __init__(
        self,
        *,
        endpoints: list[str] | None = None,
        prefix: str | None = None,
        site_id: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        env = os.environ
        raw_endpoints = endpoints or [
            e.strip() for e in (env.get("AE_ETCD_ENDPOINTS") or "http://127.0.0.1:2379").split(",")
        ]
        self.backend = "etcd"
        self._prefix = (prefix or env.get("AE_ETCD_PREFIX") or "k1s/v1").strip("/")
        self._site_id = site_id or env.get("AE_SITE_ID") or "core"
        dial_timeout = _parse_duration_seconds(env.get("AE_ETCD_DIAL_TIMEOUT"), 3.0)
        op_timeout = _parse_duration_seconds(env.get("AE_ETCD_OP_TIMEOUT"), 3.0)
        timeout_s = float(timeout_s if timeout_s is not None else op_timeout)
        self._lease_ttl_seconds = int(env.get("AE_ETCD_LEASE_TTL_SECONDS", "60") or 60)
        refresh_ratio = float(env.get("AE_ETCD_LEASE_REFRESH_RATIO", "0.5") or 0.5)
        self._lease_refresh_ratio = max(0.05, min(0.95, refresh_ratio))
        self._node_leases: dict[str, tuple[int, float]] = {}
        ca_cert = env.get("AE_ETCD_CA") or None
        cert = env.get("AE_ETCD_CERT") or None
        key = env.get("AE_ETCD_KEY") or None
        user = env.get("AE_ETCD_USER") or None
        password = env.get("AE_ETCD_PASSWORD") or None
        api_prefix = env.get("AE_ETCD_API_PREFIX") or None
        self._client = EtcdHttpClient(
            raw_endpoints,
            api_prefix=api_prefix,
            timeout_s=timeout_s,
            ca_cert=ca_cert,
            cert=cert,
            key=key,
            user=user,
            password=password,
        )
        # Basic connectivity check
        try:
            self._client.range(self._k("_health"), limit=1)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"etcd unreachable: {exc}") from exc

    def _k(self, *parts: str) -> str:
        clean = [p.strip("/") for p in parts if p]
        if self._prefix:
            return "/".join([self._prefix, *clean])
        return "/".join(clean)

    def _encode(self, payload: dict) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _decode(self, raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    @staticmethod
    def _is_lease_not_found_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "lease not found" in msg or "requested lease not found" in msg

    def _lease_refresh_after_seconds(self) -> float:
        ttl_s = max(2.0, float(self._lease_ttl_seconds))
        refresh_after = ttl_s * self._lease_refresh_ratio
        return max(1.0, min(ttl_s - 1.0, refresh_after))

    def _grant_node_lease(self, node_id: str, *, force_new: bool = False) -> tuple[int, bool]:
        now = time.monotonic()
        current = self._node_leases.get(node_id)
        if not force_new and current is not None and now < current[1]:
            return current[0], False
        lease_id = self._client.grant_lease(self._lease_ttl_seconds)
        refresh_at = now + self._lease_refresh_after_seconds()
        self._node_leases[node_id] = (lease_id, refresh_at)
        return lease_id, True

    def _put_with_node_lease(
        self,
        *,
        key: str,
        payload: dict,
        node_id: str,
        lease_id: int,
    ) -> tuple[int, bool]:
        try:
            self._put_json(key, payload, lease_id=lease_id)
            return lease_id, False
        except Exception as exc:
            if not self._is_lease_not_found_error(exc):
                raise
        lease_id, _ = self._grant_node_lease(node_id, force_new=True)
        self._put_json(key, payload, lease_id=lease_id)
        return lease_id, True

    @staticmethod
    def _node_payload_changed(existing: dict | None, payload: dict) -> bool:
        if not existing:
            return True
        for key in (
            "node_id",
            "name",
            "labels",
            "taints",
            "backend",
            "endpoint",
            "pod_cidr",
            "wg_pubkey",
            "rp_pubkey",
            "cordoned",
        ):
            if existing.get(key) != payload.get(key):
                return True
        return False

    @staticmethod
    def _record_heartbeat_metrics(*, node_rewrite: bool) -> None:
        try:
            from ae.observability.http_api import record_heartbeat_write

            record_heartbeat_write(node_rewrite=node_rewrite)
        except Exception:
            pass

    def _get_json(self, key: str) -> tuple[dict | None, int]:
        resp = self._client.range(key, limit=1)
        kvs = resp.get("kvs") or []
        if not kvs:
            return None, 0
        kv = kvs[0]
        value = _b64decode(kv.get("value")).decode("utf-8")
        mod_rev = int(kv.get("mod_revision") or 0)
        return self._decode(value), mod_rev

    def _put_json(self, key: str, payload: dict, *, lease_id: int | None = None) -> None:
        self._client.put(key, self._encode(payload), lease=lease_id)

    def _delete(self, key: str) -> None:
        self._client.delete(key)

    def _delete_prefix(self, prefix: str) -> None:
        self._client.delete_prefix(prefix)

    def _list_prefix(self, prefix: str) -> list[tuple[str, dict, int]]:
        resp = self._client.range(prefix, range_end=_prefix_end(prefix.encode("utf-8")))
        kvs = resp.get("kvs") or []
        out: list[tuple[str, dict, int]] = []
        for kv in kvs:
            key = _b64decode(kv.get("key")).decode("utf-8")
            value = _b64decode(kv.get("value")).decode("utf-8")
            mod_rev = int(kv.get("mod_revision") or 0)
            out.append((key, self._decode(value), mod_rev))
        return out

    def _manifest_hash(self, manifest: AppManifest) -> str:
        payload = json.dumps(
            manifest.model_dump(by_alias=True, exclude_none=True),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    # --- Status snapshots -------------------------------------------------
    def record_snapshot(
        self,
        manifest: AppManifest,
        runtime_result: RuntimeResult,
        health_report: HealthReport,
        revision: int,
        revision_status: str,
    ) -> None:
        app_name = app_key_for_manifest(manifest)
        status = {
            "app_name": app_name,
            "desired_replicas": int(manifest.spec.replicas),
            "ready_replicas": int(health_report.ready_replicas),
            "live_replicas": int(health_report.live_replicas),
            "revision": int(revision),
            "revision_status": str(revision_status),
            "image": str(manifest.spec.image),
            "created": int(runtime_result.created),
            "updated": int(runtime_result.updated),
            "removed": int(runtime_result.removed),
            "ingress_host": getattr(manifest.spec.ingress, "host", None)
            if manifest.spec.ingress
            else None,
            "ingress_path": getattr(manifest.spec.ingress, "path", None)
            if manifest.spec.ingress
            else None,
        }
        self._put_json(self._k("status", app_name), status)

        # Pods (preserve recent entries to avoid dashboard flicker)
        pods_prefix = self._k("pods", app_name)
        existing: dict[str, dict] = {}
        try:
            for key, rec, _rev in self._list_prefix(pods_prefix):
                pod_name = key.split("/")[-1]
                if pod_name:
                    existing[pod_name] = rec or {}
        except Exception:
            existing = {}
        ts = _now_iso()
        current_pods: set[str] = set()
        for pod in health_report.pods:
            current_pods.add(pod.pod_name)
            state = next(
                (s for s in runtime_result.pod_states if s.pod_name == pod.pod_name),
                None,
            )
            payload = {
                "pod_name": pod.pod_name,
                "ready": bool(pod.ready),
                "live": bool(pod.live),
                "status": getattr(state, "status", "unknown"),
                "endpoint": getattr(state, "endpoint", None),
                "readiness_message": pod.readiness_message,
                "liveness_message": pod.liveness_message,
                "exit_code": getattr(state, "exit_code", None),
                "finished_at": getattr(state, "finished_at", None).isoformat()
                if getattr(state, "finished_at", None)
                else None,
                "updated_at": ts,
            }
            self._put_json(self._k("pods", app_name, pod.pod_name), payload)

        try:
            ttl_seconds = int(
                os.getenv("AE_POD_STATUS_TTL_SECONDS", "30") or "30"
            )
        except Exception:
            ttl_seconds = 30
        if ttl_seconds > 0 and existing:
            cutoff = _now() - timedelta(seconds=ttl_seconds)
            for pod_name, rec in existing.items():
                if pod_name in current_pods:
                    continue
                updated_raw = rec.get("updated_at")
                updated_at = _dt_from_iso(updated_raw)
                if updated_at is None or updated_at < cutoff:
                    self._delete(self._k("pods", app_name, pod_name))

        # Pod placement hints
        # Preserve existing mappings when runtime results omit node_id to avoid dashboard flicker.
        pod_nodes_prefix = self._k("pod_nodes", app_name)
        ts = _now_iso()
        current_pods = {p.pod_name for p in health_report.pods if p.pod_name}
        existing_nodes: dict[str, dict] = {}
        try:
            for key, rec, _rev in self._list_prefix(pod_nodes_prefix):
                pod_name = key.split("/")[-1]
                if pod_name:
                    existing_nodes[pod_name] = rec or {}
        except Exception:
            existing_nodes = {}
        for rs in runtime_result.pod_states:
            node_id = getattr(rs, "node_id", None)
            if not node_id:
                continue
            payload = {"pod_name": rs.pod_name, "node_id": node_id, "updated_at": ts}
            self._put_json(self._k("pod_nodes", app_name, rs.pod_name), payload)
        # Refresh timestamps for known pods so their mappings stay warm.
        for pod_name in current_pods:
            rec = existing_nodes.get(pod_name)
            if not rec:
                continue
            node_id = rec.get("node_id")
            if not node_id:
                continue
            payload = {"pod_name": pod_name, "node_id": node_id, "updated_at": ts}
            self._put_json(self._k("pod_nodes", app_name, pod_name), payload)
        try:
            node_ttl_seconds = int(
                os.getenv("AE_POD_NODE_TTL_SECONDS", "300") or "300"
            )
        except Exception:
            node_ttl_seconds = 300
        if node_ttl_seconds > 0 and existing_nodes:
            cutoff = _now() - timedelta(seconds=node_ttl_seconds)
            for pod_name, rec in existing_nodes.items():
                if pod_name in current_pods:
                    continue
                updated_raw = rec.get("updated_at")
                updated_at = _dt_from_iso(updated_raw)
                if updated_at is None or updated_at < cutoff:
                    self._delete(self._k("pod_nodes", app_name, pod_name))

        # Probe history (keep last 50 per pod)
        for pod in health_report.pods:
            ts_key = f"{int(_now().timestamp() * 1_000_000):020d}"
            entry = {
                "pod_name": pod.pod_name,
                "check_time": ts,
                "ready": bool(pod.ready),
                "live": bool(pod.live),
                "readiness_message": pod.readiness_message,
                "liveness_message": pod.liveness_message,
            }
            key = self._k("probes", app_name, pod.pod_name, ts_key, uuid.uuid4().hex)
            self._put_json(key, entry)
            # prune
            prefix = self._k("probes", app_name, pod.pod_name)
            rows = self._list_prefix(prefix)
            rows.sort(key=lambda r: r[0], reverse=True)
            for key_row, _val, _rev in rows[50:]:
                self._delete(key_row)

        # Update revision status
        rev_key = self._k("apps", app_name, "revisions", str(revision))
        rec, _ = self._get_json(rev_key)
        if rec:
            rec["status"] = revision_status
            rec["image"] = manifest.spec.image
            self._put_json(rev_key, rec)

    def get_status(self, app_name: str) -> AppStatus | None:
        rec, _ = self._get_json(self._k("status", app_name))
        if not rec:
            return None
        return AppStatus(
            app_name=rec.get("app_name", app_name),
            desired_replicas=int(rec.get("desired_replicas", 0)),
            ready_replicas=int(rec.get("ready_replicas", 0)),
            live_replicas=int(rec.get("live_replicas", 0)),
            revision=int(rec.get("revision", 0)),
            revision_status=str(rec.get("revision_status", "")),
            image=str(rec.get("image", "")),
            created=int(rec.get("created", 0)),
            updated=int(rec.get("updated", 0)),
            removed=int(rec.get("removed", 0)),
            ingress_host=rec.get("ingress_host"),
            ingress_path=rec.get("ingress_path"),
        )

    def list_status(self) -> list[AppStatus]:
        rows = self._list_prefix(self._k("status"))
        items: list[AppStatus] = []
        for _key, rec, _rev in rows:
            items.append(
                AppStatus(
                    app_name=rec.get("app_name", ""),
                    desired_replicas=int(rec.get("desired_replicas", 0)),
                    ready_replicas=int(rec.get("ready_replicas", 0)),
                    live_replicas=int(rec.get("live_replicas", 0)),
                    revision=int(rec.get("revision", 0)),
                    revision_status=str(rec.get("revision_status", "")),
                    image=str(rec.get("image", "")),
                    created=int(rec.get("created", 0)),
                    updated=int(rec.get("updated", 0)),
                    removed=int(rec.get("removed", 0)),
                    ingress_host=rec.get("ingress_host"),
                    ingress_path=rec.get("ingress_path"),
                )
            )
        return items

    def list_pods(self, app_name: str) -> list[PodStatus]:
        rows = self._list_prefix(self._k("pods", app_name))
        items: list[PodStatus] = []
        for _key, rec, _rev in rows:
            finished = _dt_from_iso(rec.get("finished_at"))
            items.append(
                PodStatus(
                    pod_name=str(rec.get("pod_name", "")),
                    ready=bool(rec.get("ready", False)),
                    live=bool(rec.get("live", False)),
                    status=str(rec.get("status", "unknown")),
                    endpoint=rec.get("endpoint"),
                    readiness_message=str(rec.get("readiness_message", "")),
                    liveness_message=str(rec.get("liveness_message", "")),
                    exit_code=rec.get("exit_code"),
                    finished_at=finished,
                )
            )
        items.sort(key=lambda p: p.pod_name)
        return items

    def list_pod_nodes(self, app_name: str) -> list[tuple[str, str | None, bool, bool, str, str, str]]:
        pods = {p.pod_name: p for p in self.list_pods(app_name)}
        nodes = {
            key.split("/")[-1]: rec.get("node_id")
            for key, rec, _rev in self._list_prefix(self._k("pod_nodes", app_name))
        }
        rows: list[tuple[str, str | None, bool, bool, str, str, str]] = []
        all_names = sorted(set(pods.keys()) | set(nodes.keys()))
        for pod_name in all_names:
            pod = pods.get(pod_name)
            rows.append(
                (
                    pod_name,
                    nodes.get(pod_name),
                    bool(getattr(pod, "ready", False)) if pod else False,
                    bool(getattr(pod, "live", False)) if pod else False,
                    str(getattr(pod, "status", "unknown")) if pod else "unknown",
                    str(getattr(pod, "readiness_message", "")) if pod else "",
                    str(getattr(pod, "liveness_message", "")) if pod else "",
                )
            )
        return rows

    def set_pod_nodes(self, app_name: str, placements: list[tuple[str, str]]) -> None:
        prefix = self._k("pod_nodes", app_name)
        self._delete_prefix(prefix)
        ts = _now_iso()
        for pod_name, node_id in placements:
            payload = {"pod_name": pod_name, "node_id": node_id, "updated_at": ts}
            self._put_json(self._k("pod_nodes", app_name, pod_name), payload)

    def get_probe_history(self, app_name: str, limit: int) -> list[ProbeHistoryEntry]:
        rows = self._list_prefix(self._k("probes", app_name))
        rows.sort(key=lambda r: r[0], reverse=True)
        entries: list[ProbeHistoryEntry] = []
        for _key, rec, _rev in rows[: int(limit)]:
            check_time = _dt_from_iso(rec.get("check_time"), default=datetime.fromtimestamp(0, tz=timezone.utc))
            entries.append(
                ProbeHistoryEntry(
                    pod_name=str(rec.get("pod_name", "")),
                    check_time=check_time,
                    ready=bool(rec.get("ready", False)),
                    live=bool(rec.get("live", False)),
                    readiness_message=str(rec.get("readiness_message", "")),
                    liveness_message=str(rec.get("liveness_message", "")),
                )
            )
        return entries

    # --- Registry / revisions -----------------------------------------
    def prepare_revision(self, manifest: AppManifest, spec_hash: str) -> tuple[int, bool]:
        app_name = app_key_for_manifest(manifest)
        latest = self._get_latest_revision(app_name)
        if latest and latest.spec_hash == spec_hash:
            return latest.revision, False
        next_revision = (latest.revision if latest else 0) + 1 if latest else 1
        created_at = _now_iso()
        payload = {
            "revision": int(next_revision),
            "spec_hash": spec_hash,
            "spec": manifest.model_dump(by_alias=True),
            "image": manifest.spec.image,
            "status": "pending",
            "created_at": created_at,
        }
        self._put_json(self._k("apps", app_name, "revisions", str(next_revision)), payload)
        return next_revision, True

    def register_app(
        self,
        manifest: AppManifest,
        *,
        source: str | None = None,
        labels: dict | None = None,
        expected_resource_version: int | None = None,
    ) -> int:
        app_name = app_key_for_manifest(manifest)
        key = self._k("apps", app_name, "registry")
        existing, current_rv = self._get_json(key)
        if source is None and existing is not None:
            source = str(existing.get("source", ""))
        if labels is None and existing is not None:
            current_labels = existing.get("labels") or {}
            labels = current_labels if isinstance(current_labels, dict) else {}
        spec_hash = self._manifest_hash(manifest)
        updated_at = _now_iso()
        payload = {
            "app_name": app_name,
            "spec": manifest.model_dump(by_alias=True),
            "spec_hash": spec_hash,
            "source": source or "",
            "labels": labels or {},
            "updated_at": updated_at,
        }
        if expected_resource_version is None:
            self._put_json(key, payload)
            return self._get_json(key)[1]
        compare: list[dict]
        if int(expected_resource_version) == 0:
            compare = [
                {
                    "key": _b64encode(key),
                    "target": "CREATE",
                    "createRevision": "0",
                }
            ]
        else:
            compare = [
                {
                    "key": _b64encode(key),
                    "target": "MOD",
                    "modRevision": str(int(expected_resource_version)),
                }
            ]
        success = [
            {
                "requestPut": {
                    "key": _b64encode(key),
                    "value": _b64encode(self._encode(payload)),
                }
            }
        ]
        failure = [{"requestRange": {"key": _b64encode(key), "limit": 1}}]
        resp = self._client.txn(compare, success, failure)
        if not bool(resp.get("succeeded")):
            actual_rv = self._get_json(key)[1]
            raise RegistryConflictError(
                app_name,
                expected=int(expected_resource_version),
                actual=actual_rv,
            )
        return self._get_json(key)[1]

    def list_registered_apps(self) -> list[RegistryEntry]:
        rows = self._list_prefix(self._k("apps"))
        out: list[RegistryEntry] = []
        for key, rec, _rev in rows:
            if not key.endswith("/registry"):
                continue
            spec = rec.get("spec") or {}
            man = AppManifest.model_validate(spec)
            updated = _dt_from_iso(rec.get("updated_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
            out.append(
                RegistryEntry(
                    app_name=str(rec.get("app_name", "")),
                    manifest=man,
                    spec_hash=str(rec.get("spec_hash", "")),
                    source=str(rec.get("source", "")),
                    labels=rec.get("labels") or {},
                    updated_at=updated or datetime.fromtimestamp(0, tz=timezone.utc),
                    resource_version=int(_rev or 0),
                )
            )
        return out

    def list_registered_app_names(self) -> list[str]:
        names = []
        for key, rec, _rev in self._list_prefix(self._k("apps")):
            if key.endswith("/registry"):
                names.append(str(rec.get("app_name", "")))
        return [n for n in names if n]

    def get_registered_entry(self, app_name: str) -> RegistryEntry | None:
        rec, mod_rev = self._get_json(self._k("apps", app_name, "registry"))
        if not rec:
            return None
        spec = rec.get("spec") or {}
        updated = _dt_from_iso(rec.get("updated_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
        return RegistryEntry(
            app_name=app_name,
            manifest=AppManifest.model_validate(spec),
            spec_hash=str(rec.get("spec_hash", "")),
            source=str(rec.get("source", "")),
            labels=rec.get("labels") or {},
            updated_at=updated or datetime.fromtimestamp(0, tz=timezone.utc),
            resource_version=int(mod_rev or 0),
        )

    def get_registered_manifest(self, app_name: str) -> AppManifest | None:
        entry = self.get_registered_entry(app_name)
        return entry.manifest if entry else None

    def delete_registered_app(
        self,
        app_name: str,
        *,
        expected_resource_version: int | None = None,
    ) -> bool:
        key = self._k("apps", app_name, "registry")
        if expected_resource_version is None:
            existing, _rv = self._get_json(key)
            if existing is None:
                return False
            self._delete(key)
            return True
        compare = [
            {
                "key": _b64encode(key),
                "target": "MOD",
                "modRevision": str(int(expected_resource_version)),
            }
        ]
        success = [{"requestDeleteRange": {"key": _b64encode(key)}}]
        failure = [{"requestRange": {"key": _b64encode(key), "limit": 1}}]
        resp = self._client.txn(compare, success, failure)
        if not bool(resp.get("succeeded")):
            actual_rv = self._get_json(key)[1]
            raise RegistryConflictError(
                app_name,
                expected=int(expected_resource_version),
                actual=actual_rv,
            )
        return True

    @staticmethod
    def _authority_ns_key(namespace: str | None) -> str:
        return str(namespace or "_cluster")

    def _authority_key(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
    ) -> str:
        return self._k(
            "apishim",
            "authority",
            group or "_core",
            version,
            resource,
            self._authority_ns_key(namespace),
            name,
        )

    def register_authority_object(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        *,
        kind: str,
        metadata: dict | None = None,
        spec: dict | None = None,
        status: dict | None = None,
        expected_resource_version: int | None = None,
    ) -> int:
        key = self._authority_key(group, version, resource, namespace, name)
        payload = {
            "group": group,
            "version": version,
            "resource": resource,
            "namespace": namespace or "",
            "name": name,
            "kind": kind,
            "metadata": metadata or {},
            "spec": spec or {},
            "status": status or {},
            "updated_at": _now_iso(),
        }
        conflict_key = self._authority_object_conflict_key(group, version, resource, namespace, name)
        if expected_resource_version is None:
            self._put_json(key, payload)
            return self._get_json(key)[1]
        compare: list[dict]
        if int(expected_resource_version) == 0:
            compare = [{"key": _b64encode(key), "target": "CREATE", "createRevision": "0"}]
        else:
            compare = [
                {
                    "key": _b64encode(key),
                    "target": "MOD",
                    "modRevision": str(int(expected_resource_version)),
                }
            ]
        success = [
            {
                "requestPut": {
                    "key": _b64encode(key),
                    "value": _b64encode(self._encode(payload)),
                }
            }
        ]
        failure = [{"requestRange": {"key": _b64encode(key), "limit": 1}}]
        resp = self._client.txn(compare, success, failure)
        if not bool(resp.get("succeeded")):
            actual_rv = self._get_json(key)[1]
            raise RegistryConflictError(
                conflict_key,
                expected=int(expected_resource_version),
                actual=actual_rv,
            )
        return self._get_json(key)[1]

    def list_authority_objects(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None = None,
    ) -> list[AuthorityObjectEntry]:
        prefix = self._k("apishim", "authority", group or "_core", version, resource)
        if namespace is not None:
            prefix = self._k(prefix, self._authority_ns_key(namespace))
        rows = self._list_prefix(prefix)
        out: list[AuthorityObjectEntry] = []
        for _key, rec, mod_rev in rows:
            namespace_value = rec.get("namespace")
            out.append(
                AuthorityObjectEntry(
                    group=str(rec.get("group", "")),
                    version=str(rec.get("version", version)),
                    resource=str(rec.get("resource", resource)),
                    namespace=(str(namespace_value) if namespace_value not in {None, ""} else None),
                    name=str(rec.get("name", "")),
                    kind=str(rec.get("kind", "")),
                    metadata=rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {},
                    spec=rec.get("spec") if isinstance(rec.get("spec"), dict) else {},
                    status=rec.get("status") if isinstance(rec.get("status"), dict) else {},
                    updated_at=_dt_from_iso(
                        rec.get("updated_at"),
                        default=datetime.fromtimestamp(0, tz=timezone.utc),
                    )
                    or datetime.fromtimestamp(0, tz=timezone.utc),
                    resource_version=int(mod_rev or 0),
                )
            )
        out.sort(key=lambda entry: ((entry.namespace or ""), entry.name))
        return out

    def get_authority_object(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
    ) -> AuthorityObjectEntry | None:
        rec, mod_rev = self._get_json(self._authority_key(group, version, resource, namespace, name))
        if not rec:
            return None
        namespace_value = rec.get("namespace")
        return AuthorityObjectEntry(
            group=str(rec.get("group", "")),
            version=str(rec.get("version", version)),
            resource=str(rec.get("resource", resource)),
            namespace=(str(namespace_value) if namespace_value not in {None, ""} else None),
            name=str(rec.get("name", name)),
            kind=str(rec.get("kind", "")),
            metadata=rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {},
            spec=rec.get("spec") if isinstance(rec.get("spec"), dict) else {},
            status=rec.get("status") if isinstance(rec.get("status"), dict) else {},
            updated_at=_dt_from_iso(
                rec.get("updated_at"),
                default=datetime.fromtimestamp(0, tz=timezone.utc),
            )
            or datetime.fromtimestamp(0, tz=timezone.utc),
            resource_version=int(mod_rev or 0),
        )

    def delete_authority_object(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        *,
        expected_resource_version: int | None = None,
    ) -> bool:
        key = self._authority_key(group, version, resource, namespace, name)
        if expected_resource_version is None:
            existing, _rv = self._get_json(key)
            if existing is None:
                return False
            self._delete(key)
            return True
        compare = [
            {
                "key": _b64encode(key),
                "target": "MOD",
                "modRevision": str(int(expected_resource_version)),
            }
        ]
        success = [{"requestDeleteRange": {"key": _b64encode(key)}}]
        failure = [{"requestRange": {"key": _b64encode(key), "limit": 1}}]
        resp = self._client.txn(compare, success, failure)
        if not bool(resp.get("succeeded")):
            actual_rv = self._get_json(key)[1]
            raise RegistryConflictError(
                self._authority_object_conflict_key(group, version, resource, namespace, name),
                expected=int(expected_resource_version),
                actual=actual_rv,
            )
        return True

    def _workload_metrics_key(self, app_name: str) -> str:
        return self._k("hpa", "workload_metrics", app_name)

    def upsert_workload_metrics_snapshot(
        self,
        app_name: str,
        *,
        controller_id: str,
        controller_epoch: int,
        collected_at: datetime,
        cpu_utilization: float | None,
        memory_utilization: float | None,
        memory_bytes: int,
        pod_count: int,
        node_count: int,
    ) -> None:
        payload = {
            "app_name": str(app_name),
            "controller_id": str(controller_id),
            "controller_epoch": int(controller_epoch),
            "collected_at": collected_at.astimezone(timezone.utc).isoformat(),
            "cpu_utilization": float(cpu_utilization) if cpu_utilization is not None else None,
            "memory_utilization": (
                float(memory_utilization) if memory_utilization is not None else None
            ),
            "memory_bytes": int(memory_bytes),
            "pod_count": int(pod_count),
            "node_count": int(node_count),
            "updated_at": _now_iso(),
        }
        self._put_json(self._workload_metrics_key(app_name), payload)

    def get_workload_metrics_snapshot(self, app_name: str) -> WorkloadMetricsSnapshot | None:
        rec, _mod_rev = self._get_json(self._workload_metrics_key(app_name))
        return self._workload_metrics_snapshot_from_record(rec)

    def list_workload_metrics_snapshots(self) -> list[WorkloadMetricsSnapshot]:
        rows = self._list_prefix(self._k("hpa", "workload_metrics"))
        out: list[WorkloadMetricsSnapshot] = []
        for _key, rec, _mod_rev in rows:
            entry = self._workload_metrics_snapshot_from_record(rec)
            if entry is not None:
                out.append(entry)
        out.sort(key=lambda entry: entry.app_name)
        return out

    def delete_workload_metrics_snapshot(self, app_name: str) -> bool:
        key = self._workload_metrics_key(app_name)
        existing, _mod_rev = self._get_json(key)
        if existing is None:
            return False
        self._delete(key)
        return True

    def _workload_metrics_snapshot_from_record(
        self, rec: dict[str, Any] | None
    ) -> WorkloadMetricsSnapshot | None:
        if not rec:
            return None
        return WorkloadMetricsSnapshot(
            app_name=str(rec.get("app_name", "")),
            controller_id=str(rec.get("controller_id", "")),
            controller_epoch=int(rec.get("controller_epoch", 0) or 0),
            collected_at=_dt_from_iso(
                rec.get("collected_at"), default=datetime.fromtimestamp(0, tz=timezone.utc)
            )
            or datetime.fromtimestamp(0, tz=timezone.utc),
            cpu_utilization=(
                float(rec.get("cpu_utilization"))
                if rec.get("cpu_utilization") is not None
                else None
            ),
            memory_utilization=(
                float(rec.get("memory_utilization"))
                if rec.get("memory_utilization") is not None
                else None
            ),
            memory_bytes=int(rec.get("memory_bytes", 0) or 0),
            pod_count=int(rec.get("pod_count", 0) or 0),
            node_count=int(rec.get("node_count", 0) or 0),
            updated_at=_dt_from_iso(
                rec.get("updated_at"), default=datetime.fromtimestamp(0, tz=timezone.utc)
            )
            or datetime.fromtimestamp(0, tz=timezone.utc),
        )

    def _get_latest_revision(self, app_name: str) -> RevisionInfo | None:
        revs = self.list_revisions(app_name, limit=1_000_000)
        if not revs:
            return None
        return max(revs, key=lambda r: r.revision)

    def get_revision_manifest(self, app_name: str, revision: int) -> AppManifest:
        rec, _ = self._get_json(self._k("apps", app_name, "revisions", str(revision)))
        if not rec:
            raise ValueError(f"No revision {revision} recorded for {app_name}")
        spec = rec.get("spec") or {}
        return AppManifest.model_validate(spec)

    def list_revisions(self, app_name: str, limit: int = 10) -> list[RevisionInfo]:
        rows = self._list_prefix(self._k("apps", app_name, "revisions"))
        out: list[RevisionInfo] = []
        for _key, rec, _rev in rows:
            created = _dt_from_iso(rec.get("created_at"))
            out.append(
                RevisionInfo(
                    revision=int(rec.get("revision", 0)),
                    spec_hash=str(rec.get("spec_hash", "")),
                    status=str(rec.get("status", "")),
                    image=str(rec.get("image", "")),
                    created_at=created,
                )
            )
        out.sort(key=lambda r: r.revision, reverse=True)
        return out[: int(limit)]

    # --- Events ---------------------------------------------------------
    def record_event(self, app_name: str, revision: int, event_type: str, message: str) -> None:
        created_at = _now_iso()
        ts_key = f"{int(_now().timestamp() * 1_000_000):020d}"
        key = self._k("events", app_name, ts_key, uuid.uuid4().hex)
        payload = {
            "app_name": app_name,
            "revision": int(revision),
            "event_type": event_type,
            "message": message,
            "created_at": created_at,
        }
        self._put_json(key, payload)

    def list_events(self, app_name: str, limit: int = 20) -> list[AppEvent]:
        rows = self._list_prefix(self._k("events", app_name))
        rows.sort(key=lambda r: r[0], reverse=True)
        out: list[AppEvent] = []
        for _key, rec, _rev in rows[: int(limit)]:
            created = _dt_from_iso(rec.get("created_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
            out.append(
                AppEvent(
                    app_name=app_name,
                    revision=int(rec.get("revision", 0)),
                    event_type=str(rec.get("event_type", "")),
                    message=str(rec.get("message", "")),
                    created_at=created or datetime.fromtimestamp(0, tz=timezone.utc),
                )
            )
        return out

    def list_events_paginated(
        self, app_name: str, limit: int, offset: int
    ) -> tuple[list[AppEvent], int]:
        rows = self._list_prefix(self._k("events", app_name))
        rows.sort(key=lambda r: r[0], reverse=True)
        total = len(rows)
        slice_rows = rows[int(offset) : int(offset) + int(limit)]
        out: list[AppEvent] = []
        for _key, rec, _rev in slice_rows:
            created = _dt_from_iso(rec.get("created_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
            out.append(
                AppEvent(
                    app_name=app_name,
                    revision=int(rec.get("revision", 0)),
                    event_type=str(rec.get("event_type", "")),
                    message=str(rec.get("message", "")),
                    created_at=created or datetime.fromtimestamp(0, tz=timezone.utc),
                )
            )
        return out, total

    # --- Node leases (lab-edge) ----------------------------------------
    def acquire_lease(
        self,
        site_id: str,
        node_id: str,
        session_id: str,
        lease_ttl_ms: int,
        renew_after_ms: int,
        controller_epoch: int,
    ) -> NodeLease:
        now = _now()
        lease_id = str(uuid.uuid4())
        expires_at = now + timedelta(milliseconds=int(lease_ttl_ms))
        payload = {
            "node_id": node_id,
            "site_id": site_id,
            "session_id": session_id,
            "lease_id": lease_id,
            "controller_epoch": int(controller_epoch),
            "lease_ttl_ms": int(lease_ttl_ms),
            "renew_after_ms": int(renew_after_ms),
            "last_renew_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        self._put_json(self._k("node_leases", site_id, node_id), payload)
        return NodeLease(
            node_id=node_id,
            site_id=site_id,
            session_id=session_id,
            lease_id=lease_id,
            controller_epoch=int(controller_epoch),
            lease_ttl_ms=int(lease_ttl_ms),
            renew_after_ms=int(renew_after_ms),
            last_renew_at=now,
            expires_at=expires_at,
        )

    def renew_lease(
        self,
        node_id: str,
        session_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[NodeLease | None, str | None]:
        now_dt = now or _now()
        # search for lease by node_id across sites
        rows = self._list_prefix(self._k("node_leases"))
        lease_rec: dict | None = None
        for _key, rec, _rev in rows:
            if str(rec.get("node_id")) == str(node_id):
                lease_rec = rec
                break
        if not lease_rec:
            return None, "unknown_lease"
        if str(lease_rec.get("session_id")) != str(session_id):
            return None, "invalid_session"
        if str(lease_rec.get("lease_id")) != str(lease_id):
            return None, "unknown_lease"
        expires_at = _dt_from_iso(lease_rec.get("expires_at"), default=now_dt - timedelta(seconds=1))
        if expires_at and expires_at <= now_dt:
            site_id = str(lease_rec.get("site_id", ""))
            self._delete(self._k("node_leases", site_id, node_id))
            return None, "expired"
        lease_ttl_ms = int(lease_rec.get("lease_ttl_ms", 0))
        renew_after_ms = int(lease_rec.get("renew_after_ms", 0))
        new_expires = now_dt + timedelta(milliseconds=lease_ttl_ms)
        lease_rec["last_renew_at"] = now_dt.isoformat()
        lease_rec["expires_at"] = new_expires.isoformat()
        site_id = str(lease_rec.get("site_id", ""))
        self._put_json(self._k("node_leases", site_id, node_id), lease_rec)
        return (
            NodeLease(
                node_id=str(lease_rec.get("node_id")),
                site_id=site_id,
                session_id=str(lease_rec.get("session_id")),
                lease_id=str(lease_rec.get("lease_id")),
                controller_epoch=int(lease_rec.get("controller_epoch", 0)),
                lease_ttl_ms=lease_ttl_ms,
                renew_after_ms=renew_after_ms,
                last_renew_at=now_dt,
                expires_at=new_expires,
            ),
            None,
        )

    # --- Work queue (lab-edge) -----------------------------------------
    def enqueue_work(self, work_id: str, attempt: int, site_id: str, payload: dict) -> None:
        now = _now_iso()
        rec = {
            "work_id": work_id,
            "attempt": int(attempt),
            "site_id": site_id,
            "payload": payload,
            "state": "Pending",
            "lease_id": None,
            "leased_at": None,
            "lease_expires_at": None,
            "acked_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self._put_json(self._k("work_queue", site_id, work_id, str(attempt)), rec)

    def pull_work(
        self,
        site_id: str,
        limit: int,
        visibility_timeout_ms: int,
    ) -> list[WorkQueueLease]:
        now = _now()
        now_iso = now.isoformat()
        timeout_ms = max(0, int(visibility_timeout_ms))
        lease_expires_at = now + timedelta(milliseconds=timeout_ms)
        exp_iso = lease_expires_at.isoformat()
        prefix = self._k("work_queue", site_id)
        rows = self._list_prefix(prefix)
        # reset expired leases
        for key, rec, _rev in rows:
            if rec.get("state") in {"Leased", "Acked"}:
                expires = _dt_from_iso(rec.get("lease_expires_at"))
                if expires and expires < now:
                    rec["state"] = "Pending"
                    rec["lease_id"] = None
                    rec["leased_at"] = None
                    rec["lease_expires_at"] = None
                    rec["acked_at"] = None
                    rec["updated_at"] = now_iso
                    self._put_json(key, rec)
        # re-list pending
        rows = [r for r in self._list_prefix(prefix) if r[1].get("state") == "Pending"]
        rows.sort(key=lambda r: r[1].get("created_at", ""))
        leases: list[WorkQueueLease] = []
        for key, rec, _rev in rows[: int(limit)]:
            lease_id = str(uuid.uuid4())
            rec["state"] = "Leased"
            rec["lease_id"] = lease_id
            rec["leased_at"] = now_iso
            rec["lease_expires_at"] = exp_iso
            rec["updated_at"] = now_iso
            self._put_json(key, rec)
            try:
                self.update_work_state(
                    work_id=str(rec.get("work_id")),
                    attempt=int(rec.get("attempt", 0)),
                    state="Dispatched",
                )
            except Exception:
                pass
            leases.append(
                WorkQueueLease(
                    work_id=str(rec.get("work_id")),
                    attempt=int(rec.get("attempt", 0)),
                    site_id=site_id,
                    payload=rec.get("payload") or {},
                    lease_id=lease_id,
                    lease_expires_at=lease_expires_at,
                )
            )
        return leases

    def ack_work(self, lease_ids: list[str]) -> int:
        if not lease_ids:
            return 0
        now_iso = _now_iso()
        updated = 0
        rows = self._list_prefix(self._k("work_queue"))
        lease_set = set(str(l) for l in lease_ids)
        for key, rec, _rev in rows:
            if str(rec.get("lease_id")) in lease_set:
                rec["state"] = "Acked"
                rec["acked_at"] = now_iso
                rec["updated_at"] = now_iso
                self._put_json(key, rec)
                updated += 1
        return updated

    def ack_work_items(self, ack_items: list[dict]) -> int:
        if not ack_items:
            return 0
        now_iso = _now_iso()
        updated = 0
        rows = self._list_prefix(self._k("work_queue"))
        by_lease_id: dict[str, tuple[str, dict[str, Any], int]] = {}
        for key, rec, rev in rows:
            lease_id = str(rec.get("lease_id") or "").strip()
            if lease_id:
                by_lease_id[lease_id] = (key, rec, rev)
        for item in ack_items:
            if not isinstance(item, dict):
                continue
            lease_id = str(item.get("lease_id") or "").strip()
            if not lease_id:
                continue
            envelope = parse_envelope(item)
            if envelope is None:
                continue
            queued = by_lease_id.get(lease_id)
            if queued is None:
                continue
            key, rec, _rev = queued
            queued_envelope = parse_envelope(rec.get("payload") or {})
            if queued_envelope != envelope:
                continue
            work_id = str(rec.get("work_id") or "")
            if item.get("work_id") not in {None, "", work_id}:
                continue
            try:
                ack_attempt = int(item.get("attempt") or 0)
            except Exception:
                ack_attempt = 0
            attempt = int(rec.get("attempt") or 0)
            if ack_attempt not in {0, attempt}:
                continue
            rec["state"] = "Acked"
            rec["acked_at"] = now_iso
            rec["updated_at"] = now_iso
            self._put_json(key, rec)
            updated += 1
        return updated

    # --- Site ingress endpoints ----------------------------------------
    def get_site_ingress_endpoint(self, site_id: str) -> SiteIngressEndpoint | None:
        rec, _ = self._get_json(self._k("ingress", "sites", site_id))
        if not rec:
            return None
        quarantine_until = _dt_from_iso(rec.get("quarantine_until"))
        return SiteIngressEndpoint(
            site_id=site_id,
            mode=str(rec.get("mode", "")),
            core_proxy_port=rec.get("core_proxy_port"),
            public_urls=list(rec.get("public_urls") or []),
            quarantine_until=quarantine_until,
            created_at=_dt_from_iso(rec.get("created_at"), default=_now()) or _now(),
            updated_at=_dt_from_iso(rec.get("updated_at"), default=_now()) or _now(),
        )

    def ensure_site_ingress_port(
        self,
        site_id: str,
        *,
        port_min: int = 18080,
        port_max: int = 18999,
        mode: str = "core-proxy",
    ) -> int:
        if port_min > port_max:
            raise ValueError("port_min must be <= port_max")
        existing = self.get_site_ingress_endpoint(site_id)
        if existing and existing.core_proxy_port is not None:
            return int(existing.core_proxy_port)
        used = set()
        for _key, rec, _rev in self._list_prefix(self._k("ingress", "sites")):
            if rec.get("core_proxy_port") is not None:
                used.add(int(rec.get("core_proxy_port")))
        now_iso = _now_iso()
        for port in range(int(port_min), int(port_max) + 1):
            if port in used:
                continue
            payload = {
                "site_id": site_id,
                "mode": mode,
                "core_proxy_port": port,
                # Preserve endpoint metadata when assigning a core-proxy port
                # for a site that may already have public endpoint details.
                "public_urls": list(existing.public_urls) if existing else [],
                "quarantine_until": (
                    existing.quarantine_until.isoformat()
                    if existing and existing.quarantine_until
                    else None
                ),
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            if existing and existing.created_at:
                payload["created_at"] = existing.created_at.isoformat()
            self._put_json(self._k("ingress", "sites", site_id), payload)
            return port
        raise RuntimeError("no core-proxy ports available")

    def list_site_ingress_endpoints(self) -> list[SiteIngressListItem]:
        items: list[SiteIngressListItem] = []
        for _key, rec, _rev in self._list_prefix(self._k("ingress", "sites")):
            quarantine_until = _dt_from_iso(rec.get("quarantine_until"))
            items.append(
                SiteIngressListItem(
                    site_id=str(rec.get("site_id", "")),
                    mode=str(rec.get("mode", "")),
                    core_proxy_port=rec.get("core_proxy_port"),
                    public_urls=list(rec.get("public_urls") or []),
                    quarantine_until=quarantine_until,
                )
            )
        items.sort(key=lambda i: i.site_id)
        return items

    def upsert_site_ingress_endpoint(
        self,
        *,
        site_id: str,
        mode: str,
        core_proxy_port: int | None = None,
        public_urls: list[str | dict] | None = None,
        quarantine_until: datetime | None = None,
    ) -> None:
        now_iso = _now_iso()
        existing, _ = self._get_json(self._k("ingress", "sites", site_id))
        existing_port = existing.get("core_proxy_port") if existing else None
        payload = {
            "site_id": site_id,
            "mode": mode,
            "core_proxy_port": core_proxy_port if core_proxy_port is not None else existing_port,
            "public_urls": list(public_urls or []),
            "quarantine_until": quarantine_until.isoformat() if quarantine_until else None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        if existing and existing.get("created_at"):
            payload["created_at"] = existing.get("created_at")
        self._put_json(self._k("ingress", "sites", site_id), payload)

    def upsert_edge_ingress_route(
        self,
        *,
        name: str,
        namespace: str,
        site_id: str,
        policy_name: str | None,
        policy_namespace: str | None,
        document: dict,
        status: dict | None = None,
    ) -> None:
        now_iso = _now_iso()
        payload = {
            "name": name,
            "namespace": namespace,
            "site_id": site_id,
            "policy_name": policy_name,
            "policy_namespace": policy_namespace,
            "spec": document,
            "status": status,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        key = self._k("ingress", "routes", namespace, name)
        existing, _ = self._get_json(key)
        if existing and existing.get("created_at"):
            payload["created_at"] = existing.get("created_at")
        self._put_json(key, payload)

    def upsert_edge_ingress_policy(
        self,
        *,
        name: str,
        namespace: str,
        document: dict,
        status: dict | None = None,
    ) -> None:
        now_iso = _now_iso()
        payload = {
            "name": name,
            "namespace": namespace,
            "spec": document,
            "status": status,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        key = self._k("ingress", "policies", namespace, name)
        existing, _ = self._get_json(key)
        if existing and existing.get("created_at"):
            payload["created_at"] = existing.get("created_at")
        self._put_json(key, payload)

    def update_edge_ingress_route_status(
        self,
        *,
        name: str,
        namespace: str,
        status: dict,
    ) -> None:
        key = self._k("ingress", "routes", namespace, name)
        existing, _ = self._get_json(key)
        if not existing:
            return
        existing["status"] = status
        existing["updated_at"] = _now_iso()
        self._put_json(key, existing)

    def update_edge_ingress_policy_status(
        self,
        *,
        name: str,
        namespace: str,
        status: dict,
    ) -> None:
        key = self._k("ingress", "policies", namespace, name)
        existing, _ = self._get_json(key)
        if not existing:
            return
        existing["status"] = status
        existing["updated_at"] = _now_iso()
        self._put_json(key, existing)

    def list_edge_ingress_routes(self) -> list[EdgeIngressRouteRecord]:
        rows = self._list_prefix(self._k("ingress", "routes"))
        items: list[EdgeIngressRouteRecord] = []
        for _key, rec, _rev in rows:
            created = _dt_from_iso(rec.get("created_at"), default=_now()) or _now()
            updated = _dt_from_iso(rec.get("updated_at"), default=_now()) or _now()
            items.append(
                EdgeIngressRouteRecord(
                    name=str(rec.get("name", "")),
                    namespace=str(rec.get("namespace", "")),
                    site_id=str(rec.get("site_id", "")),
                    policy_name=rec.get("policy_name"),
                    policy_namespace=rec.get("policy_namespace"),
                    spec=rec.get("spec") or {},
                    status=rec.get("status"),
                    created_at=created,
                    updated_at=updated,
                )
            )
        return items

    def get_edge_ingress_route(
        self, *, name: str, namespace: str | None = None
    ) -> EdgeIngressRouteRecord | None:
        key = self._k("ingress", "routes", namespace or "default", name)
        rec, _ = self._get_json(key)
        if not rec:
            return None
        created = _dt_from_iso(rec.get("created_at"), default=_now()) or _now()
        updated = _dt_from_iso(rec.get("updated_at"), default=_now()) or _now()
        return EdgeIngressRouteRecord(
            name=str(rec.get("name", name)),
            namespace=str(rec.get("namespace", namespace or "default")),
            site_id=str(rec.get("site_id", "")),
            policy_name=rec.get("policy_name"),
            policy_namespace=rec.get("policy_namespace"),
            spec=rec.get("spec") or {},
            status=rec.get("status"),
            created_at=created,
            updated_at=updated,
        )

    def list_edge_ingress_routes_for_site(self, site_id: str) -> list[EdgeIngressRouteRecord]:
        return [r for r in self.list_edge_ingress_routes() if r.site_id == site_id]

    def get_edge_ingress_policy(
        self, *, name: str, namespace: str | None = None
    ) -> EdgeIngressPolicyRecord | None:
        key = self._k("ingress", "policies", namespace or "default", name)
        rec, _ = self._get_json(key)
        if not rec:
            return None
        created = _dt_from_iso(rec.get("created_at"), default=_now()) or _now()
        updated = _dt_from_iso(rec.get("updated_at"), default=_now()) or _now()
        return EdgeIngressPolicyRecord(
            name=str(rec.get("name", name)),
            namespace=str(rec.get("namespace", namespace or "default")),
            spec=rec.get("spec") or {},
            status=rec.get("status"),
            created_at=created,
            updated_at=updated,
        )

    def list_edge_ingress_policies(self) -> list[EdgeIngressPolicyRecord]:
        rows = self._list_prefix(self._k("ingress", "policies"))
        items: list[EdgeIngressPolicyRecord] = []
        for _key, rec, _rev in rows:
            created = _dt_from_iso(rec.get("created_at"), default=_now()) or _now()
            updated = _dt_from_iso(rec.get("updated_at"), default=_now()) or _now()
            items.append(
                EdgeIngressPolicyRecord(
                    name=str(rec.get("name", "")),
                    namespace=str(rec.get("namespace", "")),
                    spec=rec.get("spec") or {},
                    status=rec.get("status"),
                    created_at=created,
                    updated_at=updated,
                )
            )
        return items

    def list_site_ids(self) -> list[str]:
        rows = self._list_prefix(self._k("node_leases"))
        sites = {str(rec.get("site_id")) for _key, rec, _rev in rows if rec.get("site_id")}
        return sorted(sites)

    def list_route_bundle_site_ids(self) -> list[str]:
        sites = {site.strip() for site in self.list_site_ids() if site and site.strip()}
        for route in self.list_edge_ingress_routes():
            site_id = str(route.site_id or "").strip()
            if site_id:
                sites.add(site_id)
        return sorted(sites)

    def mark_work_done(self, work_id: str, attempt: int) -> None:
        rows = self._list_prefix(self._k("work_queue"))
        for key, rec, _rev in rows:
            if str(rec.get("work_id")) == str(work_id) and int(rec.get("attempt", 0)) == int(attempt):
                rec["state"] = "Done"
                rec["updated_at"] = _now_iso()
                rec["lease_id"] = None
                rec["lease_expires_at"] = None
                self._put_json(key, rec)
                return

    # --- Outbox (jetstream) -------------------------------------------
    def enqueue_work_outbox(self, work_id: str, attempt: int, site_id: str, payload: dict) -> None:
        now = _now_iso()
        rec = {
            "work_id": work_id,
            "attempt": int(attempt),
            "site_id": site_id,
            "payload": payload,
            "publish_subject": _outbox_publish_subject(site_id),
            "publish_msg_id": _outbox_publish_msg_id(work_id, attempt, payload),
            "state": "Unpublished",
            "publish_attempts": 0,
            "last_publish_at": None,
            "last_publish_error": None,
            "created_at": now,
            "updated_at": now,
        }
        self._put_json(self._k("outbox", "work", work_id, str(attempt)), rec)

    def list_outbox_unpublished(self, limit: int = 100) -> list[WorkOutboxEntry]:
        rows = self._list_prefix(self._k("outbox", "work"))
        entries: list[tuple[str, WorkOutboxEntry]] = []
        for _key, rec, _rev in rows:
            if rec.get("state") != "Unpublished":
                continue
            entry = WorkOutboxEntry(
                work_id=str(rec.get("work_id", "")),
                attempt=int(rec.get("attempt", 0)),
                site_id=str(rec.get("site_id", "")),
                payload=rec.get("payload") or {},
                publish_subject=str(
                    rec.get("publish_subject")
                    or _outbox_publish_subject(str(rec.get("site_id", "")))
                ),
                publish_msg_id=str(
                    rec.get("publish_msg_id")
                    or _outbox_publish_msg_id(
                        str(rec.get("work_id", "")),
                        int(rec.get("attempt", 0)),
                        rec.get("payload") or {},
                    )
                ),
                publish_attempts=int(rec.get("publish_attempts", 0)),
                last_publish_at=_dt_from_iso(rec.get("last_publish_at")),
                last_publish_error=(
                    str(rec.get("last_publish_error"))
                    if rec.get("last_publish_error") is not None
                    else None
                ),
            )
            entries.append((str(rec.get("created_at", "")), entry))
        entries.sort(key=lambda e: (e[0], e[1].work_id, e[1].attempt))
        return [e[1] for e in entries[: int(limit)]]

    def get_outbox_payload(self, work_id: str, attempt: int) -> dict | None:
        rec, _ = self._get_json(self._k("outbox", "work", work_id, str(attempt)))
        if not rec:
            return None
        return rec.get("payload") or {}

    def mark_outbox_published(self, work_id: str, attempt: int) -> None:
        key = self._k("outbox", "work", work_id, str(attempt))
        rec, _ = self._get_json(key)
        if not rec:
            return
        now = _now_iso()
        rec["state"] = "Published"
        rec["publish_attempts"] = int(rec.get("publish_attempts", 0)) + 1
        rec["last_publish_at"] = now
        rec["last_publish_error"] = None
        rec["updated_at"] = now
        self._put_json(key, rec)

    def record_outbox_publish_attempt(
        self,
        work_id: str,
        attempt: int,
        *,
        error: str | None = None,
    ) -> None:
        key = self._k("outbox", "work", work_id, str(attempt))
        rec, _ = self._get_json(key)
        if not rec:
            return
        now = _now_iso()
        rec["publish_attempts"] = int(rec.get("publish_attempts", 0)) + 1
        rec["last_publish_at"] = now
        rec["last_publish_error"] = error
        rec["updated_at"] = now
        self._put_json(key, rec)

    # --- Work ledger ---------------------------------------------------
    def upsert_work_ledger(
        self,
        *,
        work_id: str,
        attempt: int,
        site_id: str,
        state: str,
        controller_id: str | None = None,
        controller_epoch: int | None = None,
        operation_id: str | None = None,
        desired_generation: int | None = None,
    ) -> None:
        key = self._k("work", "ledger", work_id)
        now = _now_iso()
        rec, _ = self._get_json(key)
        if rec:
            rec.update(
                {
                    "attempt": int(attempt),
                    "site_id": site_id,
                    "state": state,
                    "controller_id": controller_id,
                    "controller_epoch": controller_epoch,
                    "operation_id": operation_id,
                    "desired_generation": desired_generation,
                    "updated_at": now,
                    "state_updated_at": now,
                }
            )
        else:
            rec = {
                "work_id": work_id,
                "attempt": int(attempt),
                "site_id": site_id,
                "state": state,
                "controller_id": controller_id,
                "controller_epoch": controller_epoch,
                "operation_id": operation_id,
                "desired_generation": desired_generation,
                "assigned_node_id": None,
                "observed_generation": None,
                "result": None,
                "created_at": now,
                "updated_at": now,
                "state_updated_at": now,
            }
        self._put_json(key, rec)

    def get_work_ledger(self, work_id: str) -> WorkLedgerEntry | None:
        rec, _ = self._get_json(self._k("work", "ledger", work_id))
        if not rec:
            return None
        return WorkLedgerEntry(
            work_id=str(rec.get("work_id", work_id)),
            attempt=int(rec.get("attempt", 0)),
            site_id=str(rec.get("site_id", "")),
            state=str(rec.get("state", "")),
            controller_id=(
                str(rec.get("controller_id")) if rec.get("controller_id") is not None else None
            ),
            controller_epoch=(
                int(rec.get("controller_epoch"))
                if rec.get("controller_epoch") is not None
                else None
            ),
            operation_id=(
                str(rec.get("operation_id")) if rec.get("operation_id") is not None else None
            ),
            desired_generation=rec.get("desired_generation"),
            assigned_node_id=rec.get("assigned_node_id"),
            observed_generation=rec.get("observed_generation"),
            result=rec.get("result"),
            created_at=_dt_from_iso(rec.get("created_at"), default=_now()) or _now(),
            updated_at=_dt_from_iso(rec.get("updated_at"), default=_now()) or _now(),
            state_updated_at=_dt_from_iso(rec.get("state_updated_at"), default=_now()) or _now(),
        )

    def update_work_state(
        self,
        *,
        work_id: str,
        attempt: int,
        state: str,
        assigned_node_id: str | None = None,
        observed_generation: int | None = None,
        result: dict | None = None,
    ) -> bool:
        key = self._k("work", "ledger", work_id)
        rec, _ = self._get_json(key)
        if not rec or int(rec.get("attempt", -1)) != int(attempt):
            return False
        now = _now_iso()
        rec["state"] = state
        if assigned_node_id is not None:
            rec["assigned_node_id"] = assigned_node_id
        if observed_generation is not None:
            rec["observed_generation"] = observed_generation
        if result is not None:
            rec["result"] = result
        rec["updated_at"] = now
        rec["state_updated_at"] = now
        self._put_json(key, rec)
        return True

    def list_work_state_before(self, state: str, cutoff: datetime) -> list[WorkLedgerEntry]:
        rows = self._list_prefix(self._k("work", "ledger"))
        out: list[WorkLedgerEntry] = []
        for _key, rec, _rev in rows:
            if str(rec.get("state")) != state:
                continue
            state_ts = _dt_from_iso(rec.get("state_updated_at"))
            if state_ts and state_ts < cutoff:
                out.append(
                    WorkLedgerEntry(
                        work_id=str(rec.get("work_id", "")),
                        attempt=int(rec.get("attempt", 0)),
                        site_id=str(rec.get("site_id", "")),
                        state=str(rec.get("state", "")),
                        controller_id=(
                            str(rec.get("controller_id"))
                            if rec.get("controller_id") is not None
                            else None
                        ),
                        controller_epoch=(
                            int(rec.get("controller_epoch"))
                            if rec.get("controller_epoch") is not None
                            else None
                        ),
                        operation_id=(
                            str(rec.get("operation_id"))
                            if rec.get("operation_id") is not None
                            else None
                        ),
                        desired_generation=rec.get("desired_generation"),
                        assigned_node_id=rec.get("assigned_node_id"),
                        observed_generation=rec.get("observed_generation"),
                        result=rec.get("result"),
                        created_at=_dt_from_iso(rec.get("created_at"), default=_now()) or _now(),
                        updated_at=_dt_from_iso(rec.get("updated_at"), default=_now()) or _now(),
                        state_updated_at=state_ts,
                    )
                )
        return out

    def reschedule_work(
        self,
        *,
        work_id: str,
        attempt: int,
        controller_id: str | None = None,
        controller_epoch: int | None = None,
    ) -> int | None:
        outbox_key = self._k("outbox", "work", work_id, str(attempt))
        outbox, _ = self._get_json(outbox_key)
        if not outbox:
            return None
        ledger_key = self._k("work", "ledger", work_id)
        ledger, _ = self._get_json(ledger_key)
        if not ledger or int(ledger.get("attempt", -1)) != int(attempt):
            return None
        now = _now_iso()
        new_attempt = int(attempt) + 1
        ledger["attempt"] = new_attempt
        ledger["state"] = "Pending"
        ledger["updated_at"] = now
        ledger["state_updated_at"] = now
        self._put_json(ledger_key, ledger)
        payload = outbox.get("payload") or {}
        payload["attempt"] = new_attempt
        payload.setdefault("work_id", work_id)
        payload.setdefault("site_id", outbox.get("site_id"))
        payload["created_at"] = now
        if controller_id:
            payload["controller_id"] = controller_id
        if controller_epoch is not None:
            payload["controller_epoch"] = int(controller_epoch)
        if payload.get("controller_id") and payload.get("controller_epoch") is not None:
            payload["operation_id"] = work_operation(work_id, new_attempt)
            ledger["controller_id"] = payload.get("controller_id")
            ledger["controller_epoch"] = payload.get("controller_epoch")
            ledger["operation_id"] = payload.get("operation_id")
        self.enqueue_work_outbox(work_id, new_attempt, outbox.get("site_id", ""), payload)
        return new_attempt

    # --- Canary rollout state ------------------------------------------
    def get_canary_state(self, app_name: str) -> dict | None:
        rec, _ = self._get_json(self._k("rollout", "canary", app_name))
        return rec or None

    def upsert_canary_state(
        self,
        *,
        app_name: str,
        weight: float,
        next_step_at: str | None,
        step: float,
        max_weight: float,
    ) -> None:
        payload = {
            "app_name": app_name,
            "weight": float(weight),
            "next_step_at": next_step_at,
            "step": float(step),
            "max_weight": float(max_weight),
            "updated_at": _now_iso(),
        }
        self._put_json(self._k("rollout", "canary", app_name), payload)

    # --- Services / endpoints -----------------------------------------
    def upsert_service(self, app_name: str, cluster_ip: str, ports: dict) -> None:
        payload = {"app_name": app_name, "cluster_ip": cluster_ip, "ports": ports}
        self._put_json(self._k("services", app_name), payload)

    def upsert_service_snapshot(
        self, app_name: str, cluster_ip: str, ports: dict, endpoints: list[ServiceEndpoint]
    ) -> None:
        self.upsert_service(app_name, cluster_ip, ports)
        self.upsert_service_endpoints(app_name, endpoints)

    def delete_service(self, app_name: str) -> None:
        self._delete(self._k("services", app_name))
        self._delete_prefix(self._k("services", app_name, "endpoints"))

    def get_service(self, app_name: str) -> ServiceRecord | None:
        rec, _ = self._get_json(self._k("services", app_name))
        if not rec:
            return None
        return ServiceRecord(app_name=app_name, cluster_ip=rec.get("cluster_ip", ""), ports=rec.get("ports") or {})

    def upsert_service_endpoints(self, app_name: str, endpoints: list[ServiceEndpoint]) -> None:
        prefix = self._k("services", app_name, "endpoints")
        self._delete_prefix(prefix)
        for ep in endpoints:
            key = self._k("services", app_name, "endpoints", f"{ep.ip}:{ep.port}")
            payload = {
                "app_name": ep.app_name,
                "port": int(ep.port),
                "ip": ep.ip,
                "target_port": int(ep.target_port),
                "ready": bool(ep.ready),
            }
            self._put_json(key, payload)

    def list_service_endpoints(self, app_name: str) -> list[ServiceEndpoint]:
        rows = self._list_prefix(self._k("services", app_name, "endpoints"))
        out: list[ServiceEndpoint] = []
        for _key, rec, _rev in rows:
            out.append(
                ServiceEndpoint(
                    app_name=str(rec.get("app_name", app_name)),
                    port=int(rec.get("port", 0)),
                    ip=str(rec.get("ip", "")),
                    target_port=int(rec.get("target_port", 0)),
                    ready=bool(rec.get("ready", False)),
                )
            )
        out.sort(key=lambda e: (e.port, e.ip))
        return out

    def record_service_endpoints(self, app_name: str, endpoints: list[ServiceEndpoint]) -> None:
        self.upsert_service_endpoints(app_name, endpoints)

    def list_services(self) -> list[ServiceListItem]:
        rows = self._list_prefix(self._k("services"))
        out: list[ServiceListItem] = []
        for key, rec, _rev in rows:
            if "/endpoints/" in key:
                continue
            out.append(
                ServiceListItem(
                    app_name=str(rec.get("app_name", "")),
                    cluster_ip=str(rec.get("cluster_ip", "")),
                )
            )
        out.sort(key=lambda s: s.app_name)
        return out

    # --- Nodes / heartbeats -------------------------------------------
    def upsert_node(
        self,
        node_id: str,
        *,
        name: str | None = None,
        labels: dict | None = None,
        taints: list | None = None,
        backend: str | None = None,
        endpoint: str | None = None,
        pod_cidr: str | None = None,
        wg_pubkey: str | None = None,
        rp_pubkey: str | None = None,
        cordoned: bool | None = None,
    ) -> None:
        node_key = self._k("nodes", self._site_id, node_id)
        existing, _ = self._get_json(node_key)
        if cordoned is None:
            cordoned = bool(existing.get("cordoned", False)) if existing else False
        created_at = existing.get("created_at") if existing else _now_iso()
        payload = {
            "node_id": node_id,
            "name": name,
            "labels": labels or {},
            "taints": taints or [],
            "backend": backend,
            "endpoint": endpoint,
            "pod_cidr": pod_cidr,
            "wg_pubkey": wg_pubkey,
            "rp_pubkey": rp_pubkey,
            "cordoned": bool(cordoned),
            "created_at": created_at,
            "updated_at": _now_iso(),
        }
        lease_id, lease_rotated = self._grant_node_lease(node_id)
        should_write = self._node_payload_changed(existing, payload) or lease_rotated
        if not should_write:
            return
        self._put_with_node_lease(key=node_key, payload=payload, node_id=node_id, lease_id=lease_id)

    def record_heartbeat(self, node_id: str, status: str) -> None:
        lease_id, lease_rotated = self._grant_node_lease(node_id)
        now_iso = _now_iso()
        payload = {"node_id": node_id, "status": status, "seen_at": now_iso}
        status_key = self._k("node_status", self._site_id, node_id)
        lease_id, retry_rotated = self._put_with_node_lease(
            key=status_key,
            payload=payload,
            node_id=node_id,
            lease_id=lease_id,
        )
        lease_rotated = lease_rotated or retry_rotated
        node_rewrite = False
        if lease_rotated:
            node_key = self._k("nodes", self._site_id, node_id)
            existing, _ = self._get_json(node_key)
            if existing:
                lease_id, _ = self._put_with_node_lease(
                    key=node_key,
                    payload=existing,
                    node_id=node_id,
                    lease_id=lease_id,
                )
                node_rewrite = True
        self._record_heartbeat_metrics(node_rewrite=node_rewrite)

    def run_maintenance_watchdog(
        self,
        *,
        threshold_pct: int | None = None,
        quota_backend_bytes: int | None = None,
    ) -> bool:
        threshold = int(
            threshold_pct
            if threshold_pct is not None
            else os.getenv("AE_ETCD_MAINTENANCE_THRESHOLD_PCT", "80") or 80
        )
        quota = int(
            quota_backend_bytes
            if quota_backend_bytes is not None
            else os.getenv("AE_ETCD_QUOTA_BACKEND_BYTES", "2147483648") or 2147483648
        )
        quota = max(1, quota)
        status = self._client.maintenance_status()
        alarms = self._client.maintenance_alarms(action="GET")
        header = status.get("header") if isinstance(status, dict) else {}
        header = header if isinstance(header, dict) else {}
        revision = int(header.get("revision") or 0)
        db_size = int(status.get("dbSize") or 0) if isinstance(status, dict) else 0
        usage_pct = int((db_size * 100) / quota)
        alarm_items = alarms.get("alarms") if isinstance(alarms, dict) else []
        alarm_items = alarm_items if isinstance(alarm_items, list) else []
        nospace = any(str((item or {}).get("alarm") or "").upper() == "NOSPACE" for item in alarm_items)
        should_reclaim = nospace or usage_pct >= threshold
        if not should_reclaim:
            return False
        if revision <= 0:
            raise RuntimeError("etcd maintenance watchdog refused compaction: revision is zero")
        self._client.compact(revision, physical=True)
        self._client.defragment()
        for item in alarm_items:
            alarm = str((item or {}).get("alarm") or "").upper()
            member_id = str((item or {}).get("memberID") or (item or {}).get("memberId") or "0")
            if not alarm:
                continue
            try:
                self._client.maintenance_alarms(
                    action="DEACTIVATE",
                    member_id=member_id,
                    alarm=alarm,
                )
            except Exception:
                pass
        return True

    def _get_node_cordoned(self, node_id: str) -> bool:
        rec, _ = self._get_json(self._k("nodes", self._site_id, node_id))
        return bool(rec.get("cordoned", False)) if rec else False

    def cordon_node(self, node_id: str, cordoned: bool = True) -> bool:
        rec, _ = self._get_json(self._k("nodes", self._site_id, node_id))
        if not rec:
            return False
        rec["cordoned"] = bool(cordoned)
        rec["updated_at"] = _now_iso()
        self._put_json(self._k("nodes", self._site_id, node_id), rec)
        return True

    def list_nodes(self) -> list[tuple[NodeRecord, NodeStatus | None]]:
        rows = self._list_prefix(self._k("nodes"))
        statuses = {
            key.split("/")[-1]: rec
            for key, rec, _rev in self._list_prefix(self._k("node_status"))
        }
        out: list[tuple[NodeRecord, NodeStatus | None]] = []
        for _key, rec, _rev in rows:
            created = _dt_from_iso(rec.get("created_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
            updated = _dt_from_iso(rec.get("updated_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
            node = NodeRecord(
                node_id=str(rec.get("node_id", "")),
                name=rec.get("name"),
                labels=rec.get("labels") or {},
                taints=rec.get("taints") or [],
                backend=rec.get("backend"),
                endpoint=rec.get("endpoint"),
                pod_cidr=rec.get("pod_cidr"),
                wg_pubkey=rec.get("wg_pubkey"),
                rp_pubkey=rec.get("rp_pubkey"),
                cordoned=bool(rec.get("cordoned", False)),
                created_at=created or datetime.fromtimestamp(0, tz=timezone.utc),
                updated_at=updated or datetime.fromtimestamp(0, tz=timezone.utc),
            )
            status_rec = statuses.get(node.node_id)
            status = None
            if status_rec:
                seen = _dt_from_iso(status_rec.get("seen_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
                status = NodeStatus(node_id=node.node_id, status=str(status_rec.get("status", "")), seen_at=seen)
            out.append((node, status))
        return out

    def get_node(self, node_id: str) -> tuple[NodeRecord, NodeStatus | None] | None:
        rec, _ = self._get_json(self._k("nodes", self._site_id, node_id))
        if not rec:
            return None
        created = _dt_from_iso(rec.get("created_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
        updated = _dt_from_iso(rec.get("updated_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
        node = NodeRecord(
            node_id=str(rec.get("node_id", "")),
            name=rec.get("name"),
            labels=rec.get("labels") or {},
            taints=rec.get("taints") or [],
            backend=rec.get("backend"),
            endpoint=rec.get("endpoint"),
            pod_cidr=rec.get("pod_cidr"),
            wg_pubkey=rec.get("wg_pubkey"),
            rp_pubkey=rec.get("rp_pubkey"),
            cordoned=bool(rec.get("cordoned", False)),
            created_at=created or datetime.fromtimestamp(0, tz=timezone.utc),
            updated_at=updated or datetime.fromtimestamp(0, tz=timezone.utc),
        )
        status_rec, _ = self._get_json(self._k("node_status", self._site_id, node_id))
        status = None
        if status_rec:
            seen = _dt_from_iso(status_rec.get("seen_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
            status = NodeStatus(node_id=node.node_id, status=str(status_rec.get("status", "")), seen_at=seen)
        return node, status

    # --- Volume attachments -------------------------------------------
    def upsert_volume_attachment(
        self,
        app_name: str,
        volume_name: str,
        node_id: str,
        retention: str | None = None,
    ) -> None:
        payload = {
            "app_name": app_name,
            "volume_name": volume_name,
            "node_id": node_id,
            "retention": retention,
            "created_at": _now_iso(),
        }
        self._put_json(self._k("storage", "attachments", app_name, volume_name), payload)

    def list_volume_attachments(self, app_name: str) -> list[VolumeAttachment]:
        rows = self._list_prefix(self._k("storage", "attachments", app_name))
        out: list[VolumeAttachment] = []
        for _key, rec, _rev in rows:
            created = _dt_from_iso(rec.get("created_at"), default=datetime.fromtimestamp(0, tz=timezone.utc))
            out.append(
                VolumeAttachment(
                    app_name=str(rec.get("app_name", app_name)),
                    volume_name=str(rec.get("volume_name", "")),
                    node_id=str(rec.get("node_id", "")),
                    retention=rec.get("retention"),
                    created_at=created or datetime.fromtimestamp(0, tz=timezone.utc),
                )
            )
        return out

    def delete_volume_attachments(self, app_name: str) -> None:
        self._delete_prefix(self._k("storage", "attachments", app_name))

    # --- Admin / maintenance ------------------------------------------
    def delete_app_state(self, app_name: str, *, purge_history: bool = False) -> None:
        self._delete_prefix(self._k("pods", app_name))
        self._delete_prefix(self._k("pod_nodes", app_name))
        self._delete(self._k("status", app_name))
        self._delete_prefix(self._k("storage", "attachments", app_name))
        if purge_history:
            self._delete_prefix(self._k("events", app_name))
            self._delete_prefix(self._k("apps", app_name, "revisions"))
