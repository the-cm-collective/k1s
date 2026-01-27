from pathlib import Path

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


def test_storage_controller_creates_volume_attachment_for_csi(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_uid = "uid-csi"
    pv_spec = {
        "accessModes": ["ReadWriteOnce"],
        "persistentVolumeReclaimPolicy": "Retain",
        "claimRef": {"namespace": "default", "name": "data", "uid": pvc_uid},
        "csi": {"driver": "csi.example.com", "volumeHandle": "vol-1"},
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-csi",
        "resources": {"requests": {"storage": "1Gi"}},
    }
    pvc_meta = {
        "name": "data",
        "namespace": "default",
        "uid": pvc_uid,
        "annotations": {"volume.kubernetes.io/selected-node": "node-a"},
    }

    store.upsert("", "v1", "persistentvolumes", None, "pv-csi", {"name": "pv-csi"}, pv_spec)
    store.upsert("", "v1", "persistentvolumeclaims", "default", "data", pvc_meta, pvc_spec)

    controller.reconcile_once()

    va_name = controller._volume_attachment_name("pv-csi", "node-a")
    va = store.get("storage.k8s.io", "v1", "volumeattachments", None, va_name)
    assert va is not None
    assert (va.spec or {}).get("nodeName") == "node-a"
    assert (va.spec or {}).get("attacher") == "csi.example.com"
    source = (va.spec or {}).get("source") or {}
    assert source.get("persistentVolumeName") == "pv-csi"
    assert (va.status or {}).get("attached") is True


def test_storage_controller_blocks_multi_attach_for_csi(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_uid = "uid-csi"
    pv_spec = {
        "accessModes": ["ReadWriteOnce"],
        "persistentVolumeReclaimPolicy": "Retain",
        "claimRef": {"namespace": "default", "name": "data", "uid": pvc_uid},
        "csi": {"driver": "csi.example.com", "volumeHandle": "vol-1"},
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-csi",
        "resources": {"requests": {"storage": "1Gi"}},
    }
    pvc_meta = {
        "name": "data",
        "namespace": "default",
        "uid": pvc_uid,
        "annotations": {"volume.kubernetes.io/selected-node": "node-b"},
    }
    existing_va = {
        "attacher": "csi.example.com",
        "nodeName": "node-a",
        "source": {"persistentVolumeName": "pv-csi"},
    }

    store.upsert("", "v1", "persistentvolumes", None, "pv-csi", {"name": "pv-csi"}, pv_spec)
    store.upsert("", "v1", "persistentvolumeclaims", "default", "data", pvc_meta, pvc_spec)
    store.upsert(
        "storage.k8s.io",
        "v1",
        "volumeattachments",
        None,
        controller._volume_attachment_name("pv-csi", "node-a"),
        {"name": controller._volume_attachment_name("pv-csi", "node-a")},
        existing_va,
        status={"attached": True},
    )

    controller.reconcile_once()

    blocked_name = controller._volume_attachment_name("pv-csi", "node-b")
    assert store.get("storage.k8s.io", "v1", "volumeattachments", None, blocked_name) is None
    events = store.list_all("", "v1", "events")
    reasons = [(e.spec or {}).get("reason") for e in events]
    assert "MultiAttachForbidden" in reasons


def test_storage_controller_expands_volume_when_allowed(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_uid = "uid-expand"
    sc_spec = {"provisioner": "k1s.io/nfs", "allowVolumeExpansion": True}
    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "expandable",
        "claimRef": {"namespace": "default", "name": "data", "uid": pvc_uid},
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-expand",
        "storageClassName": "expandable",
        "resources": {"requests": {"storage": "2Gi"}},
    }
    pvc_status = {"phase": "Bound", "capacity": {"storage": "1Gi"}}

    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "expandable",
        {"name": "expandable"},
        sc_spec,
        status={},
    )
    store.upsert("", "v1", "persistentvolumes", None, "pv-expand", {"name": "pv-expand"}, pv_spec)
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data",
        {"name": "data", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
        status=pvc_status,
    )

    controller.reconcile_once()

    pv = store.get("", "v1", "persistentvolumes", None, "pv-expand")
    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "data")
    assert pv is not None
    assert pvc is not None
    assert ((pv.spec or {}).get("capacity") or {}).get("storage") == "2Gi"
    assert ((pvc.status or {}).get("capacity") or {}).get("storage") == "2Gi"
    events = store.list_all("", "v1", "events")
    reasons = [(e.spec or {}).get("reason") for e in events]
    assert "VolumeExpanded" in reasons


def test_storage_controller_blocks_expansion_when_forbidden(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_uid = "uid-expand"
    sc_spec = {"provisioner": "k1s.io/nfs", "allowVolumeExpansion": False}
    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "no-expand",
        "claimRef": {"namespace": "default", "name": "data", "uid": pvc_uid},
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-no-expand",
        "storageClassName": "no-expand",
        "resources": {"requests": {"storage": "2Gi"}},
    }
    pvc_status = {"phase": "Bound", "capacity": {"storage": "1Gi"}}

    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "no-expand",
        {"name": "no-expand"},
        sc_spec,
        status={},
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumes",
        None,
        "pv-no-expand",
        {"name": "pv-no-expand"},
        pv_spec,
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data",
        {"name": "data", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
        status=pvc_status,
    )

    controller.reconcile_once()

    pv = store.get("", "v1", "persistentvolumes", None, "pv-no-expand")
    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "data")
    assert pv is not None
    assert pvc is not None
    assert ((pv.spec or {}).get("capacity") or {}).get("storage") == "1Gi"
    assert ((pvc.status or {}).get("capacity") or {}).get("storage") == "1Gi"
    events = store.list_all("", "v1", "events")
    reasons = [(e.spec or {}).get("reason") for e in events]
    assert "VolumeExpansionForbidden" in reasons


def test_storage_controller_snapshot_and_clone_nfs(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    host_root = tmp_path / "nfs-root"
    sc_spec = {
        "provisioner": "k1s.io/nfs",
        "parameters": {
            "server": "127.0.0.1",
            "path": "/exports/netfs",
            "hostPath": str(host_root),
        },
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "Immediate",
    }
    pvc_uid = "uid-src"
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
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
        status={},
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data-src",
        {"name": "data-src", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
        status={"phase": "Pending"},
    )

    controller.reconcile_once()

    source_path = host_root / pvc_uid
    source_path.mkdir(parents=True, exist_ok=True)
    (source_path / "data.txt").write_text("hello snapshot", encoding="utf-8")

    snap_class_spec = {"driver": "k1s.io/nfs", "deletionPolicy": "Delete"}
    store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshotclasses",
        None,
        "nfs-snap",
        {"name": "nfs-snap"},
        snap_class_spec,
        status={},
    )
    snap_uid = "snap-uid"
    snap_spec = {
        "source": {"persistentVolumeClaimName": "data-src"},
        "volumeSnapshotClassName": "nfs-snap",
    }
    store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshots",
        "default",
        "snap1",
        {"name": "snap1", "namespace": "default", "uid": snap_uid},
        snap_spec,
        status={},
    )

    controller.reconcile_once()

    pv_name = f"pvc-{pvc_uid}"
    pv = store.get("", "v1", "persistentvolumes", None, pv_name)
    snap = store.get("snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "snap1")
    assert pv is not None
    assert snap is not None
    assert (snap.status or {}).get("readyToUse") is True

    content_name = controller._snapshot_content_name(snap, pv)
    content = store.get(
        "snapshot.storage.k8s.io", "v1", "volumesnapshotcontents", None, content_name
    )
    assert content is not None
    annotations = (content.metadata or {}).get("annotations") or {}
    snap_path = annotations.get("k1s.io/snapshot-host-path")
    assert snap_path
    snap_dir = Path(str(snap_path))
    assert snap_dir.exists()
    assert (snap_dir / "data.txt").read_text(encoding="utf-8") == "hello snapshot"

    clone_uid = "uid-clone"
    clone_spec = {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "k1s-nfs",
        "resources": {"requests": {"storage": "1Gi"}},
        "dataSource": {
            "apiGroup": "snapshot.storage.k8s.io",
            "kind": "VolumeSnapshot",
            "name": "snap1",
        },
    }
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data-clone",
        {"name": "data-clone", "namespace": "default", "uid": clone_uid},
        clone_spec,
        status={"phase": "Pending"},
    )

    controller.reconcile_once()

    clone_pv = store.get("", "v1", "persistentvolumes", None, f"pvc-{clone_uid}")
    assert clone_pv is not None
    clone_path = host_root / clone_uid / "data.txt"
    assert clone_path.exists()
    assert clone_path.read_text(encoding="utf-8") == "hello snapshot"
