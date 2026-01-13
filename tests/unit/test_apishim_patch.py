import json
from io import BytesIO
from types import SimpleNamespace

from ae.apishim import server as shim_server
from ae.apishim.store import ObjectStore, K8sObject


def make_handler(path: str, method: str = "PATCH", headers=None, body: bytes = b""):
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


def test_json_patch_add_label(tmp_path, monkeypatch):
    store = ObjectStore(tmp_path / "apishim.db")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = None
    shim_server.ShimHandler.rbac_enabled = False
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda self: None)
    # seed deployment
    meta = {"name": "demo", "namespace": "default", "labels": {"app": "demo"}}
    spec = {"replicas": 1}
    store.upsert("apps", "v1", "deployments", "default", "demo", meta, spec)
    patch_body = json.dumps(
        [{"op": "add", "path": "/metadata/labels/env", "value": "staging"}]
    ).encode()
    req = make_handler(
        "/apis/apps/v1/namespaces/default/deployments/demo",
        headers={"Authorization": "Bearer a", "Content-Type": "application/json-patch+json"},
        body=patch_body,
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
    handler.rfile = BytesIO(patch_body)
    handler.wfile = BytesIO()
    handler.do_PATCH()
    obj = store.get("apps", "v1", "deployments", "default", "demo")
    assert obj
    assert obj.metadata["labels"]["env"] == "staging"


def test_apply_patch_sets_managed_fields(tmp_path, monkeypatch):
    store = ObjectStore(tmp_path / "apishim.db")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = None
    shim_server.ShimHandler.rbac_enabled = False
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda self: None)
    svc_meta = {"name": "web", "namespace": "default"}
    svc_spec = {"ports": [{"port": 80, "targetPort": 8080}]}
    store.upsert("", "v1", "services", "default", "web", svc_meta, svc_spec)
    body = json.dumps({"metadata": {"labels": {"team": "platform"}}}).encode()
    req = make_handler(
        "/api/v1/namespaces/default/services/web?fieldManager=kubectl",
        headers={"Authorization": "Bearer a", "Content-Type": "application/apply-patch+yaml"},
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
    handler.do_PATCH()
    obj = store.get("", "v1", "services", "default", "web")
    assert obj
    mfields = obj.metadata.get("managedFields")
    assert mfields
    assert any(mf.get("manager") == "kubectl" for mf in mfields)


def test_deployment_injects_sa_projection(tmp_path, monkeypatch):
    store = ObjectStore(tmp_path / "apishim.db")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    shim_server.ShimHandler.admin_token = "a"
    shim_server.ShimHandler.read_token = None
    shim_server.ShimHandler.rbac_enabled = False
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda self: None)
    body = json.dumps(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "serviceAccountName": "demo-sa",
                "template": {"spec": {"containers": [{"name": "c", "image": "nginx"}]}},
            },
        }
    ).encode()
    req = make_handler(
        "/apis/apps/v1/namespaces/default/deployments",
        method="POST",
        headers={"Authorization": "Bearer a", "Content-Type": "application/json"},
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
    obj = store.get("apps", "v1", "deployments", "default", "demo")
    tpl_spec = (obj.spec.get("template") or {}).get("spec") or {}
    vols = tpl_spec.get("volumes") or []
    assert any(v.get("projected") for v in vols)
    cmounts = tpl_spec.get("containers")[0].get("volumeMounts")
    assert cmounts and any(vm.get("mountPath") == "/var/run/secrets/kubernetes.io/serviceaccount" for vm in cmounts)
