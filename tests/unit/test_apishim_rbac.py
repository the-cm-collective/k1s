import json
from types import SimpleNamespace

import pytest

from ae.apishim import server as shim_server
from ae.apishim.store import ObjectStore, K8sObject


def make_handler(path: str, method: str = "GET", headers=None):
    headers = headers or {}
    class DummyRequest:
        def __init__(self):
            self.path = path
            self.command = method
            self.headers = headers
            self.responses = []
            self._body = b""
            self.wfile = SimpleNamespace(write=lambda b: self.responses.append(b))
        def send_response(self, code, message=None):
            self.responses.append(code)
        def send_header(self, k, v):
            pass
        def end_headers(self):
            pass
    return DummyRequest()


@pytest.fixture
def store(tmp_path):
    s = ObjectStore(tmp_path / "apishim.db")
    return s


def test_apishim_rbac_allow_get_with_admin_token(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    handler = shim_server.ShimHandler(make_handler("/api/v1/namespaces"), ("127.0.0.1", 0), None)
    handler.store = store
    handler.state = None  # not used
    handler.do_GET()
    assert 200 in handler.responses


def test_apishim_rbac_deny_get_without_token(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    handler = shim_server.ShimHandler(make_handler("/api/v1/namespaces"), ("127.0.0.1", 0), None)
    handler.store = store
    handler.state = None
    handler.do_GET()
    assert 401 in handler.responses or 403 in handler.responses


def test_apishim_rbac_eval_rolebinding(monkeypatch, store):
    # enable RBAC with evaluation
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_RBAC_EVAL", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    # Create a role and rolebinding permitting list on services
    role = K8sObject("rbac.authorization.k8s.io", "v1", "roles", "default", "viewer", {"name": "viewer", "namespace": "default"}, {"rules": [{"verbs": ["list"], "resources": ["services"]}]}, {}, 1)
    store.upsert("rbac.authorization.k8s.io", "v1", "roles", "default", "viewer", role.metadata, role.spec)
    rb = K8sObject("rbac.authorization.k8s.io", "v1", "rolebindings", "default", "bind", {"name": "bind", "namespace": "default"}, {"subjects": [{"kind": "User", "name": "admin"}], "roleRef": {"name": "viewer"}}, {}, 2)
    store.upsert("rbac.authorization.k8s.io", "v1", "rolebindings", "default", "bind", rb.metadata, rb.spec)
    handler = shim_server.ShimHandler(make_handler("/api/v1/services", headers={"Authorization": "Bearer a"}), ("127.0.0.1", 0), None)
    handler.store = store
    handler.state = None
    handler.do_GET()
    assert 200 in handler.responses
