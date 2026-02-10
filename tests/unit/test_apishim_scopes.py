import time
from io import BytesIO
from types import SimpleNamespace

import pytest

from ae.apishim import server as shim_server
from ae.apishim.store import ObjectStore


def make_handler(path: str, method: str = "GET", headers=None, body: bytes = b""):
    headers = headers or {}
    if body and "Content-Length" not in headers:
        headers["Content-Length"] = str(len(body))

    class DummySocket:
        def __init__(self, body: bytes):
            self._rbuf = BytesIO(body)
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

        def setsockopt(self, *a, **k):
            pass

        def close(self):
            pass

    class DummyRequest(DummySocket):
        def __init__(self):
            super().__init__(body)
            self._response_codes = []
            self._body = body
            self.wfile = SimpleNamespace(write=lambda b: self._response_codes.append(b))

        def send_response(self, code, _message=None):
            self._response_codes.append(code)

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

        def close(self):
            pass

    return DummyRequest()


class DummyRuntime:
    def __init__(self, running: bool = True):
        self._running = running

    def list_containers_info(self):
        return [
            {
                "name": "echo-rev1-0",
                "labels": {
                    "ae.namespace": "default",
                    "ae.app": "echo",
                    "ae.pod_name": "echo-rev1-0",
                },
                "running": bool(self._running),
            }
        ]


@pytest.fixture
def store(tmp_path):
    return ObjectStore(tmp_path / "apishim.db")


@pytest.fixture(autouse=True)
def capture_response_codes(monkeypatch):
    orig_send = shim_server.ShimHandler.send_response

    def _capture(self, code, message=None):
        codes = getattr(self, "_response_codes", None)
        if codes is None:
            codes = []
            setattr(self, "_response_codes", codes)
        codes.append(code)
        return orig_send(self, code, message)

    monkeypatch.setattr(shim_server.ShimHandler, "send_response", _capture)


def _build_handler(req, store, runtime, state=None):
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.server = SimpleNamespace(store=store, state=state or store, runtime=runtime)
    handler.store = store
    handler.state = None
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{handler.command} {handler.path} HTTP/1.1"
    handler.wfile = BytesIO()
    return handler


def test_exec_scope_denies(monkeypatch, store):
    monkeypatch.setenv("AE_API_EXEC_SCOPE", "default/other")
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.exec_token = "e"
    shim_server.ShimHandler.rbac_enabled = False
    req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/exec?command=sh",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    handler = _build_handler(req, store, DummyRuntime())
    handler.do_GET()
    assert 403 in handler._response_codes


def test_exec_scope_allows(monkeypatch, store):
    monkeypatch.setenv("AE_API_EXEC_SCOPE", "default/echo")
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.exec_token = "e"
    shim_server.ShimHandler.rbac_enabled = False
    req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/exec?command=sh",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    handler = _build_handler(req, store, DummyRuntime())
    handler.do_GET()
    assert 403 not in handler._response_codes and 401 not in handler._response_codes


def test_portforward_scope_denies(monkeypatch, store):
    monkeypatch.setenv("AE_API_PF_SCOPE", "default/other")
    monkeypatch.setenv("AE_APISHIM_PORTFORWARD_TOKEN", "p")
    shim_server.ShimHandler.portforward_token = "p"
    shim_server.ShimHandler.rbac_enabled = False
    req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/portforward?ports=8080",
        headers={"Authorization": "Bearer p", "Upgrade": "websocket"},
    )
    handler = _build_handler(req, store, DummyRuntime())
    handler.do_GET()
    assert 403 in handler._response_codes


def test_exec_pod_state_check_denies(monkeypatch, store):
    class DummyState:
        def list_pod_nodes(self, _app):  # noqa: ANN001
            return []

    monkeypatch.setenv("AE_APISHIM_POD_STATE_CHECK", "1")
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.exec_token = "e"
    shim_server.ShimHandler.pod_state_check = True
    shim_server.ShimHandler.rbac_enabled = False
    req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/exec?command=sh",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    handler = _build_handler(req, store, DummyRuntime(), state=DummyState())
    handler.do_GET()
    assert 409 in handler._response_codes


def test_exec_pod_watch_check_denies(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_POD_WATCH_CHECK", "1")
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.exec_token = "e"
    shim_server.ShimHandler.pod_watch_check = True
    shim_server.ShimHandler.pod_watch_ttl = 60.0
    shim_server.ShimHandler.pod_watch_cache = {}
    shim_server.ShimHandler.rbac_enabled = False
    req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/exec?command=sh",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    handler = _build_handler(req, store, DummyRuntime())
    handler.do_GET()
    assert 409 in handler._response_codes


def test_exec_pod_watch_check_allows_when_cached(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_POD_WATCH_CHECK", "1")
    monkeypatch.setenv("AE_APISHIM_READ_TOKEN", "r")
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.read_token = "r"
    shim_server.ShimHandler.exec_token = "e"
    shim_server.ShimHandler.pod_watch_check = True
    shim_server.ShimHandler.pod_watch_ttl = 60.0
    shim_server.ShimHandler.pod_watch_cache = {}
    shim_server.ShimHandler.rbac_enabled = False
    list_req = make_handler(
        "/api/v1/namespaces/default/pods",
        headers={"Authorization": "Bearer r"},
    )
    list_handler = _build_handler(list_req, store, DummyRuntime())
    list_handler.do_GET()
    exec_req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/exec?command=sh",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    exec_handler = _build_handler(exec_req, store, DummyRuntime())
    exec_handler.do_GET()
    assert 409 not in exec_handler._response_codes


def test_exec_pod_uid_mismatch_denies(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.exec_token = "e"
    shim_server.ShimHandler.rbac_enabled = False
    req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/exec?command=sh&podUID=bogus",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    handler = _build_handler(req, store, DummyRuntime())
    handler.do_GET()
    assert 409 in handler._response_codes


def test_exec_pod_rv_mismatch_denies(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.exec_token = "e"
    shim_server.ShimHandler.pod_watch_check = False
    shim_server.ShimHandler.pod_watch_ttl = 60.0
    shim_server.ShimHandler.pod_watch_cache = {
        ("default", "echo-rev1-0"): ("uid-1", 5, time.time())
    }
    shim_server.ShimHandler.rbac_enabled = False
    req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/exec?command=sh&resourceVersion=7",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    handler = _build_handler(req, store, DummyRuntime())
    handler.do_GET()
    assert 409 in handler._response_codes


def test_exec_pod_rv_matches_allows(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.exec_token = "e"
    shim_server.ShimHandler.pod_watch_check = False
    shim_server.ShimHandler.pod_watch_ttl = 60.0
    shim_server.ShimHandler.pod_watch_cache = {
        ("default", "echo-rev1-0"): ("uid-1", 7, time.time())
    }
    shim_server.ShimHandler.rbac_enabled = False
    req = make_handler(
        "/api/v1/namespaces/default/pods/echo-rev1-0/exec?command=sh&resourceVersion=7",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    handler = _build_handler(req, store, DummyRuntime())
    handler.do_GET()
    assert 409 not in handler._response_codes
