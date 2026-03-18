import json
import sys
import types

from ae.apishim import server as shim_server
from ae.apishim.store import ObjectStore
from tests.unit.test_apishim_storage import _handler, _json_body


def test_apishim_ha_mode_rejects_post_create_for_storage_resource(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
            },
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/persistentvolumeclaims",
        method="POST",
        body=body,
    )

    handler.do_POST()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert "later H4b" in payload["message"]
    assert store.get("", "v1", "persistentvolumeclaims", "default", "demo") is None


def test_apishim_ha_mode_rejects_put_for_storage_resource(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
            },
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/persistentvolumeclaims/demo",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert "later H4b" in payload["message"]
    assert store.get("", "v1", "persistentvolumeclaims", "default", "demo") is None


def test_apishim_ha_mode_rejects_patch_for_storage_resource(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "demo",
        metadata={"name": "demo", "namespace": "default"},
        spec={
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
        status={},
    )
    body = json.dumps({"spec": {"resources": {"requests": {"storage": "2Gi"}}}}).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/persistentvolumeclaims/demo",
        method="PATCH",
        body=body,
    )

    handler.do_PATCH()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert "later H4b" in payload["message"]
    obj = store.get("", "v1", "persistentvolumeclaims", "default", "demo")
    assert obj is not None
    assert obj.spec["resources"]["requests"]["storage"] == "1Gi"


def test_apishim_ha_mode_rejects_delete_for_storage_resource(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "demo",
        metadata={"name": "demo", "namespace": "default"},
        spec={
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
        status={},
    )
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/persistentvolumeclaims/demo",
        method="DELETE",
    )

    handler.do_DELETE()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert "later H4b" in payload["message"]
    assert store.get("", "v1", "persistentvolumeclaims", "default", "demo") is not None


def test_apishim_ha_mode_keeps_get_available(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "default",
        "demo",
        metadata={"name": "demo", "namespace": "default"},
        spec={"hello": "world"},
        status={},
    )
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps",
        method="GET",
    )

    handler.do_GET()

    assert status["code"] == 200
    payload = _json_body(handler)
    items = payload.get("items") or []
    assert any((item.get("metadata") or {}).get("name") == "demo" for item in items)


def test_apishim_ha_mode_keeps_watch_available(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    watched: dict[str, object] = {}

    def fake_stream_watch(self, group, version, plural, namespace, query, transform=None) -> None:
        watched.update(
            {
                "group": group,
                "version": version,
                "plural": plural,
                "namespace": namespace,
                "watch": (query.get("watch") or ["0"])[0],
            }
        )
        self._ok({"kind": "Status", "status": "Success"})

    monkeypatch.setattr(shim_server.ShimHandler, "_stream_watch", fake_stream_watch)
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/statefulsets?watch=1",
        method="GET",
    )

    handler.do_GET()

    assert status["code"] == 200
    assert watched == {
        "group": "apps",
        "version": "v1",
        "plural": "statefulsets",
        "namespace": "default",
        "watch": "1",
    }


def test_apishim_ha_mode_keeps_authorization_reviews_available(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    body = json.dumps(
        {
            "spec": {
                "resourceAttributes": {
                    "verb": "get",
                    "resource": "pods",
                    "namespace": "default",
                }
            }
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
        method="POST",
        body=body,
    )

    handler.do_POST()

    assert status["code"] == 201
    payload = _json_body(handler)
    assert payload["kind"] == "SelfSubjectAccessReview"
    assert "status" in payload


def test_apishim_ha_mode_disables_storage_controller_startup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_APISHIM_RUNTIME", "stub")
    monkeypatch.setenv("AE_STATE_DB", str(tmp_path / "controller.db"))
    monkeypatch.setenv("AE_APISHIM_DB", str(tmp_path / "apishim.db"))

    calls: list[str] = []

    class FakeStorageController:
        def __init__(self, store) -> None:
            calls.append("init")
            self.store = store

        def sync(self) -> int:
            calls.append("sync")
            return 1

        def start(self) -> None:
            calls.append("start")

    fake_module = types.SimpleNamespace(StorageController=FakeStorageController)
    monkeypatch.setitem(sys.modules, "ae.storage.controller", fake_module)

    server = shim_server.ShimServer(("127.0.0.1", 0), token="a")
    try:
        assert server._storage_controller is None
        assert calls == []
    finally:
        server.server_close()
        store = getattr(server, "store", None)
        if store is not None and hasattr(store, "close"):
            store.close()
