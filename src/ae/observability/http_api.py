"""Lightweight HTTP API for metrics, status, and events.

Endpoints:
- GET /metrics            -> Prometheus text format
- GET /status             -> JSON list of app statuses
- GET /status/<app>       -> JSON object for app status (404 if missing)
- GET /events/<app>?limit -> JSON list of recent events for app
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from typing import Tuple
import errno

from ae.controller.state import SQLiteStateStore
from ae.observability.metrics import MetricsService

# Simple in-memory reconcile metrics updated by the controller loop.
_LAST_RECONCILE_TS: float | None = None
_LAST_RECONCILE_DURATION: float | None = None
_APP_RECONCILE_SUM: dict[str, float] = {}
_APP_RECONCILE_COUNT: dict[str, int] = {}
_APP_ROLLOUT_OPS: dict[str, dict[str, int]] = {}


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


class _ApiHandler(http.server.BaseHTTPRequestHandler):
    store: SQLiteStateStore  # injected
    metrics: MetricsService  # injected
    # Optional mutators injected by controller when enabled
    scale_fn = None  # type: ignore[var-annotated]
    delete_fn = None  # type: ignore[var-annotated]
    # Optional system info provider injected by controller
    system_info_fn = None  # type: ignore[var-annotated]

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

        have_any = any([admin, scaler, reader])
        if not have_any:
            # No tokens configured: allow reads; other methods are handled separately
            return role in {"read", ""}

        # Determine presented level
        level = 0
        if token and reader and token == reader:
            level = 1
        if token and scaler and token == scaler:
            level = max(level, 2)
        if token and admin and token == admin:
            level = max(level, 3)

        required = {"": 0, "read": 1, "scale": 2, "admin": 3}.get(role, 0)
        return level >= required

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
        # Metrics allowed without auth
        if path_only.startswith("/metrics"):
            self._handle_metrics()
            return
        # Enforce read auth if configured
        try:
            check = self._require_role  # type: ignore[attr-defined]
        except Exception:
            check = None
        if check and not self._require_role("read"):
            self._deny(401 if not self.headers.get("Authorization") else 403)
            return
        if path_only == "/openapi.json":
            self._handle_openapi()
            return
        if path_only in ("/health", "/health/"):
            self._handle_health()
            return
        if path_only in ("/system", "/system/"):
            self._handle_system()
            return
        if path_only in ("/swagger", "/swagger/"):
            self._handle_swagger()
            return
        if path_only in ("/redoc", "/redoc/"):
            self._handle_redoc()
            return
        if path_only in ("/", "/docs"):
            self._handle_docs()
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
        if path_only == "/dashboard.js":
            self._handle_dashboard_js()
            return
        if path_only == "/status" or path_only == "/status/":
            self._handle_status_list()
            return
        if path_only.startswith("/status/"):
            self._handle_status_single(self.path.split("/", 2)[2])
            return
        if path_only.startswith("/events/"):
            self._handle_events(self.path.split("/", 2)[2])
            return
        if path_only.startswith("/logs/"):
            # SSE streaming: /logs/<app>/stream
            if path_only.endswith("/stream"):
                parts = path_only.split("/")
                if len(parts) >= 4:
                    self._handle_logs_stream(parts[2])
                else:
                    self.send_response(400)
                    self.end_headers()
                return
            self._handle_logs(self.path.split("/", 2)[2])
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # type: ignore[override]
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
            if self.path.startswith("/scale/") and not self._require_role("scale"):
                self._deny(401 if not self.headers.get("Authorization") else 403)
                return
            if self.path.startswith("/delete/") and not self._require_role("admin"):
                self._deny(401 if not self.headers.get("Authorization") else 403)
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
                result = self.delete_fn(app, purge)  # type: ignore[misc]
                self._json_ok(result)
            except Exception as exc:  # pragma: no cover
                self._json_error(500, str(exc))
            return

        self.send_response(404)
        self.end_headers()

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
        ]
        # Per-app and per-replica labeled gauges
        try:
            statuses = self.store.list_status()
            for s0 in statuses:
                app = s0.app_name
                lines.append(f'ae_app_desired_replicas{{app="{app}"}} {s0.desired_replicas}')
                lines.append(f'ae_app_ready_replicas{{app="{app}"}} {s0.ready_replicas}')
                lines.append(f'ae_app_live_replicas{{app="{app}"}} {s0.live_replicas}')
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

        payload = {"controller": ctrl, "rbac": rbac, **(extra or {})}
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
            },
        }
        payload = json.dumps(doc).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_status_list(self) -> None:
        import urllib.parse as _up

        statuses = self.store.list_status()
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
        payload = {
            "items": [
                {
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
                for s in page
            ],
            "next": str(next_cursor) if next_cursor is not None else None,
        }
        self._json_ok(payload)

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
                    }
                    for r in reps
                ]
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

    def log_message(self, fmt: str, *args):  # quiet
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
      :root { color-scheme: light dark; }
      body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
      header { display:flex; align-items:center; justify-content:space-between; padding:10px 14px; background:#0a0a0a10; position:sticky; top:0; backdrop-filter: blur(4px); }
      h1 { margin:0; font-size: 18px; }
      main { display:grid; grid-template-columns: 280px 1fr; gap:12px; padding:12px; }
      #apps { border-right:1px solid #8884; padding-right:8px; }
      .app { padding:6px 8px; border-radius:6px; cursor:pointer; }
      .app.active { background:#4f46e5; color:#fff; }
      .pill { display:inline-block; padding:1px 6px; border-radius:999px; font-size:12px; margin-left:6px; }
      .ok { background:#16a34a33; color:#16a34a; }
      .warn { background:#f59e0b33; color:#b45309; }
      .bad { background:#ef444433; color:#b91c1c; }
      .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
      .row.stretch { align-items: stretch; }
      .row.stretch { align-items: stretch; }
      .card { border:1px solid #8884; border-radius:8px; padding:8px 10px; }
      .detail-card { flex: 0 0 320px; }
      .logbox { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; width:100%; box-sizing:border-box; height:420px; overflow:auto; background:#0001; padding:8px; border-radius:6px; }
      .scrollcap { max-height:180px; overflow:auto; }
      .log-entry { white-space: pre-wrap; }
      .log-entry code { opacity:0.8; margin-right:6px; }
      #controls { gap:8px; }
      input[type=text], select { padding:6px; }
      button { padding:6px 10px; }
      table { border-collapse:collapse; width:100%; }
      th, td { border-bottom:1px solid #8884; padding:6px; text-align:left; font-size:13px; }
      code { background:#0001; padding:2px 4px; border-radius:4px; }
      h2 { font-size:14px; margin: 14px 4px 6px; opacity:0.9; }
      .divider { border-top:1px solid #8884; margin:16px 0; }
      .hover-card { position:absolute; display:none; max-width:280px; font-size:12px; line-height:1.35; background:rgba(255,255,255,0.95); color:inherit; border:1px solid #888; border-radius:6px; padding:8px 10px; box-shadow:0 2px 8px rgba(0,0,0,0.1); pointer-events:none; z-index: 1000; }
      @media (prefers-color-scheme: dark) { .hover-card { background:rgba(17,17,17,0.9); border-color:#555; } }
      h2 { font-size:14px; margin: 14px 4px 6px; opacity:0.9; }
      .divider { border-top:1px solid #8884; margin:16px 0; }
    </style>
  </head>
  <body>
    <header>
      <h1>k1s Demo Dashboard</h1>
      <div class=\"row\" id=\"controls\">
        <label>Poll <select id=\"poll-interval\">
          <option value=\"0\">off</option>
          <option value=\"2000\">2s</option>
          <option value=\"5000\" selected>5s</option>
          <option value=\"10000\">10s</option>
        </select></label>
        <label>Log filter <input id=\"log-filter\" name=\"filter\" type=\"text\" size=\"24\" placeholder=\"substring\" /></label>
        <button id=\"pause-btn\">Pause Logs</button>
      </div>
    </header>
    <main>
      <section id=\"apps\"></section>
      <section id=\"detail\">
        <h2>Application</h2>
        <div class=\"row stretch\">
          <div class=\"card detail-card\" style=\"display:flex; flex-direction:column;\">
            <div id=\"desc\" class=\"scrollcap\">
            <div><strong>App:</strong> <span id=\"d-app\">-</span></div>
            <div><strong>Image:</strong> <span id=\"d-image\">-</span></div>
            <div><strong>Ingress:</strong> <span id=\"d-ingress\">-</span></div>
            <div><strong>Replicas:</strong> <span id=\"d-replicas\">-</span></div>
            <div><strong>Revision:</strong> <span id=\"d-rev\">-</span> (<span id=\"d-rev-status\">-</span>)</div>
            <div><strong>Service:</strong> <span id=\"d-service\">-</span></div>
            <div><strong>Secrets:</strong> <span id=\"d-secrets\">-</span></div>
            <div><strong>Storage:</strong> <span id=\"d-storage\">-</span></div>
            </div>
          </div>
          <div class=\"card\" style=\"flex:1; display:flex; flex-direction:column;\">
            <strong>Events</strong>
            <div class=\"scrollcap\" id=\"events\"></div>
          </div>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\"> 
          <strong>System</strong>
          <div class=\"row\" id=\"sys-counters\" style=\"gap:10px; margin-top:6px; flex-wrap:wrap;\"></div>
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
                  .node text { font-size:12px; pointer-events:none; }
                  .node.system rect { fill:#e5e7eb; stroke:#6b7280; }
                  .node.app rect { fill:#dbeafe; stroke:#3b82f6; }
                  .node.pod circle { fill:#e5e7eb; stroke:#6b7280; }
                  .node.pod.ready circle { fill:#dcfce7; stroke:#16a34a; }
                  .node.pod.pending circle { fill:#fef3c7; stroke:#f59e0b; }
                  .link { stroke:#9ca3af; stroke-width:1.5; fill:none; marker-end:url(#arrow); }
                  .flow { stroke-dasharray:6 6; animation: flow 1.6s linear infinite; }
                  .selected rect, .selected circle { stroke-width:2.4 !important; filter: drop-shadow(0 0 2px #60a5fa); }
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
              </div>
            </div>
            <div id=\"graph-hover\" class=\"hover-card\"></div>
          </div>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\">
          <strong>Logs</strong>
          <div
            id=\"logs\"
            class=\"logbox\"
            hx-get=\"/dashboard/partials/logs\"
            hx-trigger=\"load, every 5s, refresh\"
            hx-include=\"#log-filter\"
            hx-swap=\"innerHTML\"
            hx-on::after-settle=\"this.scrollTop=this.scrollHeight\"
          ></div>
        </div>
        <div class=\"divider\"></div>
        <h2>Ingress, Services & Storage</h2>
        <div class=\"row stretch\" style=\"margin-top:12px;\">
          <div class=\"card\" style=\"flex:1;\">
            <strong>Services</strong>
            <table id=\"tbl-services\"><thead><tr><th>App</th><th>Port</th><th>Target</th><th>Replicas</th></tr></thead><tbody></tbody></table>
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
      </section>
    </main>
    <script>
      var elApps = document.getElementById('apps');
      var elEvents = document.getElementById('events');
      var pollSel = document.getElementById('poll-interval');
      var logFilter = document.getElementById('log-filter');
      var pauseBtn = document.getElementById('pause-btn');
      var current = null;
      var pollTimer = null;
      var pauseLogs = false;
      var logSource = null;
      var lastSystem = null;
      var lastStatuses = [];
      var graphHover = null;

      pauseBtn.addEventListener('click', function () {
        pauseLogs = !pauseLogs;
        pauseBtn.textContent = pauseLogs ? 'Resume Logs' : 'Pause Logs';
        updateLogsHTMX();
        updateLogStreaming();
      });
      pollSel.addEventListener('change', function () { schedulePoll(); updateLogsHTMX(); });

      function badge(cls, text){ return '<span class="pill ' + cls + '">' + text + '</span>'; }

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

      function refreshApps(){
        return fetchJSON('/status?limit=200').then(function(data){
          elApps.innerHTML = '';
          lastStatuses = data.items || [];
          data.items.forEach(function(s){
            var ok = s.ready_replicas >= s.desired_replicas && s.desired_replicas > 0;
            var warn = !ok && s.ready_replicas > 0;
            var bad = s.ready_replicas === 0 && s.desired_replicas > 0;
            var div = document.createElement('div');
            div.className = 'app' + (current===s.app_name ? ' active' : '');
            try { div.dataset.app = s.app_name; } catch(e){}
            var line1 = '<div><strong>' + s.app_name + '</strong> ' + (ok?badge('ok','ready'):warn?badge('warn','progressing'):bad?badge('bad','degraded'):'') + '</div>';
            var line2 = '<div style="font-size:12px;color:#666;">' + s.ready_replicas + '/' + s.desired_replicas + ' ready - rev ' + s.revision_status + '</div>';
            div.innerHTML = line1 + line2;
            div.onclick = function(){ selectApp(s.app_name); };
            elApps.appendChild(div);
          });
          if(!current && data.items.length){ selectApp(data.items[0].app_name); }
          renderGraphIfReady();
        });
      }

      function refreshDetail(){
        if(!current) return Promise.resolve();
        return fetchJSON('/status/' + encodeURIComponent(current) + '?details=1').then(function(s){
          document.getElementById('d-app').textContent = s.app_name;
          document.getElementById('d-image').textContent = s.image || '-';
          var inh = (s.ingress_host || '-') + (s.ingress_path || '');
          document.getElementById('d-ingress').textContent = inh;
          document.getElementById('d-replicas').textContent = s.ready_replicas + '/' + s.desired_replicas + ' (live ' + s.live_replicas + ')';
          document.getElementById('d-rev').textContent = s.revision;
          document.getElementById('d-rev-status').textContent = s.revision_status;
          try {
            var man = (s.manifest || {});
            var svc = (man.spec || {}).service || null;
            var svcText = svc ? (String(svc.port) + (svc.target_port ? (' -> ' + String(svc.target_port)) : '')) : '-';
            document.getElementById('d-service').textContent = svcText;
            var secRefs = ((man.spec || {}).secret_refs || []).length;
            document.getElementById('d-secrets').textContent = secRefs ? (secRefs + ' ref' + (secRefs>1?'s':'')) : '-';
            var storage = ((man.spec || {}).storage || []).map(function(v){ return v.name || ''; }).filter(Boolean);
            document.getElementById('d-storage').textContent = storage.length ? storage.join(', ') : '-';
          } catch(e) { }
          return fetchJSON('/events/' + encodeURIComponent(current) + '?limit=50').then(function(ev){
            elEvents.innerHTML = ev.map(function(e){ return '<div><code>' + e.created_at + '</code> ' + e.event_type + ' - ' + escapeHtml(e.message) + '</div>'; }).join('');
          }).then(function(){ updateLogsHTMX(); });
        });
      }

      function schedulePoll(){
        if(pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        var ms = parseInt(pollSel.value, 10) || 0;
        if(ms > 0){ pollTimer = setInterval(function(){ Promise.all([refreshApps(), refreshDetail(), refreshSystem()]).catch(console.error); }, ms); }
      }

      function selectApp(name){
        current = name;
        clearLogs();
        updateLogsHTMX();
        updateLogStreaming();
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
            var atBottom = (box.scrollTop + box.clientHeight) >= (box.scrollHeight - 16);
            box.appendChild(row);
            if (atBottom) { box.scrollTop = box.scrollHeight; }
          };
          es.onerror = function(){ /* default retry */ };
        } catch (e) { console.error('EventSource failed', e); }
      }

      function focusAppListItem(name){
        try {
          var el = Array.from(document.querySelectorAll('#apps .app')).find(function(e){ return (e.dataset && e.dataset.app)===name; });
          if (el) { el.scrollIntoView({block:'nearest'}); }
        } catch(e){}
      }

      function renderCounters(sys){
        var el = document.getElementById('sys-counters');
        if(!el) return;
        var lastTs = sys.controller && sys.controller.last_reconcile_timestamp ? new Date(sys.controller.last_reconcile_timestamp*1000).toISOString() : '-';
        var lastDur = sys.controller && sys.controller.last_reconcile_duration != null ? (Number(sys.controller.last_reconcile_duration).toFixed(3) + 's') : '-';
        var ingressSites = (sys.ingress && sys.ingress.sites) ? sys.ingress.sites.length : 0;
        var services = (sys.services || []).length;
        var volumes = (sys.volumes || []).length;
        var pills = [
          {k:'Last Reconcile', v:lastTs},
          {k:'Duration', v:lastDur},
          {k:'Ingress Sites', v:String(ingressSites)},
          {k:'Services', v:String(services)},
          {k:'Volumes', v:String(volumes)},
          {k:'Mutations', v: (sys.rbac && sys.rbac.mutations_enabled) ? 'enabled' : 'disabled'},
        ];
        el.innerHTML = pills.map(function(p){ return '<div class="pill" style="background:#0001">'+escapeHtml(p.k)+': <strong>'+escapeHtml(p.v)+'</strong></div>'; }).join('');
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
          var vbody = document.querySelector('#tbl-vols tbody');
          if(vbody){ vbody.innerHTML = (sys.volumes||[]).map(function(v){
            var app = (v.labels&&v.labels['ae.app'])||'';
            return '<tr><td>'+escapeHtml(v.name||'')+'</td><td>'+escapeHtml(app)+'</td><td>'+escapeHtml(v.driver||'')+'</td><td>'+escapeHtml(v.mountpoint||'')+'</td></tr>';
          }).join(''); }
          renderGraphIfReady();
        });
      }

      function renderGraphIfReady(){
        if (!lastSystem || !lastStatuses) return;
        try { drawSystemGraph(lastSystem, lastStatuses); } catch(e){ console.error('graph', e); }
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
        addNode('dns', 'DNS', 'system', padX + 160, topY);
        addNode('ingress', 'Ingress', 'system', padX + 360, topY);
        addNode('controller', 'Controller', 'system', padX + 160, midY);
        addNode('runtime', 'Runtime', 'system', padX + 360, midY);

        var apps = (statuses||[]).slice();
        // Calculate columns based on available width and minimum center-to-center gap
        var minCenterGap = nodeW + minXGap; // 120px default
        var colsCap = Math.max(1, Math.floor((W - padX*2) / Math.max(1, minCenterGap)));
        // Reduce the max-per-row by 2 to create more breathing room
        var cols = Math.max(1, Math.min(apps.length, Math.max(1, colsCap - 2)));
        var rows = Math.max(1, Math.ceil(apps.length / cols));
        var gap = (W - padX*2) / Math.max(1, cols); // actual center-to-center gap used
        var byApp = {};
        apps.forEach(function(s){ byApp[s.app_name]=s; });
        apps.forEach(function(s, i){
          var col = i % cols;
          var row = Math.floor(i / cols);
          var x = padX + gap*col + gap*0.5;
          var appY = (midY + 90) + row * (nodeH + podOffsetY + rowGap);
          addNode('app:'+s.app_name, s.app_name, 'app', x, appY, {app:s.app_name, ready:s.ready_replicas, desired:s.desired_replicas, rev:s.revision, status:s.revision_status});
          var desired = Math.max(0, Number(s.desired_replicas||0));
          var ready = Math.max(0, Number(s.ready_replicas||0));
          var pods = Math.min(desired, 12);
          for (var k=0;k<pods;k++){
            var px = x - (pods-1)*10/2 + k*10;
            var podY = appY + podOffsetY;
            var state=(k<ready?'ready':'pending');
            addNode('pod:'+s.app_name+':'+k, state, 'pod', px, podY, {app:s.app_name, podIndex:k, state:state});
          }
        });

        var links = [];
        function link(a,b, cls){ links.push({a:a,b:b,cls:cls||''}); }
        if (hasIngress) link('dns','ingress','flow');
        link('controller','runtime','flow');
        if (hasIngress) link('controller','ingress','flow');
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

        // Resize the canvas height dynamically to fit all rows
        var totalHeight = (midY + 90) + (rows-1) * (nodeH + podOffsetY + rowGap) + podOffsetY + padY;
        if (wrapEl) {
          wrapEl.style.height = Math.max(420, Math.ceil(totalHeight)) + 'px';
        }
        svg.setAttribute('viewBox', '0 0 ' + Math.max(1000, W) + ' ' + Math.max(420, Math.ceil(totalHeight)));

        function drawLink(id, src, dst, cls){
          var a = nodeById[src], b = nodeById[dst]; if(!a||!b) return;
          var d = 'M '+a.x+' '+a.y+' L '+b.x+' '+b.y;
          var p = document.createElementNS('http://www.w3.org/2000/svg','path');
          p.setAttribute('d', d);
          p.setAttribute('class', 'link '+(cls||''));
          p.setAttribute('stroke-linecap','round');
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
              'Reconciliation loop for manifests in specs/.',
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
            if(n.meta && (n.meta.podIndex!=null)) parts.push('Replica: '+String(n.meta.podIndex));
            parts.push('State: ' + (n.meta && n.meta.state ? n.meta.state : n.label));
            title.textContent = parts.join(String.fromCharCode(10));
            g.appendChild(title);
          } else {
            var rect = document.createElementNS('http://www.w3.org/2000/svg','rect');
            rect.setAttribute('width','80'); rect.setAttribute('height','32'); rect.setAttribute('rx','6'); rect.setAttribute('ry','6'); rect.setAttribute('stroke-width','1.2');
            g.appendChild(rect);
            var t = document.createElementNS('http://www.w3.org/2000/svg','text');
            t.setAttribute('x','40'); t.setAttribute('y','20'); t.setAttribute('text-anchor','middle'); t.textContent = n.label;
            g.appendChild(t);
            var title = document.createElementNS('http://www.w3.org/2000/svg','title');
            if(n.id.startsWith('app:')){
              var a = n.meta || {};
              var info = [];
              info.push('App: ' + (a.app||n.label));
              info.push('Replicas: ' + (a.ready||0) + '/' + (a.desired||0));
              if(a.rev!=null) info.push('Revision: ' + a.rev + ' (' + (a.status||'-') + ')');
              title.textContent = info.join(String.fromCharCode(10));
            } else {
              title.textContent = n.label;
            }
            g.appendChild(title);
          }
          // System node hover help
          if(n.type==='system' && (n.id==='ingress' || n.id==='controller' || n.id==='runtime')){
            var label = g.querySelector('text');
            g.addEventListener('mouseenter', function(ev){ if(label) label.style.visibility='hidden'; showHoverCard(n.id, ev); });
            g.addEventListener('mousemove', function(ev){ showHoverCard(n.id, ev); });
            g.addEventListener('mouseleave', function(){ if(label) label.style.visibility='visible'; hideHoverCard(); });
          }
          gNodes.appendChild(g);
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
                out = ("data: " + s + "\n\n").encode("utf-8", "replace")
                self.wfile.write(out)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.wfile.write(b"event: error\n" b"data: stream closed\n\n")
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
        return "-", line

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
        self._json_ok(
            {
                "app": app,
                "lines": [
                    (l.decode("utf-8", "replace") if isinstance(l, (bytes, bytearray)) else str(l))
                    for l in lines
                ],
            }
        )


def start_http_api(
    port: int,
    store: SQLiteStateStore,
    *,
    scale_fn=None,
    delete_fn=None,
    logs_fn=None,
    system_info_fn=None,
) -> Tuple[socketserver.TCPServer, int, threading.Thread]:
    """Start the HTTP API on the given port.

    If port == 0, the OS selects a free port. Returns (server, assigned_port, thread).
    """

    handler_cls = type("Handler", (_ApiHandler,), {})
    handler_cls.store = store
    handler_cls.metrics = MetricsService(store)
    # Avoid Python descriptor binding when accessed via instances: wrap as staticmethods
    handler_cls.scale_fn = staticmethod(scale_fn) if scale_fn is not None else None
    handler_cls.delete_fn = staticmethod(delete_fn) if delete_fn is not None else None
    handler_cls.logs_fn = staticmethod(logs_fn) if logs_fn is not None else None
    handler_cls.system_info_fn = (
        staticmethod(system_info_fn) if system_info_fn is not None else None
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
