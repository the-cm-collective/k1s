from ae.apishim.store import ObjectStore
from ae.storage.controller import StorageController


def test_storage_controller_binds_pvc(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)

    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteMany"],
        "storageClassName": "k1s-nfs",
    }
    pvc_spec = {
        "accessModes": ["ReadWriteMany"],
        "storageClassName": "k1s-nfs",
        "resources": {"requests": {"storage": "1Gi"}},
    }

    store.upsert("", "v1", "persistentvolumes", None, "pv1", {"name": "pv1"}, pv_spec)
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "pvc1",
        {"name": "pvc1", "namespace": "default"},
        pvc_spec,
    )

    controller.reconcile_once()

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "pvc1")
    pv = store.get("", "v1", "persistentvolumes", None, "pv1")
    assert pvc is not None
    assert pv is not None
    assert pvc.spec.get("volumeName") == "pv1"
    assert (pvc.status or {}).get("phase") == "Bound"
    claim_ref = (pv.spec or {}).get("claimRef") or {}
    assert claim_ref.get("name") == "pvc1"
    assert claim_ref.get("namespace") == "default"
    assert (pv.status or {}).get("phase") == "Bound"
