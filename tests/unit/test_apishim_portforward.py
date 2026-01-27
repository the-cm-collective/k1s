from io import BytesIO
from types import SimpleNamespace

import pytest

from ae.apishim import server as shim_server
from ae.apishim.store import ObjectStore


def _make_handler(path: str, headers: dict[str, str]) -> shim_server.ShimHandler:
    class DummySocket:
        def __init__(self) -> None:
            self.path = path
            self.command = "GET"
            self.headers = headers
            self.rbufsize = -1
            self._rbuf = BytesIO()
            self._wbuf = BytesIO()

        def makefile(self, mode, *_args, **_kwargs):
            if "r" in mode:
                return self._rbuf
            return self._wbuf

        def settimeout(self, _t):
            return None

        def setsockopt(self, *_a, **_k):
            return None

        def close(self) -> None:
            return None

    req = DummySocket()
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = path
    handler.command = "GET"
    handler.headers = headers
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.wfile = BytesIO()
    return handler


class DummyRuntime:
    def __init__(self, container_info: dict) -> None:
        self._container_info = container_info

    def list_containers_info(self):
        return [self._container_info]


@pytest.fixture
def store(tmp_path):
    return ObjectStore(tmp_path / "apishim-portforward.db")


def _base_container_info() -> dict:
    return {
        "name": "echo-rev1-0",
        "labels": {
            "ae.namespace": "default",
            "ae.app": "echo",
            "ae.replica_id": "echo-rev1-0",
        },
        "running": True,
        "host_ip": "192.0.2.10",
        "host_ports": [31080],
        "port_map": {8080: 31080},
    }


def _configure_auth(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("AE_APISHIM_PORTFORWARD_TOKEN", "p")
    monkeypatch.setenv("AE_API_PF_SCOPE", "default/echo")
    shim_server.ShimHandler.portforward_token = "p"  # noqa: S105 - test token
    shim_server.ShimHandler.rbac_enabled = False
    return {"Authorization": "Bearer p", "Upgrade": "SPDY/3.1"}


def test_portforward_prefers_pod_ip_container_port(monkeypatch, store):
    captured: dict[str, object] = {}

    def _capture(_self, host: str, ports: list[int], _ep_map=None):  # noqa: ANN001
        captured["host"] = host
        captured["ports"] = list(ports)

    monkeypatch.setattr(shim_server.ShimHandler, "_handle_port_forward_spdy", _capture)

    info = _base_container_info()
    info["pod_ip"] = "10.0.0.23"
    runtime = DummyRuntime(info)

    headers = _configure_auth(monkeypatch)
    handler = _make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/portforward?ports=8080",
        headers,
    )
    handler.server = SimpleNamespace(store=store, state=store, runtime=runtime)

    handler.do_GET()

    assert captured["host"] == "10.0.0.23"
    assert captured["ports"] == [8080]


def test_portforward_maps_container_port_to_host_without_pod_ip(monkeypatch, store):
    captured: dict[str, object] = {}

    def _capture(_self, host: str, ports: list[int], _ep_map=None):  # noqa: ANN001
        captured["host"] = host
        captured["ports"] = list(ports)

    monkeypatch.setattr(shim_server.ShimHandler, "_handle_port_forward_spdy", _capture)

    info = _base_container_info()
    runtime = DummyRuntime(info)

    headers = _configure_auth(monkeypatch)
    handler = _make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/portforward?ports=8080",
        headers,
    )
    handler.server = SimpleNamespace(store=store, state=store, runtime=runtime)

    handler.do_GET()

    assert captured["host"] == "192.0.2.10"
    assert captured["ports"] == [31080]
