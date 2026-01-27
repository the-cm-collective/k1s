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
            _ = (a, k)
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


def test_snapshot_group_listed_in_apis(tmp_path, monkeypatch) -> None:
    store = ObjectStore(tmp_path / "apishim.db")
    handler, status = _handler(store, monkeypatch, "/apis", method="GET")
    handler.do_GET()
    assert status["code"] == 200
    body = _json_body(handler)
    groups = body.get("groups") or []
    assert any(g.get("name") == "snapshot.storage.k8s.io" for g in groups)


def test_snapshot_discovery_lists_resources(tmp_path, monkeypatch) -> None:
    store = ObjectStore(tmp_path / "apishim.db")

    handler, status = _handler(store, monkeypatch, "/apis/snapshot.storage.k8s.io", method="GET")
    handler.do_GET()
    assert status["code"] == 200
    body = _json_body(handler)
    versions = {v.get("version") for v in (body.get("versions") or [])}
    assert "v1" in versions

    handler, status = _handler(store, monkeypatch, "/apis/snapshot.storage.k8s.io/v1", method="GET")
    handler.do_GET()
    assert status["code"] == 200
    body = _json_body(handler)
    resources = {r.get("name") for r in (body.get("resources") or [])}
    assert {"volumesnapshots", "volumesnapshotclasses", "volumesnapshotcontents"} <= resources


def test_volumesnapshot_crud(tmp_path, monkeypatch) -> None:
    store = ObjectStore(tmp_path / "apishim.db")

    cls_doc = {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshotClass",
        "metadata": {"name": "nfs"},
        "spec": {"driver": "k1s.io/nfs", "deletionPolicy": "Delete"},
    }
    body = json.dumps(cls_doc).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/snapshot.storage.k8s.io/v1/volumesnapshotclasses/nfs",
        method="PUT",
        body=body,
    )
    handler.do_PUT()
    assert status["code"] == 200
    snap_class = store.get("snapshot.storage.k8s.io", "v1", "volumesnapshotclasses", None, "nfs")
    assert snap_class is not None
    assert (snap_class.spec or {}).get("driver") == "k1s.io/nfs"

    snap_doc = {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshot",
        "metadata": {"name": "snap1", "namespace": "default"},
        "spec": {
            "source": {"persistentVolumeClaimName": "data"},
            "volumeSnapshotClassName": "nfs",
        },
    }
    body = json.dumps(snap_doc).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/snapshot.storage.k8s.io/v1/namespaces/default/volumesnapshots/snap1",
        method="PUT",
        body=body,
    )
    handler.do_PUT()
    assert status["code"] == 200
    snap = store.get("snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "snap1")
    assert snap is not None
    source = (snap.spec or {}).get("source") or {}
    assert source.get("persistentVolumeClaimName") == "data"

    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/snapshot.storage.k8s.io/v1/namespaces/default/volumesnapshots",
        method="GET",
    )
    handler.do_GET()
    assert status["code"] == 200
    body = _json_body(handler)
    items = body.get("items") or []
    assert any((item.get("metadata") or {}).get("name") == "snap1" for item in items)

