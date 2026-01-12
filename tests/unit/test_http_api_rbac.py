import json
from types import SimpleNamespace

import pytest

from ae.observability import http_api


def make_handler(path: str, method: str = "POST", body: dict | None = None, headers=None):
    headers = headers or {}
    payload = json.dumps(body or {}).encode()

    class DummyRequest:
        def __init__(self):
            self.path = path
            self.command = method
            self.headers = headers
            self.rfile = SimpleNamespace(read=lambda n: payload)
            self.wfile = SimpleNamespace(write=lambda b: None)
            self.responses = []

        def send_response(self, code, message=None):
            self.responses.append(code)

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

    return DummyRequest()


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    monkeypatch.delenv("AE_API_RBAC", raising=False)
    monkeypatch.delenv("AE_API_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("AE_API_SCALER_TOKEN", raising=False)
    monkeypatch.delenv("AE_API_READ_TOKEN", raising=False)
    monkeypatch.delenv("AE_API_MUTATIONS", raising=False)
    yield


def test_apply_denied_by_rbac(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_ADMIN_TOKEN", "a")
    req = make_handler("/apply", headers={"Authorization": "Bearer a"}, body={"metadata": {"name": "app"}})
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    # inject apply_fn
    called = {}

    def _apply(payload):
        called["ok"] = True
        return {"ok": True}

    handler.apply_fn = _apply
    handler.do_POST()
    assert called.get("ok") is True  # rbac allows admin


def test_apply_forbidden_for_reader(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_READ_TOKEN", "r")
    req = make_handler("/apply", headers={"Authorization": "Bearer r"}, body={"metadata": {"name": "app"}})
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    called = {}

    def _apply(payload):
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

    def _del(app, purge):
        called["app"] = app
        called["purge"] = purge
        return {"ok": True}

    handler.delete_fn = _del
    handler.do_POST()
    assert called.get("app") is None
    assert 403 in req.responses
