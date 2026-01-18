import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from ae.observability import http_api


def make_handler(path: str, method: str = "POST", body: dict | None = None, headers=None):
    headers = headers or {}
    payload = json.dumps(body or {}).encode()
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

    class DummyRequest:
        def __init__(self):
            start = (
                f"{method} {path} HTTP/1.1\r\n"
                f"{header_lines}"
                f"Content-Length: {len(payload)}\r\n"
                "Content-Type: application/json\r\n"
                "\r\n"
            ).encode()
            self._raw = start + payload
            self._wbuf = bytearray()
            self._payload = payload
            self.path = path
            self.command = method
            self.headers = headers
            self.responses: list[int] = []

        def send_response(self, code, _message=None):
            self.responses.append(code)

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

        # BaseHTTPRequestHandler.setup expects these
        def makefile(self, mode, *_args, **_kwargs):
            if "r" in mode:
                return BytesIO(self._raw)
            return BytesIO(self._wbuf)

        def settimeout(self, _t):
            pass

        def setsockopt(self, *a, **k):
            pass

        def close(self):
            pass

        def sendall(self, data):
            self._wbuf.extend(data)

        @property
        def rfile(self):
            return BytesIO(self._raw)

        @property
        def wfile(self):
            return SimpleNamespace(write=lambda b: self._wbuf.extend(b))

    return DummyRequest()


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    monkeypatch.delenv("AE_API_RBAC", raising=False)
    monkeypatch.delenv("AE_API_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("AE_API_SCALER_TOKEN", raising=False)
    monkeypatch.delenv("AE_API_READ_TOKEN", raising=False)
    monkeypatch.setenv("AE_API_MUTATIONS", "1")
    yield


def test_apply_denied_by_rbac(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_ADMIN_TOKEN", "a")
    req = make_handler(
        "/apply", headers={"Authorization": "Bearer a"}, body={"metadata": {"name": "app"}}
    )
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    # inject apply_fn
    called = {}
    handler.headers = {
        "Authorization": "Bearer a",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)

    def _apply(_payload):
        called["ok"] = True
        return {"ok": True}

    handler.apply_fn = _apply
    handler.do_POST()
    assert called.get("ok") is True  # rbac allows admin


def test_apply_forbidden_for_reader(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_READ_TOKEN", "r")
    req = make_handler(
        "/apply", headers={"Authorization": "Bearer r"}, body={"metadata": {"name": "app"}}
    )
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    called = {}
    handler.headers = {
        "Authorization": "Bearer r",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)

    def _apply(_payload):
        called["ok"] = True
        return {"ok": True}

    handler.apply_fn = _apply
    handler.do_POST()
    # Should be forbidden, so apply not called
    assert called.get("ok") is None
    assert 403 in req.responses


def test_scale_allowed_for_scaler(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_SCALER_TOKEN", "s")
    req = make_handler("/scale/app", headers={"Authorization": "Bearer s"}, body={"replicas": 2})
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    called = {}
    handler.headers = {
        "Authorization": "Bearer s",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)

    def _scale(app, replicas):
        called["app"] = app
        called["replicas"] = replicas
        return {"ok": True}

    handler.scale_fn = _scale
    handler.do_POST()
    assert called.get("app") == "app"
    assert called.get("replicas") == 2


def test_delete_denied_for_reader(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_READ_TOKEN", "r")
    req = make_handler("/delete/app", headers={"Authorization": "Bearer r"})
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    called = {}
    handler.headers = {
        "Authorization": "Bearer r",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)

    def _del(app, purge):
        called["app"] = app
        called["purge"] = purge
        return {"ok": True}

    handler.delete_fn = _del
    handler.do_POST()
    assert called.get("app") is None
    assert 403 in req.responses


# ruff: noqa: E501
