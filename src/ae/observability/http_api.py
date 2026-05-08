"""Lightweight HTTP API for metrics, status, events, and previews.

Endpoints:
- GET /metrics            -> Prometheus text format
- GET /status             -> JSON list of app statuses
- GET /status/<app>       -> JSON object for app status (404 if missing)
- GET /events/<app>?limit -> JSON list of recent events for app
- GET /history/<app>?limit -> JSON list of recent probe evaluations (pod histories)
- POST /k8s/preview      -> Render K8s YAML for a manifest (dev only; gated by AE_API_DEV_EXPORT=1)
"""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import http.server
import json
import logging
import os
import signal
import socketserver
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ae import build_info as AE_BUILD_INFO
from ae.accelerators import preferred_gpu_count, preferred_gpu_models
from ae.controller.authority import AuthorityConfig, NotLeaderError
from ae.controller.state import AppStatus, RegistryConflictError, SQLiteStateStore
from ae.observability.metrics import MetricsService
from ae.resources import loader as resource_loader

logger = logging.getLogger(__name__)

# Simple in-memory reconcile metrics updated by the controller loop.
_LAST_RECONCILE_TS: float | None = None
_LAST_RECONCILE_DURATION: float | None = None
_APP_RECONCILE_SUM: dict[str, float] = {}
_APP_RECONCILE_COUNT: dict[str, int] = {}
# Track labs-applied app names for reset coordination.
_LABS_APPS: set[str] = set()
# Prefixes to allow in demo-scoped dashboards (e.g., helm shim demo namespace).
_LABS_APP_PREFIXES: set[str] = set()
# Short-lived suppression to avoid reconciling apps immediately after labs reset.
_LABS_RESET_BLOCK: dict[str, float] = {}
_LABS_RESET_BLOCK_SECONDS = int(os.getenv("AE_LABS_RESET_BLOCK_SECONDS", "30") or 30)
# Crashloop flags: app -> unix timestamp until which the flag is considered active
_APP_CRASHLOOP_UNTIL: dict[str, float] = {}
# Hook observations: (app, hook, type) -> (duration_seconds: float, success: bool)
_HOOK_LAST: dict[tuple[str, str, str], tuple[float, bool]] = {}
# Probe backoff: (app, pod, type) -> seconds
_PROBE_BACKOFF: dict[tuple[str, str, str], int] = {}
_APP_ROLLOUT_OPS: dict[str, dict[str, int]] = {}
# Canary tracking: latest weight and step counter per app
_APP_CANARY_WEIGHT: dict[str, float] = {}
_APP_CANARY_STEPS: dict[str, int] = {}
_OUTBOX_PUBLISH_OK: int = 0
_OUTBOX_PUBLISH_FAIL: int = 0
_SITE_LAST_SEEN: dict[str, float] = {}
_SITE_GATEWAY_LAST_SEEN: dict[tuple[str, str], float] = {}
_SITE_GATEWAY_BUILD_INFO: dict[tuple[str, str], tuple[str, str, str]] = {}
_JS_STREAM_STATS: dict[str, dict[str, float]] = {}
_JS_CONSUMER_STATS: dict[tuple[str, str], dict[str, object]] = {}
_GATEWAY_WORK_METRICS: dict[str, dict[str, float]] = {}
_ROUTE_BUNDLE_METRICS: dict[str, dict[str, float]] = {}
_HA_FENCE_METRICS: dict[str, dict[str, float]] = {}
_HPA_ACTIVITY_METRICS: dict[str, float] = {
    "reconcile_total": 0.0,
    "scale_total": 0.0,
    "metrics_stale_total": 0.0,
    "metrics_missing_total": 0.0,
    "snapshot_age_seconds": 0.0,
}
_HEARTBEAT_WRITES_TOTAL: float = 0.0
_HEARTBEAT_NODE_REWRITES_TOTAL: float = 0.0
_ETCD_MAINTENANCE_RUNS_TOTAL: float = 0.0
_ETCD_MAINTENANCE_TRIGGERED_TOTAL: float = 0.0


def _read_env_file_var(path: str, key: str) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                k, v = raw.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    return ""


def _resolve_apishim_env_file() -> str:
    env_file = os.getenv("AE_APISHIM_ENV_FILE", "").strip()
    if env_file and Path(env_file).exists():
        return env_file
    prof = os.getenv("DEV_PROFILE_DIR", "").strip()
    if prof:
        candidate = Path(prof) / "apishim.env"
        if candidate.exists():
            return str(candidate)
    state_db = os.getenv("AE_STATE_DB", "").strip()
    if state_db:
        candidate = Path(state_db).parent / "apishim.env"
        if candidate.exists():
            return str(candidate)
    fallback = Path("state/profiles/labs/apishim.env")
    if fallback.exists():
        return str(fallback)
    return ""


def _resolve_apishim_verify() -> bool | str:
    override = (
        os.getenv("AE_APISHIM_CA_BUNDLE")
        or os.getenv("AE_APISHIM_CA")
        or os.getenv("AE_APISHIM_TLS_CA")
        or os.getenv("AE_APISHIM_TLS_CA_CERT")
        or ""
    ).strip()
    if override:
        try:
            path = Path(override)
            if path.exists():
                return str(path)
        except Exception:
            pass
    try:
        env_file = _resolve_apishim_env_file()
        if env_file:
            candidate = Path(env_file).parent / "apishim.ca.crt"
            if candidate.exists():
                return str(candidate)
    except Exception:
        pass
    for path in ("state/profiles/labs/apishim.ca.crt", "state/certs/combined-dev-ca.pem"):
        try:
            if Path(path).exists():
                return path
        except Exception:
            continue
    return False


def _resolve_apishim_server(*, env_file: str = "", default_port: int | None = None) -> str:
    for key in ("AE_APISHIM_SERVER", "AE_LABS_HELM_SERVER"):
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value.rstrip("/")
    if env_file:
        for key in ("AE_APISHIM_SERVER", "AE_LABS_HELM_SERVER"):
            value = _read_env_file_var(env_file, key)
            if value:
                return value.rstrip("/")
    try:
        port = int(default_port or 8455)
    except Exception:
        port = 8455
    return f"https://127.0.0.1:{port}"


def _resolve_apishim_admin_token(*, env_file: str = "") -> str:
    for key in ("AE_APISHIM_TOKEN", "AE_LABS_HELM_TOKEN"):
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value
    if env_file:
        for key in ("AE_APISHIM_TOKEN", "AE_LABS_HELM_TOKEN"):
            value = _read_env_file_var(env_file, key)
            if value:
                return value
    return str(_HELM_DEMO_STATE.get("token") or "").strip()


def _resolve_apishim_store_config(*, env_file: str = "") -> tuple[str, str]:
    dsn = str(os.getenv("AE_APISHIM_DSN", "") or "").strip()
    if not dsn and env_file:
        dsn = _read_env_file_var(env_file, "AE_APISHIM_DSN")
    db_path = str(os.getenv("AE_APISHIM_DB", "") or "").strip()
    if not db_path and env_file:
        db_path = _read_env_file_var(env_file, "AE_APISHIM_DB")
    if not db_path and env_file:
        try:
            db_path = str(Path(env_file).with_suffix(".db"))
        except Exception:
            db_path = ""
    if not db_path:
        db_path = "state/apishim.db"
    return dsn, db_path


def _describe_apishim_target(group: str, version: str, resource: str) -> str:
    prefix = f"{group}/{version}" if group else version
    return f"{prefix}/{resource}"


def record_outbox_publish(success: bool) -> None:
    global _OUTBOX_PUBLISH_OK, _OUTBOX_PUBLISH_FAIL
    if success:
        _OUTBOX_PUBLISH_OK += 1
    else:
        _OUTBOX_PUBLISH_FAIL += 1


def record_site_seen(site_id: str, *, node_id: str | None = None) -> None:
    if not site_id:
        return
    now = time.time()
    _SITE_LAST_SEEN[site_id] = now
    node_name = str(node_id or "").strip()
    if node_name:
        _SITE_GATEWAY_LAST_SEEN[(site_id, node_name)] = now


def record_gateway_identity(
    site_id: str,
    node_id: str | None,
    *,
    version: str | None,
    sha: str | None,
    date: str | None,
) -> None:
    if not site_id:
        return
    node_name = str(node_id or "").strip()
    if not node_name:
        return
    _SITE_GATEWAY_LAST_SEEN[(site_id, node_name)] = time.time()
    _SITE_GATEWAY_BUILD_INFO[(site_id, node_name)] = (
        str(version or "").strip() or "unknown",
        str(sha or "").strip() or "unknown",
        str(date or "").strip() or "unknown",
    )


def record_gateway_metrics(
    site_id: str,
    *,
    work_stale_total: float | int | None,
    work_nak_total: float | int | None,
    lease_retry_total: float | int | None,
    result_replay_total: float | int | None = None,
    result_replay_fail_total: float | int | None = None,
    result_replay_backlog: float | int | None = None,
) -> None:
    if not site_id:
        return
    stale_val = float(work_stale_total or 0.0)
    nak_val = float(work_nak_total or 0.0)
    retry_val = float(lease_retry_total or 0.0)
    replay_val = float(result_replay_total or 0.0)
    replay_fail_val = float(result_replay_fail_total or 0.0)
    replay_backlog_val = float(result_replay_backlog or 0.0)
    _GATEWAY_WORK_METRICS[site_id] = {
        "work_stale_total": stale_val,
        "work_nak_total": nak_val,
        "lease_retry_total": retry_val,
        "result_replay_total": replay_val,
        "result_replay_fail_total": replay_fail_val,
        "result_replay_backlog": replay_backlog_val,
    }


def record_route_bundle_apply(site_id: str, *, ok: bool, latency_seconds: float | None) -> None:
    if not site_id:
        return
    metrics = _ROUTE_BUNDLE_METRICS.setdefault(
        site_id,
        {
            "apply_ok_total": 0.0,
            "apply_fail_total": 0.0,
            "last_latency_s": 0.0,
            "publish_ok_total": 0.0,
            "publish_fail_total": 0.0,
            "pending": 0.0,
            "ack_age_s": 0.0,
        },
    )
    if ok:
        metrics["apply_ok_total"] = metrics.get("apply_ok_total", 0.0) + 1.0
    else:
        metrics["apply_fail_total"] = metrics.get("apply_fail_total", 0.0) + 1.0
    if latency_seconds is not None:
        metrics["last_latency_s"] = float(latency_seconds)


def record_route_bundle_publish_state(
    site_id: str,
    *,
    pending: bool | None = None,
    ack_age_seconds: float | None = None,
    publish_ok: bool | int | float = False,
    publish_fail: bool | int | float = False,
) -> None:
    if not site_id:
        return
    metrics = _ROUTE_BUNDLE_METRICS.setdefault(
        site_id,
        {
            "apply_ok_total": 0.0,
            "apply_fail_total": 0.0,
            "last_latency_s": 0.0,
            "publish_ok_total": 0.0,
            "publish_fail_total": 0.0,
            "pending": 0.0,
            "ack_age_s": 0.0,
        },
    )
    if pending is not None:
        metrics["pending"] = 1.0 if pending else 0.0
    if ack_age_seconds is not None:
        metrics["ack_age_s"] = float(ack_age_seconds)
    if publish_ok:
        metrics["publish_ok_total"] = metrics.get("publish_ok_total", 0.0) + float(
            1.0 if isinstance(publish_ok, bool) else publish_ok
        )
    if publish_fail:
        metrics["publish_fail_total"] = metrics.get("publish_fail_total", 0.0) + float(
            1.0 if isinstance(publish_fail, bool) else publish_fail
        )


def record_ha_fence_event(
    surface: str,
    *,
    stale: bool | int | float = False,
    duplicate: bool | int | float = False,
    epoch_advanced: bool | int | float = False,
) -> None:
    if not surface:
        return
    metrics = _HA_FENCE_METRICS.setdefault(
        surface,
        {
            "stale_total": 0.0,
            "duplicate_total": 0.0,
            "epoch_advance_total": 0.0,
        },
    )

    def _add(field: str, value: bool | int | float) -> None:
        if isinstance(value, bool):
            amount = 1.0 if value else 0.0
        else:
            try:
                amount = float(value)
            except Exception:
                amount = 0.0
        if amount > 0:
            metrics[field] = metrics.get(field, 0.0) + amount

    _add("stale_total", stale)
    _add("duplicate_total", duplicate)
    _add("epoch_advance_total", epoch_advanced)


def record_hpa_activity(
    *,
    reconcile: bool | int | float = False,
    scale: bool | int | float = False,
    metrics_stale: bool | int | float = False,
    metrics_missing: bool | int | float = False,
    snapshot_age_seconds: float | None = None,
) -> None:
    def _add(field: str, value: bool | int | float) -> None:
        if isinstance(value, bool):
            amount = 1.0 if value else 0.0
        else:
            try:
                amount = float(value)
            except Exception:
                amount = 0.0
        if amount > 0:
            _HPA_ACTIVITY_METRICS[field] = _HPA_ACTIVITY_METRICS.get(field, 0.0) + amount

    _add("reconcile_total", reconcile)
    _add("scale_total", scale)
    _add("metrics_stale_total", metrics_stale)
    _add("metrics_missing_total", metrics_missing)
    if snapshot_age_seconds is not None:
        try:
            _HPA_ACTIVITY_METRICS["snapshot_age_seconds"] = max(0.0, float(snapshot_age_seconds))
        except Exception:
            pass


def record_heartbeat_write(*, node_rewrite: bool) -> None:
    global _HEARTBEAT_WRITES_TOTAL, _HEARTBEAT_NODE_REWRITES_TOTAL
    _HEARTBEAT_WRITES_TOTAL += 1.0
    if node_rewrite:
        _HEARTBEAT_NODE_REWRITES_TOTAL += 1.0


def record_etcd_maintenance_run(*, triggered: bool) -> None:
    global _ETCD_MAINTENANCE_RUNS_TOTAL, _ETCD_MAINTENANCE_TRIGGERED_TOTAL
    _ETCD_MAINTENANCE_RUNS_TOTAL += 1.0
    if triggered:
        _ETCD_MAINTENANCE_TRIGGERED_TOTAL += 1.0


def record_js_stream_stats(
    *,
    stream: str,
    bytes_used: int,
    messages: int,
    max_bytes: int,
) -> None:
    if not stream:
        return
    _JS_STREAM_STATS[stream] = {
        "bytes_used": float(bytes_used),
        "messages": float(messages),
        "max_bytes": float(max_bytes),
    }


def record_js_consumer_stats(
    *,
    stream: str,
    consumer: str,
    site_id: str,
    pending: int,
    ack_pending: int,
    redelivered: int,
    waiting: int,
) -> None:
    if not stream or not consumer:
        return
    _JS_CONSUMER_STATS[(stream, consumer)] = {
        "site_id": site_id,
        "pending": float(pending),
        "ack_pending": float(ack_pending),
        "redelivered": float(redelivered),
        "waiting": float(waiting),
    }


_HELM_DEMO_LOCK = threading.RLock()
_HELM_DEMO_STATE: dict[str, object] = {
    "proc": None,
    "log": Path(os.getenv("AE_LABS_HELM_LOG", "state/profiles/labs/helm-demo.log")),
    "log_handle": None,
    "port": int(os.getenv("AE_LABS_HELM_PORT", "8455") or 8455),
    "token": os.getenv("AE_LABS_HELM_TOKEN", "helm-demo"),
    "runtime": os.getenv("AE_LABS_HELM_RUNTIME", "stub"),
    "namespace": os.getenv("AE_LABS_HELM_NAMESPACE", "demo-helm"),
    "chart": os.getenv("AE_LABS_HELM_CHART", "demochart"),
    # Track whether AE_LABS_HELM_SERVER was explicitly provided at process start.
    "server_override": bool(os.getenv("AE_LABS_HELM_SERVER")),
    "started": None,
}


def _port_available(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _pick_free_port(host: str = "127.0.0.1") -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _labs_block_app(app: str) -> None:
    if not app:
        return
    try:
        ttl = max(0, int(_LABS_RESET_BLOCK_SECONDS))
    except Exception:
        ttl = 30
    _LABS_RESET_BLOCK[app] = time.time() + ttl


def _labs_unblock_app(app: str) -> None:
    if not app:
        return
    _LABS_RESET_BLOCK.pop(app, None)


def _labs_is_blocked(app: str) -> bool:
    if not app:
        return False
    try:
        until = _LABS_RESET_BLOCK.get(app)
        if until is None:
            return False
        if time.time() <= float(until):
            return True
        _LABS_RESET_BLOCK.pop(app, None)
        return False
    except Exception:
        return False


def _helm_demo_status() -> dict[str, object]:
    with _HELM_DEMO_LOCK:
        proc = _HELM_DEMO_STATE.get("proc")
        log_path: Path = _HELM_DEMO_STATE["log"]  # type: ignore[index]
        started = _HELM_DEMO_STATE.get("started")
        running = bool(proc) and getattr(proc, "poll", lambda: None)() is None
        exit_code = None
        if proc and not running:
            exit_code = proc.poll()
        if not running and _HELM_DEMO_STATE.get("log_handle") is not None:
            try:
                _HELM_DEMO_STATE["log_handle"].close()  # type: ignore[union-attr]
            except Exception:
                pass
            _HELM_DEMO_STATE["log_handle"] = None
    log_tail = ""
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
            tail = lines[-80:]
            log_tail = "".join(tail)
    except FileNotFoundError:
        log_tail = ""
    return {
        "running": running,
        "started": started,
        "exit_code": exit_code,
        "port": _HELM_DEMO_STATE.get("port"),
        "runtime": _HELM_DEMO_STATE.get("runtime"),
        "log": log_tail,
    }


def _helm_demo_start() -> dict[str, object]:
    with _HELM_DEMO_LOCK:
        port_note = ""
        proc = _HELM_DEMO_STATE.get("proc")
        if proc and getattr(proc, "poll", lambda: None)() is None:
            return _helm_demo_status() | {"message": "demo already running"}
        # Locate repo root (dev-only). Walk parents so this still works if paths shift.
        script = None
        root = None
        here = Path(__file__).resolve()
        for candidate in [here] + list(here.parents):
            maybe = candidate / "scripts" / "helm_shim_demo.sh"
            if maybe.exists():
                script = maybe
                root = candidate
                break
        if script is None or root is None:
            raise RuntimeError("scripts/helm_shim_demo.sh not found")
        log_path: Path = _HELM_DEMO_STATE["log"]  # type: ignore[index]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - keep handle open for proc output
        env = os.environ.copy()
        # Avoid leaking a stale APISHIM_SERVER into the shim demo process.
        env.pop("APISHIM_SERVER", None)
        env.setdefault("PYTHONPATH", str(root / "src"))
        explicit_server = bool(_HELM_DEMO_STATE.get("server_override"))
        helm_server = os.getenv("AE_LABS_HELM_SERVER", "").strip() if explicit_server else ""
        if not helm_server:
            helm_server = os.getenv("AE_APISHIM_SERVER", "").strip()
        if not helm_server:
            raise RuntimeError("AE_APISHIM_SERVER is required to run the helm demo")
        token_val = (
            os.getenv("AE_LABS_HELM_TOKEN")
            or os.getenv("AE_APISHIM_TOKEN")
            or str(_HELM_DEMO_STATE.get("token") or "")
        ).strip()
        env.setdefault("TOKEN", token_val)
        env.setdefault("RUNTIME", str(_HELM_DEMO_STATE.get("runtime")))
        env.setdefault("NAMESPACE", str(_HELM_DEMO_STATE.get("namespace")))
        env.setdefault("CHART_NAME", str(_HELM_DEMO_STATE.get("chart")))
        try:
            keep = str(os.getenv("AE_LABS_HELM_KEEP", "") or "").strip()
            env.setdefault("HELM_SHIM_KEEP", keep if keep else "1")
        except Exception:
            env.setdefault("HELM_SHIM_KEEP", "1")
        # Verify the shim endpoint is reachable; do not start a local shim.
        try:
            import ssl as _ssl
            import urllib.request as _urlreq

            probe_url = helm_server.rstrip("/") + "/version"
            headers = {}
            if token_val:
                headers["Authorization"] = f"Bearer {token_val}"
            verify = _resolve_apishim_verify()
            ctx = None
            if helm_server.startswith("https://"):
                if isinstance(verify, str):
                    ctx = _ssl.create_default_context(cafile=verify)
                elif verify:
                    ctx = _ssl.create_default_context()
                else:
                    ctx = _ssl._create_unverified_context()  # noqa: S323
            req = _urlreq.Request(probe_url, headers=headers)  # noqa: S310
            with _urlreq.urlopen(req, timeout=2, context=ctx) as resp:  # noqa: S310
                if getattr(resp, "status", 200) >= 400:
                    raise RuntimeError("probe failed")
                body = resp.read().decode("utf-8", "ignore")
                if "k1s-shim" not in body:
                    raise RuntimeError("probe failed")
        except Exception as exc:
            raise RuntimeError(f"helm demo shim unreachable at {helm_server}: {exc}") from exc
        env.setdefault("APISHIM_SERVER", helm_server)
        try:
            import urllib.parse as _up

            parsed = _up.urlparse(helm_server)
            if parsed.port:
                _HELM_DEMO_STATE["port"] = parsed.port
        except Exception:
            pass
        env.setdefault("TMPDIR", str(log_path.parent))
        # Persist resolved shim endpoint + token for the controller mirror loop.
        try:
            if helm_server:
                os.environ["AE_LABS_HELM_SERVER"] = helm_server
            if token_val:
                os.environ["AE_LABS_HELM_TOKEN"] = token_val
        except Exception:
            pass
        try:
            db_hint = os.getenv("AE_APISHIM_DB", "").strip() or "<unset>"
            dsn_hint = "set" if os.getenv("AE_APISHIM_DSN") else "unset"
            logger.info(
                "labs helm demo shim resolved: server=%s db=%s dsn=%s",
                helm_server or "<unset>",
                db_hint,
                dsn_hint,
            )
        except Exception:
            pass
        # Allow shim demo apps to show up on demo-scoped dashboards.
        try:
            ns = str(_HELM_DEMO_STATE.get("namespace") or "")
            if ns:
                _LABS_APP_PREFIXES.add(f"{ns}--")
        except Exception:
            pass
        proc = subprocess.Popen(
            ["bash", str(script)],
            cwd=root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        _HELM_DEMO_STATE["proc"] = proc
        _HELM_DEMO_STATE["log_handle"] = log_handle
        _HELM_DEMO_STATE["started"] = datetime.now(timezone.utc).isoformat()
    status = _helm_demo_status()
    if port_note:
        status["message"] = port_note
    return status


def _session_pids(session_id: int) -> list[int]:
    pids: list[int] = []
    proc_root = Path("/proc")
    if proc_root.exists():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
            except Exception:
                continue
            try:
                if os.getsid(pid) == session_id:
                    pids.append(pid)
            except Exception:
                continue
        return pids
    try:
        output = subprocess.check_output(["ps", "-eo", "pid,sid"], text=True)
    except Exception:
        return pids
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            sid = int(parts[1])
        except Exception:
            continue
        if sid == session_id:
            pids.append(pid)
    return pids


def _descendant_pids(root_pid: int) -> list[int]:
    ppid_map: dict[int, list[int]] = {}
    proc_root = Path("/proc")
    if proc_root.exists():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
            except Exception:
                continue
            status_path = entry / "status"
            try:
                status_text = status_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            ppid = None
            for line in status_text.splitlines():
                if line.startswith("PPid:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            ppid = int(parts[1])
                        except Exception:
                            ppid = None
                    break
            if ppid is None:
                continue
            ppid_map.setdefault(ppid, []).append(pid)
    else:
        try:
            output = subprocess.check_output(["ps", "-eo", "pid,ppid"], text=True)
        except Exception:
            return []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except Exception:
                continue
            ppid_map.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    queue = [root_pid]
    seen = {root_pid}
    while queue:
        current = queue.pop(0)
        for child in ppid_map.get(current, []):
            if child in seen:
                continue
            seen.add(child)
            descendants.append(child)
            queue.append(child)
    return descendants


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        data = proc_stat.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True
    rparen = data.rfind(")")
    if rparen == -1:
        return True
    tail = data[rparen + 2 :].split()
    if not tail:
        return True
    state = tail[0]
    return state != "Z"


def _wait_pids_exit(pids: list[int], timeout: float = 1.0) -> None:
    if not pids:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [pid for pid in pids if _pid_exists(pid)]
        if not alive:
            return
        time.sleep(0.05)


def _signal_session(session_id: int | None, sig: int, *, exclude: set[int]) -> None:
    if session_id is None:
        return
    try:
        if os.getsid(0) == session_id:
            return
    except Exception:
        return
    for pid in _session_pids(session_id):
        if pid in exclude:
            continue
        try:
            os.kill(pid, sig)
        except Exception:
            continue


def _wait_session_exit(session_id: int | None, timeout: float = 1.0) -> None:
    if session_id is None:
        return
    try:
        if os.getsid(0) == session_id:
            return
    except Exception:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = [pid for pid in _session_pids(session_id) if pid != os.getpid()]
        if not remaining:
            return
        time.sleep(0.05)


def _helm_demo_stop() -> dict[str, object]:
    with _HELM_DEMO_LOCK:
        proc = _HELM_DEMO_STATE.get("proc")
        if proc and getattr(proc, "poll", lambda: None)() is None:
            session_id = None
            descendants = []
            try:
                session_id = os.getsid(proc.pid)
            except Exception:
                session_id = None
            try:
                descendants = _descendant_pids(proc.pid)
            except Exception:
                descendants = []
            all_pids = [proc.pid] + descendants
            try:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
                _signal_session(session_id, signal.SIGTERM, exclude={os.getpid()})
                for pid in descendants:
                    if pid == os.getpid():
                        continue
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        continue
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    _signal_session(session_id, signal.SIGKILL, exclude={os.getpid()})
                    for pid in descendants:
                        if pid == os.getpid():
                            continue
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            continue
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
            except Exception:
                pass
            _wait_pids_exit(all_pids, timeout=3.0)
            _wait_session_exit(session_id, timeout=1.0)
        _HELM_DEMO_STATE["proc"] = None
        handle = _HELM_DEMO_STATE.get("log_handle")
        if handle:
            try:
                handle.close()
            except Exception:
                pass
            _HELM_DEMO_STATE["log_handle"] = None
    return _helm_demo_status()


def set_reconcile_metrics(ts_seconds: float, duration_seconds: float) -> None:
    global _LAST_RECONCILE_TS, _LAST_RECONCILE_DURATION
    _LAST_RECONCILE_TS = ts_seconds
    _LAST_RECONCILE_DURATION = duration_seconds


def record_app_reconcile(
    app: str, duration_seconds: float, *, created: int, updated: int, removed: int
) -> None:
    """Record per-app reconcile duration and rollout operation counters."""
    _APP_RECONCILE_SUM[app] = _APP_RECONCILE_SUM.get(app, 0.0) + float(duration_seconds)
    _APP_RECONCILE_COUNT[app] = _APP_RECONCILE_COUNT.get(app, 0) + 1
    ops = _APP_ROLLOUT_OPS.setdefault(app, {"created": 0, "updated": 0, "removed": 0})
    ops["created"] += int(created)
    ops["updated"] += int(updated)
    ops["removed"] += int(removed)


def set_app_crashloop(app: str, *, ttl_seconds: float = 300.0) -> None:
    """Mark an app as in crashloop for a short TTL so metrics/UI can reflect it."""
    try:
        import time as _t

        _APP_CRASHLOOP_UNTIL[app] = float(_t.time()) + float(ttl_seconds)
    except Exception:
        pass


def record_hook_observation(
    app: str, hook: str, kind: str, duration_seconds: float, success: bool
) -> None:
    try:
        _HOOK_LAST[(str(app), str(hook), str(kind))] = (float(duration_seconds), bool(success))
    except Exception:
        pass


def record_canary_weight(app: str, weight: float) -> None:
    try:
        _APP_CANARY_WEIGHT[str(app)] = float(weight)
    except Exception:
        pass


def increment_canary_step(app: str) -> None:
    try:
        _APP_CANARY_STEPS[str(app)] = _APP_CANARY_STEPS.get(str(app), 0) + 1
    except Exception:
        pass


def record_probe_backoff(app: str, pod_name: str, probe_type: str, seconds: int) -> None:
    try:
        _PROBE_BACKOFF[(str(app), str(pod_name), str(probe_type))] = max(0, int(seconds))
    except Exception:
        pass


def _truthy_flag(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(raw: object) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _as_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            dt = (
                datetime.fromisoformat(raw[:-1] + "+00:00")
                if raw.endswith("Z")
                else datetime.fromisoformat(raw)
            )
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_iso8601(value: object) -> str | None:
    dt = _as_datetime(value)
    if dt is None:
        return None
    return dt.isoformat()


def _authority_presence_stale_after_seconds() -> float:
    try:
        return float(AuthorityConfig.from_env().presence_stale_after_seconds)
    except Exception:
        return 10.0


def _transport_site_join_key(site_id: object) -> str:
    return str(site_id or "").strip()


def _build_transport_snapshot() -> dict[str, object]:
    transport: dict[str, object] = {}
    backend = (os.getenv("AE_TRANSPORT_BACKEND") or "http").strip() or "http"
    transport["backend"] = backend
    js_domain = (os.getenv("AE_JS_DOMAIN") or "").strip()
    if js_domain:
        transport["js_domain"] = js_domain
    transport["outbox"] = {
        "ok_total": int(_OUTBOX_PUBLISH_OK),
        "fail_total": int(_OUTBOX_PUBLISH_FAIL),
    }

    try:
        grace = int(os.getenv("AE_SITE_NOTREADY_AFTER", "90") or 90)
    except Exception:
        grace = 90
    now_ts = time.time()
    site_details: dict[str, dict[str, object]] = {}
    gateway_nodes_by_site: dict[str, list[dict[str, object]]] = {}
    for site_id, last_ts in sorted(_SITE_LAST_SEEN.items()):
        age = _as_float(now_ts - float(last_ts))
        if age is None:
            continue
        site_key = _transport_site_join_key(site_id)
        site_details[site_key] = {
            "last_seen_age_s": age,
            "stale": age > grace,
        }
    for site_id, node_id in sorted(set(_SITE_GATEWAY_LAST_SEEN) | set(_SITE_GATEWAY_BUILD_INFO)):
        site_key = _transport_site_join_key(site_id)
        node_key = str(node_id or "").strip()
        if not site_key or not node_key:
            continue
        last_seen_ts = _SITE_GATEWAY_LAST_SEEN.get((site_id, node_id))
        last_seen_age = None
        if last_seen_ts is not None:
            try:
                last_seen_age = max(0.0, now_ts - float(last_seen_ts))
            except Exception:
                last_seen_age = None
        version, sha, date = _SITE_GATEWAY_BUILD_INFO.get(
            (site_id, node_id),
            ("unknown", "unknown", "unknown"),
        )
        gateway_nodes_by_site.setdefault(site_key, []).append(
            {
                "node_id": node_key,
                "last_seen_age_s": last_seen_age,
                "stale": bool(last_seen_age is not None and last_seen_age > grace),
                "version": str(version or ""),
                "sha": str(sha or ""),
                "date": str(date or ""),
            }
        )
    seen = len(site_details)
    stale = sum(1 for detail in site_details.values() if detail.get("stale"))
    last_seen_age = None
    for detail in site_details.values():
        age = _as_float(detail.get("last_seen_age_s"))
        if age is None:
            continue
        if last_seen_age is None or age < last_seen_age:
            last_seen_age = age
    transport["sites"] = {
        "seen": int(seen),
        "stale": int(stale),
        "fresh": int(max(0, seen - stale)),
        "last_seen_age_s": last_seen_age,
    }
    if site_details:
        transport["sites_detail"] = site_details

    js_summary: dict[str, object] | None = None
    if _JS_STREAM_STATS or _JS_CONSUMER_STATS:
        js_pending = 0.0
        js_ack_pending = 0.0
        js_redelivered = 0.0
        js_waiting = 0.0
        consumers_detail: list[dict[str, object]] = []
        for (stream, consumer), stats in sorted(_JS_CONSUMER_STATS.items()):
            pending = float(stats.get("pending", 0.0) or 0.0)
            ack_pending = float(stats.get("ack_pending", 0.0) or 0.0)
            redelivered = float(stats.get("redelivered", 0.0) or 0.0)
            waiting = float(stats.get("waiting", 0.0) or 0.0)
            js_pending += pending
            js_ack_pending += ack_pending
            js_redelivered += redelivered
            js_waiting += waiting
            consumers_detail.append(
                {
                    "stream": stream,
                    "consumer": consumer,
                    "site_id": str(stats.get("site_id", "") or ""),
                    "pending": pending,
                    "ack_pending": ack_pending,
                    "redelivered": redelivered,
                    "waiting": waiting,
                }
            )
        js_summary = {
            "streams": int(len(_JS_STREAM_STATS)),
            "consumers": int(len(_JS_CONSUMER_STATS)),
            "pending": js_pending,
            "ack_pending": js_ack_pending,
            "redelivered": js_redelivered,
            "waiting": js_waiting,
        }
        if consumers_detail:
            js_summary["consumers_detail"] = consumers_detail
        stream_detail: list[dict[str, object]] = []
        for stream, stats in sorted(_JS_STREAM_STATS.items()):
            stream_detail.append(
                {
                    "stream": stream,
                    "bytes_used": float(stats.get("bytes_used", 0.0) or 0.0),
                    "messages": float(stats.get("messages", 0.0) or 0.0),
                    "max_bytes": float(stats.get("max_bytes", 0.0) or 0.0),
                }
            )
        if stream_detail:
            js_summary["streams_detail"] = stream_detail
        transport["js"] = js_summary

    gateway_summary: dict[str, object] | None = None
    gateway_details: list[dict[str, object]] = []
    if _GATEWAY_WORK_METRICS:
        gw_nak = 0.0
        gw_stale = 0.0
        gw_retries = 0.0
        gw_replay = 0.0
        gw_replay_fail = 0.0
        gw_replay_backlog = 0.0
        for site_id, stats in sorted(_GATEWAY_WORK_METRICS.items()):
            site_key = _transport_site_join_key(site_id)
            detail = {
                "site_id": site_key,
                "work_nak_total": float(stats.get("work_nak_total", 0.0) or 0.0),
                "work_stale_total": float(stats.get("work_stale_total", 0.0) or 0.0),
                "lease_retry_total": float(stats.get("lease_retry_total", 0.0) or 0.0),
                "result_replay_total": float(stats.get("result_replay_total", 0.0) or 0.0),
                "result_replay_fail_total": float(
                    stats.get("result_replay_fail_total", 0.0) or 0.0
                ),
                "result_replay_backlog": float(stats.get("result_replay_backlog", 0.0) or 0.0),
            }
            gw_nak += float(detail["work_nak_total"])
            gw_stale += float(detail["work_stale_total"])
            gw_retries += float(detail["lease_retry_total"])
            gw_replay += float(detail["result_replay_total"])
            gw_replay_fail += float(detail["result_replay_fail_total"])
            gw_replay_backlog += float(detail["result_replay_backlog"])
            gateway_details.append(detail)
        gateway_summary = {
            "work_nak_total": gw_nak,
            "work_stale_total": gw_stale,
            "lease_retry_total": gw_retries,
            "result_replay_total": gw_replay,
            "result_replay_fail_total": gw_replay_fail,
            "result_replay_backlog": gw_replay_backlog,
            "sites": int(len(gateway_details)),
        }
        if gateway_details:
            gateway_summary["sites_detail"] = gateway_details
        transport["gateway"] = gateway_summary

    route_summary: dict[str, object] | None = None
    route_details: list[dict[str, object]] = []
    if _ROUTE_BUNDLE_METRICS:
        ok_total = 0.0
        fail_total = 0.0
        publish_ok_total = 0.0
        publish_fail_total = 0.0
        pending_sites = 0.0
        max_ack_age = None
        last_latency = None
        for site_id, stats in sorted(_ROUTE_BUNDLE_METRICS.items()):
            detail = {
                "site_id": _transport_site_join_key(site_id),
                "bundle_ok_total": float(stats.get("apply_ok_total", 0.0) or 0.0),
                "bundle_fail_total": float(stats.get("apply_fail_total", 0.0) or 0.0),
                "publish_ok_total": float(stats.get("publish_ok_total", 0.0) or 0.0),
                "publish_fail_total": float(stats.get("publish_fail_total", 0.0) or 0.0),
                "pending": float(stats.get("pending", 0.0) or 0.0),
                "ack_age_s": _as_float(stats.get("ack_age_s")),
                "last_latency_s": _as_float(stats.get("last_latency_s")),
            }
            ok_total += float(detail["bundle_ok_total"])
            fail_total += float(detail["bundle_fail_total"])
            publish_ok_total += float(detail["publish_ok_total"])
            publish_fail_total += float(detail["publish_fail_total"])
            pending_sites += float(detail["pending"])
            ack_age = _as_float(detail["ack_age_s"])
            if ack_age is not None and (max_ack_age is None or ack_age > max_ack_age):
                max_ack_age = ack_age
            latency = _as_float(detail["last_latency_s"])
            if latency is not None and (last_latency is None or latency > last_latency):
                last_latency = latency
            route_details.append(detail)
        route_summary = {
            "bundle_ok_total": ok_total,
            "bundle_fail_total": fail_total,
            "publish_ok_total": publish_ok_total,
            "publish_fail_total": publish_fail_total,
            "pending_sites": pending_sites,
            "max_ack_age_s": max_ack_age,
            "last_latency_s": last_latency,
            "sites": int(len(route_details)),
        }
        if route_details:
            route_summary["sites_detail"] = route_details
        transport["routes"] = route_summary

    fence_summary: dict[str, object] | None = None
    fence_details: list[dict[str, object]] = []
    if _HA_FENCE_METRICS:
        stale_total = 0.0
        duplicate_total = 0.0
        epoch_advance_total = 0.0
        for surface, stats in sorted(_HA_FENCE_METRICS.items()):
            detail = {
                "surface": surface,
                "stale_total": float(stats.get("stale_total", 0.0) or 0.0),
                "duplicate_total": float(stats.get("duplicate_total", 0.0) or 0.0),
                "epoch_advance_total": float(stats.get("epoch_advance_total", 0.0) or 0.0),
            }
            stale_total += float(detail["stale_total"])
            duplicate_total += float(detail["duplicate_total"])
            epoch_advance_total += float(detail["epoch_advance_total"])
            fence_details.append(detail)
        fence_summary = {
            "stale_total": stale_total,
            "duplicate_total": duplicate_total,
            "epoch_advance_total": epoch_advance_total,
            "surfaces": int(len(fence_details)),
        }
        if fence_details:
            fence_summary["surfaces_detail"] = fence_details
        transport["ha_fence"] = fence_summary

    site_rows: list[dict[str, object]] = []
    site_ids = sorted(
        set(site_details)
        | {detail["site_id"] for detail in gateway_details}
        | {detail["site_id"] for detail in route_details}
        | set(gateway_nodes_by_site)
    )
    gateway_by_site = {detail["site_id"]: detail for detail in gateway_details}
    route_by_site = {detail["site_id"]: detail for detail in route_details}
    for site_id in site_ids:
        site_detail = site_details.get(site_id, {})
        gateway_detail = gateway_by_site.get(site_id, {})
        route_detail = route_by_site.get(site_id, {})
        site_rows.append(
            {
                "site_id": site_id,
                "last_seen_age_s": _as_float(site_detail.get("last_seen_age_s")),
                "stale": bool(site_detail.get("stale", False)),
                "gateway_count": len(gateway_nodes_by_site.get(site_id, [])),
                "result_replay_backlog": float(
                    gateway_detail.get("result_replay_backlog", 0.0) or 0.0
                ),
                "work_nak_total": float(gateway_detail.get("work_nak_total", 0.0) or 0.0),
                "publish_pending": float(route_detail.get("pending", 0.0) or 0.0),
                "ack_age_s": _as_float(route_detail.get("ack_age_s")),
                "gateways": gateway_nodes_by_site.get(site_id, []),
            }
        )
    if site_rows:
        transport["site_rows"] = site_rows

    return transport


def _build_authority_snapshot(
    authority_snapshot: object | None,
    authority_members: list[object] | None = None,
) -> dict[str, object]:
    enabled = (
        bool(getattr(authority_snapshot, "enabled", False))
        if authority_snapshot is not None
        else _truthy_flag(os.getenv("AE_HA_MODE"))
    )
    controller_id = None
    if authority_snapshot is not None:
        controller_id = str(getattr(authority_snapshot, "controller_id", "") or "").strip() or None
    if controller_id is None:
        controller_id = str(os.getenv("AE_CONTROLLER_ID") or "").strip() or None
    is_leader = (
        bool(getattr(authority_snapshot, "is_leader", False))
        if authority_snapshot is not None
        else False
    )
    leader_info = (
        getattr(authority_snapshot, "leader_info", None) if authority_snapshot is not None else None
    )
    leader_id = (
        str(getattr(leader_info, "controller_id", "") or "").strip() or None
        if leader_info is not None
        else None
    )
    advertise_addr = (
        str(getattr(leader_info, "advertise_addr", "") or "").strip() or None
        if leader_info is not None
        else None
    )
    controller_epoch = (
        _as_int(getattr(authority_snapshot, "controller_epoch", 0))
        if authority_snapshot is not None
        else 0
    )
    if is_leader:
        if leader_id is None:
            leader_id = controller_id
        if advertise_addr is None:
            advertise_addr = str(os.getenv("AE_CONTROLLER_ADVERTISE_ADDR") or "").strip() or None
    healthy = (not enabled) or is_leader or leader_id is not None
    stale_after_seconds = _authority_presence_stale_after_seconds()
    now_ts = time.time()
    members: list[dict[str, object]] = []
    for member in list(authority_members or []):
        get = member.get if isinstance(member, dict) else lambda key: getattr(member, key, None)
        member_id = str(get("controller_id") or "").strip()
        if not member_id:
            continue
        member_is_leader = bool(get("is_leader")) or (
            leader_id is not None and member_id == str(leader_id)
        )
        role = str(get("role") or ("leader" if member_is_leader else "standby")).strip().lower()
        if role not in {"leader", "standby"}:
            role = "leader" if member_is_leader else "standby"
        heartbeat_at = _as_iso8601(get("heartbeat_at") or get("last_heartbeat_at"))
        heartbeat_dt = _as_datetime(heartbeat_at)
        heartbeat_age_s = None
        freshness = "unknown"
        if heartbeat_dt is not None:
            heartbeat_age_s = max(0.0, now_ts - heartbeat_dt.timestamp())
            freshness = "stale" if heartbeat_age_s > stale_after_seconds else "fresh"
        members.append(
            {
                "controller_id": member_id,
                "advertise_addr": str(get("advertise_addr") or "").strip() or None,
                "version": str(get("version") or "").strip() or None,
                "is_leader": bool(member_is_leader),
                "is_local": bool(get("is_local"))
                or (controller_id is not None and member_id == str(controller_id)),
                "role": role,
                "last_heartbeat_at": heartbeat_at,
                "last_heartbeat_age_s": heartbeat_age_s,
                "freshness": freshness,
                "stale_after_seconds": stale_after_seconds,
            }
        )
    members.sort(
        key=lambda member: (
            0 if member["is_leader"] else 1,
            0 if member["is_local"] else 1,
            str(member["controller_id"]),
        )
    )
    return {
        "healthy": bool(healthy),
        "is_leader": bool(is_leader),
        "controller_id": controller_id,
        "leader_id": leader_id,
        "leader_advertise_addr": advertise_addr,
        "controller_epoch": int(controller_epoch),
        "member_count": len(members),
        "members": members,
    }


def _build_ha_snapshot(
    *,
    extra: dict[str, object],
    authority_snapshot: object | None,
    authority_members: list[object] | None,
    transport: dict[str, object],
) -> dict[str, object]:
    authority = _build_authority_snapshot(authority_snapshot, authority_members)
    build = AE_BUILD_INFO()
    probe_snapshot = extra.get("ha_probes") if isinstance(extra.get("ha_probes"), dict) else {}
    probe_snapshot = dict(probe_snapshot or {})
    etcd_probe = probe_snapshot.get("etcd") if isinstance(probe_snapshot.get("etcd"), dict) else {}
    etcd_probe = dict(etcd_probe or {})

    js_summary = dict(transport.get("js") or {})
    gateway_summary = dict(transport.get("gateway") or {})
    route_summary = dict(transport.get("routes") or {})
    fence_summary = dict(transport.get("ha_fence") or {})
    site_summary = dict(transport.get("sites") or {})
    site_rows = list(transport.get("site_rows") or [])

    jetstream = {
        "stream_count": _as_int(js_summary.get("streams")),
        "consumer_count": _as_int(js_summary.get("consumers")),
        "pending": float(js_summary.get("pending", 0.0) or 0.0),
        "ack_pending": float(js_summary.get("ack_pending", 0.0) or 0.0),
        "redelivered": float(js_summary.get("redelivered", 0.0) or 0.0),
        "waiting": float(js_summary.get("waiting", 0.0) or 0.0),
        "consumers": list(js_summary.get("consumers_detail") or []),
        "streams": list(js_summary.get("streams_detail") or []),
    }
    gateway = {
        "site_count": _as_int(gateway_summary.get("sites")),
        "work_nak_total": float(gateway_summary.get("work_nak_total", 0.0) or 0.0),
        "work_stale_total": float(gateway_summary.get("work_stale_total", 0.0) or 0.0),
        "lease_retry_total": float(gateway_summary.get("lease_retry_total", 0.0) or 0.0),
        "result_replay_total": float(gateway_summary.get("result_replay_total", 0.0) or 0.0),
        "result_replay_fail_total": float(
            gateway_summary.get("result_replay_fail_total", 0.0) or 0.0
        ),
        "result_replay_backlog": float(gateway_summary.get("result_replay_backlog", 0.0) or 0.0),
        "sites": list(gateway_summary.get("sites_detail") or []),
    }
    routes = {
        "site_count": _as_int(route_summary.get("sites")),
        "bundle_ok_total": float(route_summary.get("bundle_ok_total", 0.0) or 0.0),
        "bundle_fail_total": float(route_summary.get("bundle_fail_total", 0.0) or 0.0),
        "publish_ok_total": float(route_summary.get("publish_ok_total", 0.0) or 0.0),
        "publish_fail_total": float(route_summary.get("publish_fail_total", 0.0) or 0.0),
        "pending_sites": float(route_summary.get("pending_sites", 0.0) or 0.0),
        "max_ack_age_s": _as_float(route_summary.get("max_ack_age_s")),
        "last_latency_s": _as_float(route_summary.get("last_latency_s")),
        "sites": list(route_summary.get("sites_detail") or []),
    }
    fence = {
        "surface_count": _as_int(fence_summary.get("surfaces")),
        "stale_total": float(fence_summary.get("stale_total", 0.0) or 0.0),
        "duplicate_total": float(fence_summary.get("duplicate_total", 0.0) or 0.0),
        "epoch_advance_total": float(fence_summary.get("epoch_advance_total", 0.0) or 0.0),
        "surfaces": list(fence_summary.get("surfaces_detail") or []),
    }

    etcd_members = list(etcd_probe.get("members") or [])
    etcd = {
        "configured_endpoints": _split_csv(os.getenv("AE_ETCD_ENDPOINTS")),
        "maintenance_runs_total": float(_ETCD_MAINTENANCE_RUNS_TOTAL),
        "maintenance_triggered_total": float(_ETCD_MAINTENANCE_TRIGGERED_TOTAL),
        "healthy_endpoints": _as_int(etcd_probe.get("healthy_endpoints")),
        "unhealthy_endpoints": _as_int(etcd_probe.get("unhealthy_endpoints")),
        "members": etcd_members,
        "last_probe_ts": _as_float(probe_snapshot.get("last_probe_ts")),
        "probes_enabled": bool(probe_snapshot.get("enabled")),
    }

    hpa = {
        "reconcile_total": float(_HPA_ACTIVITY_METRICS.get("reconcile_total", 0.0) or 0.0),
        "scale_total": float(_HPA_ACTIVITY_METRICS.get("scale_total", 0.0) or 0.0),
        "metrics_stale_total": float(_HPA_ACTIVITY_METRICS.get("metrics_stale_total", 0.0) or 0.0),
        "metrics_missing_total": float(
            _HPA_ACTIVITY_METRICS.get("metrics_missing_total", 0.0) or 0.0
        ),
        "snapshot_age_seconds": float(
            _HPA_ACTIVITY_METRICS.get("snapshot_age_seconds", 0.0) or 0.0
        ),
    }

    transport_snapshot = {
        "backend": transport.get("backend"),
        "js_domain": transport.get("js_domain"),
        "outbox": dict(transport.get("outbox") or {}),
        "site_summary": site_summary,
        "sites": site_rows,
        "jetstream": jetstream,
        "gateway": gateway,
        "routes": routes,
        "fence": fence,
    }
    if isinstance(probe_snapshot.get("hubs"), dict):
        transport_snapshot["hub_monitors"] = dict(probe_snapshot["hubs"])
    if isinstance(probe_snapshot.get("edges"), dict):
        transport_snapshot["edge_monitors"] = dict(probe_snapshot["edges"])

    issues: list[dict[str, str]] = []
    enabled = bool(authority_snapshot is not None and getattr(authority_snapshot, "enabled", False))
    if authority_snapshot is None:
        enabled = _truthy_flag(os.getenv("AE_HA_MODE"))
    if enabled and not bool(authority.get("healthy")):
        issues.append(
            {
                "code": "authority_unhealthy",
                "severity": "error",
                "message": "controller authority cannot resolve a leader",
            }
        )
    if bool(etcd.get("probes_enabled")) and _as_int(etcd.get("unhealthy_endpoints")) > 0:
        issues.append(
            {
                "code": "etcd_probe_degraded",
                "severity": "error" if _as_int(etcd.get("healthy_endpoints")) == 0 else "warn",
                "message": f"etcd probes report {_as_int(etcd.get('unhealthy_endpoints'))} unhealthy endpoint(s)",
            }
        )
    if _as_int(site_summary.get("stale")) > 0:
        issues.append(
            {
                "code": "stale_sites",
                "severity": "warn",
                "message": f"{_as_int(site_summary.get('stale'))} edge site(s) are stale",
            }
        )
    if float(gateway.get("result_replay_backlog", 0.0) or 0.0) > 0:
        issues.append(
            {
                "code": "gateway_replay_backlog",
                "severity": "warn",
                "message": "gateway replay backlog is non-zero",
            }
        )
    if float(routes.get("pending_sites", 0.0) or 0.0) > 0:
        issues.append(
            {
                "code": "route_publish_pending",
                "severity": "warn",
                "message": "route bundle acknowledgements are still pending",
            }
        )
    if (
        float(fence.get("stale_total", 0.0) or 0.0) > 0
        or float(fence.get("duplicate_total", 0.0) or 0.0) > 0
    ):
        issues.append(
            {
                "code": "ha_fence_activity",
                "severity": "warn",
                "message": "HA fence counters report stale or duplicate mutation activity",
            }
        )
    if (
        float(hpa.get("metrics_stale_total", 0.0) or 0.0) > 0
        or float(hpa.get("metrics_missing_total", 0.0) or 0.0) > 0
    ):
        issues.append(
            {
                "code": "hpa_metrics_quality",
                "severity": "warn",
                "message": "HPA metrics snapshots have been stale or missing",
            }
        )
    hub_monitors = (
        transport_snapshot.get("hub_monitors")
        if isinstance(transport_snapshot.get("hub_monitors"), dict)
        else {}
    )
    if hub_monitors and (hub_monitors.get("issues") or hub_monitors.get("errors")):
        issues.append(
            {
                "code": "hub_transport_probe",
                "severity": "warn",
                "message": "hub NATS/JetStream monitor probes report cluster drift or fetch failures",
            }
        )
    edge_monitors = (
        transport_snapshot.get("edge_monitors")
        if isinstance(transport_snapshot.get("edge_monitors"), dict)
        else {}
    )
    if edge_monitors and edge_monitors.get("errors"):
        issues.append(
            {
                "code": "edge_transport_probe",
                "severity": "warn",
                "message": "edge monitor probes report fetch failures",
            }
        )

    return {
        "enabled": bool(enabled),
        "authority": authority,
        "controller_build": {
            "version": str(build.get("version") or "unknown"),
            "sha": str(build.get("sha") or "unknown"),
            "date": str(build.get("date") or "unknown"),
        },
        "etcd": etcd,
        "transport": transport_snapshot,
        "hpa": hpa,
        "issues": issues,
    }


_SITE_LAYOUT_PROFILES = {
    "core",
    "edge",
    "k1s-core",
    "k1s-edge",
    "k1s-ha-core",
}


def _dashboard_node_site_id(node: object) -> str:
    if not isinstance(node, dict):
        return ""
    site_id = str(node.get("site_id") or "").strip()
    if site_id:
        return site_id
    labels = node.get("labels")
    if isinstance(labels, dict):
        site_id = str(labels.get("site") or "").strip()
        if site_id:
            return site_id
    node_id = str(node.get("id") or "").strip()
    if "--" in node_id:
        return str(node_id.split("--", 1)[0] or "").strip()
    return ""


def _dashboard_node_value(node: object, key: str) -> str:
    if not isinstance(node, dict):
        return ""
    value = str(node.get(key) or "").strip()
    if value:
        return value
    labels = node.get("labels")
    if isinstance(labels, dict):
        return str(labels.get(key) or "").strip()
    return ""


def _dashboard_layout_mode(payload: dict[str, object], ha: dict[str, object]) -> str:
    if bool(ha.get("enabled")) or _truthy_flag(os.getenv("AE_HA_MODE")):
        return "site"

    nodes = payload.get("nodes")
    if isinstance(nodes, list) and nodes:
        for node in nodes:
            role = _dashboard_node_value(node, "role").lower()
            profile = _dashboard_node_value(node, "profile").lower()
            if _dashboard_node_site_id(node):
                return "site"
            if role and role != "controller":
                return "site"
            if profile in _SITE_LAYOUT_PROFILES:
                return "site"
        return "simple"

    labels: dict[str, str] = {}
    for item in _split_csv(os.getenv("AE_NODE_LABELS")):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            labels[key] = value

    role = str(labels.get("role") or "").strip().lower()
    profile = str(os.getenv("AE_NODE_PROFILE") or labels.get("profile") or "").strip().lower()
    if str(labels.get("site") or "").strip():
        return "site"
    if role and role != "controller":
        return "site"
    if profile in _SITE_LAYOUT_PROFILES:
        return "site"
    return "simple"


def _dashboard_bootstrap_token() -> str:
    """Return a read-capable token for simple local demo/dev dashboards.

    The dashboard page itself is public in local demo/dev flows, but its data
    fetches still hit bearer-protected read endpoints. Seed a token only for
    simple local profiles so the page renders out of the box without exposing
    tokens in HA/core/site-aware lanes.
    """

    if _truthy_flag(os.getenv("AE_HA_MODE")):
        return ""

    if str(os.getenv("AE_SITE_ID") or "").strip():
        return ""

    profile = str(
        os.getenv("AE_NODE_PROFILE") or os.getenv("AE_PROFILE") or ""
    ).strip().lower()
    if profile in _SITE_LAYOUT_PROFILES:
        return ""

    transport = str(os.getenv("AE_TRANSPORT_BACKEND") or "").strip().lower()
    simple_local = False
    if _truthy_flag(os.getenv("AE_DEMO_MODE")):
        simple_local = True
    elif _truthy_flag(os.getenv("AE_LABS")) and transport in {"", "http"}:
        simple_local = True

    if not simple_local:
        return ""

    return str(
        os.getenv("AE_API_READ_TOKEN")
        or os.getenv("AE_API_SCALER_TOKEN")
        or os.getenv("AE_API_ADMIN_TOKEN")
        or ""
    ).strip()


class _ApiHandler(http.server.BaseHTTPRequestHandler):
    store: SQLiteStateStore  # injected
    metrics: MetricsService  # injected
    # Optional mutators injected by controller when enabled
    scale_fn = None  # type: ignore[var-annotated]
    delete_fn = None  # type: ignore[var-annotated]
    apply_fn = None  # type: ignore[var-annotated]
    # Optional system info provider injected by controller
    system_info_fn = None  # type: ignore[var-annotated]
    authority_info_fn = None  # type: ignore[var-annotated]
    authority_members_fn = None  # type: ignore[var-annotated]
    plan_fn = None  # type: ignore[var-annotated]
    rollout_pause_fn = None  # type: ignore[var-annotated]
    rollout_resume_fn = None  # type: ignore[var-annotated]

    def send_response(self, code: int, message=None):  # type: ignore[override]
        super().send_response(code, message)
        try:
            if hasattr(self.request, "responses"):
                self.request.responses.append(code)
        except Exception:
            pass

    def handle_one_request(self) -> None:  # type: ignore[override]
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Clients can disconnect mid-response (dashboard/labs polls); ignore noisy socket errors.
            return

    # --- Dev CORS helpers (used by the labs playground) ----------------
    def _labs_enabled(self) -> bool:
        try:
            import os as _os

            return _os.getenv("AE_LABS") == "1"
        except Exception:
            return False

    def _flag_enabled(self, name: str, default: bool = True) -> bool:
        try:
            raw = os.getenv(name)
        except Exception:
            raw = None
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _dashboard_enabled(self) -> bool:
        return self._flag_enabled("AE_DASHBOARD", True)

    def _controlplane_readonly_enabled(self) -> bool:
        return self._flag_enabled(
            "AE_CONTROLPLANE_PUBLIC_ENABLE",
            False,
        ) and self._flag_enabled("AE_CONTROLPLANE_AUTH_ENABLE", False)

    def _playground_enabled(self) -> bool:
        if self._controlplane_readonly_enabled():
            return False
        try:
            raw = os.getenv("AE_PLAYGROUND")
        except Exception:
            raw = None
        if raw is None or str(raw).strip() == "":
            return not self._flag_enabled("AE_HA_MODE", False)
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _labs_token_valid(self) -> bool:
        if not self._labs_enabled():
            return False
        import os as _os

        tok = (_os.getenv("AE_LABS_TOKEN") or "").strip()
        if not tok:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr == f"Bearer {tok}":
            return True
        _p, _, q = self.path.partition("?")
        if q:
            import urllib.parse as _up

            params = _up.parse_qs(q)
            if (params.get("token") or [""])[0] == tok:
                return True
        return False

    def _labs_request_authorized(self) -> bool:
        if not self._labs_enabled():
            return False
        import os as _os
        import urllib.parse as _up

        tok = (_os.getenv("AE_LABS_TOKEN") or "").strip()
        if not tok:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr == f"Bearer {tok}":
            return True
        _p, _, q = self.path.partition("?")
        if q:
            params = _up.parse_qs(q)
            if (params.get("token") or [""])[0] == tok:
                return True
        return False

    def _call_apply(self, payload: dict, source: str | None = None, labels: dict | None = None):
        if self.apply_fn is None:
            raise RuntimeError("apply not available")
        app = ""
        try:
            from ae.controller.spec import app_key

            meta = payload.get("metadata") if isinstance(payload, dict) else {}
            if isinstance(meta, dict):
                app = app_key(str(meta.get("name") or ""), meta.get("namespace"))
            if not app:
                # Some callers may wrap the manifest
                inner = payload.get("manifest") if isinstance(payload, dict) else None
                if isinstance(inner, dict):
                    inner_meta = inner.get("metadata") or {}
                    if isinstance(inner_meta, dict):
                        app = app_key(
                            str(inner_meta.get("name") or ""), inner_meta.get("namespace")
                        )
            peer = "unknown"
            try:
                peer = str(self.client_address[0]) if self.client_address else "unknown"
            except Exception:
                peer = "unknown"
            ua = (self.headers.get("User-Agent") or "").strip()
            if len(ua) > 160:
                ua = ua[:160] + "…"
            auth_present = bool(self.headers.get("Authorization"))
            logger.info(
                "apply request source=%s app=%s peer=%s ua=%s path=%s auth=%s",
                source or "unknown",
                app or "<unknown>",
                peer,
                ua or "<none>",
                getattr(self, "path", "") or "",
                "yes" if auth_present else "no",
            )
        except Exception:
            pass
        try:
            return self.apply_fn(payload, source=source, labels=labels)  # type: ignore[misc]
        except TypeError:
            try:
                return self.apply_fn(payload)  # type: ignore[misc]
            except Exception:
                logger.exception(
                    "apply handler failed source=%s app=%s via legacy fallback",
                    source or "unknown",
                    app or "<unknown>",
                )
                raise
        except Exception:
            logger.exception(
                "apply handler failed source=%s app=%s",
                source or "unknown",
                app or "<unknown>",
            )
            raise

    def _maybe_cors(self) -> None:
        if not self._labs_enabled():
            return
        try:
            import os as _os

            origin = (_os.getenv("AE_LABS_CORS_ORIGIN", "*") or "*").strip()
            req_origin = (self.headers.get("Origin") or "").strip()
            allow_origin = origin
            if origin in {"*", "auto"} and req_origin:
                allow_origin = req_origin
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            if allow_origin != "*":
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        except Exception:
            pass

    def end_headers(self) -> None:  # type: ignore[override]
        # Inject permissive CORS for labs when enabled
        self._maybe_cors()
        super().end_headers()

    def do_OPTIONS(self) -> None:  # type: ignore[override]
        # Preflight CORS support for labs
        if self._labs_enabled():
            self.send_response(204)
            self._maybe_cors()
            super().end_headers()
        else:
            self.send_response(404)
            super().end_headers()

    # --- Auth helpers -------------------------------------------------
    def _require_role(self, role: str) -> bool:
        """Return True if the presented bearer token satisfies the required role.

        Roles (lowest to highest): read (1), scale (2), admin (3).
        If no tokens are configured in the environment, read access is allowed
        by default and mutations remain gated by AE_API_MUTATIONS.
        """
        import os
        import urllib.parse as _up

        # Extract presented token
        auth = self.headers.get("Authorization", "")
        token = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""
        if not token and getattr(self, "command", "").upper() == "GET":
            _p, _, q = self.path.partition("?")
            if q:
                params = _up.parse_qs(q)
                token = str((params.get("token") or [""])[0] or "").strip()

        # Configured tokens
        admin = os.getenv("AE_API_ADMIN_TOKEN")
        scaler = os.getenv("AE_API_SCALER_TOKEN")
        reader = os.getenv("AE_API_READ_TOKEN")

        # Optional expiry checks (ISO8601). If set and now > expiry, treat token as absent.
        def _not_expired(env_name: str) -> bool:
            val = os.getenv(env_name)
            if not val:
                return True
            try:
                s = val.strip()
                from datetime import datetime, timezone

                if s.endswith("Z"):
                    dt = datetime.fromisoformat(s[:-1] + "+00:00")
                else:
                    dt = datetime.fromisoformat(s)
                now = datetime.now(timezone.utc)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return now <= dt
            except Exception:
                return True

        have_any = any([admin, scaler, reader])
        if not have_any:
            # No tokens configured: allow reads; other methods are handled separately
            return role in {"read", ""}

        # Determine presented level
        level = 0
        if token and reader and token == reader and _not_expired("AE_API_READ_TOKEN_EXPIRES"):
            level = 1
        if token and scaler and token == scaler and _not_expired("AE_API_SCALER_TOKEN_EXPIRES"):
            level = max(level, 2)
        if token and admin and token == admin and _not_expired("AE_API_ADMIN_TOKEN_EXPIRES"):
            level = max(level, 3)

        # Warn once per role when near expiry
        try:
            import os as _os

            warn_hours = float(_os.getenv("AE_API_TOKEN_WARN_HOURS", "24"))
            from datetime import datetime as _dt
            from datetime import timedelta as _td
            from datetime import timezone as _tz

            def _warn_if(role: str, env: str) -> None:
                global _TOKEN_WARNED
                if role in _TOKEN_WARNED:
                    return
                val = _os.getenv(env)
                if not val:
                    return
                try:
                    s = val.strip()
                    exp = (
                        _dt.fromisoformat(s[:-1] + "+00:00")
                        if s.endswith("Z")
                        else _dt.fromisoformat(s)
                    )
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=_tz.utc)
                    now = _dt.now(_tz.utc)
                    if exp - now <= _td(hours=warn_hours):
                        import logging as _log

                        rem = (exp - now).total_seconds()
                        _log.getLogger(__name__).warning(
                            "API token for role '%s' expires in %.0f seconds", role, rem
                        )
                        _TOKEN_WARNED.add(role)
                except Exception:
                    pass

            _warn_if("admin", "AE_API_ADMIN_TOKEN_EXPIRES")
            _warn_if("scaler", "AE_API_SCALER_TOKEN_EXPIRES")
            _warn_if("read", "AE_API_READ_TOKEN_EXPIRES")
        except Exception:
            pass

        required = {"": 0, "read": 1, "scale": 2, "admin": 3}.get(role, 0)
        return level >= required

    def _rbac_allows(self, verb: str, _app: str | None = None) -> bool:
        import os

        if os.getenv("AE_API_RBAC", "0") != "1":
            return True
        auth = self.headers.get("Authorization", "")
        token = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""
        admin = os.getenv("AE_API_ADMIN_TOKEN")
        scaler = os.getenv("AE_API_SCALER_TOKEN")
        reader = os.getenv("AE_API_READ_TOKEN")
        role = ""
        if token and admin and token == admin:
            role = "admin"
        elif token and scaler and token == scaler:
            role = "scale"
        elif token and reader and token == reader:
            role = "read"
        policy = {
            "get": {"admin", "scale", "read"},
            "list": {"admin", "scale", "read"},
            "watch": {"admin", "scale", "read"},
            "create": {"admin"},
            "update": {"admin", "scale"},
            "patch": {"admin", "scale"},
            "delete": {"admin"},
        }
        allowed = policy.get(verb, {"admin"})
        return role in allowed

    # Role/scope helpers -------------------------------------------------
    def _presented_role(self) -> str:
        """Return presented role name (admin|scale|read) or empty string if none."""
        import os

        auth = self.headers.get("Authorization", "")
        token = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""

        # Reuse expiry logic via a small inner function
        def _not_expired(env_name: str) -> bool:
            val = os.getenv(env_name)
            if not val:
                return True
            try:
                s = val.strip()
                from datetime import datetime, timezone

                if s.endswith("Z"):
                    dt = datetime.fromisoformat(s[:-1] + "+00:00")
                else:
                    dt = datetime.fromisoformat(s)
                now = datetime.now(timezone.utc)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return now <= dt
            except Exception:
                return True

        admin = os.getenv("AE_API_ADMIN_TOKEN")
        scaler = os.getenv("AE_API_SCALER_TOKEN")
        reader = os.getenv("AE_API_READ_TOKEN")
        if token and admin and token == admin and _not_expired("AE_API_ADMIN_TOKEN_EXPIRES"):
            return "admin"
        if token and scaler and token == scaler and _not_expired("AE_API_SCALER_TOKEN_EXPIRES"):
            return "scale"
        if token and reader and token == reader and _not_expired("AE_API_READ_TOKEN_EXPIRES"):
            return "read"
        return ""

    def _scope_allows(self, role: str, app: str) -> bool:
        """Check optional scope patterns for a role (mutations only).

        Env vars (comma-separated glob patterns):
        - AE_API_ADMIN_SCOPE
        - AE_API_SCALER_SCOPE
        - AE_API_READ_SCOPE (not currently enforced for reads)
        """
        import fnmatch
        import os

        key = {
            "admin": "AE_API_ADMIN_SCOPE",
            "scale": "AE_API_SCALER_SCOPE",
            "read": "AE_API_READ_SCOPE",
        }.get(role)
        if not key:
            return False
        raw = os.getenv(key, "").strip()
        if not raw:
            return True
        patterns = [p.strip() for p in raw.split(",") if p.strip()]
        if not patterns:
            return True
        return any(fnmatch.fnmatch(app, pat) for pat in patterns)

    def _deny(self, code: int, message: str = "unauthorized") -> None:
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if code == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # type: ignore[override]
        path_only = self.path.split("?", 1)[0]

        # Labs SSE (dev-only): stream events/status to the playground
        if path_only.startswith("/labs/sse/"):
            if not self._labs_enabled() or not self._playground_enabled():
                self.send_response(404)
                self.end_headers()
                return
            if not self._labs_request_authorized():
                self._deny(401)
                return
            if path_only == "/labs/sse/events":
                self._handle_labs_sse_events()
                return
            if path_only == "/labs/sse/events_html":
                self._handle_labs_sse_events_html()
                return
            if path_only == "/labs/sse/status":
                self._handle_labs_sse_status()
                return
            if path_only == "/labs/sse/status_badge":
                self._handle_labs_sse_status_badge()
                return
            self.send_response(404)
            self.end_headers()
            return
        if path_only == "/labs/helm-demo" and self._labs_enabled():
            if not self._playground_enabled():
                self.send_response(404)
                self.end_headers()
                return
            if not self._labs_request_authorized():
                self._deny(401)
                return
            self._json_ok(_helm_demo_status())
            return
        # Public UI feature flags (non-sensitive)
        if path_only == "/ui/features":
            self._handle_ui_features()
            return
        if path_only in ("/__ae/version", "/__ae/version/"):
            self._handle_version_info()
            return
        # Metrics allowed without auth
        if path_only.startswith("/metrics"):
            self._handle_metrics()
            return
        if path_only.startswith("/static/"):
            if self._handle_static_asset(path_only):
                return
            self.send_response(404)
            self.end_headers()
            return
        # Public pages (always allowed): OpenAPI + lightweight docs UIs
        if path_only in {
            "/openapi.json",
            "/openapi/v2",
            "/openapi/v3",
            "/swagger.json",
            "/swagger",
            "/swagger/",
            "/swagger/apishim",
            "/swagger/apishim/",
            "/redoc",
            "/redoc/",
            "/redoc/apishim",
            "/redoc/apishim/",
            "/dashboard",
            "/dashboard/",
            "/dashboard.js",
            "/",
            "/docs",
        }:
            if path_only == "/openapi.json":
                self._handle_openapi()
            elif path_only in ("/openapi/v2", "/swagger.json"):
                self._handle_apishim_openapi("v2")
            elif path_only == "/openapi/v3":
                self._handle_apishim_openapi("v3")
            elif path_only in ("/", "/docs"):
                self._handle_docs()
            elif path_only in ("/swagger", "/swagger/"):
                self._handle_swagger()
            elif path_only in ("/swagger/apishim", "/swagger/apishim/"):
                self._handle_swagger(
                    spec_url="/openapi/v3",
                    title="k1s Swagger UI (API Shim)",
                )
            elif path_only in ("/dashboard", "/dashboard/"):
                if not self._dashboard_enabled():
                    self.send_response(404)
                    self.end_headers()
                    return
                self._handle_dashboard()
            elif path_only == "/dashboard.js":
                if not self._dashboard_enabled():
                    self.send_response(404)
                    self.end_headers()
                    return
                self._handle_dashboard_js()
            elif path_only in ("/redoc", "/redoc/"):
                self._handle_redoc()
            else:
                self._handle_redoc(
                    spec_url="/openapi/v3",
                    title="k1s ReDoc (API Shim)",
                )
            return
        # Enforce read auth if configured
        try:
            check = self._require_role  # type: ignore[attr-defined]
        except Exception:
            check = None
        if check and not self._require_role("read"):
            # Allow Labs token to satisfy read-only GETs (SSE and JSON) in dev
            if self._labs_token_valid():
                pass
            else:
                self._deny(401 if not self.headers.get("Authorization") else 403)
                return
        # From here on, read requires authorization when configured
        if path_only in ("/health", "/health/"):
            self._handle_health()
            return
        if path_only == "/tls/verify":
            self._handle_tls_verify()
            return
        if path_only in ("/system", "/system/"):
            self._handle_system()
            return
        if path_only in ("/dashboard", "/dashboard/"):
            if not self._dashboard_enabled():
                self.send_response(404)
                self.end_headers()
                return
            self._handle_dashboard()
            return
        if path_only.startswith("/dashboard/partials/"):
            if not self._dashboard_enabled():
                self.send_response(404)
                self.end_headers()
                return
            # server-rendered fragments for HTMX-enhanced dashboard
            subpath = path_only[len("/dashboard/partials/") :]
            if subpath == "logs":
                self._handle_dashboard_partial_logs()
                return
            if subpath == "probe-history":
                self._handle_dashboard_partial_probe_history()
                return
        # Dashboard SSE alias for events (no labs gating)
        if path_only == "/dashboard/sse/events":
            if not self._dashboard_enabled():
                self.send_response(404)
                self.end_headers()
                return
            self._handle_labs_sse_events()
            return
        if path_only == "/dashboard.js":
            if not self._dashboard_enabled():
                self.send_response(404)
                self.end_headers()
                return
            self._handle_dashboard_js()
            return
        if path_only.startswith("/manifest/"):
            # Return the latest stored manifest for the app
            app = self.path.split("/", 2)[2].split("?", 1)[0]
            try:
                # Enforce read scope if configured
                import os as _os

                if _os.getenv("AE_API_READ_SCOPE") and not self._scope_allows("read", app):
                    self._deny(403)
                    return
                self._handle_manifest_single(app)
            except Exception:
                self._json_error(404, "manifest not found")
            return
        if path_only == "/status" or path_only == "/status/":
            self._handle_status_list()
            return
        if path_only in ("/nodes", "/nodes/"):
            self._handle_nodes()
            return
        if path_only.startswith("/status/"):
            self._handle_status_single(self._decode_app_segment("/status/"))
            return
        if path_only.startswith("/events/"):
            app = self.path.split("/", 2)[2].split("?", 1)[0]
            import os as _os

            if _os.getenv("AE_API_READ_SCOPE") and not self._scope_allows("read", app):
                self._deny(403)
                return
            self._handle_events(self.path.split("/", 2)[2])
            return
        if path_only.startswith("/history/"):
            app_and_q = self.path.split("/", 2)[2]
            app = app_and_q.split("?", 1)[0]
            try:
                import urllib.parse as _up

                q = app_and_q.split("?", 1)[1] if "?" in app_and_q else ""
                params = _up.parse_qs(q)
                limit = int((params.get("limit", ["20"])[0] or "20"))
            except Exception:
                limit = 20
            try:
                hist = self.store.get_probe_history(app, limit)
                out = [
                    {
                        "pod_name": h.pod_name,
                        "replica_id": h.pod_name,
                        "check_time": h.check_time.isoformat(),
                        "ready": bool(h.ready),
                        "live": bool(h.live),
                        "readiness_message": h.readiness_message,
                        "liveness_message": h.liveness_message,
                    }
                    for h in hist
                ]
                self._json_ok(out)
            except Exception as exc:
                self._json_error(500, str(exc))
            return
        if path_only.startswith("/logs/"):
            # SSE streaming: /logs/<app>/stream
            if path_only.endswith("/stream"):
                parts = path_only.split("/")
                if len(parts) >= 4:
                    import os as _os

                    if _os.getenv("AE_API_READ_SCOPE") and not self._scope_allows("read", parts[2]):
                        self._deny(403)
                        return
                    self._handle_logs_stream(parts[2])
                else:
                    self.send_response(400)
                    self.end_headers()
                return
            app = self.path.split("/", 2)[2].split("?", 1)[0]
            import os as _os

            if _os.getenv("AE_API_READ_SCOPE") and not self._scope_allows("read", app):
                self._deny(403)
                return
            self._handle_logs(self.path.split("/", 2)[2])
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # type: ignore[override]
        # Read-only planner (not gated by AE_API_MUTATIONS)
        if self.path in {"/plan", "/dashboard/plan"}:
            try:
                # Enforce read auth if configured
                try:
                    check = self._require_role  # type: ignore[attr-defined]
                except Exception:
                    check = None
                if check and not self._require_role("read"):
                    self._deny(401 if not self.headers.get("Authorization") else 403)
                    return
                if self.plan_fn is None:
                    self._json_error(404, "plan endpoint not available")
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8")) if body else {}
                out = self.plan_fn(payload)  # type: ignore[misc]
                self._json_ok(out)
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return
        # Dev-only exporter preview: render K8s YAML for a posted manifest
        if self.path == "/k8s/preview":
            try:
                import os as _os

                if _os.getenv("AE_API_DEV_EXPORT") != "1":
                    self._json_error(403, "disabled: set AE_API_DEV_EXPORT=1 to enable k8s preview")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b"{}"
                import json as _json

                payload = _json.loads(raw.decode("utf-8")) if raw else {}
                # Build manifest model
                from ae.controller.spec import AppManifest as _AppManifest

                man = _AppManifest.model_validate(payload)
                # Options (optional) under payload["options"]
                from ae.k8s.exporter import ExportOptions as _Opts
                from ae.k8s.exporter import export_k8s_yaml as _export

                opts_payload = payload.get("options") or {}
                opts = _Opts(**opts_payload) if isinstance(opts_payload, dict) else _Opts()
                yaml_text = _export(man, options=opts)
                self._json_ok({"yaml": yaml_text})
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return
        if self.path == "/api/apishim/session":
            try:
                secret = os.getenv("AE_APISHIM_SESSION_SECRET", "").strip()
                if not secret:
                    self._json_error(404, "apishim session tokens disabled")
                    return
                labs_token = (os.getenv("AE_LABS_TOKEN") or "").strip()
                tokens_configured = bool(
                    os.getenv("AE_API_ADMIN_TOKEN")
                    or os.getenv("AE_API_SCALER_TOKEN")
                    or os.getenv("AE_API_READ_TOKEN")
                )
                if labs_token:
                    if not (self._labs_token_valid() or self._require_role("admin")):
                        self._deny(401 if not self.headers.get("Authorization") else 403)
                        return
                elif tokens_configured:
                    if not self._require_role("admin"):
                        self._deny(401 if not self.headers.get("Authorization") else 403)
                        return
                else:
                    # Secure-by-default: require auth even if no API tokens were configured.
                    self._deny(401 if not self.headers.get("Authorization") else 403)
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    self._json_error(400, "invalid JSON body for session token")
                    return
                role = str(payload.get("role") or "exec").strip().lower()
                if role not in {"exec", "portforward", "read"}:
                    self._json_error(400, "invalid role for session token")
                    return
                scopes_val = payload.get("scopes") or payload.get("scope") or []
                scopes: list[str] = []
                if isinstance(scopes_val, str):
                    scopes = [scopes_val]
                elif isinstance(scopes_val, list):
                    scopes = [str(s) for s in scopes_val if s]
                else:
                    scopes = []
                try:
                    ttl_req = int(payload.get("ttlSeconds") or payload.get("ttl") or 0)
                except Exception:
                    ttl_req = 0
                try:
                    default_ttl = int(os.getenv("AE_APISHIM_SESSION_TTL", "600") or "600")
                except Exception:
                    default_ttl = 600
                try:
                    max_ttl = int(
                        os.getenv("AE_APISHIM_SESSION_TTL_MAX", str(default_ttl)) or default_ttl
                    )
                except Exception:
                    max_ttl = default_ttl
                ttl = ttl_req if ttl_req > 0 else default_ttl
                ttl = max(60, min(ttl, max_ttl))
                exp = int(time.time()) + ttl
                token_payload = {"role": role, "exp": exp}
                if scopes:
                    token_payload["scopes"] = scopes
                payload_raw = json.dumps(token_payload, separators=(",", ":")).encode("utf-8")

                def _b64url(data: bytes) -> str:
                    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

                payload_b64 = _b64url(payload_raw)
                sig = hmac.new(
                    secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
                ).digest()
                sig_b64 = _b64url(sig)
                token = f"sess1.{payload_b64}.{sig_b64}"
                self._json_ok(
                    {
                        "token": token,
                        "expires_in": ttl,
                        "expires_at": exp,
                        "role": role,
                        "scopes": scopes,
                    }
                )
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return
        # Labs playground micro-API (dev only)
        if self.path.startswith("/labs/") or self.path == "/labs/info":
            if not self._playground_enabled():
                self._json_error(404, "playground disabled")
                return
            self._handle_labs_post()
            return
        # Mutations are optional and gated by env
        if os.getenv("AE_API_MUTATIONS") != "1":
            self.send_response(404)
            self.end_headers()
            return

        # Role checks (if configured)
        try:
            check = self._require_role  # type: ignore[attr-defined]
        except Exception:
            check = None
        if check:
            if self.path == "/apply" and not self._require_role("admin"):
                self._deny(401 if not self.headers.get("Authorization") else 403)
                return
            if self.path.startswith("/exec/") and not self._require_role("admin"):
                self._deny(401 if not self.headers.get("Authorization") else 403)
                return
            if self.path.startswith("/scale/") and not self._require_role("scale"):
                self._deny(401 if not self.headers.get("Authorization") else 403)
                return
            if self.path.startswith("/delete/") and not self._require_role("admin"):
                self._deny(401 if not self.headers.get("Authorization") else 403)
                return
            if (
                self.path.startswith("/rollout/pause/") or self.path.startswith("/rollout/resume/")
            ) and not self._require_role("admin"):
                self._deny(401 if not self.headers.get("Authorization") else 403)
                return

        if self.path == "/apply" and self.apply_fn is not None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._json_error(400, "invalid JSON body: expected Deployment manifest")
                return
            try:
                # Scope enforcement: admin token must be allowed for target app
                role = self._presented_role()
                from ae.controller.spec import app_key

                meta = payload.get("metadata", {}) if isinstance(payload, dict) else {}
                app = app_key(str(meta.get("name") or ""), meta.get("namespace"))
                if not app:
                    self._json_error(400, "manifest missing metadata.name for scope check")
                    return
                if (
                    role != "admin"
                    or not self._scope_allows("admin", app)
                    or not self._rbac_allows("create", app)
                ):
                    self._json_error(403, "token scope denies apply to target app")
                    return
                report = self._call_apply(payload, source="api")
                self._json_ok(report)
            except NotLeaderError as exc:
                self._json_error_obj(409, exc.as_payload())
            except RegistryConflictError as exc:
                self._json_error_obj(
                    409,
                    {
                        "error": "resource_version_conflict",
                        "app": exc.app_name,
                        "expected": exc.expected,
                        "actual": exc.actual,
                    },
                )
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return

        if self.path.startswith("/scale/") and self.scale_fn is not None:
            app = self.path.split("/", 2)[2]
            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                replicas = int(payload.get("replicas"))
            except Exception:
                self._json_error(400, "invalid JSON body; expected { 'replicas': <int> }")
                return
            try:
                role = self._presented_role()
                # Only enforce scope for presented role if it's scale/admin
                if role not in {"admin", "scale"} or not self._scope_allows(role, app):
                    self._json_error(403, "token scope denies scale for target app")
                    return
                if not self._rbac_allows("update", app):
                    self._json_error(403, "rbac denies scale for target app")
                    return
                report = self.scale_fn(app, replicas)  # type: ignore[misc]
                self._json_ok(report)
            except NotLeaderError as exc:
                self._json_error_obj(409, exc.as_payload())
            except RegistryConflictError as exc:
                self._json_error_obj(
                    409,
                    {
                        "error": "resource_version_conflict",
                        "app": exc.app_name,
                        "expected": exc.expected,
                        "actual": exc.actual,
                    },
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._json_error(500, str(exc))
            return

        if self.path.startswith("/delete/") and self.delete_fn is not None:
            # optional ?purge=1
            frag = self.path.split("/", 2)[2]
            app, _, query = frag.partition("?")
            purge = False
            if query:
                for part in query.split("&"):
                    if part.startswith("purge="):
                        purge = part.split("=", 1)[1] in {"1", "true", "True"}
                        break
            try:
                role = self._presented_role()
                if role != "admin" or not self._scope_allows("admin", app):
                    self._json_error(403, "token scope denies delete for target app")
                    return
                if not self._rbac_allows("delete", app):
                    self._json_error(403, "rbac denies delete for target app")
                    return
                result = self.delete_fn(app, purge)  # type: ignore[misc]
                self._json_ok(result)
            except NotLeaderError as exc:
                self._json_error_obj(409, exc.as_payload())
            except RegistryConflictError as exc:
                self._json_error_obj(
                    409,
                    {
                        "error": "resource_version_conflict",
                        "app": exc.app_name,
                        "expected": exc.expected,
                        "actual": exc.actual,
                    },
                )
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return

        if self.path.startswith("/rollout/pause/") and self.rollout_pause_fn is not None:
            app = self.path.split("/", 3)[3] if self.path.count("/") >= 3 else ""
            if not app:
                self._json_error(400, "missing app name in path")
                return
            try:
                if not self._rbac_allows("update", app):
                    self._json_error(403, "rbac denies rollout pause")
                    return
                out = self.rollout_pause_fn(app)  # type: ignore[misc]
                self._json_ok(out)
            except NotLeaderError as exc:
                self._json_error_obj(409, exc.as_payload())
            except RegistryConflictError as exc:
                self._json_error_obj(
                    409,
                    {
                        "error": "resource_version_conflict",
                        "app": exc.app_name,
                        "expected": exc.expected,
                        "actual": exc.actual,
                    },
                )
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return

        if self.path.startswith("/rollout/resume/") and self.rollout_resume_fn is not None:
            app = self.path.split("/", 3)[3] if self.path.count("/") >= 3 else ""
            if not app:
                self._json_error(400, "missing app name in path")
                return
            try:
                if not self._rbac_allows("update", app):
                    self._json_error(403, "rbac denies rollout resume")
                    return
                out = self.rollout_resume_fn(app)  # type: ignore[misc]
                self._json_ok(out)
            except NotLeaderError as exc:
                self._json_error_obj(409, exc.as_payload())
            except RegistryConflictError as exc:
                self._json_error_obj(
                    409,
                    {
                        "error": "resource_version_conflict",
                        "app": exc.app_name,
                        "expected": exc.expected,
                        "actual": exc.actual,
                    },
                )
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return

        self.send_response(404)
        self.end_headers()

    # --- Labs playground micro-API (dev only, opt-in) ------------------
    def _handle_labs_post(self) -> None:
        import os as _os
        import secrets as _secrets

        if not self._labs_enabled():
            self._json_error(404, "labs disabled")
            return
        path = self.path.split("?", 1)[0]
        # Always allow info without auth
        if path == "/labs/info":
            backends = ["k1s-host"]
            try:
                import shutil as _sh

                if _os.getenv("AE_LABS_DOCKER") == "1":
                    backends.append("k1s-docker")
                # Detect k3d automatically when present on PATH or explicit opt-in
                k3d_present = bool(_sh.which("k3d")) or _os.getenv("AE_LABS_K3S") == "1"
            except Exception:
                k3d_present = _os.getenv("AE_LABS_K3S") == "1"
            if k3d_present:
                backends.append("k3s")
            http_port = int(_os.getenv("K3D_HTTP", "8081") or 8081)
            https_port = int(_os.getenv("K3D_HTTPS", "8444") or 8444)
            self._json_ok(
                {
                    "backends": backends,
                    "api_base": "",
                    "k3d": {
                        "present": k3d_present,
                        "ports": {"http": http_port, "https": https_port},
                    },
                }
            )
            return
        # Optional bearer token for labs
        tok = self.headers.get("Authorization", "")
        labs_token = _os.getenv("AE_LABS_TOKEN") or ""
        if labs_token and tok != f"Bearer {labs_token}":
            self._deny(401 if not tok else 403)
            return
        # Parse JSON body if present
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}
        # Session create
        if path == "/labs/session":
            sid = _secrets.token_hex(3)
            backend = str(payload.get("backend") or "k1s-host")
            # Optional auto-provision of k3d when requested
            if backend == "k3s" and _os.getenv("AE_LABS_K3D_AUTOCREATE") == "1":
                try:
                    import subprocess as _sp

                    name = _os.getenv("K3D_NAME", "k1s-labs")
                    http_port = _os.getenv("K3D_HTTP", "8081")
                    https_port = _os.getenv("K3D_HTTPS", "8444")
                    _sp.run(
                        [
                            "bash",
                            "scripts/lab_k3d.sh",
                            "ensure",
                            "--name",
                            name,
                            "--http",
                            str(http_port),
                            "--https",
                            str(https_port),
                        ],
                        check=False,
                    )
                except Exception:
                    pass
            self._json_ok({"session_id": sid, "backend": backend, "token": labs_token or None})
            return
        if path == "/labs/helm-demo":
            action = str(payload.get("action") or "status").lower()
            try:
                if action == "start":
                    self._json_ok(_helm_demo_start())
                elif action == "stop":
                    self._json_ok(_helm_demo_stop())
                else:
                    self._json_ok(_helm_demo_status())
            except Exception as exc:  # pragma: no cover - defensive
                self._json_error(500, str(exc))
            return
        # k3d ensure (auto-provision cluster)
        if path == "/labs/k3d/ensure":
            try:
                import subprocess as _sp

                name = _os.getenv("K3D_NAME", "k1s-labs")
                http_port = _os.getenv("K3D_HTTP", "8081")
                https_port = _os.getenv("K3D_HTTPS", "8444")
                _sp.run(
                    [
                        "bash",
                        "scripts/lab_k3d.sh",
                        "ensure",
                        "--name",
                        name,
                        "--http",
                        str(http_port),
                        "--https",
                        str(https_port),
                    ],
                    check=False,
                )
                self._json_ok(
                    {
                        "ok": True,
                        "name": name,
                        "ports": {"http": int(http_port), "https": int(https_port)},
                    }
                )
            except Exception as exc:
                self._json_error(500, str(exc))
            return
        # Exec: POST /exec/<app> { container?, cmd: [..], timeoutSeconds? }
        if self.path.startswith("/exec/") and getattr(self, "exec_fn", None) is not None:
            app = self.path.split("/", 2)[2]
            # Scope enforcement for admin commands
            import os as _os

            if _os.getenv("AE_API_ADMIN_SCOPE") and not self._scope_allows("admin", app):
                self._deny(403)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                payload = {}
            container = payload.get("container")
            cmd = payload.get("cmd") or payload.get("command") or []
            timeout = payload.get("timeoutSeconds") or payload.get("timeout")
            if not isinstance(cmd, list) or not cmd:
                self._json_error(400, "cmd must be a non-empty list")
                return
            try:
                rc = int(
                    self.exec_fn(
                        app,
                        container,
                        [str(x) for x in cmd],
                        int(timeout) if timeout is not None else None,
                    )
                )  # type: ignore[misc]
            except Exception as exc:
                self._json_error(500, f"exec failed: {exc}")
                return
            self._json_ok({"app": app, "container": container, "rc": rc})
            return

        # Planner passthrough (labs-safe alias for /plan)
        if path == "/labs/plan":
            if self.plan_fn is None:
                self._json_error(404, "plan not available")
                return
            try:
                out = self.plan_fn(payload)  # type: ignore[misc]
                self._json_ok(out)
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return

        # Apply curated example (labs-only)
        if path == "/labs/apply":
            if self.apply_fn is None:
                self._json_error(404, "apply not available")
                return
            try:
                from pathlib import Path as _Path

                import yaml as _yaml

                sess = str(payload.get("session_id") or "")
                ex = str(payload.get("example") or "echo")
                # Map example id to file path
                ex_map = {
                    "shell-demo": _Path("specs/examples/shell-demo.yaml"),
                    "echo": _Path("specs/examples/echo.yaml"),
                    "echo-multiport": _Path("specs/examples/echo-multiport.yaml"),
                    "echo-rollout": _Path("specs/examples/echo-rollout.yaml"),
                    "echo-hpa": _Path("specs/examples/echo-hpa.yaml"),
                    "echo-resources": _Path("specs/examples/echo-resources.yaml"),
                    "echo-stateful": _Path("specs/examples/echo-stateful.yaml"),
                    "echo-sec": _Path("specs/examples/echo-sec.yaml"),
                    "echo-sec-adv": _Path("specs/examples/echo-sec-adv.yaml"),
                    "echo-tcp": _Path("specs/examples/echo-tcp.yaml"),
                    "echo-exec": _Path("specs/examples/echo-exec.yaml"),
                    "echo-storage": _Path("specs/examples/echo-storage.yaml"),
                    "echo-storage-delete": _Path("specs/examples/echo-storage-delete.yaml"),
                    "multi-replica-echo": _Path("specs/examples/multi-replica-echo.yaml"),
                    "blue": _Path("specs/examples/blue.yaml"),
                    "green": _Path("specs/examples/green.yaml"),
                }
                src = ex_map.get(ex)
                if src is None or not src.exists():
                    self._json_error(400, "unknown or missing example")
                    return
                data = _yaml.safe_load(src.read_text(encoding="utf-8")) or {}
                # Prefix app name with session id for isolation
                name = str(((data or {}).get("metadata") or {}).get("name") or "app")
                if sess:
                    new_name = f"{name}-{sess}"
                else:
                    new_name = name
                data.setdefault("metadata", {})["name"] = new_name
                try:
                    _labs_unblock_app(new_name)
                except Exception:
                    pass
                # If ingress host is a .home.arpa, avoid session-specific hosts by default
                # to prevent DNS failures in browsers (no wildcard in /etc/hosts). Keep
                # per-session hostnames only when AE_LABS_SESSION_HOSTS=1.
                try:
                    import os as _os

                    ing = (data.get("spec") or {}).get("ingress") or {}
                    host = str(ing.get("host") or "")
                    if host.endswith(".home.arpa"):
                        use_session_host = _os.getenv("AE_LABS_SESSION_HOSTS") == "1"
                        new_host = f"{new_name}.home.arpa" if use_session_host else host
                        data.setdefault("spec", {}).setdefault("ingress", {})["host"] = new_host
                except Exception:
                    pass

                # Inject a stable Service mapping for single-replica apps so readiness probes
                # target a deterministic localhost:PORT. Controlled by AE_LABS_STABLE_SERVICE (default on).
                try:
                    import os as _os

                    enable = _os.getenv("AE_LABS_STABLE_SERVICE")
                    stable_enabled = True if (enable is None or enable == "1") else False
                    spec = data.setdefault("spec", {})
                    has_service = bool(spec.get("service"))
                    replicas = int(spec.get("replicas", 1))
                    if stable_enabled and not has_service and replicas == 1:
                        # Determine readiness target port then choose a free host port
                        target = None
                        try:
                            h = (spec.get("health") or {}).get("readiness") or {}
                            if (
                                isinstance(h.get("httpGet"), dict)
                                and h["httpGet"].get("port") is not None
                            ):
                                target = int(h["httpGet"]["port"])  # type: ignore[call-arg]
                            elif (
                                isinstance(h.get("tcpSocket"), dict)
                                and h["tcpSocket"].get("port") is not None
                            ):
                                target = int(h["tcpSocket"]["port"])  # type: ignore[call-arg]
                        except Exception:
                            target = None
                        if target is None:
                            try:
                                ports = list(spec.get("ports") or [])
                                if ports:
                                    target = int(
                                        ports[0].get("containerPort")
                                        or ports[0].get("container_port")
                                    )
                            except Exception:
                                target = None
                        if target is not None:
                            base = 18080
                            span = 200
                            try:
                                seed = abs(hash(new_name)) % span
                            except Exception:
                                seed = 0
                            chosen = None
                            for off in range(span):
                                candidate = base + ((seed + off) % span)
                                ok = True
                                # Use planner if available to avoid container hostPort conflicts
                                try:
                                    if self.plan_fn is not None:
                                        probe = dict(data)
                                        ps = probe.setdefault("spec", {})
                                        ps["service"] = {
                                            "port": int(candidate),
                                            "targetPort": int(target),
                                        }
                                        report = self.plan_fn(probe)  # type: ignore[misc]
                                        diags = (report or {}).get("diagnostics") or {}
                                        conflicts = (diags.get("service") or {}).get(
                                            "hostPortConflicts"
                                        ) or {}
                                        ok = not bool(conflicts)
                                except Exception:
                                    ok = True
                                if ok:
                                    chosen = candidate
                                    break
                            if chosen is not None:
                                spec["service"] = {"port": int(chosen), "targetPort": int(target)}
                except Exception:
                    # Best-effort only; never fail labs apply on stable-port injection
                    pass
                # If a Service already exists in the example and the chosen host port is busy,
                # auto-shift to a free port to avoid Podman/Docker start failures.
                try:
                    spec = data.setdefault("spec", {})
                    svc = spec.get("service") or {}
                    replicas = int(spec.get("replicas", 1))
                    if (
                        self.plan_fn is not None
                        and svc
                        and replicas == 1
                        and svc.get("port") is not None
                    ):
                        # Check for conflicts
                        rep0 = self.plan_fn(data)  # type: ignore[misc]
                        diags0 = (rep0 or {}).get("diagnostics") or {}
                        conflicts0 = (diags0.get("service") or {}).get("hostPortConflicts") or {}
                        cur = int(svc.get("port"))
                        needs_shift = bool(conflicts0) and (
                            cur in set(int(p) for p in conflicts0.keys())
                        )
                        if needs_shift:
                            # Choose a free alternative in the dev range
                            target = None
                            try:
                                h = (spec.get("health") or {}).get("readiness") or {}
                                if (
                                    isinstance(h.get("httpGet"), dict)
                                    and h["httpGet"].get("port") is not None
                                ):
                                    target = int(h["httpGet"]["port"])  # type: ignore[call-arg]
                                elif (
                                    isinstance(h.get("tcpSocket"), dict)
                                    and h["tcpSocket"].get("port") is not None
                                ):
                                    target = int(h["tcpSocket"]["port"])  # type: ignore[call-arg]
                            except Exception:
                                target = None
                            if target is None:
                                try:
                                    ports = list(spec.get("ports") or [])
                                    if ports:
                                        target = int(
                                            ports[0].get("containerPort")
                                            or ports[0].get("container_port")
                                        )
                                except Exception:
                                    target = None
                            base = 18080
                            span = 200
                            try:
                                seed = (
                                    abs(
                                        hash(
                                            str(
                                                ((data or {}).get("metadata") or {}).get("name")
                                                or "app"
                                            )
                                        )
                                    )
                                    % span
                                )
                            except Exception:
                                seed = 0
                            for off in range(span):
                                candidate = base + ((seed + off) % span)
                                ok = True
                                try:
                                    probe = dict(data)
                                    ps = probe.setdefault("spec", {})
                                    ps["service"] = {
                                        "port": int(candidate),
                                        "targetPort": int(target or cur),
                                    }
                                    rpt = self.plan_fn(probe)  # type: ignore[misc]
                                    dg = (rpt or {}).get("diagnostics") or {}
                                    cf = (dg.get("service") or {}).get("hostPortConflicts") or {}
                                    ok = not bool(cf)
                                except Exception:
                                    ok = True
                                if ok:
                                    spec.setdefault("service", {})["port"] = int(candidate)
                                    if target is not None:
                                        spec["service"]["targetPort"] = int(target)
                                    break
                except Exception:
                    pass
                report = self._call_apply(data, source="labs")
                try:
                    _LABS_APPS.add(new_name)
                except Exception:
                    pass
                try:
                    if isinstance(report, dict):
                        payload_out = dict(report)
                    else:
                        payload_out = {"result": report}
                    payload_out.setdefault("app", new_name)
                    self._json_ok(payload_out)
                except Exception:
                    self._json_ok({"app": new_name, "result": report})
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return
        # Scale
        if path == "/labs/scale":
            if self.scale_fn is None:
                self._json_error(404, "scale not available")
                return
            try:
                app = str(payload.get("app") or "")
                replicas = int(payload.get("replicas"))
                if not app:
                    self._json_error(400, "missing app")
                    return
                report = self.scale_fn(app, replicas)  # type: ignore[misc]
                self._json_ok(report)
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return
        # Reset: delete known apps for the session
        if path == "/labs/reset":
            if self.delete_fn is None:
                self._json_error(404, "reset not available")
                return
            try:
                logger.info("labs reset requested")
                # Stop helm demo runner first so it cannot reapply while we clean up.
                try:
                    _helm_demo_stop()
                except Exception:
                    pass
                sess = str(payload.get("session_id") or "")
                # Prefer tracked labs apps that match the session suffix; fallback to echo-<sess>
                try:
                    suffix = f"-{sess}" if sess else ""
                    tracked = list(_LABS_APPS)
                except Exception:
                    suffix = f"-{sess}" if sess else ""
                    tracked = []
                candidates = [a for a in tracked if suffix and a.endswith(suffix)]
                # If no tracked apps matched, consult the store to find session-scoped apps
                if not candidates and suffix and getattr(self, "store", None) is not None:
                    try:
                        names = [s.app_name for s in self.store.list_status()]
                        candidates = [n for n in names if n.endswith(suffix)]
                    except Exception:
                        pass
                # Fallbacks: common example names
                if not candidates and sess:
                    candidates = [f"echo-{sess}"]
                # Final fallback: delete base echo if present (covers non-session applies)
                if not candidates and getattr(self, "store", None) is not None:
                    try:
                        names = [s.app_name for s in self.store.list_status()]
                        if "echo" in names:
                            candidates = ["echo"]
                    except Exception:
                        pass
                try:
                    for app in candidates:
                        _labs_block_app(app)
                except Exception:
                    pass
                removed_apps: list[dict[str, object]] = []
                helm_prefixes: set[str] = set()
                for app in candidates:
                    try:
                        res = self.delete_fn(app, True)  # type: ignore[misc]
                        removed_apps.append(res)
                        try:
                            _LABS_APPS.discard(app)
                        except Exception:
                            pass
                    except Exception:
                        continue
                # Also clean up any helm shim demo apps by namespace prefix.
                try:
                    prefixes: set[str] = set()
                    try:
                        prefixes.update(_LABS_APP_PREFIXES)
                    except Exception:
                        pass
                    ns = str(_HELM_DEMO_STATE.get("namespace") or "demo-helm")
                    if ns:
                        prefixes.add(f"{ns}--")
                    helm_prefixes = set(prefixes)
                    if prefixes:
                        names: set[str] = set()
                        try:
                            names.update([s.app_name for s in self.store.list_status()])
                        except Exception:
                            pass
                        try:
                            names.update(self.store.list_registered_app_names())
                        except Exception:
                            pass
                        helm_candidates = sorted(
                            {n for n in names if any(n.startswith(p) for p in prefixes)}
                        )
                        if helm_candidates:
                            logger.info(
                                "labs reset removing helm demo apps: %s", ", ".join(helm_candidates)
                            )
                        try:
                            for app in helm_candidates:
                                _labs_block_app(app)
                        except Exception:
                            pass
                        for app in helm_candidates:
                            try:
                                res = self.delete_fn(app, True)  # type: ignore[misc]
                                removed_apps.append(res)
                                try:
                                    _LABS_APPS.discard(app)
                                except Exception:
                                    pass
                            except Exception:
                                continue
                        try:
                            for prefix in prefixes:
                                _LABS_APP_PREFIXES.discard(prefix)
                        except Exception:
                            pass
                except Exception:
                    pass
                # Also remove shim objects in the helm demo namespace so the adapter
                # doesn't reapply them after reset. Prefer the shim API so deletes
                # publish watch events, but verify the namespace is actually empty
                # and fall back to direct store cleanup for any survivors.
                try:
                    ns = str(_HELM_DEMO_STATE.get("namespace") or "demo-helm")
                    if ns:
                        targets = [
                            ("", "v1", "services"),
                            ("", "v1", "serviceaccounts"),
                            ("", "v1", "secrets"),
                            ("", "v1", "configmaps"),
                            ("apps", "v1", "deployments"),
                            ("apps", "v1", "daemonsets"),
                            ("apps", "v1", "statefulsets"),
                            ("batch", "v1", "jobs"),
                            ("batch", "v1", "cronjobs"),
                            ("networking.k8s.io", "v1", "ingresses"),
                            ("autoscaling", "v2", "horizontalpodautoscalers"),
                        ]
                        env_file = _resolve_apishim_env_file()
                        shim_base = _resolve_apishim_server(
                            env_file=env_file,
                            default_port=int(_HELM_DEMO_STATE.get("port") or 8455),
                        )
                        token = _resolve_apishim_admin_token(env_file=env_file)
                        dsn, db_path = _resolve_apishim_store_config(env_file=env_file)
                        removed_via_api = 0
                        removed_via_store = 0
                        shim_reachable = False
                        survivor_names: list[str] = []
                        try:
                            import requests as _req

                            headers = {"Authorization": f"Bearer {token}"} if token else {}
                            verify = _resolve_apishim_verify()
                            if token and shim_base:
                                try:
                                    probe = _req.get(
                                        f"{shim_base}/version",
                                        headers=headers,
                                        timeout=2,
                                        verify=verify,
                                    )
                                    shim_reachable = probe.status_code < 400
                                except Exception:
                                    shim_reachable = False
                            else:
                                shim_reachable = False
                            if shim_reachable and token:
                                logger.info(
                                    "labs reset using shim API at %s for namespace %s",
                                    shim_base or "<unknown>",
                                    ns,
                                )
                                for grp, ver, res in targets:
                                    if grp:
                                        list_url = (
                                            f"{shim_base}/apis/{grp}/{ver}/namespaces/{ns}/{res}"
                                        )
                                    else:
                                        list_url = f"{shim_base}/api/{ver}/namespaces/{ns}/{res}"
                                    try:
                                        resp = _req.get(
                                            list_url, headers=headers, timeout=3, verify=verify
                                        )
                                        if resp.status_code >= 400:
                                            continue
                                        data = resp.json() if resp.content else {}
                                        items = data.get("items") if isinstance(data, dict) else []
                                        for item in items or []:
                                            meta = (
                                                item.get("metadata")
                                                if isinstance(item, dict)
                                                else None
                                            )
                                            name = (
                                                meta.get("name") if isinstance(meta, dict) else None
                                            )
                                            if not name:
                                                continue
                                            del_url = f"{list_url}/{name}"
                                            try:
                                                dresp = _req.delete(
                                                    del_url,
                                                    headers=headers,
                                                    timeout=3,
                                                    verify=verify,
                                                )
                                                if dresp.status_code < 300:
                                                    removed_via_api += 1
                                            except Exception:
                                                continue
                                    except Exception:
                                        continue
                        except Exception:
                            shim_reachable = False
                        if shim_reachable and removed_via_api:
                            logger.info(
                                "labs reset removed %s shim objects via shim API in namespace %s",
                                removed_via_api,
                                ns,
                            )
                        if shim_reachable and not removed_via_api:
                            logger.info(
                                "labs reset shim API reachable at %s; no shim objects removed for namespace %s",
                                shim_base or "<unknown>",
                                ns,
                            )
                        if not shim_reachable:
                            logger.info(
                                "labs reset shim API unavailable at %s; falling back to direct store cleanup for namespace %s",
                                shim_base or "<unknown>",
                                ns,
                            )
                        from ae.apishim.store import ObjectStore as _ObjectStore

                        store = (
                            _ObjectStore(dsn=dsn)
                            if dsn
                            else _ObjectStore(db_path=Path(db_path))
                        )
                        try:
                            survivors: list[tuple[str, str, str, str]] = []
                            for grp, ver, res in targets:
                                try:
                                    items = store.list(grp, ver, res, ns)
                                except Exception:
                                    continue
                                for obj in items:
                                    survivors.append((grp, ver, res, obj.name))
                            survivor_names = [
                                f"{_describe_apishim_target(grp, ver, res)}:{name}"
                                for grp, ver, res, name in survivors
                            ]
                            if survivor_names:
                                logger.info(
                                    "labs reset found surviving shim objects in namespace %s after API cleanup: %s",
                                    ns,
                                    ", ".join(sorted(survivor_names)),
                                )
                                for grp, ver, res, name in survivors:
                                    try:
                                        if store.delete(grp, ver, res, ns, name):
                                            removed_via_store += 1
                                    except Exception:
                                        continue
                                survivors = []
                                for grp, ver, res in targets:
                                    try:
                                        items = store.list(grp, ver, res, ns)
                                    except Exception:
                                        continue
                                    for obj in items:
                                        survivors.append((grp, ver, res, obj.name))
                                survivor_names = [
                                    f"{_describe_apishim_target(grp, ver, res)}:{name}"
                                    for grp, ver, res, name in survivors
                                ]
                        finally:
                            close = getattr(store, "close", None)
                            if callable(close):
                                try:
                                    close()
                                except Exception:
                                    pass
                        if removed_via_store:
                            logger.info(
                                "labs reset removed %s shim objects via direct store cleanup for namespace %s",
                                removed_via_store,
                                ns,
                            )
                        if survivor_names:
                            logger.warning(
                                "labs reset left shim survivors in namespace %s after fallback cleanup: %s",
                                ns,
                                ", ".join(sorted(survivor_names)),
                            )
                except Exception:
                    pass
                # Reset can race with the shim adapter queue: delete controller apps
                # again until no helm-demo mirrors remain after shim cleanup.
                try:
                    if helm_prefixes:
                        deadline = time.time() + 5.0
                        empty_polls = 0
                        while time.time() < deadline:
                            names: set[str] = set()
                            try:
                                names.update([s.app_name for s in self.store.list_status()])
                            except Exception:
                                pass
                            try:
                                names.update(self.store.list_registered_app_names())
                            except Exception:
                                pass
                            lingering = sorted(
                                {n for n in names if any(n.startswith(p) for p in helm_prefixes)}
                            )
                            if not lingering:
                                empty_polls += 1
                                if empty_polls >= 2:
                                    break
                                time.sleep(0.25)
                                continue
                            empty_polls = 0
                            logger.info(
                                "labs reset purging lingering helm demo apps after shim cleanup: %s",
                                ", ".join(lingering),
                            )
                            for app in lingering:
                                try:
                                    _labs_block_app(app)
                                except Exception:
                                    pass
                                try:
                                    res = self.delete_fn(app, True)  # type: ignore[misc]
                                    removed_apps.append(res)
                                    try:
                                        _LABS_APPS.discard(app)
                                    except Exception:
                                        pass
                                except Exception:
                                    continue
                            time.sleep(0.25)
                except Exception:
                    pass
                self._json_ok({"removed": removed_apps})
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return
        # Ingress reachability check (server-side to avoid browser TLS issues in dev)
        if path == "/labs/ingress_check":
            try:
                url = str(payload.get("url") or "").strip()
                if not url:
                    # Support host+path form
                    host = str(payload.get("host") or "").strip()
                    p = str(payload.get("path") or "/")
                    if not host:
                        self._json_error(400, "missing url or host")
                        return
                    scheme = "https"
                    url = f"{scheme}://{host}{p if p.startswith('/') else ('/' + p)}"
                # Guard: restrict to dev hostnames to avoid SSRF in labs
                try:
                    from urllib.parse import urlparse

                    u = urlparse(url)
                    host = (u.hostname or "").lower()
                    if not host.endswith(".home.arpa"):
                        self._json_error(400, "host not allowed")
                        return
                except Exception:
                    self._json_error(400, "bad url")
                    return
                import os as _os
                import time as _t
                from urllib.parse import urlparse as _urlparse

                import requests as _req

                if _os.getenv("AE_DISABLE_INGRESS") == "1":
                    self._json_ok(
                        {
                            "ok": False,
                            "code": 0,
                            "elapsed_ms": 0,
                            "disabled": True,
                            "reason": "ingress disabled via AE_DISABLE_INGRESS=1",
                        }
                    )
                    return

                verify_path = "state/certs/combined-dev-ca.pem"
                verify = verify_path if _os.path.exists(verify_path) else False
                t0 = _t.time()
                # For dev *.home.arpa, try known host gateways since this process
                # might be running inside a container where 127.0.0.1 is not the host.
                u = _urlparse(url)
                host = (u.hostname or "").lower()
                scheme = (u.scheme or "https").lower()
                candidates = [
                    "127.0.0.1",
                    "host.docker.internal",
                    "gateway.docker.internal",
                    "host.containers.internal",
                ]
                last_exc = None
                r = None  # type: ignore[assignment]
                # Prefer preserving SNI by resolving the hostname to candidate addresses
                # temporarily via socket.getaddrinfo monkeypatch.
                import socket as _sock

                orig_gai = _sock.getaddrinfo
                for addr in candidates:
                    try:

                        def _fake_getaddrinfo(h, p, family=0, type=0, proto=0, flags=0):  # type: ignore[override]
                            if h == host:
                                try:
                                    return orig_gai(addr, p, family, type, proto, flags)
                                except Exception:
                                    return orig_gai(h, p, family, type, proto, flags)
                            return orig_gai(h, p, family, type, proto, flags)

                        _sock.getaddrinfo = _fake_getaddrinfo  # type: ignore[assignment]
                        r = _req.get(url, timeout=3, verify=verify)
                        break
                    except Exception as exc:
                        last_exc = exc
                        r = None
                        continue
                    finally:
                        try:
                            _sock.getaddrinfo = orig_gai  # type: ignore[assignment]
                        except Exception:
                            pass
                if r is None:
                    # Fallback to direct URL if all overrides fail
                    try:
                        r = _req.get(url, timeout=3, verify=verify)
                    except Exception as exc2:  # pragma: no cover
                        dt = int(round((_t.time() - t0) * 1000))
                        self._json_ok(
                            {
                                "ok": False,
                                "code": 0,
                                "elapsed_ms": dt,
                                "error": str(exc2 or last_exc),
                            }
                        )
                        return
                dt = int(round((_t.time() - t0) * 1000))
                self._json_ok(
                    {
                        "ok": bool(200 <= r.status_code < 400),
                        "code": int(r.status_code),
                        "elapsed_ms": dt,
                    }
                )
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return
        # Rollout helpers (pause/resume/canary)
        if path == "/labs/rollout":
            action = str(payload.get("action") or "").lower()
            app = str(payload.get("app") or "")
            if action in {"pause", "resume"}:
                fn = self.rollout_pause_fn if action == "pause" else self.rollout_resume_fn
                if fn is None:
                    self._json_error(404, f"rollout {action} not available")
                    return
                try:
                    out = fn(app)  # type: ignore[misc]
                    self._json_ok(out)
                except Exception as exc:
                    self._json_error(500, str(exc))
                return
            if action == "canary":
                # Set canary weight on existing deployment manifest when possible; fallback to curated example
                if self.apply_fn is None:
                    self._json_error(404, "apply not available")
                    return
                try:
                    raw_weight = payload.get("weight", None)
                    try:
                        weight = int(raw_weight) if raw_weight is not None else 10
                    except Exception:
                        weight = 10
                    base_revision = None
                    if app:
                        # Try to fetch current manifest and patch rollout strategy/weight
                        try:
                            revs = self.store.list_revisions(app, limit=1)  # type: ignore[attr-defined]
                        except Exception:
                            revs = []
                        if revs:
                            base_revision = revs[0].revision
                            man = self.store.get_revision_manifest(app, revs[0].revision)  # type: ignore[attr-defined]
                            data = man.model_dump(by_alias=True)
                            spec = data.setdefault("spec", {})
                            try:
                                cur_rep = int(spec.get("replicas", 1) or 1)
                            except Exception:
                                cur_rep = 1
                            if weight > 0 and cur_rep < 2:
                                spec["replicas"] = 2
                            rollout = dict(spec.get("rollout") or {})
                            if weight <= 0:
                                if str(rollout.get("strategy", "")).lower() == "canary":
                                    rollout.pop("strategy", None)
                                rollout.pop("weight", None)
                                rollout.pop("auto", None)
                                if rollout:
                                    spec["rollout"] = rollout
                                else:
                                    spec.pop("rollout", None)
                            else:
                                rollout["strategy"] = "canary"
                                rollout["weight"] = int(weight)
                                spec["rollout"] = rollout
                            try:
                                import time as _t

                                meta = data.setdefault("metadata", {})
                                anns = dict(meta.get("annotations") or {})
                                anns["labs.k1s.dev/canary-stamp"] = str(int(_t.time()))
                                meta["annotations"] = anns
                            except Exception:
                                pass
                            rep = self._call_apply(data, source="labs")
                            if isinstance(rep, dict):
                                if base_revision is not None:
                                    rep["base_revision"] = int(base_revision)
                                rep["canary_weight"] = int(weight)
                            self._json_ok(rep)
                            return
                    # Fallback to curated example
                    from pathlib import Path as _Path

                    import yaml as _yaml

                    sess = str(payload.get("session_id") or "")
                    src = _Path("specs/examples/echo-rollout.yaml")
                    if not src.exists():
                        self._json_error(404, "echo-rollout.yaml not found")
                        return
                    data = _yaml.safe_load(src.read_text(encoding="utf-8")) or {}
                    old = str(((data or {}).get("metadata") or {}).get("name") or "echo")
                    new_name = f"{old}-{sess}" if sess else old
                    data.setdefault("metadata", {})["name"] = new_name
                    spec = data.setdefault("spec", {})
                    ro = dict(spec.get("rollout") or {})
                    if weight <= 0:
                        if str(ro.get("strategy", "")).lower() == "canary":
                            ro.pop("strategy", None)
                        ro.pop("weight", None)
                        ro.pop("auto", None)
                        if ro:
                            spec["rollout"] = ro
                        else:
                            spec.pop("rollout", None)
                    else:
                        ro["strategy"] = "canary"
                        ro["weight"] = int(weight)
                        spec["rollout"] = ro
                    try:
                        cur_rep = int(spec.get("replicas", 1) or 1)
                    except Exception:
                        cur_rep = 1
                    if weight > 0 and cur_rep < 2:
                        spec["replicas"] = 2
                    try:
                        import time as _t

                        meta = data.setdefault("metadata", {})
                        anns = dict(meta.get("annotations") or {})
                        anns["labs.k1s.dev/canary-stamp"] = str(int(_t.time()))
                        meta["annotations"] = anns
                    except Exception:
                        pass
                    rep = self._call_apply(data, source="labs")
                    if isinstance(rep, dict):
                        if base_revision is not None:
                            rep["base_revision"] = int(base_revision)
                        rep["canary_weight"] = int(weight)
                    self._json_ok(rep)
                except Exception as exc:
                    self._json_error(500, str(exc))
                return
            self._json_error(400, "unknown rollout action")
            return

        # Unknown labs path
        self._json_error(404, "unknown labs endpoint")

    def _handle_labs_sse_events(self) -> None:
        import json as _json
        import time as _t
        import urllib.parse as _up

        _path, _, query = self.path.partition("?")
        params = _up.parse_qs(query)
        app = str((params.get("app", [""])[0] or "").strip())
        try:
            limit = int(params.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        if not app:
            self._json_error(400, "missing app")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_serialized = ""
        heartbeat = 10.0
        last_ping = _t.monotonic()
        try:
            # Back off reconnects to reduce proxy churn/noise on transient disconnects.
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                events = self.store.list_events(app, limit=limit)
                data = [
                    {
                        "app_name": e.app_name,
                        "revision": e.revision,
                        "event_type": e.event_type,
                        "message": e.message,
                        "created_at": e.created_at.isoformat(),
                    }
                    for e in events
                ]
                s = _json.dumps(data)
                now = _t.monotonic()
                if s != last_serialized:
                    last_serialized = s
                    self.wfile.write(("data: " + s + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    last_ping = now
                elif now - last_ping >= heartbeat:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
                _t.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.wfile.write(b"event: error\ndata: stream closed\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def _handle_labs_sse_status(self) -> None:
        import json as _json
        import time as _t
        import urllib.parse as _up

        _path, _, query = self.path.partition("?")
        params = _up.parse_qs(query)
        app = str((params.get("app", [""])[0] or "").strip())
        if not app:
            self._json_error(400, "missing app")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_serialized = ""
        heartbeat = 10.0
        last_ping = _t.monotonic()
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                s = self.store.get_status(app)
                obj = None
                if s is not None:
                    obj = {
                        "app_name": s.app_name,
                        "desired": s.desired_replicas,
                        "ready": s.ready_replicas,
                        "live": s.live_replicas,
                        "revision": s.revision,
                        "status": s.revision_status,
                        "current_revision_ready_replicas": s.current_revision_ready_replicas,
                        "current_revision_live_replicas": s.current_revision_live_replicas,
                        "old_revision_ready_replicas": s.old_revision_ready_replicas,
                        "old_revision_live_replicas": s.old_revision_live_replicas,
                        "overlap_ready_replicas": s.overlap_ready_replicas,
                        "overlap_live_replicas": s.overlap_live_replicas,
                        "ingress_host": s.ingress_host,
                        "ingress_path": s.ingress_path,
                    }
                sval = _json.dumps(obj)
                now = _t.monotonic()
                if sval != last_serialized:
                    last_serialized = sval
                    self.wfile.write(("data: " + sval + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    last_ping = now
                elif now - last_ping >= heartbeat:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
                _t.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.wfile.write(b"event: error\ndata: stream closed\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def _handle_labs_sse_events_html(self) -> None:
        import json as _json
        import time as _t
        import urllib.parse as _up

        _path, _, query = self.path.partition("?")
        params = _up.parse_qs(query)
        app = str((params.get("app", [""])[0] or "").strip())
        try:
            limit = int(params.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        if not app:
            self._json_error(400, "missing app")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_serialized = ""
        heartbeat = 10.0
        last_ping = _t.monotonic()
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                events = self.store.list_events(app, limit=limit)
                data = [
                    {
                        "app_name": e.app_name,
                        "revision": e.revision,
                        "event_type": e.event_type,
                        "message": e.message,
                        "created_at": e.created_at.isoformat(),
                    }
                    for e in events
                ]
                s = _json.dumps(data)
                now = _t.monotonic()
                if s != last_serialized:
                    last_serialized = s
                    # Emit full HTML snapshot oldest-first so new events appear at the bottom
                    html = "".join(
                        f"<div class='log-entry'><code>{self._escape_html(d['created_at'])}</code> {self._escape_html(d['message'])}</div>"
                        for d in reversed(data)
                    )
                    self.wfile.write(("data: " + html + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    last_ping = now
                elif now - last_ping >= heartbeat:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
                _t.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.wfile.write(b"event: error\ndata: stream closed\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def _handle_labs_sse_status_badge(self) -> None:
        import time as _t
        import urllib.parse as _up

        _path, _, query = self.path.partition("?")
        params = _up.parse_qs(query)
        app = str((params.get("app", [""])[0] or "").strip())
        if not app:
            self._json_error(400, "missing app")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_html = ""
        heartbeat = 10.0
        last_ping = _t.monotonic()
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                s = self.store.get_status(app)
                if s is None:
                    html = "<span class='pending'>n/a</span>"
                else:
                    ok = int(s.ready_replicas) == int(s.desired_replicas)
                    klass = "ok" if ok else "fail"
                    html = f"<span class='{klass}'>{int(s.ready_replicas)}/{int(s.desired_replicas)} ready</span>"
                now = _t.monotonic()
                if html != last_html:
                    last_html = html
                    self.wfile.write(("data: " + html + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    last_ping = now
                elif now - last_ping >= heartbeat:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
                _t.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.wfile.write(b"event: error\ndata: stream closed\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def _handle_metrics(self) -> None:
        snap = self.metrics.snapshot()
        build = AE_BUILD_INFO()
        version = _prom_escape_label_value(str(build.get("version") or "unknown"))
        sha = _prom_escape_label_value(str(build.get("sha") or "unknown"))
        date = _prom_escape_label_value(str(build.get("date") or "unknown"))
        lines = [
            "# HELP ae_apps_total Total apps recorded",
            "# TYPE ae_apps_total gauge",
            f"ae_apps_total {snap.total_apps}",
            "# HELP ae_apps_ready Ready apps",
            "# TYPE ae_apps_ready gauge",
            f"ae_apps_ready {snap.ready_apps}",
            "# HELP ae_apps_progressing Progressing apps",
            "# TYPE ae_apps_progressing gauge",
            f"ae_apps_progressing {snap.progressing_apps}",
            "# HELP ae_apps_degraded Degraded apps",
            "# TYPE ae_apps_degraded gauge",
            f"ae_apps_degraded {snap.degraded_apps}",
            "# HELP ae_replicas_total Total replicas desired",
            "# TYPE ae_replicas_total gauge",
            f"ae_replicas_total {snap.total_replicas}",
            "# HELP ae_replicas_ready Ready replicas",
            "# TYPE ae_replicas_ready gauge",
            f"ae_replicas_ready {snap.ready_replicas}",
            "# HELP ae_replicas_live Live replicas",
            "# TYPE ae_replicas_live gauge",
            f"ae_replicas_live {snap.live_replicas}",
            "# HELP ae_nodes_total Total registered nodes",
            "# TYPE ae_nodes_total gauge",
            f"ae_nodes_total {getattr(snap, 'total_nodes', 0)}",
            "# HELP ae_nodes_ready Ready nodes (within staleness window)",
            "# TYPE ae_nodes_ready gauge",
            f"ae_nodes_ready {getattr(snap, 'ready_nodes', 0)}",
            "# HELP ae_nodes_stale Nodes missing heartbeats or not Ready",
            "# TYPE ae_nodes_stale gauge",
            f"ae_nodes_stale {getattr(snap, 'stale_nodes', 0)}",
            "# HELP ae_services_total Services with allocated cluster IPs",
            "# TYPE ae_services_total gauge",
            f"ae_services_total {getattr(snap, 'total_services', 0)}",
            "# HELP ae_edge_ingress_routes_total Edge ingress routes",
            "# TYPE ae_edge_ingress_routes_total gauge",
            f"ae_edge_ingress_routes_total {getattr(snap, 'edge_routes_total', 0)}",
            "# HELP ae_edge_ingress_routes_valid Edge ingress routes with valid status",
            "# TYPE ae_edge_ingress_routes_valid gauge",
            f"ae_edge_ingress_routes_valid {getattr(snap, 'edge_routes_valid', 0)}",
            "# HELP ae_edge_ingress_routes_invalid Edge ingress routes with invalid status",
            "# TYPE ae_edge_ingress_routes_invalid gauge",
            f"ae_edge_ingress_routes_invalid {getattr(snap, 'edge_routes_invalid', 0)}",
            "# HELP ae_edge_ingress_routes_policy_unsupported Edge ingress routes with unsupported policy fields",
            "# TYPE ae_edge_ingress_routes_policy_unsupported gauge",
            f"ae_edge_ingress_routes_policy_unsupported {getattr(snap, 'edge_routes_policy_unsupported', 0)}",
            "# HELP ae_edge_ingress_policies_total Edge ingress policies",
            "# TYPE ae_edge_ingress_policies_total gauge",
            f"ae_edge_ingress_policies_total {getattr(snap, 'edge_policies_total', 0)}",
            "# HELP ae_pvs_total HostPath-backed PVs tracked for health",
            "# TYPE ae_pvs_total gauge",
            f"ae_pvs_total {getattr(snap, 'total_pvs', 0)}",
            "# HELP ae_pvs_healthy HostPath-backed PVs with healthy backing paths",
            "# TYPE ae_pvs_healthy gauge",
            f"ae_pvs_healthy {getattr(snap, 'healthy_pvs', 0)}",
            "# HELP ae_pvs_unhealthy HostPath-backed PVs with missing backing paths",
            "# TYPE ae_pvs_unhealthy gauge",
            f"ae_pvs_unhealthy {getattr(snap, 'unhealthy_pvs', 0)}",
            "# HELP ae_overlay_configured Overlay/VIP dataplane enabled (1=yes)",
            "# TYPE ae_overlay_configured gauge",
            f"ae_overlay_configured {1 if os.getenv('AE_SERVICE_PROVIDER', '').lower() == 'overlay' and os.getenv('AE_ENABLE_SERVICE_PROXY', '0') == '1' else 0}",
            "# HELP ae_controller_build_info Controller build metadata",
            "# TYPE ae_controller_build_info gauge",
            f'ae_controller_build_info{{version="{version}",sha="{sha}",date="{date}"}} 1',
        ]
        storage_used = getattr(snap, "storage_used_bytes", {}) or {}
        storage_quota = getattr(snap, "storage_quota_bytes", {}) or {}
        if storage_used or storage_quota:
            lines += [
                "# HELP ae_storage_used_bytes Requested storage per namespace",
                "# TYPE ae_storage_used_bytes gauge",
            ]
            for ns, val in sorted(storage_used.items()):
                lines.append(f'ae_storage_used_bytes{{namespace="{ns}"}} {val}')
            lines += [
                "# HELP ae_storage_quota_bytes Storage quota per namespace",
                "# TYPE ae_storage_quota_bytes gauge",
            ]
            for ns, val in sorted(storage_quota.items()):
                lines.append(f'ae_storage_quota_bytes{{namespace="{ns}"}} {val}')
        # Per-app series metadata (declared once before samples)
        lines += [
            "# HELP ae_app_desired_replicas Desired replicas per app",
            "# TYPE ae_app_desired_replicas gauge",
            "# HELP ae_app_ready_replicas Ready replicas per app",
            "# TYPE ae_app_ready_replicas gauge",
            "# HELP ae_app_live_replicas Live replicas per app",
            "# TYPE ae_app_live_replicas gauge",
            "# HELP ae_app_current_revision_ready_replicas Ready replicas for the current revision",
            "# TYPE ae_app_current_revision_ready_replicas gauge",
            "# HELP ae_app_current_revision_live_replicas Live replicas for the current revision",
            "# TYPE ae_app_current_revision_live_replicas gauge",
            "# HELP ae_app_old_revision_ready_replicas Ready replicas for older revisions",
            "# TYPE ae_app_old_revision_ready_replicas gauge",
            "# HELP ae_app_old_revision_live_replicas Live replicas for older revisions",
            "# TYPE ae_app_old_revision_live_replicas gauge",
            "# HELP ae_app_overlap_ready_replicas Ready replicas above desired during overlap",
            "# TYPE ae_app_overlap_ready_replicas gauge",
            "# HELP ae_app_overlap_live_replicas Live replicas above desired during overlap",
            "# TYPE ae_app_overlap_live_replicas gauge",
            "# HELP ae_app_status One-hot app status by label {status=ready|progressing|degraded}",
            "# TYPE ae_app_status gauge",
            # Backwards/compat aliases to match earlier docs snippets
            "# HELP ae_desired_replicas Desired replicas per app (alias)",
            "# TYPE ae_desired_replicas gauge",
            "# HELP ae_ready_replicas Ready replicas per app (alias)",
            "# TYPE ae_ready_replicas gauge",
            "# HELP ae_live_replicas Live replicas per app (alias)",
            "# TYPE ae_live_replicas gauge",
            "# HELP kube_deployment_spec_replicas Desired replicas for a Deployment",
            "# TYPE kube_deployment_spec_replicas gauge",
            "# HELP kube_deployment_status_replicas Current replicas for a Deployment",
            "# TYPE kube_deployment_status_replicas gauge",
            "# HELP kube_deployment_status_replicas_ready Ready replicas for a Deployment",
            "# TYPE kube_deployment_status_replicas_ready gauge",
            "# HELP kube_deployment_status_replicas_available Available replicas for a Deployment",
            "# TYPE kube_deployment_status_replicas_available gauge",
            "# HELP kube_deployment_status_replicas_unavailable Unavailable replicas for a Deployment",
            "# TYPE kube_deployment_status_replicas_unavailable gauge",
            "# HELP kube_pod_status_ready Pod readiness (1 for ready) by namespace/pod/condition",
            "# TYPE kube_pod_status_ready gauge",
            "# HELP ae_node_status Node condition (one-hot by status)",
            "# TYPE ae_node_status gauge",
            "# HELP ae_node_last_seen_seconds Age in seconds since last heartbeat",
            "# TYPE ae_node_last_seen_seconds gauge",
            "# HELP ae_node_stale Node heartbeat stale flag (1=stale)",
            "# TYPE ae_node_stale gauge",
            "# HELP ae_service_info Service info with cluster IP",
            "# TYPE ae_service_info gauge",
            "# HELP ae_service_endpoints_total Service endpoints observed (per service)",
            "# TYPE ae_service_endpoints_total gauge",
            "# HELP ae_service_endpoints_ready Ready service endpoints (per service)",
            "# TYPE ae_service_endpoints_ready gauge",
            "# HELP ae_service_port_endpoints_ready Ready endpoints by service port",
            "# TYPE ae_service_port_endpoints_ready gauge",
            "# HELP ae_service_port_endpoints_total Total endpoints by service port",
            "# TYPE ae_service_port_endpoints_total gauge",
            "# HELP ae_service_endpoint_ready Ready flag for individual service endpoint",
            "# TYPE ae_service_endpoint_ready gauge",
        ]
        # Token expiry metrics
        import os as _os
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        def _expiry_seconds(env: str):
            val = _os.getenv(env)
            if not val:
                return None
            try:
                s = val.strip()
                exp = (
                    _dt.fromisoformat(s[:-1] + "+00:00")
                    if s.endswith("Z")
                    else _dt.fromisoformat(s)
                )
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=_tz.utc)
                now = _dt.now(_tz.utc)
                return (exp - now).total_seconds()
            except Exception:
                return None

        for role, env in (
            ("admin", "AE_API_ADMIN_TOKEN_EXPIRES"),
            ("scaler", "AE_API_SCALER_TOKEN_EXPIRES"),
            ("read", "AE_API_READ_TOKEN_EXPIRES"),
        ):
            secs = _expiry_seconds(env)
            if secs is not None:
                lines.append(f'ae_api_token_expiry_seconds{{role="{role}"}} {secs}')
        # Per-app and per-replica labeled gauges
        try:
            statuses = self.store.list_status()
            from ae.controller.spec import split_app_key

            # Optional read scope filtering when a read-capable token is presented
            try:
                import fnmatch as _fn
                import os as _os

                scope = (_os.getenv("AE_API_READ_SCOPE") or "").strip()
                role = self._presented_role()
                if scope and role in {"read", "scale", "admin"}:
                    pats = [p.strip() for p in scope.split(",") if p.strip()]
                    if pats:
                        statuses = [
                            s for s in statuses if any(_fn.fnmatch(s.app_name, p) for p in pats)
                        ]
            except Exception:
                pass
            for s0 in statuses:
                app = s0.app_name
                ns, dep = split_app_key(app)
                dep_labels = f'namespace="{ns}",deployment="{dep}"'
                lines.append(f'ae_app_desired_replicas{{app="{app}"}} {s0.desired_replicas}')
                lines.append(f'ae_app_ready_replicas{{app="{app}"}} {s0.ready_replicas}')
                lines.append(f'ae_app_live_replicas{{app="{app}"}} {s0.live_replicas}')
                lines.append(
                    f'ae_app_current_revision_ready_replicas{{app="{app}"}} {s0.current_revision_ready_replicas}'
                )
                lines.append(
                    f'ae_app_current_revision_live_replicas{{app="{app}"}} {s0.current_revision_live_replicas}'
                )
                lines.append(
                    f'ae_app_old_revision_ready_replicas{{app="{app}"}} {s0.old_revision_ready_replicas}'
                )
                lines.append(
                    f'ae_app_old_revision_live_replicas{{app="{app}"}} {s0.old_revision_live_replicas}'
                )
                lines.append(
                    f'ae_app_overlap_ready_replicas{{app="{app}"}} {s0.overlap_ready_replicas}'
                )
                lines.append(
                    f'ae_app_overlap_live_replicas{{app="{app}"}} {s0.overlap_live_replicas}'
                )
                # Aliases used by playground docs examples
                lines.append(f'ae_desired_replicas{{app="{app}"}} {s0.desired_replicas}')
                lines.append(f'ae_ready_replicas{{app="{app}"}} {s0.ready_replicas}')
                lines.append(f'ae_live_replicas{{app="{app}"}} {s0.live_replicas}')
                # one-hot app status metric
                st = (s0.revision_status or "").strip().lower()
                for name in ("ready", "progressing", "degraded"):
                    val = 1 if st == name else 0
                    lines.append(f'ae_app_status{{app="{app}",status="{name}"}} {val}')
                lines.append(f"kube_deployment_spec_replicas{{{dep_labels}}} {s0.desired_replicas}")
                lines.append(f"kube_deployment_status_replicas{{{dep_labels}}} {s0.live_replicas}")
                lines.append(
                    f"kube_deployment_status_replicas_ready{{{dep_labels}}} {s0.ready_replicas}"
                )
                lines.append(
                    f"kube_deployment_status_replicas_available{{{dep_labels}}} {s0.ready_replicas}"
                )
                try:
                    unavailable = max(int(s0.desired_replicas) - int(s0.ready_replicas), 0)
                except Exception:
                    unavailable = 0
                lines.append(
                    f"kube_deployment_status_replicas_unavailable{{{dep_labels}}} {unavailable}"
                )
            for s0 in statuses:
                reps = self.store.list_pods(s0.app_name)
                ns, _dep = split_app_key(s0.app_name)
                for r in reps:
                    val = 1 if r.ready else 0
                    lines.append(f'ae_pod_ready{{app="{s0.app_name}",pod="{r.pod_name}"}} {val}')
                    lines.append(
                        f'ae_replica_ready{{app="{s0.app_name}",replica="{r.pod_name}"}} {val}'
                    )
                    lines.append(
                        f'kube_pod_status_ready{{namespace="{ns}",pod="{r.pod_name}",condition="true"}} {val}'
                    )
                    lines.append(
                        f'kube_pod_status_ready{{namespace="{ns}",pod="{r.pod_name}",condition="false"}} {1 - val}'
                    )
        except Exception:
            pass
        # Node metrics (status, staleness, last heartbeat age)
        try:
            import os as _os
            from datetime import datetime as _dt
            from datetime import timezone as _tz

            grace = int(_os.getenv("AE_NODE_NOTREADY_AFTER", "40") or 40)
            now = _dt.now(_tz.utc)
            for node, status in self.store.list_nodes():
                seen_at = getattr(status, "seen_at", None)
                last_age = None
                if seen_at:
                    try:
                        last_age = (now - seen_at).total_seconds()
                    except Exception:
                        last_age = None
                st = str(status.status if status else "unknown").lower()
                stale = False
                if last_age is not None and last_age > grace and st == "ready":
                    st = "notready"
                if last_age is not None and last_age > grace:
                    stale = True
                cordoned = bool(getattr(node, "cordoned", False))
                labels = f'node="{node.node_id}",name="{node.name or ""}",cordoned="{str(cordoned).lower()}"'
                lines.append(f'ae_node_status{{{labels},status="{st}"}} 1')
                lines.append(f"ae_node_stale{{{labels}}} {'1' if stale else '0'}")
                if last_age is not None:
                    lines.append(f"ae_node_last_seen_seconds{{{labels}}} {last_age}")
        except Exception:
            pass
        # Site telemetry metrics (status/logs/caps last seen)
        try:
            import os as _os
            from datetime import datetime as _dt
            from datetime import timezone as _tz

            grace = int(_os.getenv("AE_SITE_NOTREADY_AFTER", "90") or 90)
            now = _dt.now(_tz.utc)
            for site_id, last_ts in list(_SITE_LAST_SEEN.items()):
                try:
                    last_age = now.timestamp() - float(last_ts)
                except Exception:
                    continue
                stale = 1 if last_age > grace else 0
                labels = f'site="{site_id}"'
                lines.append(f"ae_site_last_seen_seconds{{{labels}}} {last_age}")
                lines.append(f"ae_site_stale{{{labels}}} {stale}")
            if _SITE_GATEWAY_LAST_SEEN:
                lines.append(
                    "# HELP ae_site_gateway_last_seen_seconds Age of the latest gateway status sample per site and node"
                )
                lines.append("# TYPE ae_site_gateway_last_seen_seconds gauge")
                for (site_id, node_id), last_ts in sorted(_SITE_GATEWAY_LAST_SEEN.items()):
                    try:
                        last_age = now.timestamp() - float(last_ts)
                    except Exception:
                        continue
                    labels = f'site="{site_id}",node="{node_id}"'
                    lines.append(f"ae_site_gateway_last_seen_seconds{{{labels}}} {last_age}")
            if _SITE_GATEWAY_BUILD_INFO:
                lines.append(
                    "# HELP ae_site_gateway_build_info Gateway build metadata observed via site telemetry"
                )
                lines.append("# TYPE ae_site_gateway_build_info gauge")
                for (site_id, node_id), (version, sha, date) in sorted(
                    _SITE_GATEWAY_BUILD_INFO.items()
                ):
                    labels = f'site="{site_id}",node="{node_id}",version="{version}",sha="{sha}",date="{date}"'
                    lines.append(f"ae_site_gateway_build_info{{{labels}}} 1")
        except Exception:
            pass
        # Controller authority metrics
        try:
            authority_fn = getattr(self, "authority_info_fn", None)
            if authority_fn is not None:
                snapshot = authority_fn()
                enabled = (
                    bool(getattr(snapshot, "enabled", False)) if snapshot is not None else False
                )
                is_leader = (
                    bool(getattr(snapshot, "is_leader", False)) if snapshot is not None else False
                )
                leader_info = (
                    getattr(snapshot, "leader_info", None) if snapshot is not None else None
                )
                epoch = 0
                if snapshot is not None:
                    try:
                        epoch = int(getattr(snapshot, "controller_epoch", 0) or 0)
                    except Exception:
                        epoch = 0
                authority_healthy = (
                    1 if (not enabled or is_leader or leader_info is not None) else 0
                )
                lines.append(
                    "# HELP ae_controller_is_leader Whether this controller currently owns mutation authority"
                )
                lines.append("# TYPE ae_controller_is_leader gauge")
                lines.append(f"ae_controller_is_leader {1 if is_leader else 0}")
                lines.append("# HELP ae_controller_epoch Current controller authority epoch")
                lines.append("# TYPE ae_controller_epoch gauge")
                lines.append(f"ae_controller_epoch {epoch}")
                lines.append(
                    "# HELP ae_controller_authority_healthy Whether controller authority can resolve a valid leader"
                )
                lines.append("# TYPE ae_controller_authority_healthy gauge")
                lines.append(f"ae_controller_authority_healthy {authority_healthy}")
        except Exception:
            pass
        # Etcd maintenance + heartbeat churn metrics
        try:
            lines += [
                "# HELP ae_heartbeat_writes_total Node heartbeat status writes to state",
                "# TYPE ae_heartbeat_writes_total counter",
                f"ae_heartbeat_writes_total {_HEARTBEAT_WRITES_TOTAL}",
                "# HELP ae_heartbeat_node_rewrites_total Node metadata rewrites during heartbeat refresh",
                "# TYPE ae_heartbeat_node_rewrites_total counter",
                f"ae_heartbeat_node_rewrites_total {_HEARTBEAT_NODE_REWRITES_TOTAL}",
                "# HELP ae_etcd_maintenance_runs_total Etcd watchdog runs executed by controller",
                "# TYPE ae_etcd_maintenance_runs_total counter",
                f"ae_etcd_maintenance_runs_total {_ETCD_MAINTENANCE_RUNS_TOTAL}",
                "# HELP ae_etcd_maintenance_triggered_total Etcd watchdog runs that triggered compact/defrag",
                "# TYPE ae_etcd_maintenance_triggered_total counter",
                f"ae_etcd_maintenance_triggered_total {_ETCD_MAINTENANCE_TRIGGERED_TOTAL}",
            ]
        except Exception:
            pass
        # Route bundle apply metrics
        try:
            if _ROUTE_BUNDLE_METRICS:
                lines += [
                    "# HELP ae_route_bundle_apply_ok_total Route bundle apply successes",
                    "# TYPE ae_route_bundle_apply_ok_total counter",
                    "# HELP ae_route_bundle_apply_fail_total Route bundle apply failures",
                    "# TYPE ae_route_bundle_apply_fail_total counter",
                    "# HELP ae_route_bundle_apply_latency_seconds Last route bundle apply latency",
                    "# TYPE ae_route_bundle_apply_latency_seconds gauge",
                    "# HELP ae_route_bundle_publish_ok_total Route bundle publishes that succeeded",
                    "# TYPE ae_route_bundle_publish_ok_total counter",
                    "# HELP ae_route_bundle_publish_fail_total Route bundle publishes that failed",
                    "# TYPE ae_route_bundle_publish_fail_total counter",
                    "# HELP ae_route_bundle_pending Route bundle sites still awaiting acknowledgement",
                    "# TYPE ae_route_bundle_pending gauge",
                    "# HELP ae_route_bundle_ack_age_seconds Age of the oldest outstanding route bundle publish",
                    "# TYPE ae_route_bundle_ack_age_seconds gauge",
                ]
                for site_id, metrics in sorted(_ROUTE_BUNDLE_METRICS.items()):
                    labels = f'site="{site_id}"'
                    lines.append(
                        f"ae_route_bundle_apply_ok_total{{{labels}}} {metrics.get('apply_ok_total', 0.0)}"
                    )
                    lines.append(
                        f"ae_route_bundle_apply_fail_total{{{labels}}} {metrics.get('apply_fail_total', 0.0)}"
                    )
                    lines.append(
                        f"ae_route_bundle_apply_latency_seconds{{{labels}}} {metrics.get('last_latency_s', 0.0)}"
                    )
                    lines.append(
                        f"ae_route_bundle_publish_ok_total{{{labels}}} {metrics.get('publish_ok_total', 0.0)}"
                    )
                    lines.append(
                        f"ae_route_bundle_publish_fail_total{{{labels}}} {metrics.get('publish_fail_total', 0.0)}"
                    )
                    lines.append(
                        f"ae_route_bundle_pending{{{labels}}} {metrics.get('pending', 0.0)}"
                    )
                    lines.append(
                        f"ae_route_bundle_ack_age_seconds{{{labels}}} {metrics.get('ack_age_s', 0.0)}"
                    )
        except Exception:
            pass
        # Service/VIP metrics
        try:
            from collections import defaultdict

            services = self.store.list_services()
            for svc in services:
                labels = f'app="{svc.app_name}",cluster_ip="{svc.cluster_ip}"'
                lines.append(f"ae_service_info{{{labels}}} 1")
                eps = self.store.list_service_endpoints(svc.app_name)
                lines.append(f'ae_service_endpoints_total{{app="{svc.app_name}"}} {len(eps)}')
                ready_eps = sum(1 for e in eps if e.ready)
                lines.append(f'ae_service_endpoints_ready{{app="{svc.app_name}"}} {ready_eps}')
                port_ready = defaultdict(int)
                port_total = defaultdict(int)
                for ep in eps:
                    port_ready[ep.port] += 1 if ep.ready else 0
                    port_total[ep.port] += 1
                    lines.append(
                        f'ae_service_endpoint_ready{{app="{svc.app_name}",port="{ep.port}",ip="{ep.ip}",target_port="{ep.target_port}"}} {1 if ep.ready else 0}'
                    )
                for port, val in port_ready.items():
                    lines.append(
                        f'ae_service_port_endpoints_ready{{app="{svc.app_name}",port="{port}"}} {val}'
                    )
                for port, val in port_total.items():
                    lines.append(
                        f'ae_service_port_endpoints_total{{app="{svc.app_name}",port="{port}"}} {val}'
                    )
        except Exception:
            pass
        # Crashloop flags from controller
        try:
            from time import time as _now

            now = float(_now())
            for app, until in list(_APP_CRASHLOOP_UNTIL.items()):
                val = 1 if float(until) > now else 0
                lines.append(f'ae_app_crashloop{{app="{app}"}} {val}')
        except Exception:
            pass
        # Hook durations
        try:
            for (app, hook, kind), (dur, ok) in list(_HOOK_LAST.items()):
                lines.append(
                    f'ae_rollout_hook_duration_seconds{{app="{app}",hook="{hook}",type="{kind}",success="{str(bool(ok)).lower()}"}} {float(dur)}'
                )
        except Exception:
            pass
        # Container restart counts via system snapshot (if available)
        try:
            fn = getattr(self, "system_info_fn", None)
            if fn is not None:
                sysinfo = dict(fn())  # type: ignore[misc]
                # App-level recreate cooldown seconds
                for app, secs in (sysinfo.get("cooldown") or {}).items():
                    try:
                        lines.append(f'ae_app_recreate_cooldown_seconds{{app="{app}"}} {int(secs)}')
                    except Exception:
                        continue
                for c in sysinfo.get("containers") or []:
                    try:
                        name = str(c.get("name", ""))
                        app = str((c.get("labels") or {}).get("ae.app", ""))
                        rc = int(c.get("restart_count", 0) or 0)
                        lines.append(
                            f'ae_container_restart_count{{app="{app}",container="{name}"}} {rc}'
                        )
                    except Exception:
                        continue
                # Overlay health (WireGuard)
                try:
                    ov = sysinfo.get("overlay") or {}
                    peers = ov.get("peers")
                    if peers is not None:
                        lines.append(f"ae_overlay_peers {int(peers)}")
                    hs = ov.get("latest_handshake_seconds")
                    if hs is not None:
                        lines.append(f"ae_overlay_latest_handshake_seconds {float(hs)}")
                    mtu = ov.get("mtu")
                    if mtu is not None:
                        lines.append(f"ae_overlay_mtu {int(mtu)}")
                except Exception:
                    pass
            # Agent cert expiry metrics (if issued.json present)
            try:
                import json as _j
                from datetime import datetime as _dt
                from datetime import timezone as _tz
                from pathlib import Path as _P

                issued_path = _P(os.getenv("AE_TLS_DIR", "state/tls")) / "issued.json"
                if issued_path.exists():
                    data = _j.loads(issued_path.read_text())
                    now = _dt.now(_tz.utc)
                    for item in data or []:
                        node = item.get("node_id") or ""
                        exp_s = item.get("expires_at")
                        if not node or not exp_s:
                            continue
                        try:
                            exp_dt = _dt.fromisoformat(exp_s)
                            if exp_dt.tzinfo is None:
                                exp_dt = exp_dt.replace(tzinfo=_tz.utc)
                            secs = (exp_dt - now).total_seconds()
                            lines.append(f'ae_agent_cert_expiry_seconds{{node="{node}"}} {secs}')
                        except Exception:
                            continue
            except Exception:
                pass
        except Exception:
            pass
        # Probe backoff seconds
        try:
            for (app, pod_name, ptype), secs in list(_PROBE_BACKOFF.items()):
                lines.append(
                    f'ae_pod_probe_backoff_seconds{{app="{app}",pod="{pod_name}",type="{ptype}"}} {int(secs)}'
                )
                lines.append(
                    f'ae_probe_backoff_seconds{{app="{app}",replica="{pod_name}",type="{ptype}"}} {int(secs)}'
                )
        except Exception:
            pass
        # Optional loop metrics
        if _LAST_RECONCILE_TS is not None:
            lines.append(
                "# HELP ae_reconcile_last_timestamp_seconds Unix timestamp of last reconcile"
            )
            lines.append("# TYPE ae_reconcile_last_timestamp_seconds gauge")
            lines.append(f"ae_reconcile_last_timestamp_seconds {_LAST_RECONCILE_TS}")
        if _LAST_RECONCILE_DURATION is not None:
            lines.append(
                "# HELP ae_reconcile_last_duration_seconds Duration of last reconcile in seconds"
            )
            lines.append("# TYPE ae_reconcile_last_duration_seconds gauge")
            lines.append(f"ae_reconcile_last_duration_seconds {_LAST_RECONCILE_DURATION}")
        # Per-app histograms (as sum/count pairs) and ops counters
        for app, s in _APP_RECONCILE_SUM.items():
            c = _APP_RECONCILE_COUNT.get(app, 0)
            lines.append("# HELP ae_reconcile_duration_seconds_sum Total reconcile duration by app")
            lines.append("# TYPE ae_reconcile_duration_seconds_sum counter")
            lines.append(f'ae_reconcile_duration_seconds_sum{{app="{app}"}} {s}')
            lines.append("# HELP ae_reconcile_duration_seconds_count Reconcile count by app")
            lines.append("# TYPE ae_reconcile_duration_seconds_count counter")
            lines.append(f'ae_reconcile_duration_seconds_count{{app="{app}"}} {c}')
        for app, od in _APP_ROLLOUT_OPS.items():
            for op, val in od.items():
                lines.append(
                    "# HELP ae_rollout_operations_total Rollout operations aggregated by op type"
                )
                lines.append("# TYPE ae_rollout_operations_total counter")
                lines.append(f'ae_rollout_operations_total{{app="{app}",op="{op}"}} {val}')
        # Canary weight and step counters
        if _APP_CANARY_WEIGHT:
            lines.append("# HELP ae_canary_weight Current canary weight (0-100) per app")
            lines.append("# TYPE ae_canary_weight gauge")
            for app, w in _APP_CANARY_WEIGHT.items():
                lines.append(f'ae_canary_weight{{app="{app}"}} {float(w)}')
        if _APP_CANARY_STEPS:
            lines.append("# HELP ae_canary_steps_total Total auto canary steps applied per app")
            lines.append("# TYPE ae_canary_steps_total counter")
            for app, n in _APP_CANARY_STEPS.items():
                lines.append(f'ae_canary_steps_total{{app="{app}"}} {int(n)}')
        lines.append("# HELP ae_outbox_publish_success_total Outbox publishes that succeeded")
        lines.append("# TYPE ae_outbox_publish_success_total counter")
        lines.append(f"ae_outbox_publish_success_total {_OUTBOX_PUBLISH_OK}")
        lines.append("# HELP ae_outbox_publish_fail_total Outbox publishes that failed")
        lines.append("# TYPE ae_outbox_publish_fail_total counter")
        lines.append(f"ae_outbox_publish_fail_total {_OUTBOX_PUBLISH_FAIL}")
        if _GATEWAY_WORK_METRICS:
            lines.append("# HELP ae_gateway_work_stale_total Gateway work items marked stale")
            lines.append("# TYPE ae_gateway_work_stale_total counter")
            lines.append("# HELP ae_gateway_work_nak_total Gateway work items NAKed for redelivery")
            lines.append("# TYPE ae_gateway_work_nak_total counter")
            lines.append("# HELP ae_gateway_lease_retry_total Gateway lease acquire retries")
            lines.append("# TYPE ae_gateway_lease_retry_total counter")
            lines.append("# HELP ae_gateway_result_replay_total Gateway result replays delivered")
            lines.append("# TYPE ae_gateway_result_replay_total counter")
            lines.append(
                "# HELP ae_gateway_result_replay_fail_total Gateway result replay attempts that failed"
            )
            lines.append("# TYPE ae_gateway_result_replay_fail_total counter")
            lines.append(
                "# HELP ae_gateway_result_replay_backlog Gateway buffered results awaiting controller delivery"
            )
            lines.append("# TYPE ae_gateway_result_replay_backlog gauge")
            for site_id, stats in _GATEWAY_WORK_METRICS.items():
                labels = f'site="{site_id}"'
                stale = float(stats.get("work_stale_total", 0.0) or 0.0)
                nacked = float(stats.get("work_nak_total", 0.0) or 0.0)
                retries = float(stats.get("lease_retry_total", 0.0) or 0.0)
                replay = float(stats.get("result_replay_total", 0.0) or 0.0)
                replay_fail = float(stats.get("result_replay_fail_total", 0.0) or 0.0)
                replay_backlog = float(stats.get("result_replay_backlog", 0.0) or 0.0)
                lines.append(f"ae_gateway_work_stale_total{{{labels}}} {stale}")
                lines.append(f"ae_gateway_work_nak_total{{{labels}}} {nacked}")
                lines.append(f"ae_gateway_lease_retry_total{{{labels}}} {retries}")
                lines.append(f"ae_gateway_result_replay_total{{{labels}}} {replay}")
                lines.append(f"ae_gateway_result_replay_fail_total{{{labels}}} {replay_fail}")
                lines.append(f"ae_gateway_result_replay_backlog{{{labels}}} {replay_backlog}")
        if _HA_FENCE_METRICS:
            lines.append("# HELP ae_ha_fence_stale_total HA fence stale-envelope rejects")
            lines.append("# TYPE ae_ha_fence_stale_total counter")
            lines.append("# HELP ae_ha_fence_duplicate_total HA fence duplicate-operation no-ops")
            lines.append("# TYPE ae_ha_fence_duplicate_total counter")
            lines.append("# HELP ae_ha_fence_epoch_advance_total HA fence scope epoch advances")
            lines.append("# TYPE ae_ha_fence_epoch_advance_total counter")
            for surface, stats in sorted(_HA_FENCE_METRICS.items()):
                labels = f'surface="{surface}"'
                lines.append(
                    f"ae_ha_fence_stale_total{{{labels}}} {float(stats.get('stale_total', 0.0) or 0.0)}"
                )
                lines.append(
                    f"ae_ha_fence_duplicate_total{{{labels}}} {float(stats.get('duplicate_total', 0.0) or 0.0)}"
                )
                lines.append(
                    f"ae_ha_fence_epoch_advance_total{{{labels}}} {float(stats.get('epoch_advance_total', 0.0) or 0.0)}"
                )
        lines.append("# HELP ae_hpa_reconcile_total HPA authority reconcile attempts")
        lines.append("# TYPE ae_hpa_reconcile_total counter")
        lines.append(
            f"ae_hpa_reconcile_total {float(_HPA_ACTIVITY_METRICS.get('reconcile_total', 0.0) or 0.0)}"
        )
        lines.append("# HELP ae_hpa_scale_total HPA authority scale actions")
        lines.append("# TYPE ae_hpa_scale_total counter")
        lines.append(
            f"ae_hpa_scale_total {float(_HPA_ACTIVITY_METRICS.get('scale_total', 0.0) or 0.0)}"
        )
        lines.append(
            "# HELP ae_hpa_metrics_stale_total HPA reconciles skipped due to stale metrics"
        )
        lines.append("# TYPE ae_hpa_metrics_stale_total counter")
        lines.append(
            f"ae_hpa_metrics_stale_total {float(_HPA_ACTIVITY_METRICS.get('metrics_stale_total', 0.0) or 0.0)}"
        )
        lines.append(
            "# HELP ae_hpa_metrics_missing_total HPA reconciles skipped due to missing metrics or requests"
        )
        lines.append("# TYPE ae_hpa_metrics_missing_total counter")
        lines.append(
            f"ae_hpa_metrics_missing_total {float(_HPA_ACTIVITY_METRICS.get('metrics_missing_total', 0.0) or 0.0)}"
        )
        lines.append(
            "# HELP ae_hpa_snapshot_age_seconds Age of the latest workload metrics snapshot used by HPA"
        )
        lines.append("# TYPE ae_hpa_snapshot_age_seconds gauge")
        lines.append(
            f"ae_hpa_snapshot_age_seconds {float(_HPA_ACTIVITY_METRICS.get('snapshot_age_seconds', 0.0) or 0.0)}"
        )
        if _JS_STREAM_STATS:
            lines.append("# HELP ae_js_stream_bytes JetStream stream bytes in use")
            lines.append("# TYPE ae_js_stream_bytes gauge")
            lines.append("# HELP ae_js_stream_messages JetStream stream message count")
            lines.append("# TYPE ae_js_stream_messages gauge")
            lines.append("# HELP ae_js_stream_max_bytes JetStream stream max bytes")
            lines.append("# TYPE ae_js_stream_max_bytes gauge")
            lines.append(
                "# HELP ae_js_stream_bytes_utilization JetStream stream bytes used / max bytes"
            )
            lines.append("# TYPE ae_js_stream_bytes_utilization gauge")
            for stream, stats in _JS_STREAM_STATS.items():
                labels = f'stream="{stream}"'
                bytes_used = float(stats.get("bytes_used", 0.0) or 0.0)
                messages = float(stats.get("messages", 0.0) or 0.0)
                max_bytes = float(stats.get("max_bytes", 0.0) or 0.0)
                lines.append(f"ae_js_stream_bytes{{{labels}}} {bytes_used}")
                lines.append(f"ae_js_stream_messages{{{labels}}} {messages}")
                lines.append(f"ae_js_stream_max_bytes{{{labels}}} {max_bytes}")
                if max_bytes > 0:
                    util = bytes_used / max_bytes
                    lines.append(f"ae_js_stream_bytes_utilization{{{labels}}} {util}")
        if _JS_CONSUMER_STATS:
            lines.append("# HELP ae_js_consumer_pending JetStream consumer pending messages")
            lines.append("# TYPE ae_js_consumer_pending gauge")
            lines.append("# HELP ae_js_consumer_ack_pending JetStream consumer ack pending")
            lines.append("# TYPE ae_js_consumer_ack_pending gauge")
            lines.append("# HELP ae_js_consumer_redelivered JetStream consumer redelivered")
            lines.append("# TYPE ae_js_consumer_redelivered gauge")
            lines.append("# HELP ae_js_consumer_waiting JetStream consumer waiting pulls")
            lines.append("# TYPE ae_js_consumer_waiting gauge")
            for (stream, consumer), stats in _JS_CONSUMER_STATS.items():
                site_id = str(stats.get("site_id", "") or "")
                labels = f'stream="{stream}",consumer="{consumer}",site="{site_id}"'
                pending = float(stats.get("pending", 0.0) or 0.0)
                ack_pending = float(stats.get("ack_pending", 0.0) or 0.0)
                redelivered = float(stats.get("redelivered", 0.0) or 0.0)
                waiting = float(stats.get("waiting", 0.0) or 0.0)
                lines.append(f"ae_js_consumer_pending{{{labels}}} {pending}")
                lines.append(f"ae_js_consumer_ack_pending{{{labels}}} {ack_pending}")
                lines.append(f"ae_js_consumer_redelivered{{{labels}}} {redelivered}")
                lines.append(f"ae_js_consumer_waiting{{{labels}}} {waiting}")
        lines.append("")
        payload = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_system(self) -> None:
        """Return system-wide info for the demo dashboard.

        Combines controller loop stats, RBAC configuration flags, and optional
        runtime/ingress snapshots when a provider function is injected.
        """
        import os

        # Base controller stats from in-module globals
        ctrl = {
            "last_reconcile_timestamp": _LAST_RECONCILE_TS,
            "last_reconcile_duration": _LAST_RECONCILE_DURATION,
            "apps": {
                app: {
                    "reconciles": int(_APP_RECONCILE_COUNT.get(app, 0)),
                    "duration_sum": float(_APP_RECONCILE_SUM.get(app, 0.0)),
                    "ops": {k: int(v) for k, v in (_APP_ROLLOUT_OPS.get(app, {}) or {}).items()},
                }
                for app in set(
                    list(_APP_RECONCILE_COUNT.keys())
                    + list(_APP_RECONCILE_SUM.keys())
                    + list(_APP_ROLLOUT_OPS.keys())
                )
            },
        }
        # RBAC/mutations flags (never echo secrets)
        rbac = {
            "mutations_enabled": os.getenv("AE_API_MUTATIONS") == "1",
            "read_token_configured": bool(os.getenv("AE_API_READ_TOKEN")),
            "scaler_token_configured": bool(os.getenv("AE_API_SCALER_TOKEN")),
            "admin_token_configured": bool(os.getenv("AE_API_ADMIN_TOKEN")),
        }
        # Optional provider for runtime/ingress/services/storage snapshots
        extra: dict = {}
        fn = getattr(self, "system_info_fn", None)
        if fn is not None:
            try:
                extra = dict(fn())  # type: ignore[misc]
            except Exception:
                extra = {}
        raw_probe_snapshot = extra.pop("ha_probes", None)
        if isinstance(raw_probe_snapshot, dict):
            extra["ha_probes"] = raw_probe_snapshot

        # Crashloop flags snapshot (apps with active TTL)
        try:
            from time import time as _now

            now = float(_now())
            crash = {app: (float(until) > now) for app, until in list(_APP_CRASHLOOP_UNTIL.items())}
        except Exception:
            crash = {}
        authority_snapshot = None
        authority_fn = getattr(self, "authority_info_fn", None)
        if authority_fn is not None:
            try:
                authority_snapshot = authority_fn()
            except Exception:
                authority_snapshot = None
        authority_members = None
        authority_members_fn = getattr(self, "authority_members_fn", None)
        if authority_members_fn is not None:
            try:
                authority_members = list(authority_members_fn())
            except Exception:
                authority_members = None
        try:
            transport = _build_transport_snapshot()
        except Exception:
            transport = {}

        payload = {"controller": ctrl, "rbac": rbac, "crashloop": crash, **(extra or {})}
        payload["transport"] = transport
        payload["ha"] = _build_ha_snapshot(
            extra=payload,
            authority_snapshot=authority_snapshot,
            authority_members=authority_members,
            transport=transport,
        )
        payload["dashboard"] = {
            "layout_mode": _dashboard_layout_mode(payload, payload["ha"]),
        }
        payload.pop("ha_probes", None)
        self._json_ok(payload)

    def _handle_ui_features(self) -> None:
        controlplane_readonly = self._controlplane_readonly_enabled()
        payload = {
            "dashboard": bool(self._dashboard_enabled()),
            "playground": bool(self._playground_enabled()),
            "dashboard_interactive_tools": bool(
                self._flag_enabled("AE_DASHBOARD_INTERACTIVE_TOOLS", True)
            ),
        }
        self._json_ok(payload)

    def _handle_swagger(
        self, *, spec_url: str = "/openapi.json", title: str = "k1s Swagger UI"
    ) -> None:
        spec_literal = json.dumps(spec_url)
        labs_token = ""
        try:
            import os as _os

            if _os.getenv("AE_DEMO_MODE") == "1":
                labs_token = (_os.getenv("AE_LABS_TOKEN") or "").strip()
        except Exception:
            labs_token = ""
        html = resource_loader.render_text(
            "observability",
            "swagger.html",
            TITLE=title,
            SPEC_URL=spec_literal,
            LABS_TOKEN=json.dumps(labs_token),
        )
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_redoc(self, *, spec_url: str = "/openapi.json", title: str = "k1s ReDoc") -> None:
        spec_literal = json.dumps(spec_url)
        html = resource_loader.render_text(
            "observability", "redoc.html", TITLE=title, SPEC_URL=spec_literal
        )
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_apishim_openapi(self, version: str) -> None:
        try:
            from ae.apishim import server as _apishim_server
        except Exception as exc:
            self._json_error(500, f"unable to load apishim OpenAPI: {exc}")
            return
        try:
            if version == "v2":
                doc = _apishim_server._swagger_doc()
            else:
                doc = _apishim_server._openapi_v3_stub()
        except Exception as exc:
            self._json_error(500, f"unable to render apishim OpenAPI: {exc}")
            return
        payload = json.dumps(doc).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_openapi(self) -> None:
        # OpenAPI document with bearer auth and basic schemas
        import os as _os

        tokens_configured = bool(
            _os.getenv("AE_API_ADMIN_TOKEN")
            or _os.getenv("AE_API_SCALER_TOKEN")
            or _os.getenv("AE_API_READ_TOKEN")
        )
        doc = {
            "openapi": "3.0.0",
            "info": {"title": "k1s Controller API", "version": "0.1.5.dev0"},
            "components": {
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
                },
                "schemas": {
                    "AppStatus": {
                        "type": "object",
                        "properties": {
                            "app_name": {"type": "string"},
                            "name": {"type": "string"},
                            "namespace": {"type": "string"},
                            "deployment": {"type": "string"},
                            "desired_replicas": {"type": "integer"},
                            "ready_replicas": {"type": "integer"},
                            "live_replicas": {"type": "integer"},
                            "revision": {"type": "integer"},
                            "revision_status": {"type": "string"},
                            "image": {"type": "string"},
                            "current_revision_ready_replicas": {"type": "integer"},
                            "current_revision_live_replicas": {"type": "integer"},
                            "old_revision_ready_replicas": {"type": "integer"},
                            "old_revision_live_replicas": {"type": "integer"},
                            "overlap_ready_replicas": {"type": "integer"},
                            "overlap_live_replicas": {"type": "integer"},
                            "ingress_host": {"type": "string", "nullable": True},
                            "ingress_path": {"type": "string", "nullable": True},
                        },
                        "required": [
                            "app_name",
                            "desired_replicas",
                            "ready_replicas",
                            "live_replicas",
                            "revision",
                            "revision_status",
                            "image",
                        ],
                    },
                    "Event": {
                        "type": "object",
                        "properties": {
                            "app_name": {"type": "string"},
                            "revision": {"type": "integer"},
                            "event_type": {"type": "string"},
                            "message": {"type": "string"},
                            "created_at": {"type": "string", "format": "date-time"},
                        },
                        "required": ["app_name", "revision", "event_type", "message", "created_at"],
                    },
                },
            },
            "paths": {
                "/metrics": {
                    "get": {
                        "summary": "Prometheus metrics",
                        "responses": {"200": {"description": "Prometheus text"}},
                    }
                },
                "/health": {
                    "get": {
                        "summary": "Controller health",
                        "responses": {"200": {"description": "OK"}},
                    }
                },
                "/status": {
                    "get": {
                        "summary": "List app statuses (paginated)",
                        "parameters": [
                            {
                                "name": "limit",
                                "in": "query",
                                "schema": {"type": "integer", "default": 50},
                            },
                            {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                            {"name": "app", "in": "query", "schema": {"type": "string"}},
                            {"name": "wildcard", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/status/{app}": {
                    "get": {
                        "summary": "Get a single app status",
                        "parameters": [
                            {
                                "name": "app",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {
                            "200": {"description": "OK"},
                            "404": {"description": "Not Found"},
                        },
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/manifest/{app}": {
                    "get": {
                        "summary": "Get the latest stored manifest for an app",
                        "parameters": [
                            {
                                "name": "app",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {
                            "200": {"description": "OK"},
                            "404": {"description": "Not Found"},
                        },
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/events/{app}": {
                    "get": {
                        "summary": "List app events (paginated)",
                        "parameters": [
                            {
                                "name": "app",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "limit",
                                "in": "query",
                                "schema": {"type": "integer", "default": 20},
                            },
                            {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/scale/{app}": {
                    "post": {
                        "summary": "Scale an app",
                        "parameters": [
                            {
                                "name": "app",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/delete/{app}": {
                    "post": {
                        "summary": "Delete an app",
                        "parameters": [
                            {
                                "name": "app",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {"name": "purge", "in": "query", "schema": {"type": "boolean"}},
                        ],
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/logs/{app}": {
                    "get": {
                        "summary": "Tail application logs",
                        "parameters": [
                            {
                                "name": "app",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {"name": "container", "in": "query", "schema": {"type": "string"}},
                            {"name": "tail", "in": "query", "schema": {"type": "integer"}},
                            {"name": "since", "in": "query", "schema": {"type": "integer"}},
                            {"name": "follow", "in": "query", "schema": {"type": "boolean"}},
                        ],
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/plan": {
                    "post": {
                        "summary": "Validate a manifest and return plan diagnostics",
                        "requestBody": {"required": True},
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/tls/verify": {
                    "get": {
                        "summary": "Verify tlsSecretName resolvability under AE_TLS_DIR",
                        "parameters": [
                            {
                                "name": "name",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {"name": "root", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/rollout/pause/{app}": {
                    "post": {
                        "summary": "Pause rollout for an app",
                        "parameters": [
                            {
                                "name": "app",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
                "/rollout/resume/{app}": {
                    "post": {
                        "summary": "Resume rollout for an app",
                        "parameters": [
                            {
                                "name": "app",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "OK"}},
                        "security": [{"bearerAuth": []}],
                    }
                },
            },
        }
        # If tokens are configured, mark API as requiring bearer auth to surface the Authorize button in Swagger/ReDoc
        if tokens_configured:
            doc["security"] = [{"bearerAuth": []}]
        payload = json.dumps(doc).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_manifest_single(self, app: str) -> None:
        try:
            revs = self.store.list_revisions(app, limit=1)
            if not revs:
                self._json_error(404, "no manifest stored")
                return
            man = self.store.get_revision_manifest(app, revs[0].revision)
            payload = json.dumps(man.model_dump(by_alias=True, exclude_none=True)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            self._json_error(404, str(exc))

    def _handle_tls_verify(self) -> None:
        import urllib.parse as _u

        qs = _u.urlparse(self.path).query
        params = _u.parse_qs(qs)
        name = (params.get("name") or [""])[0]
        root = (params.get("root") or [None])[0]
        if not name:
            self._json_error(400, "missing name query param")
            return
        try:
            import os

            from ae.ingress.tls_sync import TlsSecretResolver

            tls_root = Path(root) if root else Path(os.getenv("AE_TLS_DIR", "state/tls"))
            res = TlsSecretResolver(tls_root).resolve(name)
            self._json_ok(
                {
                    "name": name,
                    "root": str(tls_root),
                    "ok": bool(res),
                    "cert": str(res[0]) if res else None,
                    "key": str(res[1]) if res else None,
                }
            )
        except Exception as exc:
            self._json_error(500, str(exc))

    def _handle_status_list(self) -> None:
        import urllib.parse as _up

        from ae.controller.spec import split_app_key

        statuses = self.store.list_status()
        # Optional read scope filtering when a read-capable token is presented
        try:
            import fnmatch as _fn
            import os as _os

            scope = (_os.getenv("AE_API_READ_SCOPE") or "").strip()
            role = self._presented_role()
            if scope and role in {"read", "scale", "admin"}:
                pats = [p.strip() for p in scope.split(",") if p.strip()]
                if pats:
                    statuses = [
                        s for s in statuses if any(_fn.fnmatch(s.app_name, p) for p in pats)
                    ]
        except Exception:
            pass
        path, _, query = self.path.partition("?")
        params = _up.parse_qs(query)
        app_filter = params.get("app", [None])[0]
        wildcard = params.get("wildcard", [None])[0]
        limit = int(params.get("limit", [50])[0])
        try:
            offset = int(params.get("cursor", [0])[0] or 0)
        except ValueError:
            offset = 0
        items = statuses
        if app_filter:
            items = [s for s in items if s.app_name == app_filter]
        elif wildcard:
            import fnmatch

            items = [s for s in items if fnmatch.fnmatch(s.app_name, wildcard)]
        total = len(items)
        page = items[offset : offset + limit]
        next_cursor = offset + limit if (offset + limit) < total else None
        out_items = []
        for s in page:
            ns, name = split_app_key(s.app_name)
            item = {
                "app_name": s.app_name,
                "name": name,
                "namespace": ns,
                "deployment": name,
                "desired_replicas": s.desired_replicas,
                "ready_replicas": s.ready_replicas,
                "live_replicas": s.live_replicas,
                "revision": s.revision,
                "revision_status": s.revision_status,
                "image": s.image,
                "ingress_host": s.ingress_host,
                "ingress_path": s.ingress_path,
                "current_revision_ready_replicas": s.current_revision_ready_replicas,
                "current_revision_live_replicas": s.current_revision_live_replicas,
                "old_revision_ready_replicas": s.old_revision_ready_replicas,
                "old_revision_live_replicas": s.old_revision_live_replicas,
                "overlap_ready_replicas": s.overlap_ready_replicas,
                "overlap_live_replicas": s.overlap_live_replicas,
            }
            # Best-effort rollout summary for list view
            try:
                man = self.store.get_revision_manifest(s.app_name, s.revision)
                ro = getattr(man.spec, "rollout", {}) or {}
                if isinstance(ro, dict) and ro:
                    item["rollout"] = {
                        "strategy": str(ro.get("strategy", "parallel")),
                        "weight": ro.get("weight"),
                        "pause": bool(ro.get("pause", False)),
                    }
            except Exception:
                pass
            out_items.append(item)

        payload = {
            "items": out_items,
            "next": str(next_cursor) if next_cursor is not None else None,
        }
        self._json_ok(payload)

    def _handle_nodes(self) -> None:
        import os as _os
        from datetime import datetime, timezone

        try:
            grace = int(_os.getenv("AE_NODE_NOTREADY_AFTER", "40") or 40)
        except Exception:
            grace = 40
        items = []
        for node, status in self.store.list_nodes():
            seen_at = status.seen_at if status else None
            stale = False
            if seen_at:
                try:
                    stale = (datetime.now(timezone.utc) - seen_at).total_seconds() > grace
                except Exception:
                    stale = False
            st = status.status if status else "Unknown"
            if stale and st == "Ready":
                st = "NotReady"
            items.append(
                {
                    "node_id": node.node_id,
                    "name": node.name,
                    "backend": node.backend,
                    "endpoint": node.endpoint,
                    "capabilities": getattr(node, "capabilities", {}) or {},
                    "labels": node.labels,
                    "gpu_count": preferred_gpu_count(node.labels, getattr(node, "capabilities", {}) or {}),
                    "gpu_models": preferred_gpu_models(
                        node.labels, getattr(node, "capabilities", {}) or {}
                    ),
                    "taints": node.taints,
                    "pod_cidr": node.pod_cidr,
                    "wg_pubkey": node.wg_pubkey,
                    "rp_pubkey": getattr(node, "rp_pubkey", None),
                    "cordoned": bool(getattr(node, "cordoned", False)),
                    "status": st,
                    "seen_at": seen_at.isoformat() if seen_at else None,
                    "stale": stale,
                }
            )
        self._json_ok({"nodes": items, "count": len(items), "stale_after_seconds": grace})

    def _parse_query(self) -> dict[str, list[str]]:
        import urllib.parse as _up

        return _up.parse_qs(_up.urlsplit(self.path).query)

    def _decode_app_segment(self, prefix: str) -> str:
        import urllib.parse as _up

        path = _up.urlsplit(self.path).path
        return _up.unquote(path[len(prefix) :].strip("/"))

    def _resolve_status_for_app(self, raw_app: str) -> AppStatus | None:
        import urllib.parse as _up

        from ae.controller.spec import app_key, parse_app_ref, split_app_key

        raw = str(raw_app or "").strip()
        decoded = _up.unquote(raw)
        candidates: list[str] = []

        def add(value: str | None) -> None:
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)

        add(raw)
        add(decoded)

        for ref in (decoded, raw):
            try:
                ns, name = parse_app_ref(ref)
                add(app_key(name, ns))
            except Exception:
                pass

        for candidate in candidates:
            status = self.store.get_status(candidate)
            if status is not None:
                return status

        statuses = list(self.store.list_status())
        for candidate in candidates:
            for status in statuses:
                if status.app_name == candidate:
                    return status

        short_matches = []
        if decoded and "--" not in decoded and "/" not in decoded:
            for status in statuses:
                _ns, name = split_app_key(status.app_name)
                if name == decoded:
                    short_matches.append(status)
        if len(short_matches) == 1:
            return short_matches[0]

        return None

    def _status_payload(self, s: AppStatus, want_details: bool) -> dict[str, object]:
        from ae.controller.spec import split_app_key

        ns, name = split_app_key(s.app_name)
        data: dict[str, object] = {
            "app_name": s.app_name,
            "name": name,
            "namespace": ns,
            "deployment": name,
            "desired_replicas": s.desired_replicas,
            "ready_replicas": s.ready_replicas,
            "live_replicas": s.live_replicas,
            "revision": s.revision,
            "revision_status": s.revision_status,
            "image": s.image,
            "ingress_host": s.ingress_host,
            "ingress_path": s.ingress_path,
            "current_revision_ready_replicas": s.current_revision_ready_replicas,
            "current_revision_live_replicas": s.current_revision_live_replicas,
            "old_revision_ready_replicas": s.old_revision_ready_replicas,
            "old_revision_live_replicas": s.old_revision_live_replicas,
            "overlap_ready_replicas": s.overlap_ready_replicas,
            "overlap_live_replicas": s.overlap_live_replicas,
        }
        if want_details:
            try:
                manifest = self.store.get_revision_manifest(s.app_name, s.revision)
                reps = self.store.list_pods(s.app_name)
                data["manifest"] = manifest.model_dump()
                pods = [
                    {
                        "pod_name": r.pod_name,
                        "replica_id": r.pod_name,
                        "ready": bool(r.ready),
                        "live": bool(r.live),
                        "status": r.status,
                        "readiness_message": r.readiness_message,
                        "liveness_message": r.liveness_message,
                    }
                    for r in reps
                ]
                data["pods"] = pods
                data["replicas"] = pods
                # Include per-container runtime info when system_info_fn is available
                try:
                    sys = self.system_info_fn() if getattr(self, "system_info_fn", None) else None  # type: ignore[misc]
                    conts = []
                    for c in (sys.get("containers") if isinstance(sys, dict) else []) or []:
                        labels = c.get("labels") or {}
                        if labels.get("ae.app") == s.app_name:
                            conts.append(
                                {
                                    "name": c.get("name"),
                                    "labels": labels,
                                    "restart_count": c.get("restart_count", 0),
                                }
                            )
                    if conts:
                        data["containers"] = conts
                except Exception:
                    pass
            except Exception:
                pass
        return data

    def _handle_status_single(self, app: str) -> None:
        # Support optional query on the path segment (e.g., "<app>?details=1")
        import os as _os
        import urllib.parse as _up

        app_ref = str(app or "")
        params = self._parse_query()
        if "?" in app_ref:
            app_ref, query = app_ref.split("?", 1)
            if query:
                params = _up.parse_qs(query)

        status = self._resolve_status_for_app(app_ref)
        if status is None:
            self.send_response(404)
            self.end_headers()
            return

        if _os.getenv("AE_API_READ_SCOPE") and not self._scope_allows("read", status.app_name):
            self._deny(403)
            return

        want_details = str(params.get("details", ["0"])[0]).lower() in {"1", "true", "yes"}
        data = self._status_payload(status, want_details)
        self._json_ok(data)

    def _handle_events(self, app_and_query: str) -> None:
        # crude query parsing for ?limit=NUM
        if "?" in app_and_query:
            app, query = app_and_query.split("?", 1)
            limit = 20
            for part in query.split("&"):
                if part.startswith("limit="):
                    try:
                        limit = int(part.split("=", 1)[1])
                    except ValueError:
                        limit = 20
        else:
            app = app_and_query
            limit = 20

        events = self.store.list_events(app, limit=limit)
        data = [
            {
                "app_name": e.app_name,
                "revision": e.revision,
                "event_type": e.event_type,
                "message": e.message,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]
        self._json_ok(data)

    def _json_ok(self, obj) -> None:  # noqa: ANN001
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_version_info(self) -> None:
        payload = dict(AE_BUILD_INFO())
        payload["component"] = "controller"
        self._json_ok(payload)

    def _json_error(self, code: int, message: str) -> None:
        self._json_error_obj(code, {"error": message})

    def _json_error_obj(self, code: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _fmt: str, *_args):  # quiet
        return

    def _handle_docs(self) -> None:
        html = resource_loader.load_text("observability", "docs.html")
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_health(self) -> None:
        # Basic liveness: store reachable and thread is alive.
        try:
            _ = self.store.list_status()
            ok = True
        except Exception:
            ok = False
        payload = json.dumps({"status": "ok" if ok else "degraded"}).encode("utf-8")
        self.send_response(200 if ok else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_static_asset(self, path_only: str) -> bool:
        from pathlib import Path

        rel = path_only.lstrip("/")
        if not rel.startswith("static/"):
            return False
        rel_path = rel[len("static/") :]
        if not rel_path:
            return False
        if ".." in Path(rel_path).parts:
            return False
        base_file = Path(__file__).resolve()
        repo_root = base_file.parents[3]
        candidates = [
            repo_root / "docs" / "static" / rel_path,
            repo_root / "docs" / "site" / "static" / rel_path,
            base_file.parent / "static" / rel_path,
        ]
        target = next((p for p in candidates if p.is_file()), None)
        if target is None:
            return False
        try:
            data = target.read_bytes()
        except Exception:
            return False
        ext = target.suffix.lower()
        content_type = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return True

    def _handle_dashboard(self) -> None:
        # Simple static dashboard that polls status, events, and logs.
        labs_token = ""
        try:
            import os as _os

            if _os.getenv("AE_LABS") == "1":
                labs_token = (_os.getenv("AE_LABS_TOKEN") or "").strip()
        except Exception:
            labs_token = ""
        apishim_base = ""
        try:
            apishim_base = (
                os.getenv("AE_APISHIM_SERVER") or os.getenv("AE_APISHIM_BASE") or ""
            ).strip()
        except Exception:
            apishim_base = ""
        dashboard_token = ""
        try:
            dashboard_token = _dashboard_bootstrap_token()
        except Exception:
            dashboard_token = ""
        html = resource_loader.render_text(
            "observability",
            "dashboard.html",
            LABS_TOKEN=json.dumps(labs_token),
            DASHBOARD_TOKEN=json.dumps(dashboard_token),
            APISHIM_BASE=json.dumps(apishim_base),
        )
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_dashboard_partial_logs(self) -> None:
        # Render logs as an HTML fragment suitable for hx-swap=innerHTML
        import urllib.parse as _up

        frag, _, query = self.path.partition("?")
        params = _up.parse_qs(query)
        app = (params.get("app", [""])[0] or "").strip()
        container = params.get("container", [None])[0]
        try:
            tail = int(params.get("tail", ["200"])[0])
        except ValueError:
            tail = 200
        filt = (params.get("filter", [""])[0] or "").lower()

        fn = getattr(self, "logs_fn", None)
        if not app or fn is None:
            html = '<div class="log-entry">No logs</div>'
            payload = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        try:
            lines = list(fn(app, container, tail, None, False))  # type: ignore[misc]
        except Exception as exc:
            html = f'<div class="log-entry"><code>error</code> {self._escape_html(str(exc))}</div>'
            payload = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        out: list[str] = []
        for l in lines:
            s = l.decode("utf-8", "replace") if isinstance(l, (bytes, bytearray)) else str(l)
            lowered = s.lower()
            if "no container with name or id" in lowered or "no such container" in lowered:
                continue
            if filt and (filt not in s.lower()):
                continue
            ts, msg = self._split_ts(s)
            out.append(
                f'<div class="log-entry"><code>{self._escape_html(ts)}</code> {self._escape_html(msg)}</div>'
            )
        html = "\n".join(out) if out else '<div class="log-entry">No recent log lines</div>'
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_logs_stream(self, app: str) -> None:
        """Stream logs as Server-Sent Events (SSE): one log entry per event."""
        import queue as _queue
        import threading as _threading
        import time as _t
        import urllib.parse as _up

        _path, _, query = self.path.partition("?")
        params = _up.parse_qs(query)
        container = params.get("container", [None])[0]
        try:
            tail = int(params.get("tail", ["100"])[0])
        except ValueError:
            tail = 100
        try:
            since = int(params.get("since", ["0"])[0]) or None
        except ValueError:
            since = None

        fn = getattr(self, "logs_fn", None)
        if fn is None:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        stop = _threading.Event()
        q: _queue.Queue[object] = _queue.Queue(maxsize=1000)
        sentinel = object()

        def _pump() -> None:
            try:
                for line in fn(app, container, tail, since, True):  # type: ignore[misc]
                    if stop.is_set():
                        break
                    try:
                        q.put(line, timeout=0.5)
                    except Exception:
                        continue
            except Exception:
                pass
            finally:
                try:
                    q.put(sentinel, timeout=0.5)
                except Exception:
                    pass

        worker = _threading.Thread(target=_pump, name="ae-logs-sse", daemon=True)
        worker.start()
        heartbeat = 10.0
        try:
            # Hint client retry interval
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=heartbeat)
                except _queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if item is sentinel:
                    break
                if isinstance(item, (bytes, bytearray)):
                    s = item.decode("utf-8", "replace").rstrip("\n")
                else:
                    s = str(item).rstrip("\n")
                lowered = s.lower()
                if "no container with name or id" in lowered or "no such container" in lowered:
                    continue
                out = ("data: " + s + "\n\n").encode("utf-8", "replace")
                self.wfile.write(out)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            stop.set()
            return
        except Exception:
            stop.set()
            try:
                self.wfile.write(b"event: error\ndata: stream closed\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def _split_ts(self, line: str) -> tuple[str, str]:
        # Best-effort split: Docker timestamps are RFC3339 then a space
        try:
            if len(line) >= 20 and line[4] == "-" and "T" in line[:25]:
                ts, msg = line.split(" ", 1)
                return ts, msg
        except Exception:
            pass
        return "", line

    def _handle_dashboard_partial_probe_history(self) -> None:
        # Render probe histories as an HTML fragment for hx-swap
        import urllib.parse as _up

        frag, _, query = self.path.partition("?")
        params = _up.parse_qs(query)
        app = (params.get("app", [""])[0] or "").strip()
        try:
            limit = int(params.get("limit", ["50"])[0])
        except ValueError:
            limit = 50
        if not app:
            html = '<div class="muted">Select an app to view probe history.</div>'
        else:
            rows = self.store.get_probe_history(app, limit)
            if not rows:
                html = '<div class="muted">No probe evaluations recorded.</div>'
            else:
                out = [
                    '<table class="mini"><thead><tr><th>Time</th><th>Replica</th><th>Ready</th><th>Live</th><th>R msg</th><th>L msg</th></tr></thead><tbody>'
                ]
                esc = self._escape_html
                for r in rows:
                    rd = "yes" if r.ready else "no"
                    lv = "yes" if r.live else "no"
                    out.append(
                        f"<tr><td>{esc(r.check_time.isoformat())}</td><td>{esc(r.pod_name)}</td><td>{rd}</td><td>{lv}</td>"
                        f"<td>{esc(r.readiness_message or '')}</td><td>{esc(r.liveness_message or '')}</td></tr>"
                    )
                out.append("</tbody></table>")
                html = "".join(out)
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _escape_html(s: str) -> str:
        import html as _html

        return _html.escape(s, quote=False)

    def _handle_logs(self, app_and_query: str) -> None:
        # Return logs as JSON {app, lines:[...]}. Prefer polling over streaming for demo simplicity.
        import urllib.parse as _up

        frag, _, query = app_and_query.partition("?")
        params = _up.parse_qs(query)
        app = frag
        container = params.get("container", [None])[0]
        try:
            tail = int(params.get("tail", ["200"])[0])
        except ValueError:
            tail = 200
        try:
            since = int(params.get("since", ["0"])[0]) or None
        except ValueError:
            since = None
        follow = str(params.get("follow", ["0"])[0]).lower() in {"1", "true"}

        if follow:
            self._json_error(400, "follow streaming not supported; poll without follow")
            return

        fn = getattr(self, "logs_fn", None)
        if fn is None:
            self._json_error(404, "logs endpoint not available")
            return
        try:
            lines = list(fn(app, container, tail, since, False))  # type: ignore[misc]
        except Exception as exc:
            self._json_error(500, str(exc))
            return
        filtered: list[str] = []
        for l in lines:
            s = l.decode("utf-8", "replace") if isinstance(l, (bytes, bytearray)) else str(l)
            lowered = s.lower()
            if "no container with name or id" in lowered or "no such container" in lowered:
                continue
            filtered.append(s)
        self._json_ok(
            {
                "app": app,
                "lines": filtered,
            }
        )


def start_http_api(
    port: int,
    store: SQLiteStateStore,
    *,
    scale_fn=None,
    delete_fn=None,
    apply_fn=None,
    exec_fn=None,
    logs_fn=None,
    system_info_fn=None,
    authority_info_fn=None,
    authority_members_fn=None,
    plan_fn=None,
    rollout_pause_fn=None,
    rollout_resume_fn=None,
) -> tuple[socketserver.TCPServer, int, threading.Thread]:
    """Start the HTTP API on the given port.

    If port == 0, the OS selects a free port. Returns (server, assigned_port, thread).
    """

    handler_cls = type("Handler", (_ApiHandler,), {})
    handler_cls.store = store
    handler_cls.metrics = MetricsService(store)
    # Avoid Python descriptor binding when accessed via instances: wrap as staticmethods
    handler_cls.scale_fn = staticmethod(scale_fn) if scale_fn is not None else None
    handler_cls.delete_fn = staticmethod(delete_fn) if delete_fn is not None else None
    handler_cls.apply_fn = staticmethod(apply_fn) if apply_fn is not None else None
    handler_cls.exec_fn = staticmethod(exec_fn) if exec_fn is not None else None
    handler_cls.logs_fn = staticmethod(logs_fn) if logs_fn is not None else None
    handler_cls.system_info_fn = (
        staticmethod(system_info_fn) if system_info_fn is not None else None
    )
    handler_cls.authority_info_fn = (
        staticmethod(authority_info_fn) if authority_info_fn is not None else None
    )
    handler_cls.authority_members_fn = (
        staticmethod(authority_members_fn) if authority_members_fn is not None else None
    )
    handler_cls.plan_fn = staticmethod(plan_fn) if plan_fn is not None else None
    handler_cls.rollout_pause_fn = (
        staticmethod(rollout_pause_fn) if rollout_pause_fn is not None else None
    )
    handler_cls.rollout_resume_fn = (
        staticmethod(rollout_resume_fn) if rollout_resume_fn is not None else None
    )

    # Allow quick restarts by enabling SO_REUSEADDR to avoid TIME_WAIT bind errors
    class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        httpd = ReusableTCPServer(("0.0.0.0", port), handler_cls)
    except OSError as exc:
        # Only fall back to an ephemeral port when caller asked for 0 (auto-assign).
        # If a concrete port was requested, propagate the error so the caller can react.
        if exc.errno == errno.EADDRINUSE and int(port) == 0:
            httpd = ReusableTCPServer(("0.0.0.0", 0), handler_cls)
        else:
            raise
    assigned = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, name="ae-http-api", daemon=True)
    thread.start()
    return httpd, assigned, thread


def _prom_escape_label_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# ruff: noqa: E501,S603,S607,S110,S112,SIM105,SIM108,SIM118,SIM210,S104,UP017,UP038,E741,B023,C401,UP035,E402,UP034
