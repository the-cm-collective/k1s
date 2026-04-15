from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

from ae.apishim import server as shim_server
from ae.apishim.store import ObjectStore


def _handler(store: ObjectStore, path: str):
    class DummySocket:
        def __init__(self):
            self._rbuf = BytesIO()
            self._wbuf = BytesIO()
            self.timeout = None
            self.path = path
            self.command = "GET"
            self.headers = {}

        def makefile(self, mode, *_args, **_kwargs):
            if "r" in mode:
                return self._rbuf
            return self._wbuf

        def settimeout(self, t):
            self.timeout = t

        def setsockopt(self, *a, **k):
            _ = (a, k)
            pass

        def close(self):
            pass

    shim_server.ShimHandler.admin_token = None
    shim_server.ShimHandler.read_token = None
    shim_server.ShimHandler.rbac_enabled = False
    req = DummySocket()
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = path
    handler.command = "GET"
    handler.headers = {}
    handler.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler.store = store
    handler.state = None
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"GET {handler.path} HTTP/1.1"
    handler.rfile = BytesIO()
    handler.wfile = BytesIO()

    status: dict[str, int | None] = {"code": None}
    handler.send_response = lambda code, _msg=None: status.__setitem__("code", code)
    handler.send_header = lambda *_a, **_k: None
    handler.end_headers = lambda: None
    return handler, status


def test_apishim_internal_version_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AE_BUILD_SHA", "sha-456")
    monkeypatch.setenv("AE_BUILD_DATE", "2026-03-18")
    store = ObjectStore(tmp_path / "apishim.db")
    handler, status = _handler(store, "/__ae/version")

    handler.do_GET()

    assert status["code"] == 200
    body = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert body["component"] == "apishim"
    assert body["sha"] == "sha-456"
    assert body["date"] == "2026-03-18"
