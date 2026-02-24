from __future__ import annotations

import threading
from http.server import HTTPServer

import pytest
import requests

from ae.node.server import AgentHandler
from ae.runtime import StubRuntime


def _run_agent_server():
    AgentHandler.runtime = StubRuntime()  # type: ignore[assignment]
    AgentHandler.node_id = "node-test"
    AgentHandler.volume_manager = None
    AgentHandler.fabric_sessions = {}
    try:
        server = HTTPServer(("127.0.0.1", 0), AgentHandler)
    except PermissionError:
        pytest.skip("listener sockets not permitted in sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_fabric_session_lifecycle() -> None:
    server, thread = _run_agent_server()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        ensure_resp = requests.post(
            base + "/v1/fabric/ensure_session",
            json={
                "node_id": "node-test",
                "session": {
                    "session_id": "s-1234",
                    "mode": "lan_direct",
                    "policy_mode": "strict_membership",
                    "ifname": "wg-cell-test",
                    "member_ips": {"node-test": "10.250.1.1"},
                    "allowed_rules": [{"proto": "tcp", "port": 18080}],
                    "expires_at": "2026-02-24T00:00:00Z",
                },
            },
            timeout=5,
        )
        assert ensure_resp.status_code == 200
        assert ensure_resp.json().get("ok") is True

        list_resp = requests.get(base + "/v1/fabric/sessions", timeout=5)
        assert list_resp.status_code == 200
        sessions = list_resp.json().get("sessions") or []
        assert len(sessions) == 1
        assert sessions[0].get("session_id") == "s-1234"

        teardown_resp = requests.post(
            base + "/v1/fabric/teardown_session",
            json={"session_id": "s-1234"},
            timeout=5,
        )
        assert teardown_resp.status_code == 200
        assert teardown_resp.json().get("removed") is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
