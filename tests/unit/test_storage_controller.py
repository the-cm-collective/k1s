from pathlib import Path

from ae.apishim.store import ObjectStore
from ae.storage.controller import StorageController


def test_storage_controller_seeds_default_local_class(tmp_path, monkeypatch):
    monkeypatch.delenv("AE_STORAGE_PROVISIONERS", raising=False)
    monkeypatch.setenv("AE_STORAGE_SEED_DEFAULTS", "1")
    monkeypatch.setenv("AE_STORAGE_LOCAL_CLASS", "k1s-local")
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)

    seeded = controller.sync()
    assert seeded == 1
    sc = store.get("storage.k8s.io", "v1", "storageclasses", None, "k1s-local")
    assert sc is not None
    spec = sc.spec or {}
    assert spec.get("provisioner") == "k1s.io/local-path"
    assert spec.get("volumeBindingMode") == "WaitForFirstConsumer"
    annotations = (sc.metadata or {}).get("annotations") or {}
    assert annotations.get("storageclass.kubernetes.io/is-default-class") == "true"


def test_storage_controller_seeds_nfs_class_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("AE_STORAGE_PROVISIONERS", raising=False)
    monkeypatch.setenv("AE_STORAGE_SEED_DEFAULTS", "1")
    monkeypatch.setenv("AE_STORAGE_LOCAL_CLASS", "k1s-local")
    monkeypatch.setenv("AE_STORAGE_NFS_SERVER", "127.0.0.1")
    monkeypatch.setenv("AE_STORAGE_NFS_PATH", "/export/netfs")
    monkeypatch.setenv("AE_STORAGE_NFS_HOSTPATH", str(tmp_path / "nfs-root"))

    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)

    seeded = controller.sync()
    assert seeded == 2

    sc = store.get("storage.k8s.io", "v1", "storageclasses", None, "k1s-nfs")
    assert sc is not None
    spec = sc.spec or {}
    assert spec.get("provisioner") == "k1s.io/nfs"
    params = spec.get("parameters") or {}
    assert params.get("server") == "127.0.0.1"
    assert params.get("path") == "/export/netfs"
    assert params.get("hostPath") == str(tmp_path / "nfs-root")
    assert spec.get("reclaimPolicy") == "Retain"
    assert spec.get("volumeBindingMode") == "Immediate"
    assert spec.get("allowVolumeExpansion") is True
    annotations = (sc.metadata or {}).get("annotations") or {}
    assert annotations.get("storageclass.kubernetes.io/is-default-class") != "true"


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


def test_storage_controller_adds_pvc_pv_finalizers(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }
    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "k1s-local",
    }
    store.upsert("", "v1", "persistentvolumes", None, "pv-final", {"name": "pv-final"}, pv_spec)
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "pvc-final",
        {"name": "pvc-final", "namespace": "default"},
        pvc_spec,
    )

    controller.reconcile_once()

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "pvc-final")
    pv = store.get("", "v1", "persistentvolumes", None, "pv-final")
    assert pvc is not None
    assert pv is not None
    pvc_finalizers = (pvc.metadata or {}).get("finalizers") or []
    pv_finalizers = (pv.metadata or {}).get("finalizers") or []
    assert "kubernetes.io/pvc-protection" in pvc_finalizers
    assert "kubernetes.io/pv-protection" in pv_finalizers


def test_storage_controller_respects_explicit_volume_name(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    sc_spec = {
        "provisioner": "k1s.io/local-path",
        "parameters": {"hostPath": str(tmp_path / "local-root")},
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "Immediate",
    }
    pvc_uid = "uid-explicit"
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "k1s-local",
        "volumeName": "pv-explicit",
        "resources": {"requests": {"storage": "1Gi"}},
    }

    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "k1s-local",
        {"name": "k1s-local"},
        sc_spec,
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "pvc-explicit",
        {"name": "pvc-explicit", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
        status={"phase": "Pending"},
    )

    controller.reconcile_once()

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "pvc-explicit")
    assert pvc is not None
    assert (pvc.status or {}).get("phase") == "Pending"
    assert store.get("", "v1", "persistentvolumes", None, "pv-explicit") is None
    assert store.get("", "v1", "persistentvolumes", None, f"pvc-{pvc_uid}") is None


def test_storage_controller_explicit_volume_name_conflict(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "k1s-local",
        "claimRef": {"namespace": "default", "name": "other"},
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "k1s-local",
        "volumeName": "pv-conflict",
        "resources": {"requests": {"storage": "1Gi"}},
    }

    store.upsert(
        "",
        "v1",
        "persistentvolumes",
        None,
        "pv-conflict",
        {"name": "pv-conflict"},
        pv_spec,
        status={"phase": "Available"},
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "pvc-conflict",
        {"name": "pvc-conflict", "namespace": "default", "uid": "uid-conflict"},
        pvc_spec,
        status={"phase": "Pending"},
    )

    controller.reconcile_once()

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "pvc-conflict")
    assert pvc is not None
    assert (pvc.status or {}).get("phase") == "Pending"
    pv = store.get("", "v1", "persistentvolumes", None, "pv-conflict")
    assert pv is not None
    assert (pv.spec or {}).get("claimRef", {}).get("name") == "other"


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


def test_storage_controller_dynamic_local_path_block(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    host_root = tmp_path / "local-root"
    sc_spec = {
        "provisioner": "k1s.io/local-path",
        "parameters": {"hostPath": str(host_root)},
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "Immediate",
    }
    pvc_uid = "uid-block"
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeMode": "Block",
        "storageClassName": "local-path",
        "resources": {"requests": {"storage": "8Mi"}},
    }

    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "local-path",
        {"name": "local-path"},
        sc_spec,
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "pvc-block",
        {"name": "pvc-block", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
    )

    controller.reconcile_once()

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "pvc-block")
    assert pvc is not None
    pv_name = pvc.spec.get("volumeName")
    assert pv_name == f"pvc-{pvc_uid}"
    pv = store.get("", "v1", "persistentvolumes", None, pv_name)
    assert pv is not None
    assert (pv.spec or {}).get("volumeMode") == "Block"
    host_path = Path(((pv.spec or {}).get("hostPath") or {}).get("path"))
    assert host_path.is_file()
    assert host_path.stat().st_size == 8 * 1024 * 1024


def test_storage_controller_clone_local_path(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    host_root = tmp_path / "local-root"
    sc_spec = {
        "provisioner": "k1s.io/local-path",
        "parameters": {"hostPath": str(host_root)},
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "Immediate",
    }
    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "local-path",
        {"name": "local-path"},
        sc_spec,
    )

    src_uid = "uid-src"
    src_host_path = host_root / "src-vol"
    src_host_path.mkdir(parents=True, exist_ok=True)
    (src_host_path / "data.txt").write_text("clone-me", encoding="utf-8")
    src_pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "local-path",
        "claimRef": {"namespace": "default", "name": "src", "uid": src_uid},
        "hostPath": {"path": str(src_host_path)},
    }
    src_pv_meta = {
        "name": "pv-src",
        "annotations": {
            "k1s.io/local-host-root": str(host_root),
            "k1s.io/local-host-path": str(src_host_path),
        },
    }
    src_pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "local-path",
        "volumeName": "pv-src",
        "resources": {"requests": {"storage": "1Gi"}},
    }
    store.upsert("", "v1", "persistentvolumes", None, "pv-src", src_pv_meta, src_pv_spec)
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "src",
        {"name": "src", "namespace": "default", "uid": src_uid},
        src_pvc_spec,
        status={"phase": "Bound"},
    )

    clone_uid = "uid-clone"
    clone_spec = {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "local-path",
        "resources": {"requests": {"storage": "1Gi"}},
        "dataSourceRef": {"kind": "PersistentVolumeClaim", "name": "src", "namespace": "default"},
    }
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "clone",
        {"name": "clone", "namespace": "default", "uid": clone_uid},
        clone_spec,
        status={"phase": "Pending"},
    )

    controller.reconcile_once()

    clone_pvc = store.get("", "v1", "persistentvolumeclaims", "default", "clone")
    assert clone_pvc is not None
    clone_pv_name = clone_pvc.spec.get("volumeName")
    assert clone_pv_name == f"pvc-{clone_uid}"
    clone_pv = store.get("", "v1", "persistentvolumes", None, clone_pv_name)
    assert clone_pv is not None
    clone_host_path = Path(((clone_pv.spec or {}).get("hostPath") or {}).get("path"))
    assert (clone_host_path / "data.txt").read_text(encoding="utf-8") == "clone-me"
    annotations = (clone_pv.metadata or {}).get("annotations") or {}
    assert annotations.get("k1s.io/clone-source") == "default/src"


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


def test_storage_controller_pvc_delete_retain_marks_pv_released(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_uid = "uid-retain"
    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteMany"],
        "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": "k1s-nfs",
    }
    pvc_spec = {
        "accessModes": ["ReadWriteMany"],
        "volumeName": "pv-retain",
        "resources": {"requests": {"storage": "1Gi"}},
    }
    store.upsert("", "v1", "persistentvolumes", None, "pv-retain", {"name": "pv-retain"}, pv_spec)
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "pvc-retain",
        {"name": "pvc-retain", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
    )

    controller._handle_pvc_deleted(
        store.get("", "v1", "persistentvolumeclaims", "default", "pvc-retain")
    )

    pv = store.get("", "v1", "persistentvolumes", None, "pv-retain")
    assert pv is not None
    assert (pv.status or {}).get("phase") == "Released"


def test_storage_controller_pv_delete_marks_pvc_lost(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-gone",
        "resources": {"requests": {"storage": "1Gi"}},
    }
    pvc_meta = {"name": "pvc-gone", "namespace": "default"}
    pv_spec = {
        "accessModes": ["ReadWriteOnce"],
        "persistentVolumeReclaimPolicy": "Delete",
        "claimRef": {"namespace": "default", "name": "pvc-gone"},
    }
    store.upsert("", "v1", "persistentvolumeclaims", "default", "pvc-gone", pvc_meta, pvc_spec)
    store.upsert("", "v1", "persistentvolumes", None, "pv-gone", {"name": "pv-gone"}, pv_spec)

    pv = store.get("", "v1", "persistentvolumes", None, "pv-gone")
    assert pv is not None
    controller._handle_pv_deleted(pv)

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "pvc-gone")
    assert pvc is not None
    assert (pvc.status or {}).get("phase") == "Lost"


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


def test_storage_controller_quota_blocks_pvc(tmp_path, monkeypatch):
    quota_path = tmp_path / "quotas.yaml"
    quota_path.write_text(
        "apiVersion: k1s.io/v1\n"
        "kind: StorageQuota\n"
        "metadata:\n"
        "  name: default\n"
        "spec:\n"
        "  namespace: default\n"
        "  hard:\n"
        "    requests.storage: 1Gi\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AE_STORAGE_QUOTAS", str(quota_path))
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "k1s-nfs",
        "resources": {"requests": {"storage": "2Gi"}},
    }
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data",
        {"name": "data", "namespace": "default", "uid": "pvc-uid"},
        pvc_spec,
        status={"phase": "Pending"},
    )

    controller.reconcile_once()

    pvc = store.get("", "v1", "persistentvolumeclaims", "default", "data")
    assert pvc is not None
    assert (pvc.status or {}).get("phase") == "Pending"
    events = store.list_all("", "v1", "events")
    reasons = [(e.spec or {}).get("reason") for e in events]
    assert "StorageQuotaExceeded" in reasons


def test_storage_controller_quota_blocks_expansion(tmp_path, monkeypatch):
    quota_path = tmp_path / "quotas.yaml"
    quota_path.write_text(
        "apiVersion: k1s.io/v1\n"
        "kind: StorageQuota\n"
        "metadata:\n"
        "  name: default\n"
        "spec:\n"
        "  namespace: default\n"
        "  hard:\n"
        "    requests.storage: 2Gi\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AE_STORAGE_QUOTAS", str(quota_path))
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "k1s-nfs",
        "claimRef": {"namespace": "default", "name": "data", "uid": "pvc-uid"},
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-quota",
        "storageClassName": "k1s-nfs",
        "resources": {"requests": {"storage": "3Gi"}},
    }
    store.upsert(
        "",
        "v1",
        "persistentvolumes",
        None,
        "pv-quota",
        {"name": "pv-quota"},
        pv_spec,
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data",
        {"name": "data", "namespace": "default", "uid": "pvc-uid"},
        pvc_spec,
        status={"phase": "Bound"},
    )

    controller.reconcile_once()

    pv = store.get("", "v1", "persistentvolumes", None, "pv-quota")
    assert pv is not None
    assert ((pv.spec or {}).get("capacity") or {}).get("storage") == "1Gi"
    events = store.list_all("", "v1", "events")
    reasons = [(e.spec or {}).get("reason") for e in events]
    assert "StorageQuotaExceeded" in reasons


def test_storage_controller_volume_health_events(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    host_root = tmp_path / "local-root"
    host_path = host_root / "vol-1"
    pvc_uid = "uid-health"
    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "local-path",
        "claimRef": {"namespace": "default", "name": "data", "uid": pvc_uid},
    }
    pv_meta = {
        "name": "pv-health",
        "annotations": {
            "k1s.io/local-host-root": str(host_root),
            "k1s.io/local-host-path": str(host_path),
        },
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-health",
        "resources": {"requests": {"storage": "1Gi"}},
    }
    pvc_meta = {"name": "data", "namespace": "default", "uid": pvc_uid}

    store.upsert("", "v1", "persistentvolumes", None, "pv-health", pv_meta, pv_spec)
    store.upsert("", "v1", "persistentvolumeclaims", "default", "data", pvc_meta, pvc_spec)

    controller.reconcile_once()
    events = store.list_all("", "v1", "events")
    reasons = [(e.spec or {}).get("reason") for e in events]
    assert "VolumeUnhealthy" in reasons

    host_path.mkdir(parents=True, exist_ok=True)
    controller.reconcile_once()
    events = store.list_all("", "v1", "events")
    reasons = [(e.spec or {}).get("reason") for e in events]
    assert "VolumeHealthy" in reasons


def test_storage_controller_storage_capacity_topology(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    host_root = tmp_path / "local-root"
    host_root.mkdir(parents=True, exist_ok=True)
    sc_spec = {
        "provisioner": "k1s.io/local-path",
        "volumeBindingMode": "Immediate",
        "parameters": {"hostPath": str(host_root)},
    }
    pv_spec = {
        "capacity": {"storage": "1Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "local-path",
        "nodeAffinity": {
            "required": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": ["node-a"],
                            }
                        ]
                    }
                ]
            }
        },
    }
    pv_meta = {
        "name": "pv-capacity",
        "annotations": {
            "k1s.io/local-host-root": str(host_root),
            "k1s.io/local-host-path": str(host_root / "vol-1"),
        },
    }
    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "local-path",
        {"name": "local-path"},
        sc_spec,
        status={},
    )
    store.upsert("", "v1", "persistentvolumes", None, "pv-capacity", pv_meta, pv_spec)

    controller.reconcile_once()
    caps = store.list_all("storage.k8s.io", "v1", "csistoragecapacities")
    assert caps
    cap = next((c for c in caps if (c.spec or {}).get("storageClassName") == "local-path"), None)
    assert cap is not None
    assert int((cap.spec or {}).get("capacity") or 0) > 0
    topo = (cap.spec or {}).get("nodeTopology") or {}
    exprs = topo.get("matchExpressions") if isinstance(topo, dict) else []
    assert any(
        isinstance(e, dict)
        and e.get("key") == "kubernetes.io/hostname"
        and "node-a" in (e.get("values") or [])
        for e in (exprs or [])
    )


def test_storage_controller_storage_capacity_override(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    sc_spec = {
        "provisioner": "csi.example.com",
        "parameters": {"capacity": "5Gi"},
        "allowedTopologies": [
            {
                "matchLabelExpressions": [
                    {
                        "key": "topology.kubernetes.io/zone",
                        "operator": "In",
                        "values": ["zone-a"],
                    }
                ]
            }
        ],
    }
    store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "csi-ext",
        {"name": "csi-ext"},
        sc_spec,
        status={},
    )

    controller.reconcile_once()

    caps = store.list_all("storage.k8s.io", "v1", "csistoragecapacities")
    assert caps
    cap = next((c for c in caps if (c.spec or {}).get("storageClassName") == "csi-ext"), None)
    assert cap is not None
    assert int((cap.spec or {}).get("capacity") or 0) == 5 * 1024 * 1024 * 1024
    topo = (cap.spec or {}).get("nodeTopology") or {}
    exprs = topo.get("matchExpressions") if isinstance(topo, dict) else []
    assert any(
        isinstance(e, dict)
        and e.get("key") == "topology.kubernetes.io/zone"
        and "zone-a" in (e.get("values") or [])
        for e in (exprs or [])
    )


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


def test_storage_controller_defaults_snapshot_class(tmp_path):
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
    pvc_uid = "uid-src-default"
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
    (source_path / "data.txt").write_text("hello default", encoding="utf-8")

    snap_class_meta = {
        "name": "nfs-default",
        "annotations": {"snapshot.storage.kubernetes.io/is-default-class": "true"},
    }
    snap_class_spec = {"driver": "k1s.io/nfs", "deletionPolicy": "Delete"}
    store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshotclasses",
        None,
        "nfs-default",
        snap_class_meta,
        snap_class_spec,
        status={},
    )
    snap_spec = {"source": {"persistentVolumeClaimName": "data-src"}}
    store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshots",
        "default",
        "snap-default",
        {"name": "snap-default", "namespace": "default", "uid": "snap-default-uid"},
        snap_spec,
        status={},
    )

    controller.reconcile_once()

    snap = store.get(
        "snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "snap-default"
    )
    assert snap is not None
    assert (snap.spec or {}).get("volumeSnapshotClassName") == "nfs-default"
    assert (snap.status or {}).get("readyToUse") is True


def test_storage_controller_snapshot_csi_ready(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    controller = StorageController(store)
    pvc_uid = "uid-csi"
    pv_spec = {
        "capacity": {"storage": "2Gi"},
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "csi-sc",
        "claimRef": {"namespace": "default", "name": "data-csi", "uid": pvc_uid},
        "csi": {"driver": "csi.example.com", "volumeHandle": "vol-123"},
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-csi",
        "storageClassName": "csi-sc",
        "resources": {"requests": {"storage": "2Gi"}},
    }
    store.upsert(
        "",
        "v1",
        "persistentvolumes",
        None,
        "pv-csi",
        {"name": "pv-csi"},
        pv_spec,
    )
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data-csi",
        {"name": "data-csi", "namespace": "default", "uid": pvc_uid},
        pvc_spec,
        status={"phase": "Bound"},
    )
    snap_class_spec = {"driver": "csi.example.com", "deletionPolicy": "Retain"}
    store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshotclasses",
        None,
        "csi-snap",
        {"name": "csi-snap"},
        snap_class_spec,
        status={},
    )
    snap_uid = "snap-csi-uid"
    snap_spec = {
        "source": {"persistentVolumeClaimName": "data-csi"},
        "volumeSnapshotClassName": "csi-snap",
    }
    store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshots",
        "default",
        "snap-csi",
        {"name": "snap-csi", "namespace": "default", "uid": snap_uid},
        snap_spec,
        status={},
    )

    pv = store.get("", "v1", "persistentvolumes", None, "pv-csi")
    snap = store.get("snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "snap-csi")
    assert pv is not None
    assert snap is not None
    content_name = controller._snapshot_content_name(snap, pv)
    content_spec = {
        "deletionPolicy": "Retain",
        "driver": "csi.example.com",
        "volumeSnapshotRef": {
            "name": "snap-csi",
            "namespace": "default",
            "uid": snap_uid,
        },
        "source": {"volumeHandle": "vol-123"},
    }
    content_status = {"readyToUse": True, "snapshotHandle": "snap-123", "restoreSize": "2Gi"}
    store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshotcontents",
        None,
        content_name,
        {"name": content_name},
        content_spec,
        status=content_status,
    )

    controller.reconcile_once()

    snap = store.get("snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "snap-csi")
    assert snap is not None
    assert (snap.status or {}).get("readyToUse") is True
    assert (snap.status or {}).get("boundVolumeSnapshotContentName") == content_name
    content = store.get(
        "snapshot.storage.k8s.io", "v1", "volumesnapshotcontents", None, content_name
    )
    assert content is not None
    source = (content.spec or {}).get("source") or {}
    assert source.get("volumeHandle") == "vol-123"
    assert (content.status or {}).get("readyToUse") is True
