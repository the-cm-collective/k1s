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


def record_app_reconcile(app: str, duration_seconds: float, *, created: int, updated: int, removed: int) -> None:
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
        path_only = self.path.split('?', 1)[0]
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
            for s0 in statuses:
                reps = self.store.list_replicas(s0.app_name)
                for r in reps:
                    val = 1 if r.ready else 0
                    lines.append(f'ae_replica_ready{{app="{s0.app_name}",replica="{r.replica_id}"}} {val}')
        except Exception:
            pass
        # Optional loop metrics
        if _LAST_RECONCILE_TS is not None:
            lines.append("# HELP ae_reconcile_last_timestamp_seconds Unix timestamp of last reconcile")
            lines.append("# TYPE ae_reconcile_last_timestamp_seconds gauge")
            lines.append(f"ae_reconcile_last_timestamp_seconds {_LAST_RECONCILE_TS}")
        if _LAST_RECONCILE_DURATION is not None:
            lines.append("# HELP ae_reconcile_last_duration_seconds Duration of last reconcile in seconds")
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
                lines.append("# HELP ae_rollout_operations_total Rollout operations aggregated by op type")
                lines.append("# TYPE ae_rollout_operations_total counter")
                lines.append(f'ae_rollout_operations_total{{app="{app}",op="{op}"}} {val}')
        lines.append("")
        payload = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
        payload = html.encode('utf-8')
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
        payload = html.encode('utf-8')
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
                            "ingress_path": {"type": "string", "nullable": True}
                        },
                        "required": ["app_name", "desired_replicas", "ready_replicas", "live_replicas", "revision", "revision_status", "image"]
                    },
                    "Event": {
                        "type": "object",
                        "properties": {
                            "app_name": {"type": "string"},
                            "revision": {"type": "integer"},
                            "event_type": {"type": "string"},
                            "message": {"type": "string"},
                            "created_at": {"type": "string", "format": "date-time"}
                        },
                        "required": ["app_name", "revision", "event_type", "message", "created_at"]
                    }
                }
            },
            "paths": {
                "/metrics": {"get": {"summary": "Prometheus metrics", "responses": {"200": {"description": "Prometheus text"}}}},
                "/health": {"get": {"summary": "Controller health", "responses": {"200": {"description": "OK"}}}},
                "/status": {"get": {"summary": "List app statuses (paginated)", "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                    {"name": "app", "in": "query", "schema": {"type": "string"}},
                    {"name": "wildcard", "in": "query", "schema": {"type": "string"}}
                ], "responses": {"200": {"description": "OK"}}, "security": [{"bearerAuth": []}]}},
                "/status/{app}": {"get": {"summary": "Get a single app status", "parameters": [{"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "OK"}, "404": {"description": "Not Found"}}, "security": [{"bearerAuth": []}]}},
                "/events/{app}": {"get": {"summary": "List app events (paginated)", "parameters": [
                    {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}},
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}}
                ], "responses": {"200": {"description": "OK"}}, "security": [{"bearerAuth": []}]}},
                "/scale/{app}": {"post": {"summary": "Scale an app", "parameters": [{"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "OK"}}, "security": [{"bearerAuth": []}]}},
                "/delete/{app}": {"post": {"summary": "Delete an app", "parameters": [
                    {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "purge", "in": "query", "schema": {"type": "boolean"}}
                ], "responses": {"200": {"description": "OK"}}, "security": [{"bearerAuth": []}]}},
                "/logs/{app}": {"get": {"summary": "Tail application logs",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "container", "in": "query", "schema": {"type": "string"}},
                        {"name": "tail", "in": "query", "schema": {"type": "integer"}},
                        {"name": "since", "in": "query", "schema": {"type": "integer"}},
                        {"name": "follow", "in": "query", "schema": {"type": "boolean"}}
                    ], "responses": {"200": {"description": "OK"}}, "security": [{"bearerAuth": []}]}}
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
        page = items[offset: offset + limit]
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
    <div id="endpoints">Loading…</div>
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
        payload = html.encode('utf-8')
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
      .card { border:1px solid #8884; border-radius:8px; padding:8px 10px; }
      pre { width:100%; box-sizing:border-box; }
      #controls { gap:8px; }
      input[type=text], select { padding:6px; }
      button { padding:6px 10px; }
      table { border-collapse:collapse; width:100%; }
      th, td { border-bottom:1px solid #8884; padding:6px; text-align:left; font-size:13px; }
      code { background:#0001; padding:2px 4px; border-radius:4px; }
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
        <label>Log filter <input id=\"log-filter\" type=\"text\" size=\"24\" placeholder=\"substring\" /></label>
        <button id=\"pause-btn\">Pause Logs</button>
      </div>
    </header>
    <main>
      <section id=\"apps\"></section>
      <section id=\"detail\">
        <div class=\"row\">
          <div class=\"card\" style=\"min-width:220px;\">
            <div><strong>App:</strong> <span id=\"d-app\">-</span></div>
            <div><strong>Image:</strong> <span id=\"d-image\">-</span></div>
            <div><strong>Ingress:</strong> <span id=\"d-ingress\">-</span></div>
            <div><strong>Replicas:</strong> <span id=\"d-replicas\">-</span></div>
            <div><strong>Revision:</strong> <span id=\"d-rev\">-</span> (<span id=\"d-rev-status\">-</span>)</div>
          </div>
          <div class=\"card\" style=\"flex:1;\">
            <strong>Events</strong>
            <div style=\"max-height:180px; overflow:auto;\" id=\"events\"></div>
          </div>
        </div>
        <div class=\"card\" style=\"margin-top:12px;\">
          <strong>Logs</strong>
          <pre id=\"logs\" style=\"max-height:360px; overflow:auto; background:#0001; padding:8px; border-radius:6px;\"></pre>
        </div>
      </section>
    </main>
    <script>
      var elApps = document.getElementById('apps');
      var elLogs = document.getElementById('logs');
      var elEvents = document.getElementById('events');
      var pollSel = document.getElementById('poll-interval');
      var logFilter = document.getElementById('log-filter');
      var pauseBtn = document.getElementById('pause-btn');
      var current = null;
      var pollTimer = null;
      var pauseLogs = false;

      pauseBtn.addEventListener('click', function () { pauseLogs = !pauseLogs; pauseBtn.textContent = pauseLogs ? 'Resume Logs' : 'Pause Logs'; });
      pollSel.addEventListener('change', function () { schedulePoll(); });

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
          data.items.forEach(function(s){
            var ok = s.ready_replicas >= s.desired_replicas && s.desired_replicas > 0;
            var warn = !ok && s.ready_replicas > 0;
            var bad = s.ready_replicas === 0 && s.desired_replicas > 0;
            var div = document.createElement('div');
            div.className = 'app' + (current===s.app_name ? ' active' : '');
            var line1 = '<div><strong>' + s.app_name + '</strong> ' + (ok?badge('ok','ready'):warn?badge('warn','progressing'):bad?badge('bad','degraded'):'') + '</div>';
            var line2 = '<div style="font-size:12px;color:#666;">' + s.ready_replicas + '/' + s.desired_replicas + ' ready - rev ' + s.revision_status + '</div>';
            div.innerHTML = line1 + line2;
            div.onclick = function(){ selectApp(s.app_name); };
            elApps.appendChild(div);
          });
          if(!current && data.items.length){ selectApp(data.items[0].app_name); }
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
          return fetchJSON('/events/' + encodeURIComponent(current) + '?limit=50').then(function(ev){
            elEvents.innerHTML = ev.map(function(e){ return '<div><code>' + e.created_at + '</code> ' + e.event_type + ' - ' + escapeHtml(e.message) + '</div>'; }).join('');
          }).then(function(){
            if(!pauseLogs){
              var q = new URLSearchParams({ tail: '200' });
              return fetchJSON('/logs/' + encodeURIComponent(current) + '?' + q.toString()).then(function(data){
                var filt = (logFilter.value || '').toLowerCase();
                var lines = (data.lines || []).filter(function(l){ return !filt || String(l).toLowerCase().indexOf(filt) !== -1; });
                elLogs.textContent = lines.join(' ');
                elLogs.scrollTop = elLogs.scrollHeight;
              });
            }
          });
        });
      }

      function schedulePoll(){
        if(pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        var ms = parseInt(pollSel.value, 10) || 0;
        if(ms > 0){ pollTimer = setInterval(function(){ refreshApps().then(refreshDetail).catch(console.error); }, ms); }
      }

      function selectApp(name){ current = name; refreshDetail().then(refreshApps); }

      function escapeHtml(s){
        s = (s === null || s === undefined) ? '' : String(s);
        return s.replace(/[&<>]/g, function(c){ return ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]); });
      }

      refreshApps().then(refreshDetail).catch(console.error);
      schedulePoll();
    </script>
  </body>
</html>
"""
        payload = html.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
        self._json_ok({
            "app": app,
            "lines": [
                (l.decode('utf-8', 'replace') if isinstance(l, (bytes, bytearray)) else str(l))
                for l in lines
            ]
        })


def start_http_api(
    port: int,
    store: SQLiteStateStore,
    *,
    scale_fn=None,
    delete_fn=None,
    logs_fn=None,
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
    # Allow quick restarts by enabling SO_REUSEADDR to avoid TIME_WAIT bind errors
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

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
