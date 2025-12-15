import threading
import time
from http.server import HTTPServer

import pytest
import requests

from ae.controller.agent_api import make_handler
from ae.controller.state import SQLiteStateStore
from ae.network.pod_cidr import PodCIDRAllocator


def _run_server(store: SQLiteStateStore, token: str | None = None, allocator=None):
    handler = make_handler(store, token, allocator)
    try:
        server = HTTPServer(("127.0.0.1", 0), handler)
    except PermissionError:
        pytest.skip("listener sockets not permitted in sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_heartbeat_updates_store(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    alloc = PodCIDRAllocator(store, "10.70.0.0/30", 30)
    server, thread = _run_server(store, token="secret", allocator=alloc)
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/heartbeat"
        resp = requests.post(
            url,
            json={"node_id": "n1", "backend": "docker", "endpoint": "http://n1:9109"},
            headers={"X-Agent-Token": "secret"},
            timeout=5,
        )
        assert resp.status_code == 200
        res = store.get_node("n1")
        assert res is not None
        node, status = res
        assert node.backend == "docker"
        assert status is not None
        assert status.status == "Ready"
        # CIDR should be allocated and returned
        assert node.pod_cidr == "10.70.0.0/30"
        assert resp.json().get("pod_cidr") == "10.70.0.0/30"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_heartbeat_requires_token(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    server, thread = _run_server(store, token="secret")
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/heartbeat"
        resp = requests.post(url, json={"node_id": "n1"}, timeout=5)
        assert resp.status_code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
