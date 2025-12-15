"""Lightweight controller-side API for node agents (heartbeats, node info).

This server is intentionally minimal and runs alongside the controller to
record node heartbeats in the shared state store. Authentication is a simple
shared token header (`X-Agent-Token`) when configured.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from ae.controller.state import SQLiteStateStore
try:  # Optional: Phase 3 pod CIDR allocator
    from ae.network.pod_cidr import PodCIDRAllocator
except Exception:  # pragma: no cover - allocator optional
    PodCIDRAllocator = None  # type: ignore

LOGGER = logging.getLogger(__name__)


def _json(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _serialize_nodes(store: SQLiteStateStore) -> list[dict]:
    items = []
    for node, status in store.list_nodes():
        items.append(
            {
                "node_id": node.node_id,
                "name": node.name,
                "backend": node.backend,
                "endpoint": node.endpoint,
                "pod_cidr": node.pod_cidr,
                "wg_pubkey": node.wg_pubkey,
                "labels": node.labels,
                "taints": node.taints,
                "cordoned": bool(getattr(node, "cordoned", False)),
                "status": status.status if status else None,
                "seen_at": status.seen_at.isoformat() if status else None,
            }
        )
    return items


def make_handler(
    store: SQLiteStateStore,
    token: str | None = None,
    cidr_allocator: "PodCIDRAllocator | None" = None,
) -> type[BaseHTTPRequestHandler]:
    """Factory so tests can spin a server without global state."""

    class AgentAPIHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib API
            LOGGER.info("%s - %s", self.address_string(), fmt % args)

        def _auth_ok(self) -> bool:
            if not token:
                return True
            return self.headers.get("X-Agent-Token") == token

        def _unauthorized(self) -> None:
            _json(self, 401, {"error": "unauthorized"})

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path in ("/healthz", "/readyz"):
                _json(self, 200, {"ok": True})
                return
            if self.path.startswith("/v1/nodes"):
                _json(self, 200, {"nodes": _serialize_nodes(store)})
                return
            _json(self, 404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            if not self._auth_ok():
                return self._unauthorized()
            if self.path != "/v1/heartbeat":
                _json(self, 404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                _json(self, 400, {"error": "invalid json"})
                return

            node_id = str(payload.get("node_id") or payload.get("id") or "").strip()
            status = str(payload.get("status") or "Ready")
            if not node_id:
                _json(self, 400, {"error": "node_id required"})
                return

            labels = payload.get("labels") or {}
            taints = payload.get("taints") or []
            backend = payload.get("backend")
            endpoint = payload.get("endpoint")
            name = payload.get("name")
            pod_cidr = payload.get("pod_cidr")
            wg_pubkey = payload.get("wg_pubkey")

            # Optional: assign a Pod CIDR if not provided by the agent
            assigned_cidr = None
            if pod_cidr in (None, "") and cidr_allocator is not None:
                try:
                    assigned_cidr = cidr_allocator.allocate(node_id)
                    pod_cidr = assigned_cidr
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("pod CIDR allocation failed for %s: %s", node_id, exc)

            try:
                store.upsert_node(
                    node_id,
                    name=name,
                    labels=labels,
                    taints=taints,
                    backend=backend,
                    endpoint=endpoint,
                    pod_cidr=pod_cidr,
                    wg_pubkey=wg_pubkey,
                )
                store.record_heartbeat(node_id, status)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("heartbeat failed for %s", node_id)
                _json(self, 500, {"error": str(exc)})
                return
            response = {"ok": True}
            if assigned_cidr:
                response["pod_cidr"] = assigned_cidr
            _json(self, 200, response)

    return AgentAPIHandler


def start_agent_api(
    store: SQLiteStateStore,
    host: str = "0.0.0.0",
    port: int = 9110,
    *,
    token: str | None = None,
) -> HTTPServer:
    """Start the agent API server in a daemon thread."""
    # Optional Pod CIDR allocator (Phase 3)
    allocator = None
    try:
        if PodCIDRAllocator is not None:
            allocator = PodCIDRAllocator.from_env(store)
    except Exception as exc:
        LOGGER.warning("pod CIDR allocator disabled: %s", exc)
        allocator = None

    handler = make_handler(store, token, allocator)
    server = HTTPServer((host, int(port)), handler)
    thread = threading.Thread(target=server.serve_forever, name="agent-api", daemon=True)
    thread.start()
    LOGGER.info("Agent API listening on %s:%s", host, port)
    return server
