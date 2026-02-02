import json
from io import BytesIO
from types import SimpleNamespace

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
            self.responses = []
            self._body = body
            self.wfile = SimpleNamespace(write=lambda b: self.responses.append(b))

        def send_response(self, code, _message=None):
            self.responses.append(code)

        def send_header(self, k, v):
            _ = (k, v)
            pass

        def end_headers(self):
            pass

        def close(self):
            pass

    return DummyRequest()


def _handler(store: ObjectStore, monkeypatch, path: str, *, method: str, body: bytes = b""):
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    shim_server.ShimHandler.admin_token = "a"  # noqa: S105 - test token
    shim_server.ShimHandler.read_token = None
    shim_server.ShimHandler.rbac_enabled = False
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda _self: None)

    headers = {"Authorization": "Bearer a"}
    if body:
        headers["Content-Type"] = "application/json"
    req = make_handler(path, method=method, headers=headers, body=body)
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

    status: dict[str, int | None] = {"code": None}
    handler.send_response = lambda code, _msg=None: status.__setitem__("code", code)
    handler.send_header = lambda *_a, **_k: None
    handler.end_headers = lambda: None
    return handler, status


def _json_body(handler: shim_server.ShimHandler) -> dict:
    raw = handler.wfile.getvalue()
    assert raw
    return json.loads(raw.decode("utf-8"))


def test_storage_group_listed_in_apis(tmp_path, monkeypatch) -> None:
    store = ObjectStore(tmp_path / "apishim.db")
    handler, status = _handler(store, monkeypatch, "/apis", method="GET")
    handler.do_GET()
    assert status["code"] == 200
    body = _json_body(handler)
    groups = body.get("groups") or []
    assert any(g.get("name") == "storage.k8s.io" for g in groups)


def test_storage_group_and_version_discovery(tmp_path, monkeypatch) -> None:
    store = ObjectStore(tmp_path / "apishim.db")
    handler, status = _handler(store, monkeypatch, "/apis/storage.k8s.io", method="GET")
    handler.do_GET()
    assert status["code"] == 200
    body = _json_body(handler)
    versions = {v.get("version") for v in (body.get("versions") or [])}
    assert "v1" in versions

    handler, status = _handler(store, monkeypatch, "/apis/storage.k8s.io/v1", method="GET")
    handler.do_GET()
    assert status["code"] == 200
    body = _json_body(handler)
    resources = {r.get("name") for r in (body.get("resources") or [])}
    assert {
        "storageclasses",
        "volumeattachments",
        "csidrivers",
        "csinodes",
        "csistoragecapacities",
    } <= resources


def test_storageclass_and_volumeattachment_crud(tmp_path, monkeypatch) -> None:
    store = ObjectStore(tmp_path / "apishim.db")

    sc_doc = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": "k1s-nfs"},
        "spec": {
            "provisioner": "k1s.io/nfs",
            "parameters": {"server": "127.0.0.1", "path": "/exports/netfs"},
            "reclaimPolicy": "Delete",
            "volumeBindingMode": "WaitForFirstConsumer",
        },
    }
    body = json.dumps(sc_doc).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/storage.k8s.io/v1/storageclasses/k1s-nfs",
        method="PUT",
        body=body,
    )
    handler.do_PUT()
    assert status["code"] == 200
    sc = store.get("storage.k8s.io", "v1", "storageclasses", None, "k1s-nfs")
    assert sc is not None
    assert (sc.spec or {}).get("provisioner") == "k1s.io/nfs"

    va_doc = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "VolumeAttachment",
        "metadata": {"name": "va-demo"},
        "spec": {
            "attacher": "csi.example.com",
            "nodeName": "node-a",
            "source": {"persistentVolumeName": "pv-demo"},
        },
        "status": {"attached": True},
    }
    body = json.dumps(va_doc).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/storage.k8s.io/v1/volumeattachments/va-demo",
        method="PUT",
        body=body,
    )
    handler.do_PUT()
    assert status["code"] == 200
    va = store.get("storage.k8s.io", "v1", "volumeattachments", None, "va-demo")
    assert va is not None
    assert (va.spec or {}).get("nodeName") == "node-a"

    handler, status = _handler(
        store, monkeypatch, "/apis/storage.k8s.io/v1/volumeattachments", method="GET"
    )
    handler.do_GET()
    assert status["code"] == 200
    body = _json_body(handler)
    items = body.get("items") or []
    assert any((item.get("metadata") or {}).get("name") == "va-demo" for item in items)

    driver_doc = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "CSIDriver",
        "metadata": {"name": "csi.example.com"},
        "spec": {"attachRequired": True, "podInfoOnMount": False},
    }
    body = json.dumps(driver_doc).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/storage.k8s.io/v1/csidrivers/csi.example.com",
        method="PUT",
        body=body,
    )
    handler.do_PUT()
    assert status["code"] == 200
    driver = store.get("storage.k8s.io", "v1", "csidrivers", None, "csi.example.com")
    assert driver is not None

    node_doc = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "CSINode",
        "metadata": {"name": "node-a"},
        "spec": {"drivers": [{"name": "csi.example.com", "nodeID": "node-a"}]},
    }
    body = json.dumps(node_doc).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/storage.k8s.io/v1/csinodes/node-a",
        method="PUT",
        body=body,
    )
    handler.do_PUT()
    assert status["code"] == 200
    node = store.get("storage.k8s.io", "v1", "csinodes", None, "node-a")
    assert node is not None

    cap_doc = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "CSIStorageCapacity",
        "metadata": {"name": "cap-1", "namespace": "default"},
        "spec": {"storageClassName": "k1s-nfs", "capacity": "1Gi"},
    }
    body = json.dumps(cap_doc).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/storage.k8s.io/v1/namespaces/default/csistoragecapacities/cap-1",
        method="PUT",
        body=body,
    )
    handler.do_PUT()
    assert status["code"] == 200
    cap = store.get("storage.k8s.io", "v1", "csistoragecapacities", "default", "cap-1")
    assert cap is not None
