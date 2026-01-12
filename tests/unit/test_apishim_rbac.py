import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from ae.apishim import server as shim_server
from ae.apishim.store import ObjectStore, K8sObject


def make_handler(path: str, method: str = "GET", headers=None, body: bytes = b""):
    headers = headers or {}

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
            self.responses = []
            self._body = body
            self.wfile = SimpleNamespace(write=lambda b: self.responses.append(b))
        def send_response(self, code, message=None):
            self.responses.append(code)
        def send_header(self, k, v):
            pass
        def end_headers(self):
            pass
        def close(self):
            pass

    return DummyRequest()


@pytest.fixture
def store(tmp_path):
    s = ObjectStore(tmp_path / "apishim.db")
    return s


def test_apishim_rbac_allow_get_with_admin_token(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    req = make_handler("/api/v1/namespaces")
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.request_version = "HTTP/1.1"
    handler.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler.store = store
    handler.state = None  # not used
    handler.requestline = f"{handler.command} {handler.path} HTTP/1.1"
    handler.wfile = BytesIO()
    handler.do_GET()
    assert 200 in handler.responses


def test_apishim_rbac_deny_get_without_token(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    req = make_handler("/api/v1/namespaces")
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler.store = store
    handler.state = None
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{handler.command} {handler.path} HTTP/1.1"
    handler.wfile = BytesIO()
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
    req = make_handler("/api/v1/services", headers={"Authorization": "Bearer a"})
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler.store = store
    handler.state = None
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{handler.command} {handler.path} HTTP/1.1"
    handler.wfile = BytesIO()
    handler.do_GET()
    assert 200 in handler.responses


def test_apishim_rbac_exec_requires_admin(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    # reader token
    monkeypatch.setenv("AE_APISHIM_READ_TOKEN", "r")
    # admin allowed
    req = make_handler("/api/v1/namespaces/default/pods/p1/exec?command=sh", headers={"Authorization": "Bearer a"})
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler.store = store
    handler.state = None
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{handler.command} {handler.path} HTTP/1.1"
    handler.wfile = BytesIO()
    handler.do_GET()
    assert 101 in handler.responses or 200 in handler.responses  # upgrade success
    # read should be denied
    req2 = make_handler("/api/v1/namespaces/default/pods/p1/exec?command=sh", headers={"Authorization": "Bearer r"})
    handler2 = shim_server.ShimHandler(req2, ("127.0.0.1", 0), None)
    handler2.path = req2.path
    handler2.command = req2.command
    handler2.headers = req2.headers
    handler2.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler2.store = store
    handler2.state = None
    handler2.request_version = "HTTP/1.1"
    handler2.requestline = f"{handler2.command} {handler2.path} HTTP/1.1"
    handler2.wfile = BytesIO()
    handler2.do_GET()
    assert 403 in handler2.responses or 401 in handler2.responses
