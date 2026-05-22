from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest

from ae.apishim import server as shim_server
from ae.controller.state import NodeRecord
from ae.apishim.store import ObjectStore


REMOTE_ENDPOINT = "http://192.0.2.42:9111"


def make_handler(path: str, method: str = "GET", headers=None, body: bytes = b""):
    headers = headers or {}
    if body and "Content-Length" not in headers:
        headers["Content-Length"] = str(len(body))

    class DummySocket:
        def __init__(self, payload: bytes):
            self._rbuf = BytesIO(payload)
            self._wbuf = BytesIO()
            self.timeout = None
            self.path = path
            self.command = method
            self.headers = headers
            self.rbufsize = -1

        def makefile(self, mode, *_args, **_kwargs):
            if "r" in mode:
                return self._rbuf
            return self._wbuf

        def settimeout(self, t):
            self.timeout = t

        def setsockopt(self, *_a, **_k):
            return None

        def close(self):
            return None

    return DummySocket(body)


class RuntimeStub:
    def __init__(
        self,
        *,
        containers: list[dict] | None = None,
        log_lines: list[str] | None = None,
        fail_list: str | None = None,
    ) -> None:
        self._containers = list(containers or [])
        self._log_lines = list(log_lines or [])
        self._fail_list = fail_list
        self.log_calls: list[tuple[str, bool, int | None, int | None]] = []

    def list_containers_info(self):
        if self._fail_list:
            raise RuntimeError(self._fail_list)
        return list(self._containers)

    def read_logs(self, pod_name: str, *, follow: bool = False, tail=None, since=None):
        self.log_calls.append((pod_name, bool(follow), tail, since))
        return iter(self._log_lines)


class RemoteStateStub:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.node = NodeRecord(
            node_id="core-a--hub",
            name="core-a--hub",
            labels={"role": "hub", "site": "core-a"},
            capabilities={},
            taints=[],
            backend="cri",
            endpoint=REMOTE_ENDPOINT,
            pod_cidr="10.42.0.0/24",
            wg_pubkey=None,
            rp_pubkey=None,
            cordoned=False,
            created_at=now,
            updated_at=now,
        )

    def list_status(self):
        return [SimpleNamespace(app_name="netfs-core-a-writer")]

    def list_pod_nodes(self, app_name: str):
        if app_name != "netfs-core-a-writer":
            return []
        return [
            (
                "netfs-core-a-writer-rev2-0",
                "core-a--hub",
                True,
                True,
                "running",
                "readiness default ok",
                "liveness default ok",
            )
        ]

    def get_node(self, node_id: str):
        if node_id != self.node.node_id:
            return None
        return self.node, None

    def list_nodes(self):
        return [(self.node, None)]


@pytest.fixture
def store(tmp_path):
    return ObjectStore(tmp_path / "apishim-remote.db")


@pytest.fixture(autouse=True)
def reset_class_state(monkeypatch):
    monkeypatch.setattr(shim_server.ShimHandler, "allow_anonymous", True)
    monkeypatch.setattr(shim_server.ShimHandler, "rbac_enabled", False)
    monkeypatch.setattr(shim_server.ShimHandler, "pod_state_check", False)
    monkeypatch.setattr(shim_server.ShimHandler, "pod_watch_check", False)


def _build_handler(path: str, store, state, local_runtime, body: bytes = b"", method: str = "GET"):
    req = make_handler(path, method=method, body=body)
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.wfile = BytesIO()
    handler.server = SimpleNamespace(
        store=store,
        state=state,
        runtime=local_runtime,
        _runtime_base=local_runtime,
        _runtime_cache={},
        _agent_url=None,
    )
    handler.store = store
    handler.state = state
    return handler


def _response_body_bytes(handler) -> bytes:
    raw = handler.wfile.getvalue()
    if b"\r\n\r\n" in raw:
        return raw.split(b"\r\n\r\n", 1)[1]
    return raw


def _remote_container() -> dict:
    return {
        "name": "netfs-core-a-writer-rev2-0",
        "labels": {
            "ae.namespace": "default",
            "ae.app": "netfs-core-a-writer",
            "ae.pod_name": "netfs-core-a-writer-rev2-0",
            "ae.node": "core-a--hub",
        },
        "uid": "remote-pod-uid",
        "pod_ip": "10.42.0.6",
        "host_ip": "127.0.0.1",
        "running": True,
    }


def test_pod_list_includes_remote_controller_pod_and_local_infra(monkeypatch, store):
    state = RemoteStateStub()
    local_runtime = RuntimeStub(
        containers=[
            {
                "name": "k1s-core-apishim",
                "labels": {"ae.namespace": "default", "ae.pod_name": "k1s-core-apishim"},
                "uid": "infra-uid",
                "running": True,
            }
        ]
    )
    remote_runtime = RuntimeStub(containers=[_remote_container()])

    monkeypatch.setattr(
        shim_server.ShimHandler,
        "_runtime_for_endpoint",
        lambda self, endpoint: remote_runtime if endpoint == REMOTE_ENDPOINT else local_runtime,
    )

    handler = _build_handler("/api/v1/namespaces/default/pods", store, state, local_runtime)
    handler.do_GET()

    payload = json.loads(_response_body_bytes(handler).decode("utf-8"))
    items = {item["metadata"]["name"]: item for item in payload["items"]}
    assert {"netfs-core-a-writer-rev2-0", "k1s-core-apishim"} <= set(items)
    remote_pod = items["netfs-core-a-writer-rev2-0"]
    assert remote_pod["spec"]["nodeName"] == "core-a--hub"
    assert remote_pod["status"]["podIP"] == "10.42.0.6"
    assert remote_pod["metadata"]["uid"] == "remote-pod-uid"


def test_exec_routes_remote_pod_to_remote_runtime(monkeypatch, store):
    state = RemoteStateStub()
    local_runtime = RuntimeStub()
    remote_runtime = RuntimeStub(containers=[_remote_container()])
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        shim_server.ShimHandler,
        "_runtime_for_endpoint",
        lambda self, endpoint: remote_runtime if endpoint == REMOTE_ENDPOINT else local_runtime,
    )

    def _capture_exec(
        self,
        *,
        namespace: str,
        pod_name: str,
        command: list[str],
        container: str | None,
        tty: bool,
        want_stdin: bool,
        want_stdout: bool,
        want_stderr: bool,
        runtime,
    ):
        captured["namespace"] = namespace
        captured["pod_name"] = pod_name
        captured["command"] = list(command)
        captured["runtime"] = runtime

    monkeypatch.setattr(shim_server.ShimHandler, "_handle_exec_ws", _capture_exec)

    handler = _build_handler(
        "/api/v1/namespaces/default/pods/netfs-core-a-writer-rev2-0/exec?command=cat&command=/data/hello.txt",
        store,
        state,
        local_runtime,
    )
    handler.headers["Upgrade"] = "websocket"
    handler.do_GET()

    assert captured["namespace"] == "default"
    assert captured["pod_name"] == "netfs-core-a-writer-rev2-0"
    assert captured["command"] == ["cat", "/data/hello.txt"]
    assert captured["runtime"] is remote_runtime


def test_logs_route_reads_from_remote_runtime(monkeypatch, store):
    state = RemoteStateStub()
    local_runtime = RuntimeStub(log_lines=["from-local"])
    remote_runtime = RuntimeStub(containers=[_remote_container()], log_lines=["from-remote\n"])

    monkeypatch.setattr(
        shim_server.ShimHandler,
        "_runtime_for_endpoint",
        lambda self, endpoint: remote_runtime if endpoint == REMOTE_ENDPOINT else local_runtime,
    )

    handler = _build_handler(
        "/api/v1/namespaces/default/pods/netfs-core-a-writer-rev2-0/log",
        store,
        state,
        local_runtime,
    )
    handler.do_GET()

    assert remote_runtime.log_calls == [("netfs-core-a-writer-rev2-0", False, 100, None)]
    assert local_runtime.log_calls == []
    assert "from-remote" in _response_body_bytes(handler).decode("utf-8")


def test_exec_returns_502_when_controller_maps_pod_to_unreachable_runtime(monkeypatch, store):
    state = RemoteStateStub()
    local_runtime = RuntimeStub()
    failing_runtime = RuntimeStub(fail_list="agent unavailable")

    monkeypatch.setattr(
        shim_server.ShimHandler,
        "_runtime_for_endpoint",
        lambda self, endpoint: failing_runtime if endpoint == REMOTE_ENDPOINT else local_runtime,
    )
    monkeypatch.setattr(shim_server.ShimHandler, "_json_status", lambda self, code, **_kw: setattr(self, "_status_code", code))

    handler = _build_handler(
        "/api/v1/namespaces/default/pods/netfs-core-a-writer-rev2-0/exec?command=sh",
        store,
        state,
        local_runtime,
    )
    handler.headers["Upgrade"] = "websocket"
    handler.do_GET()

    assert getattr(handler, "_status_code", None) == 502
