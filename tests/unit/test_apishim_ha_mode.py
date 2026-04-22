import json
import sys
import types

from ae.apishim import server as shim_server
from ae.apishim.ha_store import MultiplexApishimStore
from ae.apishim.store import ObjectStore
from ae.controller.state import SQLiteStateStore
from tests.unit.test_apishim_storage import _handler, _json_body


def test_apishim_ha_mode_routes_core_storage_create_through_shared_authority(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    store = MultiplexApishimStore.from_state_and_legacy(state, legacy)
    shim_server.ShimHandler.state = state
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

    assert status["code"] == 201
    payload = _json_body(handler)
    assert payload["kind"] == "PersistentVolumeClaim"
    assert legacy.get("", "v1", "persistentvolumeclaims", "default", "demo") is None
    assert state.get_authority_object("", "v1", "persistentvolumeclaims", "default", "demo") is not None


def test_apishim_ha_mode_rejects_put_for_csi_storage_resource(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    body = json.dumps(
        {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "VolumeAttachment",
            "metadata": {"name": "demo"},
            "spec": {
                "attacher": "csi.example.com",
                "nodeName": "node-a",
                "source": {"persistentVolumeName": "pv-a"},
            },
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/storage.k8s.io/v1/volumeattachments/demo",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert "controller-owned storage" in payload["message"]
    assert store.get("storage.k8s.io", "v1", "volumeattachments", None, "demo") is None


def test_apishim_ha_mode_routes_snapshot_create_through_shared_authority(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    store = MultiplexApishimStore.from_state_and_legacy(state, legacy)
    shim_server.ShimHandler.state = state
    body = json.dumps(
        {
            "apiVersion": "snapshot.storage.k8s.io/v1",
            "kind": "VolumeSnapshot",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "source": {"persistentVolumeClaimName": "demo-pvc"},
                "volumeSnapshotClassName": "snapclass",
            },
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/snapshot.storage.k8s.io/v1/namespaces/default/volumesnapshots",
        method="POST",
        body=body,
    )

    handler.do_POST()

    assert status["code"] == 201
    payload = _json_body(handler)
    assert payload["kind"] == "VolumeSnapshot"
    assert legacy.get("snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "demo") is None
    assert (
        state.get_authority_object(
            "snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "demo"
        )
        is not None
    )


def test_apishim_ha_mode_routes_csidriver_create_through_shared_authority(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    store = MultiplexApishimStore.from_state_and_legacy(state, legacy)
    shim_server.ShimHandler.state = state
    body = json.dumps(
        {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "CSIDriver",
            "metadata": {"name": "csi.example.com"},
            "spec": {"attachRequired": True, "podInfoOnMount": False},
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/storage.k8s.io/v1/csidrivers/csi.example.com",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "CSIDriver"
    assert legacy.get("storage.k8s.io", "v1", "csidrivers", None, "csi.example.com") is None
    assert (
        state.get_authority_object("storage.k8s.io", "v1", "csidrivers", None, "csi.example.com")
        is not None
    )


def test_apishim_ha_mode_rejects_patch_for_controller_owned_snapshot_resource(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshotcontents",
        None,
        "demo",
        metadata={"name": "demo"},
        spec={"source": {"volumeHandle": "vol-1"}},
        status={},
    )
    body = json.dumps({"spec": {"source": {"volumeHandle": "vol-2"}}}).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/snapshot.storage.k8s.io/v1/volumesnapshotcontents/demo",
        method="PATCH",
        body=body,
    )

    handler.do_PATCH()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert "controller-owned storage" in payload["message"]
    obj = store.get("snapshot.storage.k8s.io", "v1", "volumesnapshotcontents", None, "demo")
    assert obj is not None
    assert obj.spec["source"]["volumeHandle"] == "vol-1"


def test_apishim_ha_mode_rejects_delete_for_controller_owned_storage_capacity(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    store.upsert(
        "storage.k8s.io",
        "v1",
        "csistoragecapacities",
        "default",
        "demo",
        metadata={"name": "demo", "namespace": "default"},
        spec={"storageClassName": "csi-fast", "capacity": "1Gi"},
        status={},
    )
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/storage.k8s.io/v1/namespaces/default/csistoragecapacities/demo",
        method="DELETE",
    )

    handler.do_DELETE()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert "controller-owned storage" in payload["message"]
    assert store.get("storage.k8s.io", "v1", "csistoragecapacities", "default", "demo") is not None


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


def test_apishim_ha_mode_routes_crd_create_through_shared_authority(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    store = MultiplexApishimStore.from_state_and_legacy(state, legacy)
    shim_server.ShimHandler.state = state
    shim_server.ShimHandler.crd_registry = {}
    shim_server.ShimHandler.crd_index = {}
    shim_server.ShimHandler._crd_refresh_monotonic = 0.0
    body = json.dumps(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "apps.ae.dev"},
            "spec": {
                "group": "ae.dev",
                "scope": "Namespaced",
                "names": {"plural": "apps", "singular": "app", "kind": "Deployment"},
                "versions": [{"name": "v1alpha1", "served": True, "storage": True}],
            },
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/apps.ae.dev",
        method="PUT",
        body=body,
    )
    handler.server = types.SimpleNamespace(store=store, state=state, runtime=None)
    handler.state = state

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "CustomResourceDefinition"
    assert payload["metadata"]["name"] == "apps.ae.dev"
    assert legacy.get("apiextensions.k8s.io", "v1", "customresourcedefinitions", None, "apps.ae.dev") is None
    entry = state.get_authority_object(
        "apiextensions.k8s.io",
        "v1",
        "customresourcedefinitions",
        None,
        "apps.ae.dev",
    )
    assert entry is not None


def test_apishim_ha_mode_routes_custom_resource_create_through_shared_authority(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    store = MultiplexApishimStore.from_state_and_legacy(state, legacy)
    shim_server.ShimHandler.state = state
    shim_server.ShimHandler.crd_registry = {}
    shim_server.ShimHandler.crd_index = {}
    shim_server.ShimHandler._crd_refresh_monotonic = 0.0

    crd_body = json.dumps(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "apps.ae.dev"},
            "spec": {
                "group": "ae.dev",
                "scope": "Namespaced",
                "names": {"plural": "apps", "singular": "app", "kind": "Deployment"},
                "versions": [{"name": "v1alpha1", "served": True, "storage": True}],
            },
        }
    ).encode("utf-8")
    crd_handler, crd_status = _handler(
        store,
        monkeypatch,
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/apps.ae.dev",
        method="PUT",
        body=crd_body,
    )
    crd_handler.server = types.SimpleNamespace(store=store, state=state, runtime=None)
    crd_handler.state = state
    crd_handler.do_PUT()
    assert crd_status["code"] == 200

    body = json.dumps(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {"image": "busybox"},
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/ae.dev/v1alpha1/namespaces/default/apps",
        method="POST",
        body=body,
    )
    handler.server = types.SimpleNamespace(store=store, state=state, runtime=None)
    handler.state = state

    handler.do_POST()

    assert status["code"] == 201
    payload = _json_body(handler)
    assert payload["kind"] == "Deployment"
    assert payload["metadata"]["name"] == "demo"
    assert legacy.list_all("ae.dev", "v1alpha1", "apps") == []
    entry = state.get_authority_object("ae.dev", "v1alpha1", "apps", "default", "demo")
    assert entry is not None
