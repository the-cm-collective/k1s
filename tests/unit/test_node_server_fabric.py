from __future__ import annotations

import threading
from http.server import HTTPServer
from pathlib import Path

import pytest
import requests

from ae.ha.fencing import SQLiteFenceStore
from ae.node.server import AgentHandler
from ae.runtime import StubRuntime


def _run_agent_server(*, fence_path: Path | None = None):
    AgentHandler.runtime = StubRuntime()  # type: ignore[assignment]
    AgentHandler.node_id = "node-test"
    AgentHandler.volume_manager = None
    AgentHandler.fabric_sessions = {}
    AgentHandler.fence_store = None
    if fence_path is not None:
        AgentHandler.fence_store = SQLiteFenceStore(fence_path)
        AgentHandler.fence_store.init()
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


def test_fabric_duplicate_ensure_rehydrates_session_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    fence_path = tmp_path / "fence.db"
    request_payload = {
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
        "controller_id": "ctrl-a",
        "controller_epoch": 7,
        "operation_id": "fabric.ensure:s-1234:node-test",
    }

    server, thread = _run_agent_server(fence_path=fence_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        ensure_resp = requests.post(
            base + "/v1/fabric/ensure_session",
            json=request_payload,
            timeout=5,
        )
        assert ensure_resp.status_code == 200
        assert ensure_resp.json().get("duplicate") is not True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    server2, thread2 = _run_agent_server(fence_path=fence_path)
    base2 = f"http://127.0.0.1:{server2.server_port}"
    try:
        ensure_resp = requests.post(
            base2 + "/v1/fabric/ensure_session",
            json=request_payload,
            timeout=5,
        )
        assert ensure_resp.status_code == 200
        assert ensure_resp.json().get("duplicate") is True

        list_resp = requests.get(base2 + "/v1/fabric/sessions", timeout=5)
        assert list_resp.status_code == 200
        sessions = list_resp.json().get("sessions") or []
        assert sessions and sessions[0].get("session_id") == "s-1234"
    finally:
        server2.shutdown()
        server2.server_close()
        thread2.join(timeout=2)
