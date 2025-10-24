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

    def do_GET(self):  # type: ignore[override]
        # Metrics allowed without auth
        if self.path.startswith("/metrics"):
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
        if self.path == "/openapi.json":
            self._handle_openapi()
            return
        if self.path in ("/swagger", "/swagger/"):
            self._handle_swagger()
            return
        if self.path in ("/redoc", "/redoc/"):
            self._handle_redoc()
            return
        if self.path in ("/", "/docs"):
            self._handle_docs()
            return
        if self.path == "/status" or self.path == "/status/":
            self._handle_status_list()
            return
        if self.path.startswith("/status/"):
            self._handle_status_single(self.path.split("/", 2)[2])
            return
        if self.path.startswith("/events/"):
            self._handle_events(self.path.split("/", 2)[2])
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
    <script>
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
        # Minimal static document describing read-only endpoints
        doc = {
            "openapi": "3.0.0",
            "info": {"title": "k1s Controller API", "version": "0.1.0"},
            "paths": {
                "/metrics": {"get": {"summary": "Prometheus metrics", "responses": {"200": {"description": "Prometheus text"}}}},
                "/status": {"get": {"summary": "List app statuses", "responses": {"200": {"description": "OK"}}}},
                "/status/{app}": {"get": {"summary": "Get a single app status", "parameters": [{"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "OK"}, "404": {"description": "Not Found"}}}},
                "/events/{app}": {"get": {"summary": "List app events", "parameters": [{"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 20}}], "responses": {"200": {"description": "OK"}}}},
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


def start_http_api(
    port: int,
    store: SQLiteStateStore,
    *,
    scale_fn=None,
    delete_fn=None,
) -> Tuple[socketserver.TCPServer, int, threading.Thread]:
    """Start the HTTP API on the given port.

    If port == 0, the OS selects a free port. Returns (server, assigned_port, thread).
    """

    handler_cls = type("Handler", (_ApiHandler,), {})
    handler_cls.store = store
    handler_cls.metrics = MetricsService(store)
    handler_cls.scale_fn = scale_fn
    handler_cls.delete_fn = delete_fn
    httpd = socketserver.TCPServer(("0.0.0.0", port), handler_cls)
    assigned = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, name="ae-http-api", daemon=True)
    thread.start()
    return httpd, assigned, thread
