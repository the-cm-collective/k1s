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


def test_storage_controller_dynamic_nfs_provisioning(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    host_root = tmp_path / "nfs-root"
    sc_spec = {
        "provisioner": "k1s.io/nfs",
        "parameters": {
            "server": "127.0.0.1",
            "path": "/export",
            "hostPath": str(host_root),
        },
        "reclaimPolicy": "Retain",
        "volumeBindingMode": "Immediate",
    }
    pvc_uid = "uid-dyn-nfs"
    pvc_spec = {
        "accessModes": ["ReadWriteMany"],
        "storageClassName": "k1s-nfs",
        "resources": {"requests": {"storage": "1Gi"}},
    }

    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "k1s-nfs",
        {"name": "k1s-nfs"},
        sc_spec,
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "pvc-dyn",
        {"name": "pvc-dyn", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
    )

    controller.reconcile_once()

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "pvc-dyn")
    assert pvc is not None
    pv_name = pvc.spec.get("volumeName")
    assert pv_name == f"pvc-{pvc_uid}"
    pv = store.get("", "v1", "persistentvolumes", None, pv_name)
    assert pv is not None
    nfs_spec = (pv.spec or {}).get("nfs") or {}
    assert nfs_spec.get("server") == "127.0.0.1"
    assert nfs_spec.get("path") == f"/export/{pvc_uid}"
    assert (pv.status or {}).get("phase") == "Bound"
    assert (host_root / pvc_uid).is_dir()


def test_storage_controller_reclaim_policy_delete_cleans_backing(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    host_root = tmp_path / "nfs-root"
    sc_spec = {
        "provisioner": "k1s.io/nfs",
        "parameters": {
            "server": "127.0.0.1",
            "path": "/export",
            "hostPath": str(host_root),
        },
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "Immediate",
    }
    pvc_uid = "uid-delete"
    pvc_spec = {
        "accessModes": ["ReadWriteMany"],
        "storageClassName": "k1s-nfs",
        "resources": {"requests": {"storage": "1Gi"}},
    }

    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "k1s-nfs",
        {"name": "k1s-nfs"},
        sc_spec,
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "pvc-delete",
        {"name": "pvc-delete", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
    )

    controller.reconcile_once()

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "pvc-delete")
    assert pvc is not None
    pv_name = pvc.spec.get("volumeName")
    assert pv_name
    backing = host_root / pvc_uid
    backing.mkdir(parents=True, exist_ok=True)
    (backing / "data.txt").write_text("hello", encoding="utf-8")

    controller._handle_pvc_deleted(pvc)

    assert store.get("", "v1", "persistentvolumes", None, pv_name) is None
    assert not backing.exists()
