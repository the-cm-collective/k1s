# ruff: noqa: E501,I001,S110,S112,SIM105,UP017
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

from ae.controller.state import SQLiteStateStore

try:  # Optional: Phase 3 pod CIDR allocator
    from ae.network.pod_cidr import PodCIDRAllocator
except Exception:  # pragma: no cover - allocator optional
    PodCIDRAllocator = None  # type: ignore
try:
    from ae.security.ca import (
        issue_cert,
        is_revoked,
        record_used_token,
        token_used,
    )
    from ae.security.tokens import verify_token
except Exception:  # pragma: no cover
    issue_cert = None  # type: ignore
    verify_token = None  # type: ignore
    is_revoked = None  # type: ignore
    record_used_token = None  # type: ignore
    token_used = None  # type: ignore

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
    cidr_allocator: PodCIDRAllocator | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Factory so tests can spin a server without global state."""

    class AgentAPIHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib API
            LOGGER.info("%s - %s", self.address_string(), fmt % args)

        def _auth_ok(self) -> bool:
            if not token:
                return True
            if self.headers.get("X-Agent-Token") != token:
                return False
            # Optional token expiry check
            import os as _os
            from datetime import datetime as _dt, timezone as _tz

            exp = (_os.getenv("AE_AGENT_TOKEN_EXPIRES") or "").strip()
            if not exp:
                return True
            try:
                dt = (
                    _dt.fromisoformat(exp[:-1] + "+00:00")
                    if exp.endswith("Z")
                    else _dt.fromisoformat(exp)
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                return _dt.now(_tz.utc) < dt
            except Exception:
                return True

        def _unauthorized(self) -> None:
            _json(self, 401, {"error": "unauthorized"})

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path in ("/healthz", "/readyz"):
                try:
                    if is_revoked is not None and hasattr(self.connection, "getpeercert"):
                        cert = self.connection.getpeercert()  # type: ignore[attr-defined]
                        if cert and cert.get("serialNumber") and is_revoked(cert["serialNumber"]):
                            _json(self, 401, {"error": "certificate revoked"})
                            return
                except Exception:
                    pass
                _json(self, 200, {"ok": True})
                return
            if self.path.startswith("/v1/nodes"):
                _json(self, 200, {"nodes": _serialize_nodes(store)})
                return
            _json(self, 404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            if not self._auth_ok():
                return self._unauthorized()
            if self.path not in {"/v1/heartbeat", "/v1/bootstrap"}:
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

            if self.path == "/v1/bootstrap":
                if issue_cert is None:
                    _json(self, 500, {"error": "ca helper unavailable"})
                    return
                node_id = str(payload.get("node_id") or "").strip()
                if not node_id:
                    _json(self, 400, {"error": "node_id required"})
                    return
                join = str(payload.get("join_token") or "").strip()
                if verify_token is not None:
                    try:
                        claimed, _ = verify_token(join)
                        if claimed != node_id:
                            _json(self, 401, {"error": "token node mismatch"})
                            return
                        if token_used and token_used(join):
                            _json(self, 401, {"error": "token already used"})
                            return
                    except Exception as exc:
                        _json(self, 401, {"error": f"invalid token: {exc}"})
                        return
                try:
                    crt, key, ca = issue_cert(node_id)
                    if record_used_token:
                        record_used_token(join)
                    _json(
                        self,
                        200,
                        {
                            "cert": crt.read_text(encoding="utf-8"),
                            "key": key.read_text(encoding="utf-8"),
                            "ca": ca.read_text(encoding="utf-8"),
                        },
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("bootstrap failed for %s", node_id)
                    _json(self, 500, {"error": str(exc)})
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
                # If mTLS client cert presented and revoked, reject early
                try:
                    if is_revoked is not None and hasattr(self.connection, "getpeercert"):
                        cert = self.connection.getpeercert()  # type: ignore[attr-defined]
                        if cert:
                            serial = cert.get("serialNumber")
                            if serial and is_revoked(serial):
                                _json(self, 401, {"error": "certificate revoked"})
                                return
                except Exception:
                    pass
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
    host: str = "0.0.0.0",  # noqa: S104 - exposed for node agents
    port: int = 9110,
    *,
    token: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    client_ca: str | None = None,
    require_client_cert: bool = False,
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
    if tls_cert and tls_key:
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        if client_ca:
            ctx.load_verify_locations(client_ca)
            if require_client_cert:
                ctx.verify_mode = ssl.CERT_REQUIRED
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, name="agent-api", daemon=True)
    thread.start()
    LOGGER.info("Agent API listening on %s:%s", host, port)
    return server
