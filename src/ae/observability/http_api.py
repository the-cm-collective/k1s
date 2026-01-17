"""Lightweight HTTP API for metrics, status, events, and previews.

Endpoints:
- GET /metrics            -> Prometheus text format
- GET /status             -> JSON list of app statuses
- GET /status/<app>       -> JSON object for app status (404 if missing)
- GET /events/<app>?limit -> JSON list of recent events for app
 - GET /history/<app>?limit -> JSON list of recent probe evaluations (replica histories)
 - POST /k8s/preview      -> Render K8s YAML for a manifest (dev only; gated by AE_API_DEV_EXPORT=1)
"""

from __future__ import annotations

import errno
import http.server
import json
import logging
import os
import socketserver
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ae.controller.state import SQLiteStateStore

logger = logging.getLogger(__name__)


# Helper: when running in demo mode, restrict visible apps to those registered
# in the app registry (typically source=specs). This keeps the dashboard scoped
# to the selected demo and avoids surfacing historical apps from the state DB.
def _demo_filter_enabled() -> bool:
    try:
        raw = str(os.getenv("AE_DEMO_FILTER", "") or "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
    except Exception:
        pass
    return True


def _demo_allowed_sources() -> set[str]:
    raw = str(os.getenv("AE_DEMO_SOURCES", "specs") or "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def _demo_label_selector() -> dict[str, str | None]:
    raw = str(os.getenv("AE_DEMO_LABELS", "") or "").strip()
    if not raw:
        return {}
    out: dict[str, str | None] = {}
    for expr in raw.split(","):
        expr = expr.strip()
        if not expr:
            continue
        if "=" in expr:
            k, v = expr.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            out[expr] = None
    return out


def _labels_match(labels: dict, selector: dict[str, str | None]) -> bool:
    for key, val in selector.items():
        if key not in labels:
            return False
        if val is not None and str(labels.get(key)) != val:
            return False
    return True


def _demo_allowed_apps() -> set[str]:
    try:
        # Only filter when explicitly in demo mode
        if os.getenv("AE_DEMO_MODE") != "1" or not _demo_filter_enabled():
            return set()
        sources = _demo_allowed_sources()
        label_sel = _demo_label_selector()
        db_path = Path(os.getenv("AE_STATE_DB", "state/controller.db"))
        store = SQLiteStateStore(db_path)
        entries = store.list_registered_apps()
        allowed: set[str] = set()
        for entry in entries:
            if sources and entry.source in sources:
                allowed.add(entry.app_name)
                continue
            if label_sel and _labels_match(entry.labels, label_sel):
                allowed.add(entry.app_name)
        return allowed
    except Exception:
        return set()


def _filter_statuses_for_demo(items):
    try:
        if not _demo_filter_enabled():
            return items
        # Compute the allowed app set from registry demo sources and any Labs-applied apps.
        demo_allowed = set(_demo_allowed_apps())
        try:
            labs_allowed = set(_LABS_APPS)
        except Exception:
            labs_allowed = set()
        try:
            prefix_allowed = set(_LABS_APP_PREFIXES)
        except Exception:
            prefix_allowed = set()
        if os.getenv("AE_DEMO_MODE") != "1":
            prefix_allowed = set()
        allowed = demo_allowed | labs_allowed
        if not allowed and not prefix_allowed:
            # No demo scope and no labs apps tracked: do not filter.
            return items
        # Restrict to the allowed set or allowed prefixes.
        subset = [
            s
            for s in items
            if (
                getattr(s, "app_name", None) in allowed
                or any(str(getattr(s, "app_name", "")).startswith(p) for p in prefix_allowed)
            )
        ]
        if subset:
            return subset
        # If a demo scope exists (apps registered under demo sources/labels), strictly enforce it
        # even if the controller hasn't recorded any of those apps yet. This prevents
        # leaking historical apps from previous runs.
        if demo_allowed:
            return []
        # Otherwise, this is a Labs-only race (session app not yet materialized in the store):
        # fall back to the unfiltered list to avoid an empty UI during the brief apply window.
        return items
    except Exception:
        return items


from ae.observability.metrics import MetricsService

# Simple in-memory reconcile metrics updated by the controller loop.
_LAST_RECONCILE_TS: float | None = None
_LAST_RECONCILE_DURATION: float | None = None
_APP_RECONCILE_SUM: dict[str, float] = {}
_APP_RECONCILE_COUNT: dict[str, int] = {}
# Track labs-applied app names so demo filters include them
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
# Probe backoff: (app, replica, type) -> seconds
_PROBE_BACKOFF: dict[tuple[str, str, str], int] = {}
_APP_ROLLOUT_OPS: dict[str, dict[str, int]] = {}
# Canary tracking: latest weight and step counter per app
_APP_CANARY_WEIGHT: dict[str, float] = {}
_APP_CANARY_STEPS: dict[str, int] = {}

_HELM_DEMO_LOCK = threading.RLock()
_HELM_DEMO_STATE: dict[str, object] = {
    "proc": None,
    "log": Path(os.getenv("AE_LABS_HELM_LOG", "state/labs/helm-demo.log")),
    "log_handle": None,
    "port": int(os.getenv("AE_LABS_HELM_PORT", "8455") or 8455),
    "token": os.getenv("AE_LABS_HELM_TOKEN", "helm-demo"),
    "runtime": os.getenv("AE_LABS_HELM_RUNTIME", "stub"),
    "namespace": os.getenv("AE_LABS_HELM_NAMESPACE", "demo-helm"),
    "chart": os.getenv("AE_LABS_HELM_CHART", "demochart"),
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
        env.setdefault("PYTHONPATH", str(root / "src"))
        helm_server = os.getenv("AE_LABS_HELM_SERVER", "").strip()
        port_note = ""
        port = int(_HELM_DEMO_STATE.get("port") or 8455)
        if not helm_server and not _port_available("127.0.0.1", port):
            fallback = _pick_free_port("127.0.0.1")
            _HELM_DEMO_STATE["port"] = fallback
            port_note = f"port {port} busy; using {fallback}"
            port = fallback
        env.setdefault("PORT", str(port))
        env.setdefault("TOKEN", str(_HELM_DEMO_STATE.get("token")))
        env.setdefault("RUNTIME", str(_HELM_DEMO_STATE.get("runtime")))
        env.setdefault("NAMESPACE", str(_HELM_DEMO_STATE.get("namespace")))
        env.setdefault("CHART_NAME", str(_HELM_DEMO_STATE.get("chart")))
        try:
            keep = str(os.getenv("AE_LABS_HELM_KEEP", "") or "").strip()
            env.setdefault("HELM_SHIM_KEEP", keep if keep else "1")
        except Exception:
            env.setdefault("HELM_SHIM_KEEP", "1")
        if helm_server:
            env.setdefault("APISHIM_SERVER", helm_server)
            try:
                import urllib.parse as _up

                parsed = _up.urlparse(helm_server)
                if parsed.port:
                    _HELM_DEMO_STATE["port"] = parsed.port
            except Exception:
                pass
        env.setdefault("TMPDIR", str(log_path.parent))
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
        )
        _HELM_DEMO_STATE["proc"] = proc
        _HELM_DEMO_STATE["log_handle"] = log_handle
        _HELM_DEMO_STATE["started"] = datetime.now(timezone.utc).isoformat()
    status = _helm_demo_status()
    if port_note:
        status["message"] = port_note
    return status


def _helm_demo_stop() -> dict[str, object]:
    with _HELM_DEMO_LOCK:
        proc = _HELM_DEMO_STATE.get("proc")
        if proc and getattr(proc, "poll", lambda: None)() is None:
            try:
                proc.terminate()
            except Exception:
                pass
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


def record_probe_backoff(app: str, replica: str, probe_type: str, seconds: int) -> None:
    try:
        _PROBE_BACKOFF[(str(app), str(replica), str(probe_type))] = max(0, int(seconds))
    except Exception:
        pass


class _ApiHandler(http.server.BaseHTTPRequestHandler):
    store: SQLiteStateStore  # injected
    metrics: MetricsService  # injected
    # Optional mutators injected by controller when enabled
    scale_fn = None  # type: ignore[var-annotated]
    delete_fn = None  # type: ignore[var-annotated]
    apply_fn = None  # type: ignore[var-annotated]
    # Optional system info provider injected by controller
    system_info_fn = None  # type: ignore[var-annotated]
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

    # --- Dev CORS helpers (used by the labs playground) ----------------
    def _labs_enabled(self) -> bool:
        try:
            import os as _os

            return _os.getenv("AE_LABS") == "1"
        except Exception:
            return False

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
        try:
            meta = payload.get("metadata") if isinstance(payload, dict) else {}
            app = ""
            if isinstance(meta, dict):
                app = str(meta.get("name") or "")
            if not app:
                # Some callers may wrap the manifest
                inner = payload.get("manifest") if isinstance(payload, dict) else None
                if isinstance(inner, dict):
                    inner_meta = inner.get("metadata") or {}
                    if isinstance(inner_meta, dict):
                        app = str(inner_meta.get("name") or "")
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
            return self.apply_fn(payload)  # type: ignore[misc]

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

        # Extract presented token
        auth = self.headers.get("Authorization", "")
        token = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else ""

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
            if not self._labs_enabled():
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
            if not self._labs_request_authorized():
                self._deny(401)
                return
            self._json_ok(_helm_demo_status())
            return
        # Metrics allowed without auth
        if path_only.startswith("/metrics"):
            self._handle_metrics()
            return
        # Public pages (always allowed): OpenAPI + lightweight docs UIs
        if path_only in {
            "/openapi.json",
            "/swagger",
            "/swagger/",
            "/redoc",
            "/redoc/",
            "/",
            "/docs",
        }:
            if path_only == "/openapi.json":
                self._handle_openapi()
            elif path_only in ("/", "/docs"):
                self._handle_docs()
            elif path_only in ("/swagger", "/swagger/"):
                self._handle_swagger()
            else:
                self._handle_redoc()
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
            self._handle_dashboard()
            return
        if path_only.startswith("/dashboard/partials/"):
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
            self._handle_labs_sse_events()
            return
        if path_only == "/dashboard.js":
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
            # Enforce read scope for single-app read if configured
            app = self.path.split("/", 2)[2].split("?", 1)[0]
            import os as _os

            if _os.getenv("AE_API_READ_SCOPE") and not self._scope_allows("read", app):
                self._deny(403)
                return
            self._handle_status_single(self.path.split("/", 2)[2])
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
            app = app_and_q.split("?",1)[0]
            try:
                import urllib.parse as _up
                q = app_and_q.split("?",1)[1] if "?" in app_and_q else ""
                params = _up.parse_qs(q)
                limit = int((params.get("limit", ["20"])[0] or "20"))
            except Exception:
                limit = 20
            try:
                hist = self.store.get_probe_history(app, limit)
                out = [
                    {
                        "replica_id": h.replica_id,
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
        # Labs playground micro-API (dev only)
        if self.path.startswith("/labs/") or self.path == "/labs/info":
            self._handle_labs_post()
            return
        # Mutations are optional and gated by env
        import os

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
                self._json_error(400, "invalid JSON body: expected App manifest")
                return
            try:
                # Scope enforcement: admin token must be allowed for target app
                role = self._presented_role()
                app = str(payload.get("metadata", {}).get("name", ""))
                if not app:
                    self._json_error(400, "manifest missing metadata.name for scope check")
                    return
                if role != "admin" or not self._scope_allows("admin", app) or not self._rbac_allows("create", app):
                    self._json_error(403, "token scope denies apply to target app")
                    return
                report = self._call_apply(payload, source="api")
                self._json_ok(report)
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
                rc = int(self.exec_fn(app, container, [str(x) for x in cmd], int(timeout) if timeout is not None else None))  # type: ignore[misc]
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
                self._json_ok(report)
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
                        helm_candidates = sorted({n for n in names if any(n.startswith(p) for p in prefixes)})
                        if helm_candidates:
                            logger.info("labs reset removing helm demo apps: %s", ", ".join(helm_candidates))
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
                # doesn't reapply them after reset. Prefer the running shim API so
                # deletes publish watch events; fall back to direct DB cleanup only
                # if the shim server is unreachable.
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
                        removed_shim = 0
                        shim_reachable = False
                        shim_base = ""
                        try:
                            import os as _os
                            import requests as _req

                            base = str(_os.getenv("AE_LABS_HELM_SERVER", "") or "").strip()
                            if not base:
                                port = int(_HELM_DEMO_STATE.get("port") or 8455)
                                base = f"https://127.0.0.1:{port}"
                            base = base.rstrip("/")
                            shim_base = base
                            token = (
                                str(_os.getenv("AE_LABS_HELM_TOKEN") or "").strip()
                                or str(_os.getenv("AE_APISHIM_TOKEN") or "").strip()
                                or str(_HELM_DEMO_STATE.get("token") or "").strip()
                            )
                            headers = {"Authorization": f"Bearer {token}"} if token else {}
                            verify_path = "state/certs/combined-dev-ca.pem"
                            verify = verify_path if _os.path.exists(verify_path) else False
                            try:
                                probe = _req.get(f"{base}/version", headers=headers, timeout=2, verify=verify)
                                shim_reachable = probe.status_code < 500
                            except Exception:
                                shim_reachable = False
                            if shim_reachable:
                                logger.info(
                                    "labs reset using shim API at %s for namespace %s",
                                    shim_base or "<unknown>",
                                    ns,
                                )
                                for grp, ver, res in targets:
                                    if grp:
                                        list_url = f"{base}/apis/{grp}/{ver}/namespaces/{ns}/{res}"
                                    else:
                                        list_url = f"{base}/api/{ver}/namespaces/{ns}/{res}"
                                    try:
                                        resp = _req.get(list_url, headers=headers, timeout=3, verify=verify)
                                        if resp.status_code >= 400:
                                            continue
                                        data = resp.json() if resp.content else {}
                                        items = data.get("items") if isinstance(data, dict) else []
                                        for item in items or []:
                                            meta = item.get("metadata") if isinstance(item, dict) else None
                                            name = meta.get("name") if isinstance(meta, dict) else None
                                            if not name:
                                                continue
                                            del_url = f"{list_url}/{name}"
                                            try:
                                                dresp = _req.delete(del_url, headers=headers, timeout=3, verify=verify)
                                                if dresp.status_code < 300:
                                                    removed_shim += 1
                                            except Exception:
                                                continue
                                    except Exception:
                                        continue
                        except Exception:
                            shim_reachable = False
                        if shim_reachable and removed_shim:
                            logger.info("labs reset removed %s shim objects via shim API in namespace %s", removed_shim, ns)
                        if shim_reachable and not removed_shim:
                            logger.info(
                                "labs reset shim API reachable at %s; no shim objects removed for namespace %s",
                                shim_base or "<unknown>",
                                ns,
                            )
                        if not shim_reachable:
                            logger.info(
                                "labs reset shim API unavailable; falling back to direct DB cleanup for namespace %s",
                                ns,
                            )
                            import os as _os
                            from pathlib import Path as _Path

                            from ae.apishim.store import ObjectStore as _ObjectStore

                            dsn = _os.getenv("AE_APISHIM_DSN")
                            db_path = _os.getenv("AE_APISHIM_DB", "state/apishim.db")
                            store = _ObjectStore(dsn=dsn) if dsn else _ObjectStore(db_path=_Path(db_path))
                            for grp, ver, res in targets:
                                try:
                                    items = store.list(grp, ver, res, ns)
                                except Exception:
                                    continue
                                for obj in items:
                                    if store.delete(grp, ver, res, ns, obj.name):
                                        removed_shim += 1
                            if removed_shim:
                                logger.info("labs reset removed %s shim objects in namespace %s", removed_shim, ns)
                except Exception:
                    pass
                # Ensure the shim demo process is stopped so it doesn't reapply.
                try:
                    _helm_demo_stop()
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
                import time as _t
                from urllib.parse import urlparse as _urlparse

                import requests as _req

                verify_path = "state/certs/combined-dev-ca.pem"
                # Use the already-imported _os rather than os to avoid NameError
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
                # Set canary weight on existing app manifest when possible; fallback to curated example
                if self.apply_fn is None:
                    self._json_error(404, "apply not available")
                    return
                try:
                    weight = int(payload.get("weight") or 10)
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
                            if cur_rep < 2:
                                spec["replicas"] = 2
                            rollout = dict(spec.get("rollout") or {})
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
                    ro["strategy"] = "canary"
                    ro["weight"] = int(weight)
                    spec["rollout"] = ro
                    try:
                        cur_rep = int(spec.get("replicas", 1) or 1)
                    except Exception:
                        cur_rep = 1
                    if cur_rep < 2:
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
        try:
            self.wfile.write(b"retry: 1500\n\n")
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
                if s != last_serialized:
                    last_serialized = s
                    self.wfile.write(("data: " + s + "\n\n").encode("utf-8"))
                    self.wfile.flush()
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
        try:
            self.wfile.write(b"retry: 1500\n\n")
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
                        "ingress_host": s.ingress_host,
                        "ingress_path": s.ingress_path,
                    }
                sval = _json.dumps(obj)
                if sval != last_serialized:
                    last_serialized = sval
                    self.wfile.write(("data: " + sval + "\n\n").encode("utf-8"))
                    self.wfile.flush()
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
        try:
            self.wfile.write(b"retry: 1500\n\n")
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
                if s != last_serialized:
                    last_serialized = s
                    # Emit full HTML snapshot oldest-first so new events appear at the bottom
                    html = "".join(
                        f"<div class='log-entry'><code>{self._escape_html(d['created_at'])}</code> {self._escape_html(d['message'])}</div>"
                        for d in reversed(data)
                    )
                    self.wfile.write(("data: " + html + "\n\n").encode("utf-8"))
                    self.wfile.flush()
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
        try:
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while True:
                s = self.store.get_status(app)
                if s is None:
                    html = "<span class='pending'>n/a</span>"
                else:
                    ok = int(s.ready_replicas) == int(s.desired_replicas)
                    klass = "ok" if ok else "fail"
                    html = f"<span class='{klass}'>{int(s.ready_replicas)}/{int(s.desired_replicas)} ready</span>"
                if html != last_html:
                    last_html = html
                    self.wfile.write(("data: " + html + "\n\n").encode("utf-8"))
                    self.wfile.flush()
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
            "# HELP ae_overlay_configured Overlay/VIP dataplane enabled (1=yes)",
            "# TYPE ae_overlay_configured gauge",
            f"ae_overlay_configured {1 if os.getenv('AE_SERVICE_PROVIDER', '').lower() == 'overlay' and os.getenv('AE_ENABLE_SERVICE_PROXY', '0') == '1' else 0}",
        ]
        # Per-app series metadata (declared once before samples)
        lines += [
            "# HELP ae_app_desired_replicas Desired replicas per app",
            "# TYPE ae_app_desired_replicas gauge",
            "# HELP ae_app_ready_replicas Ready replicas per app",
            "# TYPE ae_app_ready_replicas gauge",
            "# HELP ae_app_live_replicas Live replicas per app",
            "# TYPE ae_app_live_replicas gauge",
            "# HELP ae_app_status One-hot app status by label {status=ready|progressing|degraded}",
            "# TYPE ae_app_status gauge",
            # Backwards/compat aliases to match earlier docs snippets
            "# HELP ae_desired_replicas Desired replicas per app (alias)",
            "# TYPE ae_desired_replicas gauge",
            "# HELP ae_ready_replicas Ready replicas per app (alias)",
            "# TYPE ae_ready_replicas gauge",
            "# HELP ae_live_replicas Live replicas per app (alias)",
            "# TYPE ae_live_replicas gauge",
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
            # Demo filter: keep only apps registered under the demo scope
            statuses = _filter_statuses_for_demo(statuses)
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
                lines.append(f'ae_app_desired_replicas{{app="{app}"}} {s0.desired_replicas}')
                lines.append(f'ae_app_ready_replicas{{app="{app}"}} {s0.ready_replicas}')
                lines.append(f'ae_app_live_replicas{{app="{app}"}} {s0.live_replicas}')
                # Aliases used by playground docs examples
                lines.append(f'ae_desired_replicas{{app="{app}"}} {s0.desired_replicas}')
                lines.append(f'ae_ready_replicas{{app="{app}"}} {s0.ready_replicas}')
                lines.append(f'ae_live_replicas{{app="{app}"}} {s0.live_replicas}')
                # one-hot app status metric
                st = (s0.revision_status or "").strip().lower()
                for name in ("ready", "progressing", "degraded"):
                    val = 1 if st == name else 0
                    lines.append(f'ae_app_status{{app="{app}",status="{name}"}} {val}')
            for s0 in statuses:
                reps = self.store.list_replicas(s0.app_name)
                for r in reps:
                    val = 1 if r.ready else 0
                    lines.append(
                        f'ae_replica_ready{{app="{s0.app_name}",replica="{r.replica_id}"}} {val}'
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
                lines.append(f'ae_node_stale{{{labels}}} {"1" if stale else "0"}')
                if last_age is not None:
                    lines.append(f'ae_node_last_seen_seconds{{{labels}}} {last_age}')
        except Exception:
            pass
        # Service/VIP metrics
        try:
            from collections import defaultdict

            services = self.store.list_services()
            for svc in services:
                labels = f'app="{svc.app_name}",cluster_ip="{svc.cluster_ip}"'
                lines.append(f'ae_service_info{{{labels}}} 1')
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
                        lines.append(f'ae_overlay_peers {int(peers)}')
                    hs = ov.get("latest_handshake_seconds")
                    if hs is not None:
                        lines.append(f'ae_overlay_latest_handshake_seconds {float(hs)}')
                    mtu = ov.get("mtu")
                    if mtu is not None:
                        lines.append(f'ae_overlay_mtu {int(mtu)}')
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
            for (app, replica, ptype), secs in list(_PROBE_BACKOFF.items()):
                lines.append(
                    f'ae_probe_backoff_seconds{{app="{app}",replica="{replica}",type="{ptype}"}} {int(secs)}'
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
        # In demo mode, restrict controller app counters to allowed apps from registry
        try:
            demo_allowed = set(_demo_allowed_apps())
            labs_allowed = set(_LABS_APPS)
            allowed_apps = demo_allowed | labs_allowed
            if allowed_apps:
                ctrl["apps"] = {k: v for k, v in ctrl["apps"].items() if k in allowed_apps}
        except Exception:
            pass
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

        # Crashloop flags snapshot (apps with active TTL)
        try:
            from time import time as _now

            now = float(_now())
            crash = {app: (float(until) > now) for app, until in list(_APP_CRASHLOOP_UNTIL.items())}
        except Exception:
            crash = {}
        payload = {"controller": ctrl, "rbac": rbac, "crashloop": crash, **(extra or {})}
        self._json_ok(payload)

    def _handle_swagger(self) -> None:
        html = """
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>k1s Swagger UI</title>
    <link rel=\"stylesheet\" href=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui.css\" />
    <style>body { margin: 0; } .swagger-ui { max-width: 1100px; margin: 20px auto; }</style>
  </head>
  <body>
    <div id=\"swagger-ui\"></div>
    <script src=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js\"></script>
    <script type=\"text/javascript\">
      window.ui = SwaggerUIBundle({ url: '/openapi.json', dom_id: '#swagger-ui' });
    </script>
  </body>
</html>
"""
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_redoc(self) -> None:
        html = """
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>k1s ReDoc</title>
    <style>body { margin: 0; } #redoc { width: 100vw; height: 100vh; }</style>
    <script src=\"https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js\"></script>
  </head>
  <body>
    <div id=\"redoc\"></div>
    <script>
      document.addEventListener('DOMContentLoaded', function () {
        Redoc.init('/openapi.json', {}, document.getElementById('redoc'));
      });
    </script>
  </body>
</html>
"""
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
            "info": {"title": "k1s Controller API", "version": "0.1.0"},
            "components": {
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
                },
                "schemas": {
                    "AppStatus": {
                        "type": "object",
                        "properties": {
                            "app_name": {"type": "string"},
                            "desired_replicas": {"type": "integer"},
                            "ready_replicas": {"type": "integer"},
                            "live_replicas": {"type": "integer"},
                            "revision": {"type": "integer"},
                            "revision_status": {"type": "string"},
                            "image": {"type": "string"},
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

        statuses = self.store.list_status()
        # Demo filter: keep only apps registered under the demo scope
        statuses = _filter_statuses_for_demo(statuses)
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
            item = {
                "app_name": s.app_name,
                "desired_replicas": s.desired_replicas,
                "ready_replicas": s.ready_replicas,
                "live_replicas": s.live_replicas,
                "revision": s.revision,
                "revision_status": s.revision_status,
                "image": s.image,
                "ingress_host": s.ingress_host,
                "ingress_path": s.ingress_path,
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
                    "labels": node.labels,
                    "taints": node.taints,
                    "pod_cidr": node.pod_cidr,
                    "wg_pubkey": node.wg_pubkey,
                    "cordoned": bool(getattr(node, "cordoned", False)),
                    "status": st,
                    "seen_at": seen_at.isoformat() if seen_at else None,
                    "stale": stale,
                }
            )
        self._json_ok({"nodes": items, "count": len(items), "stale_after_seconds": grace})

    def _handle_status_single(self, app: str) -> None:
        # Support optional query on the path segment (e.g., "<app>?details=1")
        import urllib.parse as _up

        if "?" in app:
            app, query = app.split("?", 1)
            params = _up.parse_qs(query)
        else:
            params = {}
        s = self.store.get_status(app)
        if s is None:
            self.send_response(404)
            self.end_headers()
            return
        data = {
            "app_name": s.app_name,
            "desired_replicas": s.desired_replicas,
            "ready_replicas": s.ready_replicas,
            "live_replicas": s.live_replicas,
            "revision": s.revision,
            "revision_status": s.revision_status,
            "image": s.image,
            "ingress_host": s.ingress_host,
            "ingress_path": s.ingress_path,
        }
        # If details requested, include manifest and replica summaries
        want_details = str(params.get("details", ["0"])[0]).lower() in {"1", "true", "yes"}
        if want_details:
            try:
                manifest = self.store.get_revision_manifest(s.app_name, s.revision)
                reps = self.store.list_replicas(s.app_name)
                data["manifest"] = manifest.model_dump()
                data["replicas"] = [
                    {
                        "replica_id": r.replica_id,
                        "ready": bool(r.ready),
                        "live": bool(r.live),
                        "status": r.status,
                        "readiness_message": r.readiness_message,
                        "liveness_message": r.liveness_message,
                    }
                    for r in reps
                ]
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

    def _json_error(self, code: int, message: str) -> None:
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _fmt: str, *_args):  # quiet
        return

    def _handle_docs(self) -> None:
        html = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>k1s API Docs</title>
    <style>
      body { font-family: sans-serif; margin: 2rem; }
      code { background:#f3f3f3; padding: 2px 4px; }
      .path { font-weight: bold; }
    </style>
  </head>
  <body>
    <h1>k1s Controller API</h1>
    <p>Minimal, read-only endpoints. OpenAPI at <code>/openapi.json</code>.</p>
    <div id="endpoints">Loading...</div>
    <script>
      fetch('/openapi.json').then(r => r.json()).then(doc => {
        const container = document.getElementById('endpoints');
        container.innerHTML = '';
        const paths = doc.paths || {};
        Object.keys(paths).sort().forEach(p => {
          const div = document.createElement('div');
          div.innerHTML = `<div class="path">GET <code>${p}</code></div>`;
          container.appendChild(div);
        });
      }).catch(() => {
        document.getElementById('endpoints').textContent = 'Failed to load OpenAPI';
      });
    </script>
  </body>
</html>
"""
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

    def _handle_dashboard(self) -> None:
        # Simple static dashboard that polls status, events, and logs.
        html = """
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>k1s Demo Dashboard</title>
    <link rel=\"icon\" href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='28' fill='%234f46e5'/%3E%3Ctext x='32' y='40' font-size='28' text-anchor='middle' fill='white' font-family='sans-serif'%3Ek%3C/text%3E%3C/svg%3E\" />
    <script src=\"https://unpkg.com/htmx.org@1.9.12\" integrity=\"sha384-ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2\" crossorigin=\"anonymous\"></script>
    <style>
      :root { color-scheme: light dark; --header-h: 60px; }
      body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; overflow-x:hidden; }
      header { display:flex; align-items:center; justify-content:space-between; padding:10px 14px; background:#0a0a0a10; position:sticky; top:0; backdrop-filter: blur(4px); z-index: 40; }
      h1 { margin:0; font-size: 18px; }
      main { display:grid; grid-template-columns: 1fr; gap:12px; padding:12px 12px 48px; overflow-x:hidden; align-items:start; margin-left:222px; transition: margin-left .15s ease; }
      #detail { min-width:0; overflow:hidden; }
      #apps { width:210px; }
      /* Collapsible left apps pane */
      body.apps-collapsed main { margin-left: 22px; }
      /* Apps rail fixed under the header; independent scroll */
      #apps { position:fixed; left:0; top: calc(var(--header-h, 60px)); border-right:1px solid #8884; padding:0 8px 0 0; min-height:0; height: calc(100vh - var(--header-h, 60px)); background: transparent; box-sizing: border-box; }
      body.apps-collapsed #apps { border-right:0; padding-right:0; }
      .scrollbar-hide { scrollbar-width: none; -ms-overflow-style: none; }
      .scrollbar-hide::-webkit-scrollbar { width:0; height:0; }
      #apps-list { display:block; overflow-y:auto; height: calc(100vh - (var(--header-h, 60px)) - 12px); padding:6px 6px 12px; }
      body.apps-collapsed #apps-list { display:none; }
      .ns-header { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:#94a3b8; display:flex; align-items:center; gap:6px; margin:8px 6px 4px; }
      .ns-header .ns-dot { width:8px; height:8px; border-radius:999px; background:var(--ns-color, #64748b); box-shadow:0 0 0 2px rgba(0,0,0,.25); }
      .ns-header .ns-count { margin-left:auto; font-size:11px; opacity:.7; }
      .app { padding:6px 8px; border-radius:6px; cursor:pointer; border-left:3px solid var(--ns-color, #334155); background:linear-gradient(90deg, var(--ns-tint, transparent), transparent 70%); margin:2px 0; }
      .app.active { background:linear-gradient(90deg, var(--ns-tint, rgba(31,41,55,.65)), #1f2937 70%); color:#e5e7eb; }
      .app-title { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
      .app-sub { font-size:12px; color:#94a3b8; }
      .pill { display:inline-block; padding:1px 6px; border-radius:999px; font-size:12px; margin-left:6px; }
      .ns-pill { display:inline-block; padding:1px 6px; border-radius:999px; font-size:12px; border:1px solid var(--ns-color, #64748b); background:var(--ns-tint, #33415533); color:#e2e8f0; }
      .ok { background:#16a34a33; color:#16a34a; }
      .warn { background:#f59e0b33; color:#b45309; }
      .bad { background:#ef444433; color:#b91c1c; }
      .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
      .row.stretch { align-items: stretch; flex-wrap: nowrap; overflow-x: hidden; }
      .row.stretch::-webkit-scrollbar { height:0; }
      .card { border:1px solid #8884; border-radius:8px; padding:8px 10px; min-width:0; max-width:100%; overflow:hidden; }
      .card table { display:block; overflow:auto; white-space: nowrap; scrollbar-width: none; -ms-overflow-style: none; }
      .card table::-webkit-scrollbar { width:0; height:0; }
      .card pre { overflow:auto; }
      /* Ensure flex children can shrink and let inner boxes scroll */
      .row.stretch > .card { min-width: 0; }
      .detail-card { flex: 0 0 320px; }
      /* Reduce default Logs panel height by ~20% (294px → ~235px) */
      .logbox { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; width:100%; box-sizing:border-box; height:235px; overflow:auto; overflow-x:auto; white-space: pre; background:#0001; padding:8px; border-radius:6px; }
      .scrollcap { max-height:180px; overflow:auto; overflow-x:auto; white-space: normal; max-width:100%; width:100%; }
      /* Ensure detail text uses compact line spacing */
      #desc { line-height: 1.3; min-height: 12em; max-height: none; overflow-y: hidden; }
      /* Match events panel baseline height to keep row stable */
      #events { min-height: 12em; }
      .log-entry { white-space: pre; }
      /* Ensure events panel never widens the layout; scroll inside */
      #events { max-width:100%; width:100%; overflow-x:auto; }
      .log-entry code { opacity:0.8; margin-right:6px; }
      #controls { gap:8px; }
      /* Unified rounded controls */
      input[type=text], input[type=password], select, textarea { padding:6px; border:1px solid #8884; border-radius:6px; background:#0001; color:inherit; }
      /* All selects — dark UI: light text on dark background */
      select { background:#0f172a; color:#e5e7eb; border-color:#334155; color-scheme: dark; }
      select option { background:#0f172a; color:#e5e7eb; }
      button { padding:6px 10px; border:1px solid #8884; border-radius:6px; background:#0001; color:inherit; cursor:pointer; }
      /* Hide legacy header toggle and add pane handle */
      header #apps-toggle { display:none !important; }
      /* Small round chevron handle; positioned by JS for exact edge alignment */
      #apps-pane-toggle { position:fixed; left: 0; top: 50vh; transform: translateY(-50%); width:28px; height:28px; border-radius:9999px; padding:0; border:1px solid #334155; background:#0f172a; color:#e5e7eb; display:flex; align-items:center; justify-content:center; box-shadow:0 2px 6px rgba(0,0,0,.2); cursor:pointer; z-index: 10; transition: left .12s ease, top .12s ease; }
      /* When collapsed, keep the handle fully within the thin bar */
      /* Collapsed state handled in JS so the button centers on the 16px rail */
      #apps-pane-toggle:hover { background:#111827; }
      #apps-pane-toggle svg { width:18px; height:18px; transition: transform .15s ease; }
      /* Chevron points left (collapse) when expanded; rotate to point right when collapsed */
      body.apps-collapsed #apps-pane-toggle svg { transform: rotate(180deg); }
      table { border-collapse:collapse; width:100%; }
      th, td { border-bottom:1px solid #8884; padding:6px; text-align:left; font-size:13px; }
      code { background:#0001; padding:2px 4px; border-radius:4px; }
      h2 { font-size:14px; margin: 14px 4px 6px; opacity:0.9; }
      .divider { border-top:1px solid #8884; margin:16px 0; }
      footer.site-footer { margin: 0 12px 12px; border-top:1px solid #8884; padding-top:10px; opacity:.85; }
      .hover-card { position:absolute; display:none; max-width:280px; font-size:12px; line-height:1.35; background:rgba(255,255,255,0.95); color:inherit; border:1px solid #888; border-radius:6px; padding:8px 10px; box-shadow:0 2px 8px rgba(0,0,0,0.1); pointer-events:none; z-index: 1000; }
      @media (prefers-color-scheme: dark) { .hover-card { background:rgba(17,17,17,0.9); border-color:#555; } }
      h2 { font-size:14px; margin: 14px 4px 6px; opacity:0.9; }
      .divider { border-top:1px solid #8884; margin:16px 0; }
    </style>
  </head>
  <body>
    <header>
      <div class=\"row\" style=\"gap:10px; align-items:center;\">
        <button id=\"apps-toggle\" class=\"icon-btn\" title=\"Collapse apps pane\" aria-pressed=\"false\" aria-label=\"Toggle apps panel\">
          <svg viewBox=\"0 0 24 24\" fill=\"currentColor\" aria-hidden=\"true\"><path d=\"M15.41 7.41 14 6l-6 6 6 6 1.41-1.41L10.83 12z\"/></svg>
        </button>
        <h1>k1s Demo Dashboard</h1>
      </div>
      <div class=\"row\" id=\"controls\"> 
        <label>Poll <select id=\"poll-interval\">
          <option value=\"0\">off</option>
          <option value=\"2000\">2s</option>
          <option value=\"5000\" selected>5s</option>
          <option value=\"10000\">10s</option>
        </select></label>
        <label>Log filter <input id=\"log-filter\" name=\"filter\" type=\"text\" size=\"24\" placeholder=\"substring\" /></label>
        <button id=\"pause-btn\">Pause Logs</button>
        <label>Bearer <input id=\"auth-token\" type=\"password\" size=\"22\" placeholder=\"read/scaler/admin token\" title=\"Optional bearer token. Roles: read (GET), scaler (POST /scale), admin (mutations & rollout).\" /></label>
        <button id=\"save-token\">Save</button>
        <button id=\"clear-token\">Clear</button>
        <label title=\"Hide healthy counters (show warn/bad only)\"><input type=\"checkbox\" id=\"hide-healthy\" /> Hide Healthy</label>
        <label title=\"Hide less critical counters (services, volumes, containers, restarts)\"><input type=\"checkbox\" id=\"compact-counters\" /> Compact Counters</label>
      </div>
    </header>
    <main>
  <section id=\"apps\"><div id=\"apps-list\" class=\"scrollbar-hide\"></div><button id=\"apps-pane-toggle\" title=\"Collapse apps pane\" aria-pressed=\"false\" aria-label=\"Toggle apps panel\"><svg viewBox=\"0 0 24 24\" fill=\"currentColor\" aria-hidden=\"true\"><circle cx=\"12\" cy=\"12\" r=\"0\" fill=\"none\"/><path d=\"M15.41 7.41 14 6l-6 6 6 6 1.41-1.41L10.83 12z\"/></svg></button></section>
      <section id=\"detail\">
        <h2>Application</h2>
        <div class=\"row stretch\">
          <div class=\"card detail-card\" style=\"display:flex; flex-direction:column;\">
            <div id=\"desc\" class=\"scrollcap scrollbar-hide\">
            <div><strong>App:</strong> <span id=\"d-app\">-</span></div>
            <div><strong>Namespace:</strong> <span id=\"d-namespace\">-</span></div>
            <div><strong>Image:</strong> <span id=\"d-image\">-</span></div>
            <div><strong>Ingress:</strong> <span id=\"d-ingress\">-</span></div>
            <div><strong>Replicas:</strong> <span id=\"d-replicas\">-</span></div>
            <div><strong>Revision:</strong> <span id=\"d-rev\">-</span> (<span id=\"d-rev-status\">-</span>)</div>
            <div><strong>Service:</strong> <span id=\"d-service\">-</span></div>
            <div><strong>Rollout:</strong> <span id=\"d-rollout\">-</span></div>
            <div><strong>Secrets:</strong> <span id=\"d-secrets\">-</span></div>
            <div><strong>Storage:</strong> <span id=\"d-storage\">-</span></div>
            </div>
          </div>
          <div class=\"card\" style=\"flex:1; display:flex; flex-direction:column;\">
            <strong>Events</strong>
            <div class=\"scrollcap scrollbar-hide\" id=\"events\"></div>
          </div>
        </div>
        <!-- Logs directly below Application/Events -->
        <div class=\"card\" style=\"margin-top:12px;\">
          <strong>Logs</strong>
          <div id=\"logs\" class=\"logbox scrollbar-hide\" style=\"height:235px;\" hx-get=\"/dashboard/partials/logs\" hx-trigger=\"load, every 5s, refresh\" hx-include=\"#log-filter\" hx-swap=\"innerHTML\" hx-on::after-settle=\"this.scrollTop=this.scrollHeight\"></div>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\"> 
          <strong>System</strong>
          <div class=\"row\" id=\"sys-counters\" style=\"gap:10px; margin-top:6px; flex-wrap:wrap;\"></div>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\">
          <strong>Probe History</strong>
          <div id=\"probe-history\" class=\"scrollcap scrollbar-hide\" style=\"max-height:220px;\"
               hx-get=\"/dashboard/partials/probe-history\"
               hx-trigger=\"load, every 10s, refresh\"
               hx-include=\"#app-select\"
               hx-swap=\"innerHTML\"></div>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\">
          <div class=\"row\" style=\"align-items:center; justify-content:space-between;\">
            <strong>Replicas</strong>
            <label title=\"Rows history count\">Show last
              <select id=\"hist-count\" style=\"margin:0 4px;\">
                <option value=\"5\" selected>5</option>
                <option value=\"10\">10</option>
                <option value=\"20\">20</option>
              </select>
              checks
            </label>
          </div>
          <table id=\"tbl-replicas\"><thead><tr><th>Replica</th><th>Ready</th><th>Live</th><th>Status</th><th>Backoff</th></tr></thead><tbody></tbody></table>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\">
          <strong>System Graph</strong>
          <div id=\"graph-wrap\" style=\"position:relative; width:100%; height:420px; margin-top:8px; background:#0001; border-radius:6px;\">
            <svg id=\"sys-graph\" viewBox=\"0 0 1000 420\" preserveAspectRatio=\"xMidYMid meet\" style=\"width:100%; height:100%;\">
              <defs>
                <marker id=\"arrow\" markerWidth=\"10\" markerHeight=\"10\" refX=\"10\" refY=\"3\" orient=\"auto\">
                  <path d=\"M0,0 L10,3 L0,6 Z\" fill=\"#9ca3af\" />
                </marker>
                <style>
                  .node text { font-size:12px; pointer-events:none; fill:#f8fafc; paint-order: stroke; stroke:rgba(0,0,0,0.65); stroke-width:2; }
                  .node .node-shape { stroke-width:1.2; }
                  .node.system .node-shape { fill:#e5e7eb; stroke:#6b7280; }
                  .node.worker .node-shape { fill:#e0f2fe; stroke:#0284c7; }
                  .node.worker.stale .node-shape { fill:#fee2e2; stroke:#ef4444; }
                  .node.worker.cordoned .node-shape { fill:#fef3c7; stroke:#f59e0b; }
                  .node.app .node-shape { fill:var(--ns-color, #3b82f6); stroke:var(--ns-color, #3b82f6); }
                  .node.app .ns-stripe { fill:var(--ns-color, #3b82f6); opacity:.35; }
                  .node.pod circle { fill:#e5e7eb; stroke:#6b7280; }
                  .label-chip { fill:rgba(8,12,18,0.85); stroke:rgba(255,255,255,0.18); stroke-width:0.8; }
                  .node.pod.ready circle { fill:#dcfce7; stroke:#16a34a; }
                  .node.pod.pending circle { fill:#fef3c7; stroke:#f59e0b; }
                  .link { stroke:#9ca3af; stroke-width:1.5; fill:none; marker-end:url(#arrow); }
                  .flow { stroke-dasharray:6 6; }
                  .flow-fwd { animation: flow 1.6s linear infinite; }
                  .flow-rev { animation: flow 1.6s linear infinite reverse; }
                  .selected .node-shape, .selected circle { stroke-width:2.4 !important; filter: drop-shadow(0 0 2px #60a5fa); }
                  .selected.link { stroke:#2563eb; }
                  .faded { opacity:0.35; }
                  @keyframes flow { to { stroke-dashoffset: -24; } }
                </style>
              </defs>
              <g id=\"links\"></g>
              <g id=\"nodes\"></g>
            </svg>
            <div id=\"graph-legend\" style=\"position:absolute; right:8px; top:8px; background:rgba(17,24,39,0.82); color:#e5e7eb; padding:6px 8px; border-radius:6px; border:1px solid #334155; backdrop-filter: blur(2px); font-size:12px;\">
              <div style=\"display:flex; gap:10px; align-items:center; flex-wrap:wrap;\">
                <span><svg width=\"14\" height=\"14\"><rect x=\"1\" y=\"1\" width=\"12\" height=\"12\" rx=\"3\" fill=\"#e5e7eb\" stroke=\"#6b7280\"/></svg> System</span>
                <span><svg width=\"14\" height=\"14\"><rect x=\"1\" y=\"1\" width=\"12\" height=\"12\" rx=\"3\" fill=\"#dbeafe\" stroke=\"#3b82f6\"/></svg> App</span>
                <span><svg width=\"14\" height=\"14\"><circle cx=\"7\" cy=\"7\" r=\"5\" fill=\"#dcfce7\" stroke=\"#16a34a\"/></svg> Pod ready</span>
                <span><svg width=\"14\" height=\"14\"><circle cx=\"7\" cy=\"7\" r=\"5\" fill=\"#fef3c7\" stroke=\"#f59e0b\"/></svg> Pod pending</span>
                <span><svg width=\"30\" height=\"8\"><path d=\"M1 4 L22 4\" stroke=\"#9ca3af\" stroke-width=\"1.5\" stroke-dasharray=\"6 6\"/><polygon points=\"22,1 29,4 22,7\" fill=\"#9ca3af\"/></svg> Flow</span>
                <span><button id=\"graph-path-toggle\" style=\"font-size:12px; padding:4px 8px; border:1px solid #4b5563; border-radius:6px; background:#111827; color:#e5e7eb; cursor:pointer;\">Paths: Orth</button></span>
              </div>
            </div>
            <div id=\"graph-hover\" class=\"hover-card\"></div>
          </div>
        </div>
        <div class=\"row stretch\" style=\"margin-top:12px; gap:12px;\"> 
          <div class=\"card detail-card\" style=\"flex:0 0 360px;\">
            <strong>Plan Diagnostics</strong>
            <div style=\"font-size:12px; opacity:.9; margin:6px 0;\">Paste an App manifest (YAML or JSON) to preview warnings and diagnostics before applying. Or use the button to load the selected app's last applied manifest.</div>
            <form id=\"plan-form\" onsubmit=\"return false;\">
              <div><textarea id=\"plan-json\" rows=\"12\" cols=\"40\" placeholder=\"Paste App manifest YAML or JSON here...\"></textarea></div>
              <div class=\"row\" style=\"margin-top:6px\">
                <button type=\"button\" id=\"plan-run\">Run Plan</button>
                <button type=\"button\" id=\"plan-load\">Load from App</button>
                <span id=\"plan-status\" class=\"pill\" style=\"margin-left:8px\"></span>
              </div>
            </form>
          </div>
          <div class=\"card\" style=\"flex:1;\"> 
            <div class=\"row\" style=\"justify-content:space-between; align-items:center;\">
              <strong>Plan Result</strong>
              <button type=\"button\" id=\"plan-copy\" title=\"Copy JSON to clipboard\">Copy</button>
            </div>
            <pre id=\"plan-output\" class=\"logbox scrollbar-hide\" style=\"height:280px\"></pre>
          </div>
        </div>
        
        <div class=\"divider\"></div>
        <h2>Ingress, Services & Storage</h2>
        <div class=\"row stretch\" style=\"margin-top:12px;\">
          <div class=\"card\" style=\"flex:1;\">
            <strong>Services</strong>
            <table id=\"tbl-services\"><thead><tr><th>App</th><th>Port</th><th>Target</th><th>Replicas</th></tr></thead><tbody></tbody></table>
          </div>
          <div class=\"card\" style=\"flex:1;\">
            <strong>Nodes</strong>
            <table id=\"tbl-nodes\"><thead><tr><th>Name</th><th>Status</th><th>Cordoned</th><th>Last Seen (s)</th></tr></thead><tbody></tbody></table>
          </div>
          <div class=\"card\" style=\"flex:1;\">
            <strong>Ingress</strong>
            <table id=\"tbl-ingress\"><thead><tr><th>App</th><th>Host</th><th>Config Path</th><th>Exists</th></tr></thead><tbody></tbody></table>
          </div>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\">
          <strong>Storage Volumes</strong>
          <table id=\"tbl-vols\"><thead><tr><th>Name</th><th>App</th><th>Driver</th><th>Mountpoint</th></tr></thead><tbody></tbody></table>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\">
          <strong>Runtime Containers</strong>
          <table id=\"tbl-containers\"><thead><tr><th>Name</th><th>App</th><th>Host Ports</th><th>Restarts</th><th>Restarts (1m)</th></tr></thead><tbody></tbody></table>
        </div>
      </section>
    </main>
    <footer class=\"site-footer\"> 
      <div class=\"row\" style=\"justify-content:space-between;\">
        <span>k1s Demo Dashboard</span>
        <span id=\"build-ts\"></span>
      </div>
    </footer>
    <script>
      (function(){
        try { document.getElementById('build-ts').textContent = new Date().toLocaleString(); } catch (e) {}
      })();
    </script>
    <script>
      var elApps = document.getElementById('apps');
      var elAppsList = document.getElementById('apps-list');
      var elEvents = document.getElementById('events');
      var pollSel = document.getElementById('poll-interval');
      var logFilter = document.getElementById('log-filter');
      var pauseBtn = document.getElementById('pause-btn');
      var appsToggle = document.getElementById('apps-pane-toggle');
      var current = null;
      var pollTimer = null;
      var pauseLogs = false;
      var logSource = null;
      var eventsSource = null;
      var lastSystem = null;
      var lastStatuses = [];
      var graphHover = null;
      var graphPathMode = (localStorage.getItem('graph_path_mode') || 'orth'); // 'orth' or 'straight'
      // Keep apps rail aligned under the header
      function syncHeaderHeight(){
        try {
          var hdr = document.querySelector('header');
          if (!hdr) return;
          var apps = document.getElementById('apps');
          var list = document.getElementById('apps-list');
          var h = hdr.getBoundingClientRect().height || hdr.offsetHeight || 60;
          document.documentElement.style.setProperty('--header-h', (h) + 'px');
          // Fixed rail already uses CSS calc with var(--header-h); no per-element top needed
          if (list) {
            var listHeight = Math.max(120, Math.floor(window.innerHeight - h - 12));
            list.style.height = listHeight + 'px';
          }
        } catch(e){}
      }
      try { syncHeaderHeight(); window.addEventListener('resize', syncHeaderHeight); } catch(e){}

      // Token helpers
      var tokInput = document.getElementById('auth-token');
      var saveBtn = document.getElementById('save-token');
      var clearBtn = document.getElementById('clear-token');
      try { tokInput.value = localStorage.getItem('ae_token') || ''; } catch(e) {}
      saveBtn.addEventListener('click', function(){ try { localStorage.setItem('ae_token', tokInput.value||''); } catch(e){}; window.location.reload(); });
      clearBtn.addEventListener('click', function(){ try { localStorage.removeItem('ae_token'); } catch(e){}; window.location.reload(); });

      // UI prefs: hide healthy pills / compact counters
      var hideHealthy = false, compactCounters = false;
      var hhStored = '', ccStored = '';
      try { hhStored = (localStorage.getItem('ae_hide_healthy')||''); } catch(e){}
      try { ccStored = (localStorage.getItem('ae_compact_counters')||''); } catch(e){}
      try { hideHealthy = (hhStored==='1'); } catch(e){}
      try { compactCounters = (ccStored==='1'); } catch(e){}
      // Mobile default: if no stored pref, default hideHealthy/compactCounters on small screens
      try {
        var isMobile = (window.matchMedia && window.matchMedia('(max-width: 640px)').matches) || (Math.min(screen.width, screen.height) <= 640);
        if (!hhStored && isMobile) hideHealthy = true;
        if (!ccStored && isMobile) compactCounters = true;
      } catch(e){}
      try { var hh = document.getElementById('hide-healthy'); if (hh) { hh.checked = hideHealthy; hh.addEventListener('change', function(){ try{ localStorage.setItem('ae_hide_healthy', hh.checked?'1':''); }catch(e){}; renderCounters(lastSystem||{}); }); } } catch(e){}
      try { var cc = document.getElementById('compact-counters'); if (cc) { cc.checked = compactCounters; cc.addEventListener('change', function(){ try{ localStorage.setItem('ae_compact_counters', cc.checked?'1':''); }catch(e){}; renderCounters(lastSystem||{}); }); } } catch(e){}

      // Apps pane collapse/expand (expanded by default)
      try {
        var initCollapsed = (localStorage.getItem('dash_apps_collapsed')||'') === '1';
        if (initCollapsed) {
          document.body.classList.add('apps-collapsed');
          appsToggle.setAttribute('aria-pressed','true');
          appsToggle.title='Expand apps pane';
          try { requestAnimationFrame(positionAppsToggle); } catch(e){}
        }
        appsToggle.addEventListener('click', function(){
          var collapsed = document.body.classList.toggle('apps-collapsed');
          try { localStorage.setItem('dash_apps_collapsed', collapsed ? '1' : ''); } catch(e){}
          appsToggle.setAttribute('aria-pressed', collapsed ? 'true' : 'false');
          appsToggle.title = collapsed ? 'Expand apps pane' : 'Collapse apps pane';
          try { requestAnimationFrame(function(){ requestAnimationFrame(positionAppsToggle); }); } catch(e){}
        });
      } catch(e){}

      // Precisely position the chevron relative to the apps pane (account for collapse/expand)
      function positionAppsToggle(){
        try {
          var pane = document.getElementById('apps');
          var btn = document.getElementById('apps-pane-toggle');
          if (!pane || !btn) return;
          var rect = pane.getBoundingClientRect();
          var isCollapsed = document.body.classList.contains('apps-collapsed');
          // Horizontal target (center the 28px button explicitly since we removed translateX):
          //  - Expanded: hug the pane's right edge (inset 12px)
          //  - Collapsed: center on the 16px rail, then nudge left by ~50px per feedback
          var btnW = (btn.getBoundingClientRect().width || 28);
          // Collapsed: place handle at a fixed gutter (visual spec: ~8px from viewport left).
          // Expanded: hug apps pane right edge minus 12px (minus half button width to center).
          var targetX = isCollapsed
            ? (8 - btnW / 2)  // center the button at an 8px gutter
            : (rect.right - 12 - btnW / 2);
          btn.style.left = Math.round(targetX) + 'px';
          // Let CSS keep the vertical centering via top:50vh; avoid JS scroll offset bugs.
        } catch(_){}
      }
      // Initial placement and on interactions
      try { positionAppsToggle(); } catch(_){}
      window.addEventListener('scroll', positionAppsToggle, { passive: true });
      window.addEventListener('resize', positionAppsToggle);

      // Removed dynamic apps pane sizer; rely on CSS max-height.

      pauseBtn.addEventListener('click', function () {
        pauseLogs = !pauseLogs;
        pauseBtn.textContent = pauseLogs ? 'Resume Logs' : 'Pause Logs';
        updateLogsHTMX();
        updateLogStreaming();
      });
      pollSel.addEventListener('change', function () { schedulePoll(); updateLogsHTMX(); });

      function badge(cls, text){ return '<span class="pill ' + cls + '">' + text + '</span>'; }

      function splitAppName(full){
        var s = String(full || '');
        var idx = s.indexOf('--');
        if (idx > 0) {
          return { namespace: s.slice(0, idx), name: s.slice(idx + 2) };
        }
        return { namespace: 'default', name: s };
      }

      function hashHue(str){
        var h = 0;
        for (var i = 0; i < str.length; i++) {
          h = (h * 31 + str.charCodeAt(i)) % 360;
        }
        return h;
      }

      function namespaceColors(ns){
        var n = (ns && String(ns)) ? String(ns) : 'default';
        if (n === 'default') {
          return { color: '#64748b', tint: 'rgba(100,116,139,0.18)' };
        }
        var hue = hashHue(n);
        return { color: 'hsl(' + hue + ', 70%, 55%)', tint: 'hsla(' + hue + ', 70%, 20%, 0.18)' };
      }

      function clamp(n, lo, hi){ return Math.min(hi, Math.max(lo, n)); }

      function parseHslColor(str){
        var m = String(str || '').match(/hsla?\(\s*([0-9.]+)\s*,\s*([0-9.]+)%\s*,\s*([0-9.]+)%/i);
        if (!m) return null;
        return { h: parseFloat(m[1]), s: parseFloat(m[2]), l: parseFloat(m[3]) };
      }

      function hexToRgb(str){
        var hex = String(str || '').trim().replace(/^#/, '');
        if (hex.length === 3){
          hex = hex[0]+hex[0] + hex[1]+hex[1] + hex[2]+hex[2];
        }
        if (hex.length !== 6) return null;
        var num = parseInt(hex, 16);
        if (Number.isNaN(num)) return null;
        return { r: (num >> 16) & 255, g: (num >> 8) & 255, b: num & 255 };
      }

      function rgbToHsl(r, g, b){
        r /= 255; g /= 255; b /= 255;
        var max = Math.max(r, g, b), min = Math.min(r, g, b);
        var h = 0, s = 0, l = (max + min) / 2;
        if (max !== min){
          var d = max - min;
          s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
          switch (max) {
            case r: h = (g - b) / d + (g < b ? 6 : 0); break;
            case g: h = (b - r) / d + 2; break;
            case b: h = (r - g) / d + 4; break;
          }
          h *= 60;
        }
        return { h: h, s: s * 100, l: l * 100 };
      }

      function shadeHsl(color, deltaL){
        var hsl = parseHslColor(color);
        if (!hsl){
          var rgb = String(color || '').trim().startsWith('#') ? hexToRgb(color) : null;
          if (rgb) hsl = rgbToHsl(rgb.r, rgb.g, rgb.b);
        }
        if (!hsl) return String(color || '');
        var l = clamp(hsl.l + deltaL, 0, 100);
        return 'hsl(' + Math.round(hsl.h) + ', ' + Math.round(hsl.s) + '%, ' + Math.round(l) + '%)';
      }

      function namespaceGradient(ns){
        var c = namespaceColors(ns);
        return {
          top: shadeHsl(c.color, 8),
          bottom: shadeHsl(c.color, -8)
        };
      }

      function renderNamespacePill(ns){
        var c = namespaceColors(ns);
        return '<span class="ns-pill" style="--ns-color:' + c.color + '; --ns-tint:' + c.tint + ';">' + escapeHtml(ns) + '</span>';
      }

      function fetchJSON(path){
        return fetch(path, {headers: authHeaders()}).then(function(r){
          if(!r.ok) return r.text().then(function(t){ throw new Error(t); });
          return r.json();
        });
      }

      function authHeaders(){
        var tok = localStorage.getItem('ae_token') || '';
        return tok ? { 'Authorization': 'Bearer ' + tok } : {};
      }

      // Clear detail panels/logs when the selected app disappears
      function clearDetailPanels(){
        try { document.getElementById('d-app').textContent = '-'; } catch(e){}
        try { document.getElementById('d-namespace').textContent = '-'; } catch(e){}
        try { document.getElementById('d-image').textContent = '-'; } catch(e){}
        try { document.getElementById('d-ingress').textContent = '-'; } catch(e){}
        try { document.getElementById('d-replicas').textContent = '-'; } catch(e){}
        try { document.getElementById('d-rev').textContent = '-'; } catch(e){}
        try { document.getElementById('d-rev-status').textContent = '-'; } catch(e){}
        try { document.getElementById('d-service').textContent = '-'; } catch(e){}
        try { document.getElementById('d-rollout').textContent = '-'; } catch(e){}
        try { document.getElementById('d-secrets').textContent = '-'; } catch(e){}
        try { document.getElementById('d-storage').textContent = '-'; } catch(e){}
        try {
          var rbody = document.querySelector('#tbl-replicas tbody');
          if (rbody) rbody.innerHTML = '';
        } catch(e){}
        try { if (elEvents) elEvents.innerHTML = '<div class="log-entry">No recent events</div>'; } catch(e){}
        clearLogs();
      }

      function refreshApps(){
        return fetchJSON('/status?limit=200').then(function(data){
          if (elAppsList) elAppsList.innerHTML = '';
          var items = data.items || [];
          lastStatuses = items;
          var names = items.map(function(s){ return s.app_name; });
          var groups = {};
          items.forEach(function(s){
            var info = splitAppName(s.app_name);
            s._ns = info.namespace;
            s._short = info.name;
            if (!groups[info.namespace]) groups[info.namespace] = [];
            groups[info.namespace].push(s);
          });
          var namespaces = Object.keys(groups);
          namespaces.sort(function(a, b){
            if (a === 'default') return -1;
            if (b === 'default') return 1;
            return String(a).localeCompare(String(b));
          });
          namespaces.forEach(function(ns){
            var colors = namespaceColors(ns);
            if (elAppsList) {
              var header = document.createElement('div');
              header.className = 'ns-header';
              header.style.setProperty('--ns-color', colors.color);
              header.style.setProperty('--ns-tint', colors.tint);
              header.innerHTML = '<span class="ns-dot"></span><span class="ns-name">' + escapeHtml(ns) + '</span><span class="ns-count">' + String(groups[ns].length) + '</span>';
              elAppsList.appendChild(header);
            }
            groups[ns].sort(function(a, b){
              return String(a._short || a.app_name).localeCompare(String(b._short || b.app_name));
            });
            groups[ns].forEach(function(s){
              // Use server-derived revision_status for the primary badge to avoid
              // drift with controller semantics (ready/progressing/degraded).
              var statusBadge = '';
              var rs = String(s.revision_status||'').toLowerCase();
              if (rs === 'ready') statusBadge = badge('ok','ready');
              else if (rs === 'progressing') statusBadge = badge('warn','progressing');
              else statusBadge = badge('bad','degraded');
              var div = document.createElement('div');
              div.className = 'app' + (current===s.app_name ? ' active' : '');
              div.style.setProperty('--ns-color', colors.color);
              div.style.setProperty('--ns-tint', colors.tint);
              try { div.dataset.app = s.app_name; div.dataset.ns = ns; } catch(e){}
              // Canary pill if rollout strategy is canary with weight>0
              var canary = '';
              var pausedPill = '';
              try {
                var ro = s.rollout || null;
                var w = (ro && ro.weight!=null) ? Number(ro.weight) : 0;
                if (ro && String((ro.strategy||'')).toLowerCase()==='canary' && w>0) {
                  canary = badge('warn', 'canary ' + String(w) + '%');
                }
                if (ro && ro.pause === true) {
                  pausedPill = badge('warn', 'paused');
                }
              } catch(e){}
              var crash = (lastSystem && lastSystem.crashloop && lastSystem.crashloop[s.app_name]) ? badge('bad','crashloop') : '';
              var cdsec = (lastSystem && lastSystem.cooldown && lastSystem.cooldown[s.app_name]) ? Number(lastSystem.cooldown[s.app_name]||0) : 0;
              var cd = cdsec>0 ? badge('warn','cooldown '+String(cdsec)+'s') : '';
              var displayName = s._short || s.app_name;
              var line1 = '<div class="app-title"><strong class="app-name">' + escapeHtml(displayName) + '</strong> ' + statusBadge + ' ' + canary + ' ' + pausedPill + ' ' + crash + ' ' + cd + '</div>';
              var revStatus = String(s.revision_status || '-');
              var line2 = '<div class="app-sub">' + String(s.ready_replicas) + '/' + String(s.desired_replicas) + ' ready - rev ' + escapeHtml(revStatus) + '</div>';
              div.innerHTML = line1 + line2;
              try {
                var nameEl = div.querySelector('.app-name');
                if (nameEl) nameEl.title = s.app_name;
              } catch(e){}
              div.onclick = function(){ selectApp(s.app_name); };
              if (elAppsList) elAppsList.appendChild(div);
            });
          });
          // If the currently viewed app was removed, fall back to the first available.
          if (current && names.indexOf(current) === -1) {
            current = null;
            historyCache = null;
            clearDetailPanels();
            updateLogStreaming();
            updateEventsStreaming();
          }
          if(!current && items.length){ selectApp(items[0].app_name); }
          else if(!current && !items.length){ clearDetailPanels(); }
          renderGraphIfReady();
        });
      }

      function refreshDetail(){
        if(!current) return Promise.resolve();
        return fetchJSON('/status/' + encodeURIComponent(current) + '?details=1').then(function(s){
          document.getElementById('d-app').textContent = s.app_name;
          try {
            var nsInfo = splitAppName(s.app_name);
            var nsEl = document.getElementById('d-namespace');
            if (nsEl) { nsEl.innerHTML = renderNamespacePill(nsInfo.namespace); }
          } catch(e){}
          document.getElementById('d-image').textContent = s.image || '-';
          var inh = (s.ingress_host || '-') + (s.ingress_path || '');
          document.getElementById('d-ingress').textContent = inh;
          var repText = s.ready_replicas + '/' + s.desired_replicas + ' (live ' + s.live_replicas + ')';
          if (lastSystem && lastSystem.crashloop && lastSystem.crashloop[s.app_name]) { repText += '  crashloop'; }
          try {
            var cdsec = (lastSystem && lastSystem.cooldown && lastSystem.cooldown[s.app_name]) ? Number(lastSystem.cooldown[s.app_name]||0) : 0;
            if (cdsec>0) { repText += '  cooldown ' + String(cdsec) + 's'; }
          } catch(e){}
          document.getElementById('d-replicas').textContent = repText;
          document.getElementById('d-rev').textContent = s.revision;
          document.getElementById('d-rev-status').textContent = s.revision_status;
          try {
            var man = (s.manifest || {});
            var svc = (man.spec || {}).service || null;
            var svcText = svc ? (String(svc.port) + (svc.target_port ? (' -> ' + String(svc.target_port)) : '')) : '-';
            document.getElementById('d-service').textContent = svcText;
            // Rollout config (show strategy/weight/pause when present)
            var ro = (man.spec || {}).rollout || null;
            var roText = '-';
            if (ro) {
              var strat = String(ro.strategy||'parallel');
              var weight = (ro.weight!=null) ? Number(ro.weight) : null;
              var paused = (ro.pause===true);
              if (strat==='canary') {
                roText = 'canary' + (weight!=null? (' weight '+String(weight)+'%'):'');
              } else {
                roText = strat;
              }
              if (paused) roText += ' (paused)';
            }
            document.getElementById('d-rollout').textContent = roText;
            var secRefs = ((man.spec || {}).secret_refs || []).length;
            document.getElementById('d-secrets').textContent = secRefs ? (secRefs + ' ref' + (secRefs>1?'s':'')) : '-';
            var storage = ((man.spec || {}).storage || []).map(function(v){ return v.name || ''; }).filter(Boolean);
            document.getElementById('d-storage').textContent = storage.length ? storage.join(', ') : '-';
          } catch(e) { }
          // Render replicas table with backoff countdown
          try {
            var rbody = document.querySelector('#tbl-replicas tbody');
            if (rbody) {
              var rows = (s.replicas||[]).map(function(r){
                // Extract backoff seconds from readiness/liveness messages
                function parseBackoff(msg){
                  var m = String(msg||'').match(/backoff \((\d+)s\)/);
                  return m ? Number(m[1]) : 0;
                }
                var bo = Math.max(parseBackoff(r.readiness_message), parseBackoff(r.liveness_message));
                var now = Date.now();
                var deadline = bo > 0 ? (now + bo*1000) : 0;
                var boText = bo>0 ? (String(bo)+'s') : '';
                return {
                  html: '<tr data-rid="'+escapeHtml(r.replica_id)+'" data-deadline="'+String(deadline)+'">'
                        + '<td>'+escapeHtml(r.replica_id)+'</td>'
                        + '<td>'+(r.ready?'yes':'no')+'</td>'
                        + '<td>'+(r.live?'yes':'no')+'</td>'
                        + '<td>'+escapeHtml(r.status||'')+'</td>'
                        + '<td class="bo">'+boText+'</td>'
                        + '</tr>'
                };
              });
              rbody.innerHTML = rows.map(function(x){ return x.html; }).join('');
              scheduleBackoffTick();
              attachReplicaHistoryHandlers();
            }
          } catch(e) { console.error('replica table', e); }
          // Events are now streamed via SSE; keep logs HTMX config fresh
          updateLogsHTMX();
        });
      }

      function schedulePoll(){
        if(pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        var ms = parseInt(pollSel.value, 10) || 0;
        if(ms > 0){ pollTimer = setInterval(function(){ Promise.all([refreshApps(), refreshDetail(), refreshSystem()]).catch(console.error); }, ms); }
      }

      var backoffTimer = null;
      function scheduleBackoffTick(){
        if (backoffTimer) return;
        backoffTimer = setInterval(function(){
          try {
            var rows = Array.from(document.querySelectorAll('#tbl-replicas tbody tr'));
            var now = Date.now();
            rows.forEach(function(tr){
              var td = tr.querySelector('td.bo');
              var deadline = Number(tr.getAttribute('data-deadline')||'0');
              if (!td || !deadline) { if(td) td.textContent=''; return; }
              var rem = Math.max(0, Math.floor((deadline - now)/1000));
              td.textContent = rem>0 ? (String(rem)+'s') : '';
              if (rem <= 0) tr.setAttribute('data-deadline','0');
            });
          } catch(e){}
        }, 1000);
      }

      var historyCache = null; // cache last fetched /history for current app
      function attachReplicaHistoryHandlers(){
        try {
          var tbody = document.querySelector('#tbl-replicas tbody');
          if (!tbody) return;
          Array.from(tbody.querySelectorAll('tr')).forEach(function(tr){
            tr.addEventListener('click', function(){ toggleReplicaHistory(tr); });
          });
        } catch(e){}
      }
      function toggleReplicaHistory(tr){
        try {
          var rid = tr.getAttribute('data-rid');
          // If next row is a history row, remove it
          var next = tr.nextElementSibling;
          if (next && next.classList.contains('hist')) { next.parentNode.removeChild(next); return; }
          // else insert one and populate
          var row = document.createElement('tr');
          row.className = 'hist';
          var td = document.createElement('td');
          td.colSpan = 5;
          td.textContent = 'loading…';
          row.appendChild(td);
          tr.parentNode.insertBefore(row, tr.nextSibling);
          var nSel = document.getElementById('hist-count');
          var n = 5;
          try { n = parseInt(nSel && nSel.value || '5', 10) || 5; } catch(e) { n = 5; }
          (historyCache ? Promise.resolve(historyCache) : fetchJSON('/history/' + encodeURIComponent(current) + '?limit=50'))
            .then(function(list){ historyCache = list; return list; })
            .then(function(list){
              var items = (list||[]).filter(function(h){ return String(h.replica_id||'') === String(rid||''); }).slice(0, n);
              if (!items.length) { td.innerHTML = '<div class="muted">No recent probe checks for '+escapeHtml(rid)+'</div>'; return; }
              var html = '<table class="mini"><thead><tr><th>Time</th><th>Ready</th><th>Live</th><th>R msg</th><th>L msg</th></tr></thead><tbody>'
                + items.map(function(h){ return '<tr><td>'+escapeHtml(h.check_time)+'</td><td>'+(h.ready?'yes':'no')+'</td><td>'+(h.live?'yes':'no')+'</td><td>'+escapeHtml(h.readiness_message||'')+'</td><td>'+escapeHtml(h.liveness_message||'')+'</td></tr>'; }).join('')
                + '</tbody></table>';
              td.innerHTML = html;
            })
            .catch(function(err){ td.innerHTML = '<div class="bad">history error: '+escapeHtml(String(err))+'</div>'; });
        } catch(e){}
      }

      function selectApp(name){
        current = name;
        clearLogs();
        updateLogsHTMX();
        updateLogStreaming();
        updateEventsStreaming();
        refreshDetail().then(function(){ refreshApps(); focusAppListItem(name); renderGraphIfReady(); });
      }

      function escapeHtml(s){
        s = (s === null || s === undefined) ? '' : String(s);
        return s.replace(/[&<>]/g, function(c){ return ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]); });
      }

      function updateLogsHTMX(){
        var el = document.getElementById('logs');
        if(!el) return;
        // Disable HTMX polling; we will stream via SSE. We still allow one-shot refresh events.
        var trig = 'none';
        el.setAttribute('hx-trigger', trig);
        var app = current ? encodeURIComponent(current) : '';
        var url = '/dashboard/partials/logs' + (app ? ('?app=' + app + '&tail=200') : '');
        el.setAttribute('hx-get', url);
        if (window.htmx) { window.htmx.process(el); }
      }

      function clearLogs(){ var el = document.getElementById('logs'); if (el) el.innerHTML = ''; }

      function updateLogStreaming(){
        // Close existing source
        if (logSource) { try { logSource.close(); } catch(e){} logSource = null; }
        if (pauseLogs || !current) return;
        var el = document.getElementById('logs');
        if (window.htmx && el && el.getAttribute('hx-get')) {
          // One-shot initial fill for context
          window.htmx.trigger(el, 'refresh');
        }
        var url = '/logs/' + encodeURIComponent(current) + '/stream?' + new URLSearchParams({ tail: '200' }).toString();
        try {
          var es = new EventSource(url);
          logSource = es;
          es.onmessage = function(ev){
            var line = ev.data || '';
            var filt = (logFilter.value || '').toLowerCase();
            if (filt && String(line).toLowerCase().indexOf(filt) === -1) return;
            var ts='-', msg=line; var i=line.indexOf(' ');
            if (i>18 && line.indexOf('T')>0 && line.indexOf('T')<25){ ts=line.slice(0,i); msg=line.slice(i+1); }
            var row = document.createElement('div');
            row.className='log-entry';
            row.innerHTML='<code>'+escapeHtml(ts)+'</code> '+escapeHtml(msg);
            var box = document.getElementById('logs');
            if(!box) return;
            box.appendChild(row);
            // Always follow newest line to bottom edge
            box.scrollTop = box.scrollHeight;
          };
          es.onerror = function(){ /* default retry */ };
        } catch (e) { console.error('EventSource failed', e); }
      }
      function updateEventsStreaming(){
        if (eventsSource) { try { eventsSource.close(); } catch(e){} eventsSource = null; }
        if (!current) return;
        var url = '/dashboard/sse/events?' + new URLSearchParams({ app: current, limit: '50' }).toString();
        try {
          var es = new EventSource(url);
          eventsSource = es;
          es.onmessage = function(ev){
            var list = [];
            try { list = JSON.parse(ev.data || '[]') || []; } catch(_) { list = []; }
            list = list.slice().reverse();
            elEvents.innerHTML = list.map(function(e){ return '<div class="log-entry"><code>' + e.created_at + '</code> ' + e.event_type + ' - ' + escapeHtml(e.message) + '</div>'; }).join('') || '<div class="log-entry">No recent events</div>';
            try { elEvents.scrollTop = elEvents.scrollHeight; } catch(_){}
          };
          es.onerror = function(){ /* default retry */ };
        } catch (e) { console.error('EventSource events failed', e); }
      }

      // Plan diagnostics UI
      function runPlan(){
        var txt = document.getElementById('plan-json').value || '';
        var out = document.getElementById('plan-output');
        var status = document.getElementById('plan-status');
        status.textContent = '…'; status.className='pill';
        out.textContent = '';
        var payload = null;
        try {
          if (txt.trim().startsWith('{')) {
            payload = JSON.parse(txt);
          } else if (window.jsyaml) {
            payload = window.jsyaml.load(txt);
          } else {
            payload = JSON.parse(txt);
          }
        } catch(e) { status.textContent='Invalid YAML/JSON'; status.className='pill bad'; return; }
        function postJSON(url, data){
          return fetch(url, { method: 'POST', headers: Object.assign({'Content-Type':'application/json'}, authHeaders()), body: JSON.stringify(data) })
            .then(function(r){ return r.text().then(function(t){ if(!r.ok){ throw new Error(t || ('HTTP '+r.status)); } try { return t ? JSON.parse(t) : {}; } catch(e){ throw new Error('Bad JSON'); } }); });
        }
        postJSON('/plan', payload)
          .catch(function(){ return postJSON('/dashboard/plan', payload); })
          .catch(function(){ return postJSON('/labs/plan', payload); })
          .then(function(data){ out.textContent = JSON.stringify(data, null, 2); var ok=!!data.ok; status.textContent = ok? 'ok' : 'warnings'; status.className='pill '+(ok?'ok':'warn'); })
          .catch(function(err){ status.textContent='error'; status.className='pill bad'; out.textContent=String(err); });
      }
      function loadPlanFromApp(){
        if (!current) { return; }
        // Prefer /status?details=1 (proxied by docs) to avoid /manifest proxy gaps
        fetchJSON('/status/' + encodeURIComponent(current) + '?details=1')
          .then(function(s){
            var man = (s && s.manifest) ? s.manifest : null;
            if (!man) throw new Error('no manifest on status');
            document.getElementById('plan-json').value = JSON.stringify(man, null, 2);
          })
          .catch(function(){
            // Fallback to /manifest/<app>
            return fetch('/manifest/' + encodeURIComponent(current), { headers: authHeaders() })
              .then(function(r){ if(!r.ok) throw new Error('manifest not found'); return r.json(); })
              .then(function(data){ document.getElementById('plan-json').value = JSON.stringify(data, null, 2); })
              .catch(function(err){ console.error('load manifest failed', err); });
          });
      }

      function focusAppListItem(name){
        try {
          var el = Array.from(document.querySelectorAll('#apps .app')).find(function(e){ return (e.dataset && e.dataset.app)===name; });
          if (el) { el.scrollIntoView({block:'nearest'}); }
        } catch(e){}
      }
      try { document.getElementById('plan-run').addEventListener('click', runPlan); } catch(e) {}
      try { document.getElementById('plan-load').addEventListener('click', loadPlanFromApp); } catch(e) {}
      try { document.getElementById('plan-copy').addEventListener('click', function(){
        try { navigator.clipboard.writeText(document.getElementById('plan-output').textContent || ''); } catch(e){}
      }); } catch(e) {}

      function renderCounters(sys){
        var el = document.getElementById('sys-counters');
        if(!el) return;
        var lastTs = sys.controller && sys.controller.last_reconcile_timestamp ? new Date(sys.controller.last_reconcile_timestamp*1000).toISOString() : '-';
        var lastDur = sys.controller && sys.controller.last_reconcile_duration != null ? (Number(sys.controller.last_reconcile_duration).toFixed(3) + 's') : '-';
        var ingressSites = (sys.ingress && sys.ingress.sites) ? sys.ingress.sites.length : 0;
        var services = (sys.services || []).length;
        var serviceReady = 0;
        try {
          var se = sys.service_endpoints || {};
          Object.keys(se).forEach(function(k){ serviceReady += Number(se[k].ready||0); });
        } catch(e){}
        var volumes = (sys.volumes || []).length;
        var containers = (sys.containers || []);
        var containerCount = containers.length;
        var restartSum = containers.reduce(function(acc, c){ return acc + (Number(c.restart_count||0)||0); }, 0);
        var nodes = sys.nodes || [];
        var readyNodes = nodes.filter(function(n){ return String(n.status||'').toLowerCase()==='ready' && !n.stale; }).length;
        var staleNodes = nodes.filter(function(n){ return n.stale; }).length;
        var ov = sys.overlay || {};
        var pills = [
          {k:'Last Reconcile', v:lastTs},
          {k:'Duration', v:lastDur},
          {k:'Ingress Sites', v:String(ingressSites)},
          {k:'Services', v:String(services)},
          {k:'Service Endpoints Ready', v:String(serviceReady)},
          {k:'Volumes', v:String(volumes)},
          {k:'Containers', v:String(containerCount)},
          {k:'Restarts', v:String(restartSum)},
          {k:'Nodes', v: readyNodes + ' / ' + nodes.length},
          {k:'Stale Nodes', v: String(staleNodes)},
          {k:'Overlay Peers', v: ov.peers!=null ? String(ov.peers) : '-'},
          {k:'Overlay OK', v: ov.ok ? 'yes' : 'no'},
          {k:'Mutations', v: (sys.rbac && sys.rbac.mutations_enabled) ? 'enabled' : 'disabled'},
        ];
        try {
          var docs = sys.docs || null;
          if (docs) {
            pills.push({k:'Docs', v: (docs.ok ? ('up :'+String(docs.port)) : 'down')});
          }
          var api = sys.api || null;
          if (api) { pills.push({k:'API', v: (api.ok ? ('up :'+String(api.port)) : 'down')}); }
        } catch(e){}
        el.innerHTML = pills.map(function(p){ return '<div class="pill" style="background:#0001">'+escapeHtml(p.k)+': <strong>'+escapeHtml(p.v)+'</strong></div>'; }).join('');
      }

      // Restart sparkline state (per-container ring buffers)
      var restartSeries = {}; // name -> [{t, rc}]
      function _seriesCap(){
        try {
          var ms = parseInt(pollSel.value, 10) || 0;
          if (ms <= 0) return 60; // default when manual refresh
          return Math.max(10, Math.min(120, Math.floor(60000 / ms)));
        } catch(e){ return 60; }
      }
      function _updateRestartSeries(containers){
        var now = Date.now();
        var cap = _seriesCap(); // ~last minute worth of samples
        (containers||[]).forEach(function(c){
          var name = String(c.name||'');
          if (!name) return;
          var rc = Number(c.restart_count||0)||0;
          var arr = restartSeries[name] || [];
          arr.push({t: now, rc: rc});
          if (arr.length > cap) arr = arr.slice(arr.length - cap);
          restartSeries[name] = arr;
        });
      }
      function _sparklineSVG(name){
        var arr = restartSeries[name] || [];
        var W = 60, H = 12;
        if (arr.length < 2) return '<svg width="'+W+'" height="'+H+'"></svg>';
        // Build deltas across samples
        var deltas = [];
        for (var i=1;i<arr.length;i++){ var d = Math.max(0, (arr[i].rc - arr[i-1].rc)); deltas.push(d); }
        var maxd = 0; for (var j=0;j<deltas.length;j++){ if (deltas[j]>maxd) maxd=deltas[j]; }
        // Choose color by severity
        var color = '#9ca3af'; // gray baseline
        if (maxd <= 0) color = '#9ca3af';
        else if (maxd === 1) color = '#16a34a';       // green for occasional single restarts
        else if (maxd <= 3) color = '#f59e0b';        // orange for moderate
        else color = '#ef4444';                       // red for high
        // Avoid division by zero; flat line near bottom when no restarts
        var points = [];
        for (var k=0;k<deltas.length;k++){
          var x = Math.floor(k * (W-1) / Math.max(1, deltas.length-1));
          var y;
          if (maxd <= 0){ y = H-2; }
          else { var v = deltas[k] / maxd; y = Math.max(1, H - 1 - Math.floor(v * (H-2))); }
          points.push(x+','+y);
        }
        var tip = '';
        try {
          var tail = deltas.slice(-5);
          tip = ' last deltas: ' + tail.join(', ');
        } catch(e){}
        var poly = '<polyline fill="none" stroke="'+color+'" stroke-width="1" points="'+points.join(' ')+'" />';
        return '<svg width="'+W+'" height="'+H+'"><title>'+('Restarts per sample,'+tip)+'</title>'+poly+'</svg>';
      }

      function refreshSystem(){
        return fetchJSON('/system').then(function(sys){
          lastSystem = sys;
          renderCounters(sys);
          var sbody = document.querySelector('#tbl-services tbody');
          if(sbody){ sbody.innerHTML = (sys.services||[]).map(function(s){
            return '<tr><td>'+escapeHtml(s.app||'')+'</td><td>'+(s.port!=null?escapeHtml(String(s.port)):'-')+'</td><td>'+(s.target_port!=null?escapeHtml(String(s.target_port)):'-')+'</td><td>'+(s.replicas!=null?escapeHtml(String(s.replicas)):'-')+'</td></tr>';
          }).join(''); }
          var ibody = document.querySelector('#tbl-ingress tbody');
          if(ibody){ ibody.innerHTML = ((sys.ingress&&sys.ingress.sites)||[]).map(function(r){
            return '<tr><td>'+escapeHtml(r.app||'')+'</td><td>'+escapeHtml((r.host||'')||'-')+'</td><td>'+escapeHtml(String(r.path||''))+'</td><td>'+(r.exists?'yes':'no')+'</td></tr>';
          }).join(''); }
          var nbody = document.querySelector('#tbl-nodes tbody');
          if(nbody){ nbody.innerHTML = (sys.nodes||[]).map(function(n){
            var st = String(n.status||'').toLowerCase();
            var stale = n.stale ? ' (stale)' : '';
            var age = n.last_seen_seconds!=null ? Math.round(n.last_seen_seconds) : '-';
            return '<tr><td>'+escapeHtml(n.name||n.id||'')+'</td><td>'+escapeHtml(st)+stale+'</td><td>'+(n.cordoned?'yes':'no')+'</td><td>'+age+'</td></tr>';
          }).join(''); }
          var vbody = document.querySelector('#tbl-vols tbody');
          if(vbody){ vbody.innerHTML = (sys.volumes||[]).map(function(v){
            var app = (v.labels&&v.labels['ae.app'])||'';
            return '<tr><td>'+escapeHtml(v.name||'')+'</td><td>'+escapeHtml(app)+'</td><td>'+escapeHtml(v.driver||'')+'</td><td>'+escapeHtml(v.mountpoint||'')+'</td></tr>';
          }).join(''); }
          // Update restart series, then render containers
          _updateRestartSeries(sys.containers||[]);
          var cbody = document.querySelector('#tbl-containers tbody');
          if(cbody){ cbody.innerHTML = (sys.containers||[]).map(function(c){
            var app = (c.labels&&c.labels['ae.app'])||'';
            var ports = (c.host_ports||[]).join(', ');
            var restarts = Number(c.restart_count||0)||0;
            var spark = _sparklineSVG(String(c.name||''));
            return '<tr><td>'+escapeHtml(c.name||'')+'</td><td>'+escapeHtml(app)+'</td><td>'+escapeHtml(ports)+'</td><td>'+String(restarts)+'</td><td>'+spark+'</td></tr>';
          }).join(''); }
          renderGraphIfReady();
        });
      }

      function renderGraphIfReady(){
        if (!lastSystem || !lastStatuses) return;
        try { drawSystemGraph(lastSystem, lastStatuses); } catch(e){ console.error('graph', e); }
      }

      // Legend toggle for path mode
      (function(){
        function setLabel(){
          var b = document.getElementById('graph-path-toggle');
          if (!b) return;
          b.textContent = 'Paths: ' + (graphPathMode==='orth' ? 'Orth' : 'Straight');
        }
        var btn = document.getElementById('graph-path-toggle');
        if (btn){
          setLabel();
          btn.addEventListener('click', function(){
            graphPathMode = (graphPathMode==='orth' ? 'straight' : 'orth');
            try { localStorage.setItem('graph_path_mode', graphPathMode); } catch(e){}
            setLabel();
            renderGraphIfReady();
          });
        }
      })();

      function fitTextToWidth(textEl, label, maxWidth){
        var full = String(label || '');
        if (!textEl) return { text: full, truncated: false, full: full };
        try {
          textEl.textContent = full;
          if (textEl.getComputedTextLength() <= maxWidth) {
            return { text: full, truncated: false, full: full };
          }
          var lo = 0, hi = full.length, best = '…';
          while (lo <= hi) {
            var mid = Math.floor((lo + hi) / 2);
            var cand = full.slice(0, Math.max(0, mid)) + '…';
            textEl.textContent = cand;
            var w = textEl.getComputedTextLength();
            if (w <= maxWidth) { best = cand; lo = mid + 1; }
            else { hi = mid - 1; }
          }
          textEl.textContent = best;
          return { text: best, truncated: true, full: full };
        } catch(e){
          textEl.textContent = full;
          return { text: full, truncated: false, full: full };
        }
      }

      function drawSystemGraph(sys, statuses){
        var svg = document.getElementById('sys-graph');
        if(!svg) return;
        graphHover = graphHover || document.getElementById('graph-hover');
        if (graphHover) { graphHover.style.display='none'; }
        var wrapEl = document.getElementById('graph-wrap');
        var W = svg.clientWidth || (wrapEl ? wrapEl.clientWidth : 1000) || 1000;
        var H = svg.clientHeight || 420;
        var padX = 40, padY = 30;
        var topY = 40, midY = 150;
        // Node metrics
        var nodeW = 80, nodeH = 32;
        var minXGap = 40; // horizontal spacing between app cards
        var rowGap = 48;  // vertical spacing between app rows
        var podOffsetY = 60; // pods rendered below their app card

        var nodes = [];
        var nodeById = {};
        function addNode(id, label, type, x, y, meta){ var n={id:id,label:label,type:type,x:x,y:y,meta:meta||{}}; nodes.push(n); nodeById[id]=n; return n; }

        var hasIngress = !!(sys.ingress && (sys.ingress.sites||[]).length);
        // Worker nodes row
        var workerY = topY;
        var workerGap = 140;
        var workerCount = (sys.nodes||[]).length;
        var workerRowWidth = (workerCount > 1) ? ((workerCount - 1) * workerGap) : 0;
        var workerStartX = (W - workerRowWidth) / 2;
        if (workerStartX < (padX + nodeW/2)) workerStartX = padX + nodeW/2;
        (sys.nodes||[]).forEach(function(n, idx){
          var st = String(n.status||'').toLowerCase();
          var cls = 'worker';
          if (n.stale) cls += ' stale';
          if (n.cordoned) cls += ' cordoned';
          var x = workerStartX + idx * workerGap;
          addNode('node:'+n.id, n.name||n.id, cls, x, workerY, {status:st, stale:n.stale, cordoned:n.cordoned});
        });

        // Shift system nodes down if workers present
        if ((sys.nodes||[]).length > 0){ topY += 50; midY += 50; }
        var apps = (statuses||[]).slice();
        var appCount = apps.length;
        // Grid layout: system column + app columns, centered within the graph bounds
        var minCenterGap = nodeW + minXGap; // minimum center-to-center gap
        var maxCenterGap = 200; // prevent over-stretching on wide viewports
        var availableSpan = Math.max(1, W - padX*2 - nodeW);
        var colsCap = Math.max(1, Math.floor(availableSpan / Math.max(1, minCenterGap)) - 1);
        var cols = Math.max(1, Math.min(appCount || 1, colsCap));
        var rows = Math.max(1, Math.ceil(appCount / cols));
        var gap = availableSpan / Math.max(1, cols + 1);
        if (gap > maxCenterGap) gap = maxCenterGap;
        var baseX = (W - (cols + 1) * gap) / 2;

        addNode('dns', 'DNS', 'system', baseX, topY);
        addNode('ingress', 'Ingress', 'system', baseX + gap, topY);
        addNode('controller', 'Controller', 'system', baseX, midY);
        addNode('runtime', 'Runtime', 'system', baseX + gap, midY);
        var byApp = {};
        apps.forEach(function(s){ byApp[s.app_name]=s; });
        var placements = sys.placements || {};
        apps.forEach(function(s, i){
          var info = splitAppName(s.app_name);
          var col = i % cols;
          var row = Math.floor(i / cols);
          var x = baseX + gap * (2 + col);
          var appY = (midY + 90) + row * (nodeH + podOffsetY + rowGap);
          addNode('app:'+s.app_name, info.name, 'app', x, appY, {app:s.app_name, app_short: info.name, ns: info.namespace, ready:s.ready_replicas, desired:s.desired_replicas, rev:s.revision, status:s.revision_status, row:row, col:col, idx:i});
          var reps = placements[s.app_name] || [];
          reps.slice(0,12).forEach(function(p, idx){
            var podY = appY + podOffsetY;
            var nid = p.node_id ? ('node:'+p.node_id) : null;
            var nodePos = nid && nodeById[nid] ? nodeById[nid] : null;
            var px = nodePos ? nodePos.x : (x - (reps.length-1)*10/2 + idx*10);
            var state = p.ready ? 'ready' : 'pending';
            addNode('pod:'+s.app_name+':'+idx, state, 'pod', px, podY, {app:s.app_name, ns: info.namespace, podIndex:idx, state:state, node:p.node_id});
          });
        });

        var links = [];
        function link(a,b, cls){ links.push({a:a,b:b,cls:cls||''}); }
        if (hasIngress) link('dns','ingress','flow');
        link('controller','runtime','flow');
        if (hasIngress) link('controller','ingress','flow');
        (sys.nodes||[]).forEach(function(n){
          link('controller','node:'+n.id,'');
          link('runtime','node:'+n.id,'');
        });
        var sites = (sys.ingress && sys.ingress.sites) || [];
        var appsWithIngress = new Set(sites.map(function(s){ return s.app; }));
        appsWithIngress.forEach(function(name){ link('ingress','app:'+name,'flow'); });
        (statuses||[]).forEach(function(s){
          link('runtime','app:'+s.app_name,'');
          var desired = Math.max(0, Number(s.desired_replicas||0));
          var pods = Math.min(desired, 12);
          for (var k=0;k<pods;k++){ link('app:'+s.app_name, 'pod:'+s.app_name+':'+k, ''); }
        });

        var gNodes = svg.querySelector('#nodes');
        var gLinks = svg.querySelector('#links');
        if(!gNodes||!gLinks) return;
        gNodes.innerHTML = '';
        gLinks.innerHTML = '';
        var defs = svg.querySelector('defs');
        if (defs) {
          Array.from(defs.querySelectorAll('linearGradient[data-app-grad="1"]')).forEach(function(el){
            if (el && el.parentNode) el.parentNode.removeChild(el);
          });
        }

        function ensureAppGradient(ns){
          if (!defs) return null;
          var key = String(ns || 'default').toLowerCase().replace(/[^a-z0-9_-]+/g, '_');
          var gradId = 'app-grad-' + key;
          var existing = defs.querySelector('#' + gradId);
          if (existing) return gradId;
          var colors = namespaceGradient(ns);
          var grad = document.createElementNS('http://www.w3.org/2000/svg','linearGradient');
          grad.setAttribute('id', gradId);
          grad.setAttribute('data-app-grad', '1');
          grad.setAttribute('x1','0'); grad.setAttribute('y1','0');
          grad.setAttribute('x2','0'); grad.setAttribute('y2','1');
          var stop1 = document.createElementNS('http://www.w3.org/2000/svg','stop');
          stop1.setAttribute('offset','0%');
          stop1.setAttribute('stop-color', colors.top);
          var stop2 = document.createElementNS('http://www.w3.org/2000/svg','stop');
          stop2.setAttribute('offset','100%');
          stop2.setAttribute('stop-color', colors.bottom);
          grad.appendChild(stop1);
          grad.appendChild(stop2);
          defs.appendChild(grad);
          return gradId;
        }

        // Resize the canvas height dynamically to fit all rows
        var totalHeight = (midY + 90) + (rows-1) * (nodeH + podOffsetY + rowGap) + podOffsetY + padY;
        if (wrapEl) {
          wrapEl.style.height = Math.max(420, Math.ceil(totalHeight)) + 'px';
        }
        svg.setAttribute('viewBox', '0 0 ' + Math.max(1000, W) + ' ' + Math.max(420, Math.ceil(totalHeight)));

        // Orthogonal routing helpers
        var gutterLeftX = padX + 8;
        var gutterRightX = W - padX - 8;
        var lanePad = 12; // horizontal lane above an app row

        // Track counts per destination to slightly offset and reduce overlap
        var dstCounts = {};

        function topEdgeY(n){ return (n.type==='pod') ? (n.y-5) : (n.y - (n.type==='app' || n.type==='system' ? nodeH/2 : 0)); }
        function bottomEdgeY(n){ return (n.type==='pod') ? (n.y+5) : (n.y + (n.type==='app' || n.type==='system' ? nodeH/2 : 0)); }

        function drawLink(id, src, dst, cls){
          var a = nodeById[src], b = nodeById[dst]; if(!a||!b) return;
          // Small per-destination jitter to reduce perfect overlap
          dstCounts[dst] = (dstCounts[dst]||0) + 1;
          var jitter = ((dstCounts[dst] % 5) - 2) * 2; // -4..+4 px

          var points = [];
          if (graphPathMode === 'straight'){
            points = [[a.x, a.y], [b.x, b.y]];
          } else {
          // Select routing strategy by pair types
          if (a.id==='ingress' && b.type==='app'){
            var laneBase = (b.y - nodeH/2 - lanePad);
            // Stagger lanes by index to minimize overlay, and separate source types
            var idx = (b.meta && b.meta.idx!=null) ? b.meta.idx : 0;
            var yBand = ((idx % 7) - 3) * 5; // -15..+15
            var srcSep = -6; // ingress a bit higher than runtime
            var laneY = laneBase + yBand + srcSep + jitter;
            // Direct turn into center of app to avoid U-turns
            points.push([a.x, a.y]);
            points.push([a.x, laneY]);
            points.push([b.x, laneY]);
            points.push([b.x, topEdgeY(b)]);
          } else if (a.id==='runtime' && b.type==='app'){
            var laneBase2 = (b.y - nodeH/2 - lanePad);
            var idx2 = (b.meta && b.meta.idx!=null) ? b.meta.idx : 0;
            var yBand2 = ((idx2 % 7) - 3) * 5; // -15..+15
            var srcSep2 = +6; // runtime a bit lower than ingress
            var laneY2 = laneBase2 + yBand2 + srcSep2 + jitter;
            // Direct into center: down, across, down
            points.push([a.x, a.y]);
            points.push([a.x, laneY2]);
            points.push([b.x, laneY2]);
            points.push([b.x, topEdgeY(b)]);
          } else if (a.type==='app' && b.type==='pod'){
            // Drop from bottom of app, then short horizontal, then into pod
            var sy = bottomEdgeY(a);
            var ey = topEdgeY(b);
            points.push([a.x, sy]);
            points.push([a.x, ey]);
            points.push([b.x, ey]);
            points.push([b.x, b.y-5]);
          } else {
            // Default orthogonal: vertical then horizontal then vertical
            var sy2 = (b.y > a.y) ? bottomEdgeY(a) : a.y;
            var ty2 = (b.y > a.y) ? topEdgeY(b) : b.y;
            points.push([a.x, sy2]);
            var midY = a.y + (b.y - a.y)/2;
            points.push([a.x, midY]);
            points.push([b.x, midY]);
            points.push([b.x, ty2]);
          }
          }
          // Build path (rounded corners for orth mode)
          var d = '';
          if (graphPathMode === 'straight'){
            for (var i=0;i<points.length;i++){
              d += (i===0 ? 'M ' : ' L ') + points[i][0] + ' ' + points[i][1];
            }
          } else {
            var r = 8; // corner radius
            if (points.length > 0){ d = 'M ' + points[0][0] + ' ' + points[0][1]; }
            for (var i=1;i<points.length;i++){
              var prev = points[i-1];
              var curr = points[i];
              var next = (i+1<points.length) ? points[i+1] : null;
              if (!next){
                d += ' L ' + curr[0] + ' ' + curr[1];
                break;
              }
              var v1x = curr[0]-prev[0], v1y = curr[1]-prev[1];
              var v2x = next[0]-curr[0], v2y = next[1]-curr[1];
              var len1 = Math.max(1, Math.abs(v1x)+Math.abs(v1y));
              var len2 = Math.max(1, Math.abs(v2x)+Math.abs(v2y));
              // If colinear, keep straight
              var colinear = (v1x===0 && v2x===0) || (v1y===0 && v2y===0);
              if (colinear){ d += ' L ' + curr[0] + ' ' + curr[1]; continue; }
              var r1 = Math.min(r, Math.floor((Math.abs(v1x)+Math.abs(v1y))/2));
              var r2 = Math.min(r, Math.floor((Math.abs(v2x)+Math.abs(v2y))/2));
              var rin = Math.min(r1, r2);
              // Offset along v1 to approach corner
              var u1x = v1x===0 ? 0 : (v1x>0?1:-1);
              var u1y = v1y===0 ? 0 : (v1y>0?1:-1);
              var pInX = curr[0] - u1x * rin;
              var pInY = curr[1] - u1y * rin;
              // Offset along v2 to exit corner
              var u2x = v2x===0 ? 0 : (v2x>0?1:-1);
              var u2y = v2y===0 ? 0 : (v2y>0?1:-1);
              var pOutX = curr[0] + u2x * rin;
              var pOutY = curr[1] + u2y * rin;
              d += ' L ' + pInX + ' ' + pInY + ' Q ' + curr[0] + ' ' + curr[1] + ' ' + pOutX + ' ' + pOutY;
            }
          }
          var p = document.createElementNS('http://www.w3.org/2000/svg','path');
          p.setAttribute('d', d);
          var klass = 'link ' + (cls||'');
          if ((cls||'').indexOf('flow') !== -1){
            // Decide animation direction based on net displacement from src->dst
            var dx = b.x - a.x, dy = b.y - a.y;
            var horiz = Math.abs(dx) >= Math.abs(dy);
            var forward = horiz ? (dx >= 0) : (dy >= 0);
            klass += forward ? ' flow-fwd' : ' flow-rev';
          }
          p.setAttribute('class', klass);
          p.setAttribute('stroke-linecap','round');
          p.setAttribute('fill','none');
          gLinks.appendChild(p);
        }

        var sysHelp = {
          ingress: {
            title:'Ingress (Caddy)',
            body:[
              'Front door for HTTP/HTTPS traffic.',
              'Routes host/path to healthy app endpoints after readiness.',
              'Controller writes site snippets and reloads the proxy on rollouts.',
              'When proxy runs in a container, upstream 127.0.0.1 is rewritten to host.docker.internal.'
            ].join('<br>')
          },
          controller: {
            title:'Controller',
            body:[
              'Reconciliation loop for registered apps (imported from specs/).',
              'Computes desired state and converges containers and ingress.',
              'Single-node rollout: maxUnavailable=0, maxSurge=1 (zero-downtime).',
              'Records events, exposes /status, /events, /metrics, and serves the dashboard.',
              'Cleans up old revisions after switching traffic.'
            ].join('<br>')
          },
          runtime: {
            title:'Runtime (Docker)',
            body:[
              'Container adapter that manages replicas for each app.',
              'Reads logs, reports readiness/liveness, and lists ports.',
              'Ensures named storage volumes exist; prunes when retention=Delete.',
              'Provides container info for conflict checks and observability.'
            ].join('<br>')
          }
        };

        function showHoverCard(kind, evt){
          if (!graphHover) return;
          var wrap = document.getElementById('graph-wrap');
          var rect = wrap.getBoundingClientRect();
          var x = (evt.clientX - rect.left) + 10;
          var y = (evt.clientY - rect.top) + 10;
          var info = sysHelp[kind];
          if (!info) return;
          graphHover.innerHTML = '<div style="font-weight:600; margin-bottom:4px;">'+info.title+'</div><div>'+info.body+'</div>';
          graphHover.style.left = x + 'px';
          graphHover.style.top = y + 'px';
          graphHover.style.display = 'block';
        }
        function hideHoverCard(){ if (graphHover) graphHover.style.display='none'; }

        function drawNode(n){
          var g = document.createElementNS('http://www.w3.org/2000/svg','g');
          g.setAttribute('class','node '+n.type);
          g.setAttribute('transform','translate('+(n.x-40)+','+(n.y-16)+')');
          // map to app for interactions
          var appName = null;
          if(n.id.startsWith('app:')) appName = n.id.slice(4);
          if(n.id.startsWith('pod:')) appName = n.id.split(':')[1] || null;
          if(appName){ g.setAttribute('data-app', appName); g.style.cursor='pointer';
            g.addEventListener('click', function(ev){ ev.preventDefault(); ev.stopPropagation(); try { selectApp(appName); focusAppListItem(appName); } catch(e){} });
          }
          if(n.type==='app' && n.meta && n.meta.ns){
            var nsc = namespaceColors(n.meta.ns);
            g.style.setProperty('--ns-color', nsc.color);
            g.style.setProperty('--ns-tint', nsc.tint);
          }
          if(n.type==='pod'){
            g.setAttribute('transform','translate('+(n.x-5)+','+(n.y-5)+')');
            var c = document.createElementNS('http://www.w3.org/2000/svg','circle');
            c.setAttribute('r','5'); c.setAttribute('cx','5'); c.setAttribute('cy','5');
            c.setAttribute('stroke-width','1.2');
            // class for ready/pending
            if(n.meta && n.meta.state){ g.setAttribute('class', g.getAttribute('class') + ' ' + n.meta.state); }
            g.appendChild(c);
            var title = document.createElementNS('http://www.w3.org/2000/svg','title');
            var parts = [];
            if(n.meta && n.meta.app) parts.push('App: '+n.meta.app);
            if(n.meta && n.meta.ns) parts.push('Namespace: '+n.meta.ns);
            if(n.meta && (n.meta.podIndex!=null)) parts.push('Replica: '+String(n.meta.podIndex));
            parts.push('State: ' + (n.meta && n.meta.state ? n.meta.state : n.label));
            title.textContent = parts.join(String.fromCharCode(10));
            g.appendChild(title);
            gNodes.appendChild(g);
          } else {
            var rect = document.createElementNS('http://www.w3.org/2000/svg','rect');
            rect.setAttribute('width','80'); rect.setAttribute('height','32'); rect.setAttribute('rx','6'); rect.setAttribute('ry','6');
            rect.setAttribute('class','node-shape');
            if(n.type==='app' && n.meta && n.meta.ns){
              var gradId = ensureAppGradient(n.meta.ns);
              if (gradId) rect.setAttribute('fill', 'url(#' + gradId + ')');
              else rect.setAttribute('fill', nsc && nsc.color ? nsc.color : '#3b82f6');
            }
            g.appendChild(rect);
            if(n.type==='app' && n.meta && n.meta.ns){
              var stripe = document.createElementNS('http://www.w3.org/2000/svg','rect');
              stripe.setAttribute('class','ns-stripe');
              stripe.setAttribute('x','1'); stripe.setAttribute('y','1'); stripe.setAttribute('width','6'); stripe.setAttribute('height','30');
              stripe.setAttribute('rx','4'); stripe.setAttribute('ry','4');
              g.appendChild(stripe);
            }
            var t = document.createElementNS('http://www.w3.org/2000/svg','text');
            t.setAttribute('x','40'); t.setAttribute('y','20'); t.setAttribute('text-anchor','middle'); t.textContent = String(n.label || '');
            g.appendChild(t);
            gNodes.appendChild(g);
            var labelInfo = fitTextToWidth(t, n.label, (nodeW * 1.4) - 8);
            try {
              var bbox = t.getBBox();
              var chip = document.createElementNS('http://www.w3.org/2000/svg','rect');
              chip.setAttribute('class','label-chip');
              chip.setAttribute('x', String(bbox.x - 4));
              chip.setAttribute('y', String(bbox.y - 2));
              chip.setAttribute('width', String(bbox.width + 8));
              chip.setAttribute('height', String(bbox.height + 4));
              chip.setAttribute('rx','4'); chip.setAttribute('ry','4');
              g.insertBefore(chip, t);
            } catch(e){}
            var title = document.createElementNS('http://www.w3.org/2000/svg','title');
            if(n.id.startsWith('app:')){
              var a = n.meta || {};
              var info = [];
              info.push('App: ' + (a.app||n.label));
              if (a.ns) info.push('Namespace: ' + a.ns);
              info.push('Replicas: ' + (a.ready||0) + '/' + (a.desired||0));
              if(a.rev!=null) info.push('Revision: ' + a.rev + ' (' + (a.status||'-') + ')');
              title.textContent = info.join(String.fromCharCode(10));
            } else {
              title.textContent = labelInfo.full;
            }
            g.appendChild(title);
          }
          // System node hover help
          if(n.type==='system' && (n.id==='ingress' || n.id==='controller' || n.id==='runtime')){
            g.addEventListener('mouseenter', function(ev){ showHoverCard(n.id, ev); });
            g.addEventListener('mousemove', function(ev){ showHoverCard(n.id, ev); });
            g.addEventListener('mouseleave', function(){ hideHoverCard(); });
          }
        }

        links.forEach(function(L,i){ drawLink('e'+i, L.a, L.b, L.cls); });
        nodes.forEach(drawNode);

        // Highlight selection
        try {
          var sel = (typeof current==='string' && current) ? String(current) : null;
          if (sel){
            // nodes
            Array.from(gNodes.children).forEach(function(n){
              var a = n.getAttribute('data-app');
              var isSel = (a===sel);
              if(isSel) n.classList.add('selected'); else if(n.className.baseVal.indexOf('system')===-1) n.classList.add('faded');
            });
            // links
            Array.from(gLinks.children).forEach(function(p){
              var d = p.getAttribute('d')||''; // fallback: we can't easily parse ends; recompute using id map instead
            });
            // More precise: mark links whose endpoints include the selected app
            links.forEach(function(L, i){
              var p = gLinks.children[i];
              if(!p) return;
              var involved = (L.a.indexOf('app:'+sel)===0) || (L.b.indexOf('app:'+sel)===0) || (L.b.indexOf('pod:'+sel+':')===0) || (L.a.indexOf('pod:'+sel+':')===0);
              if(involved) p.classList.add('selected'); else p.classList.add('faded');
            });
          }
        } catch(e){}
      }

      refreshApps().then(function(){ updateLogsHTMX(); updateLogStreaming(); return Promise.all([refreshDetail(), refreshSystem()]); }).catch(console.error);
      schedulePoll();
    </script>
  </body>
</html>
"""
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
        try:
            # Hint client retry interval
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            for line in fn(app, container, tail, since, True):  # type: ignore[misc]
                if isinstance(line, (bytes, bytearray)):
                    s = line.decode("utf-8", "replace").rstrip("\n")
                else:
                    s = str(line).rstrip("\n")
                lowered = s.lower()
                if "no container with name or id" in lowered or "no such container" in lowered:
                    continue
                out = ("data: " + s + "\n\n").encode("utf-8", "replace")
                self.wfile.write(out)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
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
                        f"<tr><td>{esc(r.check_time.isoformat())}</td><td>{esc(r.replica_id)}</td><td>{rd}</td><td>{lv}</td>"
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
# ruff: noqa: E501,S603,S607,S110,S112,SIM105,SIM108,SIM118,SIM210,S104,UP017,UP038,E741,B023,C401,UP035,E402,UP034
