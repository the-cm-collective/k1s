from types import SimpleNamespace

import pytest

from ae.storage.netfs import NetFSManager
from ae.storage.state import InMemoryStorageState
from ae.storage.types import PvcRef, PvRef


class FakeState(InMemoryStorageState):
    def __init__(self, pv_obj, attachment, secrets):
        super().__init__()
        self._pv_obj = pv_obj
        self._attachment = attachment
        self._secrets = secrets
        self.secret_calls: list[tuple[str, str]] = []
        self.events: list[tuple[str, str]] = []

    def get_pv_for_pvc(self, pvc: PvcRef) -> PvRef | None:
        _ = pvc
        return PvRef(name="pv1")

    def get_pv(self, pv: PvRef):
        _ = pv
        return self._pv_obj

    def get_volume_attachment(self, pv: PvRef, node_id: str):
        _ = pv
        return self._attachment if node_id == "node1" else None

    def get_secret(self, namespace: str, name: str):
        self.secret_calls.append((namespace, name))
        return self._secrets.get((namespace, name))

    def record_pvc_event(self, pvc: PvcRef, reason: str, message: str) -> None:
        _ = pvc
        self.events.append((reason, message))


def _attached_va() -> SimpleNamespace:
    return SimpleNamespace(
        spec={"nodeName": "node1", "source": {"persistentVolumeName": "pv1"}},
        status={"attached": True},
    )


def test_netfs_csi_mount_resolves_secret_refs(tmp_path) -> None:
    pv_obj = {
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "csi": {
                "driver": "csi.example.com",
                "volumeHandle": "vol-1",
                "nodeStageSecretRef": {"name": "stage", "namespace": "demo"},
                "nodePublishSecretRef": {"name": "publish", "namespace": "demo"},
            },
        }
    }
    secrets = {
        ("demo", "stage"): {"username": "user"},
        ("demo", "publish"): {"password": "pass"},
    }
    state = FakeState(pv_obj, _attached_va(), secrets)
    manager = NetFSManager(state, root=tmp_path)

    mount = manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node1")
    assert mount.host_path
    assert ("demo", "stage") in state.secret_calls
    assert ("demo", "publish") in state.secret_calls

    marker = tmp_path / "default" / "pvc" / ".csi-volume"
    content = marker.read_text(encoding="utf-8")
    assert "nodeStageSecretRef=demo/stage" in content
    assert "nodePublishSecretRef=demo/publish" in content
    assert "nodeStageSecretRef.keys=username" in content
    assert "nodePublishSecretRef.keys=password" in content


def test_netfs_csi_mount_fails_when_secret_missing(tmp_path) -> None:
    pv_obj = {
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "csi": {
                "driver": "csi.example.com",
                "volumeHandle": "vol-1",
                "nodeStageSecretRef": {"name": "stage", "namespace": "demo"},
            },
        }
    }
    state = FakeState(pv_obj, _attached_va(), secrets={})
    manager = NetFSManager(state, root=tmp_path)

    with pytest.raises(KeyError):
        manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node1")
    assert any(reason == "SecretNotFound" for reason, _msg in state.events)

