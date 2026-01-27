import pytest

from ae.storage.netfs import NetFSManager
from ae.storage.state import InMemoryStorageState
from ae.storage.types import PvcRef, PvRef


class FakeState(InMemoryStorageState):
    def __init__(self, pv_obj, sc_obj=None):
        super().__init__()
        self._pv_obj = pv_obj
        self._sc_obj = sc_obj

    def get_pv_for_pvc(self, pvc: PvcRef) -> PvRef | None:
        return PvRef(name="pv1")

    def get_pv(self, pv: PvRef):
        return self._pv_obj

    def get_storage_class(self, name: str):
        return self._sc_obj if name == "k1s-nfs" else None


def test_netfs_mount_uses_storageclass_options(tmp_path, monkeypatch) -> None:
    pv_obj = {
        "spec": {
            "nfs": {"server": "10.0.0.1", "path": "/export"},
            "storageClassName": "k1s-nfs",
            "mountOptions": ["vers=4.2", "rw"],
        }
    }
    sc_obj = {"spec": {"mountOptions": ["rsize=1048576"]}}
    state = FakeState(pv_obj, sc_obj)
    manager = NetFSManager(state, root=tmp_path)
    calls = {}

    def _fake_mount(source, target, options):
        calls["source"] = source
        calls["target"] = target
        calls["options"] = options

    monkeypatch.setattr(manager, "_mount_nfs", _fake_mount)
    monkeypatch.setattr(manager, "_mount_info", lambda _target: None)
    monkeypatch.setattr(manager, "_ensure_nfs_tools", lambda: None)

    mount = manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node1")
    assert calls["source"] == "10.0.0.1:/export"
    assert "rsize=1048576" in calls["options"]
    assert "vers=4.2" in calls["options"]
    assert calls["options"][-1] == "rw"
    assert mount.read_only is False


def test_netfs_mount_respects_read_only(tmp_path, monkeypatch) -> None:
    pv_obj = {
        "spec": {
            "nfs": {"server": "10.0.0.2", "path": "/export", "readOnly": True},
            "mountOptions": ["vers=4.1"],
        }
    }
    state = FakeState(pv_obj)
    manager = NetFSManager(state, root=tmp_path)
    calls = {}

    def _fake_mount(source, target, options):
        calls["source"] = source
        calls["target"] = target
        calls["options"] = options

    monkeypatch.setattr(manager, "_mount_nfs", _fake_mount)
    monkeypatch.setattr(manager, "_mount_info", lambda _target: None)
    monkeypatch.setattr(manager, "_ensure_nfs_tools", lambda: None)

    mount = manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node1")
    assert calls["source"] == "10.0.0.2:/export"
    assert calls["options"][-1] == "ro"
    assert mount.read_only is True


def test_netfs_rwop_blocks_second_node(tmp_path, monkeypatch) -> None:
    pv_obj = {
        "spec": {
            "nfs": {"server": "10.0.0.3", "path": "/export"},
            "accessModes": ["ReadWriteOncePod"],
        }
    }
    state = FakeState(pv_obj)
    manager = NetFSManager(state, root=tmp_path)

    monkeypatch.setattr(manager, "_mount_nfs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_mount_info", lambda _target: None)
    monkeypatch.setattr(manager, "_ensure_nfs_tools", lambda: None)

    manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node1")
    with pytest.raises(RuntimeError):
        manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node2")
