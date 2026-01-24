import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from ae.apishim import server as shim_server
from ae.apishim.store import K8sObject, ObjectStore


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


@pytest.fixture
def store(tmp_path):
    s = ObjectStore(tmp_path / "apishim.db")
    return s


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


def test_apishim_rbac_allow_get_with_admin_token(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = False
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = None
    req = make_handler("/api/v1/namespaces", headers={"Authorization": "Bearer a"})
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
    assert 200 in handler._response_codes


def test_apishim_rbac_deny_get_without_token(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = False
    shim_server.ShimHandler.admin_token = None
    shim_server.ShimHandler.read_token = None
    shim_server.ShimHandler.allow_anonymous = False
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
    assert 401 in handler._response_codes or 403 in handler._response_codes


def test_apishim_allow_anonymous(monkeypatch, store):
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    monkeypatch.setenv("AE_APISHIM_ALLOW_ANON", "1")
    shim_server.ShimHandler.rbac_enabled = False
    shim_server.ShimHandler.rbac_eval_roles = False
    shim_server.ShimHandler.admin_token = None
    shim_server.ShimHandler.read_token = None
    monkeypatch.setattr(shim_server.ShimHandler, "allow_anonymous", True)
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
    assert 200 in handler._response_codes


def test_apishim_rbac_eval_rolebinding(monkeypatch, store):
    # enable RBAC with evaluation
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_RBAC_EVAL", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = True
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = None
    # Create a role and rolebinding permitting list on services
    role = K8sObject(
        "rbac.authorization.k8s.io",
        "v1",
        "roles",
        "default",
        "viewer",
        {"name": "viewer", "namespace": "default"},
        {"rules": [{"verbs": ["list"], "resources": ["services"]}]},
        {},
        1,
    )
    store.upsert(
        "rbac.authorization.k8s.io", "v1", "roles", "default", "viewer", role.metadata, role.spec
    )
    rb = K8sObject(
        "rbac.authorization.k8s.io",
        "v1",
        "rolebindings",
        "default",
        "bind",
        {"name": "bind", "namespace": "default"},
        {"subjects": [{"kind": "User", "name": "admin"}], "roleRef": {"name": "viewer"}},
        {},
        2,
    )
    store.upsert(
        "rbac.authorization.k8s.io", "v1", "rolebindings", "default", "bind", rb.metadata, rb.spec
    )
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
    assert 200 in handler._response_codes


def test_apishim_rbac_exec_requires_exec_or_admin(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    # reader token
    monkeypatch.setenv("AE_APISHIM_READ_TOKEN", "r")
    monkeypatch.setenv("AE_APISHIM_EXEC_TOKEN", "e")
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = False
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = "r"
    shim_server.ShimHandler.exec_token = "e"
    # admin allowed
    req = make_handler(
        "/api/v1/namespaces/default/pods/p1/exec?command=sh",
        headers={"Authorization": "Bearer a", "Upgrade": "websocket"},
    )
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
    assert 403 not in handler._response_codes and 401 not in handler._response_codes
    # exec token allowed
    req_exec = make_handler(
        "/api/v1/namespaces/default/pods/p1/exec?command=sh",
        headers={"Authorization": "Bearer e", "Upgrade": "websocket"},
    )
    handler_exec = shim_server.ShimHandler(req_exec, ("127.0.0.1", 0), None)
    handler_exec.path = req_exec.path
    handler_exec.command = req_exec.command
    handler_exec.headers = req_exec.headers
    handler_exec.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler_exec.store = store
    handler_exec.state = None
    handler_exec.request_version = "HTTP/1.1"
    handler_exec.requestline = f"{handler_exec.command} {handler_exec.path} HTTP/1.1"
    handler_exec.wfile = BytesIO()
    handler_exec.do_GET()
    assert 403 not in handler_exec._response_codes and 401 not in handler_exec._response_codes
    # read should be denied
    req2 = make_handler(
        "/api/v1/namespaces/default/pods/p1/exec?command=sh", headers={"Authorization": "Bearer r"}
    )
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
    assert 403 in handler2._response_codes or 401 in handler2._response_codes


def test_subject_access_review_allows(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_RBAC_EVAL", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda _self: None)
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = True
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = None
    role = K8sObject(
        "rbac.authorization.k8s.io",
        "v1",
        "roles",
        "default",
        "viewer",
        {"name": "viewer", "namespace": "default"},
        {"rules": [{"verbs": ["list"], "resources": ["services"]}]},
        {},
        1,
    )
    store.upsert(
        "rbac.authorization.k8s.io", "v1", "roles", "default", "viewer", role.metadata, role.spec
    )
    rb = K8sObject(
        "rbac.authorization.k8s.io",
        "v1",
        "rolebindings",
        "default",
        "bind",
        {"name": "bind", "namespace": "default"},
        {"subjects": [{"kind": "User", "name": "admin"}], "roleRef": {"name": "viewer"}},
        {},
        2,
    )
    store.upsert(
        "rbac.authorization.k8s.io", "v1", "rolebindings", "default", "bind", rb.metadata, rb.spec
    )
    body = json.dumps(
        {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SubjectAccessReview",
            "spec": {
                "resourceAttributes": {
                    "verb": "list",
                    "resource": "services",
                    "namespace": "default",
                }
            },
        }
    ).encode()
    req = make_handler(
        "/apis/authorization.k8s.io/v1/subjectaccessreviews",
        method="POST",
        headers={"Authorization": "Bearer a"},
        body=body,
    )
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler.store = store
    handler.state = None
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{handler.command} {handler.path} HTTP/1.1"
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.do_POST()
    assert 201 in handler._response_codes
    raw = handler.wfile.getvalue()
    payload = raw.split(b"\r\n\r\n")[-1] if raw else raw
    status = json.loads(payload.decode())
    assert status["status"]["allowed"] is True
    assert status["status"]["denied"] is False


def test_subject_access_review_denied(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_RBAC_EVAL", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    monkeypatch.setenv("AE_APISHIM_READ_TOKEN", "r")
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda _self: None)
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = True
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = "r"
    body = json.dumps(
        {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SubjectAccessReview",
            "spec": {
                "resourceAttributes": {
                    "verb": "delete",
                    "resource": "services",
                    "namespace": "default",
                }
            },
        }
    ).encode()
    req = make_handler(
        "/apis/authorization.k8s.io/v1/subjectaccessreviews",
        method="POST",
        headers={"Authorization": "Bearer r"},
        body=body,
    )
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler.store = store
    handler.state = None
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{handler.command} {handler.path} HTTP/1.1"
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.do_POST()
    assert 201 in handler._response_codes
    raw = handler.wfile.getvalue()
    payload = raw.split(b"\r\n\r\n")[-1] if raw else raw
    status = json.loads(payload.decode())
    assert status["status"]["allowed"] is False
    assert status["status"]["denied"] is True


def test_serviceaccount_token_auth(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_RBAC_EVAL", "1")
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = True
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = None
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda _self: None)
    # create Role allowing list configmaps
    role = K8sObject(
        "rbac.authorization.k8s.io",
        "v1",
        "roles",
        "default",
        "cm-reader",
        {"name": "cm-reader", "namespace": "default"},
        {"rules": [{"verbs": ["list"], "resources": ["configmaps"]}]},
        {},
        1,
    )
    store.upsert(
        "rbac.authorization.k8s.io", "v1", "roles", "default", "cm-reader", role.metadata, role.spec
    )
    # create serviceaccount via shim to mint token
    sa_body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "default", "namespace": "default"},
        }
    ).encode()
    req_sa = make_handler(
        "/api/v1/namespaces/default/serviceaccounts",
        method="POST",
        headers={"Authorization": "Bearer a"},
        body=sa_body,
    )
    handler_sa = shim_server.ShimHandler(req_sa, ("127.0.0.1", 0), None)
    handler_sa.path = req_sa.path
    handler_sa.command = req_sa.command
    handler_sa.headers = req_sa.headers
    handler_sa.server = SimpleNamespace(store=store, state=store, runtime=None)
    handler_sa.store = store
    handler_sa.state = None
    handler_sa.request_version = "HTTP/1.1"
    handler_sa.requestline = f"{handler_sa.command} {handler_sa.path} HTTP/1.1"
    handler_sa.rfile = BytesIO(sa_body)
    handler_sa.wfile = BytesIO()
    handler_sa.do_POST()
    sa_obj = store.get("", "v1", "serviceaccounts", "default", "default")
    token = (sa_obj.metadata.get("annotations") or {}).get("ae.apishim/token")
    assert token
    # bind role to serviceaccount
    rb = K8sObject(
        "rbac.authorization.k8s.io",
        "v1",
        "rolebindings",
        "default",
        "bind-sa",
        {"name": "bind-sa", "namespace": "default"},
        {
            "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": "default"}],
            "roleRef": {"name": "cm-reader"},
        },
        {},
        2,
    )
    store.upsert(
        "rbac.authorization.k8s.io",
        "v1",
        "rolebindings",
        "default",
        "bind-sa",
        rb.metadata,
        rb.spec,
    )
    # attempt list configmaps with SA token
    req = make_handler(
        "/api/v1/namespaces/default/configmaps", headers={"Authorization": f"Bearer {token}"}
    )
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
    assert 200 in handler._response_codes


def test_expired_sa_token_denied(monkeypatch, store):
    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = True
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = None
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda _self: None)
    # insert expired token
    expired_token = "old"
    shim_server.ShimHandler.sa_tokens[expired_token] = ("default", "default", 0)
    req = make_handler("/api/v1/namespaces", headers={"Authorization": f"Bearer {expired_token}"})
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
    assert 401 in handler._response_codes or 403 in handler._response_codes


# ruff: noqa: S105,E501
