from ae.apishim.store import ObjectStore
from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import SQLiteStateStore
from ae.runtime import StubRuntime


def test_reconciler_sets_selected_node_for_local_path(tmp_path, monkeypatch):
    apishim_db = tmp_path / "apishim.db"
    monkeypatch.setenv("AE_APISHIM_DB", str(apishim_db))
    shim = ObjectStore(db_path=apishim_db)
    sc_spec = {
        "provisioner": "k1s.io/local-path",
        "parameters": {"hostPath": str(tmp_path / "storage-root")},
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "WaitForFirstConsumer",
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "k1s-local",
        "resources": {"requests": {"storage": "1Gi"}},
    }
    shim.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "k1s-local",
        {"name": "k1s-local"},
        sc_spec,
        status={},
    )
    shim.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data",
        {"name": "data", "namespace": "default", "uid": "pvc-uid"},
        pvc_spec,
        status={"phase": "Pending"},
    )

    state = SQLiteStateStore(tmp_path / "state.db")
    state.upsert_node("n1", name="n1", labels={}, taints=[], backend="podman", endpoint="")
    state.upsert_node("n2", name="n2", labels={}, taints=[], backend="podman", endpoint="")
    state.record_heartbeat("n1", "Ready")
    state.record_heartbeat("n2", "Ready")

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="app"),
        spec=AppSpec(
            image="busybox",
            replicas=1,
            pvc_mounts=[{"claimName": "data", "mountPath": "/data"}],
        ),
    )

    reconciler = Reconciler(StubRuntime(), state)
    reconciler.reconcile(manifest)

    pvc = shim.get("", "v1", "persistentvolumeclaims", "default", "data")
    assert pvc is not None
    annotations = (pvc.metadata or {}).get("annotations") or {}
    assert annotations.get("volume.kubernetes.io/selected-node") == "n1"


def test_reconciler_sets_selected_node_for_csi_single_writer(tmp_path, monkeypatch):
    apishim_db = tmp_path / "apishim.db"
    monkeypatch.setenv("AE_APISHIM_DB", str(apishim_db))
    shim = ObjectStore(db_path=apishim_db)

    pv_spec = {
        "accessModes": ["ReadWriteOnce"],
        "claimRef": {"namespace": "default", "name": "data", "uid": "pvc-uid"},
        "csi": {"driver": "csi.example.com", "volumeHandle": "vol-1"},
    }
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "volumeName": "pv-csi",
        "resources": {"requests": {"storage": "1Gi"}},
    }
    shim.upsert("", "v1", "persistentvolumes", None, "pv-csi", {"name": "pv-csi"}, pv_spec)
    shim.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data",
        {"name": "data", "namespace": "default", "uid": "pvc-uid"},
        pvc_spec,
        status={"phase": "Bound"},
    )

    state = SQLiteStateStore(tmp_path / "state.db")
    state.upsert_node("n1", name="n1", labels={}, taints=[], backend="podman", endpoint="")
    state.upsert_node("n2", name="n2", labels={}, taints=[], backend="podman", endpoint="")
    state.record_heartbeat("n1", "Ready")
    state.record_heartbeat("n2", "Ready")

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="app"),
        spec=AppSpec(
            image="busybox",
            replicas=1,
            pvc_mounts=[{"claimName": "data", "mountPath": "/data"}],
        ),
    )

    reconciler = Reconciler(StubRuntime(), state)
    reconciler.reconcile(manifest)

    pvc = shim.get("", "v1", "persistentvolumeclaims", "default", "data")
    assert pvc is not None
    annotations = (pvc.metadata or {}).get("annotations") or {}
    assert annotations.get("volume.kubernetes.io/selected-node") == "n1"
