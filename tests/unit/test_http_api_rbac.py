import json
import logging
from io import BytesIO
from types import SimpleNamespace

import pytest

from ae.controller.authority import LeaderInfo, NotLeaderError
from ae.controller.inference_api import apply_manifest_payload
from ae.controller.state import RegistryConflictError, SQLiteStateStore
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


def test_apply_returns_not_leader_payload(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_ADMIN_TOKEN", "a")
    req = make_handler(
        "/apply", headers={"Authorization": "Bearer a"}, body={"metadata": {"name": "app"}}
    )
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.headers = {
        "Authorization": "Bearer a",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)

    def _apply(_payload):
        raise NotLeaderError(
            LeaderInfo(
                controller_id="ctrl-b",
                controller_epoch=12,
                lease_id=501,
                advertise_addr="http://ctrl-b:9108",
                acquired_at=None,
                version="v1",
            )
        )

    handler.apply_fn = _apply
    handler.do_POST()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    assert 409 in req.responses
    assert '"error": "not_leader"' in body
    assert '"controller_id": "ctrl-b"' in body


def test_apply_logs_server_exception(monkeypatch, caplog):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_ADMIN_TOKEN", "a")
    req = make_handler(
        "/apply", headers={"Authorization": "Bearer a"}, body={"metadata": {"name": "app"}}
    )
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.headers = {
        "Authorization": "Bearer a",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
        "User-Agent": "pytest",
    }
    handler.rfile = BytesIO(req._payload)

    def _apply(_payload):
        raise RuntimeError("boom")

    handler.apply_fn = _apply
    with caplog.at_level(logging.ERROR, logger="ae.observability.http_api"):
        handler.do_POST()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    assert 500 in req.responses
    assert '"error": "boom"' in body
    assert "apply handler failed source=api app=app" in caplog.text


def test_scale_returns_resource_version_conflict(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_SCALER_TOKEN", "s")
    req = make_handler("/scale/app", headers={"Authorization": "Bearer s"}, body={"replicas": 2})
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.headers = {
        "Authorization": "Bearer s",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)

    def _scale(_app, _replicas):
        raise RegistryConflictError("app", expected=3, actual=4)

    handler.scale_fn = _scale
    handler.do_POST()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    assert 409 in req.responses
    assert '"error": "resource_version_conflict"' in body
    assert '"expected": 3' in body
    assert '"actual": 4' in body


def test_rollout_restart_allowed_for_admin(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_ADMIN_TOKEN", "a")
    req = make_handler("/rollout/restart/app", headers={"Authorization": "Bearer a"})
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.headers = {
        "Authorization": "Bearer a",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)
    called = {}

    def _restart(app):
        called["app"] = app
        return {"app": app, "status": "ready", "restartAt": "2026-06-02T00:00:00+00:00"}

    handler.rollout_restart_fn = _restart
    handler.do_POST()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    assert called["app"] == "app"
    assert 200 in req.responses
    assert '"restartAt": "2026-06-02T00:00:00+00:00"' in body


def test_rollout_restart_denied_for_reader(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_READ_TOKEN", "r")
    req = make_handler("/rollout/restart/app", headers={"Authorization": "Bearer r"})
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.headers = {
        "Authorization": "Bearer r",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)
    called = {}

    def _restart(app):
        called["app"] = app
        return {"ok": True}

    handler.rollout_restart_fn = _restart
    handler.do_POST()

    assert called.get("app") is None
    assert 403 in req.responses


def test_rollout_restart_returns_resource_version_conflict(monkeypatch):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_ADMIN_TOKEN", "a")
    req = make_handler("/rollout/restart/app", headers={"Authorization": "Bearer a"})
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.headers = {
        "Authorization": "Bearer a",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)

    def _restart(_app):
        raise RegistryConflictError("app", expected=5, actual=6)

    handler.rollout_restart_fn = _restart
    handler.do_POST()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    assert 409 in req.responses
    assert '"error": "resource_version_conflict"' in body
    assert '"expected": 5' in body
    assert '"actual": 6' in body


def test_require_role_accepts_query_token_for_get(monkeypatch):
    monkeypatch.setenv("AE_API_READ_TOKEN", "reader")
    handler = object.__new__(http_api._ApiHandler)
    handler.path = "/dashboard/sse/events?app=blue&limit=50&token=reader"
    handler.command = "GET"
    handler.headers = {}

    assert handler._require_role("read") is True


def test_require_role_rejects_query_token_for_post(monkeypatch):
    monkeypatch.setenv("AE_API_READ_TOKEN", "reader")
    handler = object.__new__(http_api._ApiHandler)
    handler.path = "/api/apishim/session?token=reader"
    handler.command = "POST"
    handler.headers = {}

    assert handler._require_role("read") is False


def test_dashboard_js_public_route_serves_fallback(monkeypatch):
    monkeypatch.setenv("AE_DASHBOARD", "1")
    handler = object.__new__(http_api._ApiHandler)
    handler.path = "/dashboard.js"
    handler.command = "GET"
    handler.headers = {}
    handler.wfile = BytesIO()
    statuses: list[int] = []
    headers: dict[str, str] = {}

    handler.send_response = lambda code, _message=None: statuses.append(code)  # type: ignore[method-assign]
    handler.send_header = lambda key, value: headers.__setitem__(key, value)  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]

    handler.do_GET()

    assert statuses == [200]
    assert headers["Content-Type"] == "application/javascript; charset=utf-8"
    assert "dashboard JavaScript is embedded" in handler.wfile.getvalue().decode("utf-8")


def test_inference_discovery_get_respects_read_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("AE_API_READ_TOKEN", "r")
    monkeypatch.setenv("AE_API_READ_SCOPE", "ml--demo-cell")
    store = SQLiteStateStore(tmp_path / "state.db")
    apply_manifest_payload(
        store,
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCell",
            "metadata": {"name": "demo-cell", "namespace": "ml"},
            "spec": {
                "model": {"modelId": "llama", "localPath": "/models/llama"},
                "members": [{"siteId": "site-a", "nodeId": "node-a", "gpuCount": 1}],
            },
        },
        source="test",
    )
    req = make_handler(
        "/inference/cells/ml/demo-cell",
        method="GET",
        headers={"Authorization": "Bearer r"},
    )
    monkeypatch.setattr(http_api._ApiHandler, "store", store, raising=False)
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.store = store
    handler.path = "/inference/cells/ml/demo-cell"
    handler.command = "GET"
    handler.headers = {"Authorization": "Bearer r"}

    handler.do_GET()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    assert 200 in req.responses
    assert '"kind": "InferenceCell"' in body
    assert '"name": "demo-cell"' in body


def test_inference_delete_route_requires_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_ADMIN_TOKEN", "a")
    store = SQLiteStateStore(tmp_path / "state.db")
    apply_manifest_payload(
        store,
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCell",
            "metadata": {"name": "demo-cell", "namespace": "ml"},
            "spec": {"model": {"modelId": "llama", "localPath": "/models/llama"}},
        },
        source="test",
    )
    req = make_handler(
        "/inference/delete/cells/demo-cell?namespace=ml",
        headers={"Authorization": "Bearer a"},
    )
    monkeypatch.setattr(http_api._ApiHandler, "store", store, raising=False)
    monkeypatch.setattr(
        http_api._ApiHandler,
        "inference_delete_fn",
        lambda kind, name, namespace: {
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "removed": store.get_inference_cell(name, namespace=namespace) is not None,
        },
    )
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.store = store
    handler.path = "/inference/delete/cells/demo-cell?namespace=ml"
    handler.command = "POST"
    handler.headers = {
        "Authorization": "Bearer a",
        "Content-Length": str(len(req._payload)),
        "Content-Type": "application/json",
    }
    handler.rfile = BytesIO(req._payload)
    handler.inference_delete_fn = lambda kind, name, namespace: {
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "removed": store.get_inference_cell(name, namespace=namespace) is not None,
    }

    handler.do_POST()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    assert 200 in req.responses
    assert '"removed": true' in body


# ruff: noqa: E501
