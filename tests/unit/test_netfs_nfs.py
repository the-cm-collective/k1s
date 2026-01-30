from pathlib import Path

import pytest

from ae.storage.netfs import NetFSManager
from ae.storage.state import InMemoryStorageState
from ae.storage.types import PvcRef, PvRef


class FakeState(InMemoryStorageState):
    def __init__(self, pv_obj, sc_obj=None):
        super().__init__()
        self._pv_obj = pv_obj
        self._sc_obj = sc_obj
        self.events: list[tuple[str, str]] = []

    def get_pv_for_pvc(self, pvc: PvcRef) -> PvRef | None:
        return PvRef(name="pv1")

    def get_pv(self, pv: PvRef):
        return self._pv_obj

    def get_storage_class(self, name: str):
        if self._sc_obj is None:
            return None
        return self._sc_obj

    def record_pvc_event(self, pvc: PvcRef, reason: str, message: str) -> None:
        _ = pvc
        self.events.append((reason, message))


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


def test_netfs_applies_fs_group(tmp_path, monkeypatch) -> None:
    pv_obj = {"spec": {"nfs": {"server": "10.0.0.4", "path": "/export"}}}
    state = FakeState(pv_obj)
    manager = NetFSManager(state, root=tmp_path)

    monkeypatch.setattr(manager, "_mount_nfs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_mount_info", lambda _target: None)
    monkeypatch.setattr(manager, "_ensure_nfs_tools", lambda: None)

    seen = {}

    def _fake_apply(pvc, target, fs_group):
        seen["pvc"] = pvc
        seen["target"] = target
        seen["fs_group"] = fs_group

    monkeypatch.setattr(manager, "_apply_fs_group", _fake_apply)

    manager.ensure_mount(
        PvcRef(name="pvc", namespace="default"), node_id="node1", fs_group=1234
    )
    assert seen["fs_group"] == 1234


def test_netfs_applies_selinux(tmp_path, monkeypatch) -> None:
    pv_obj = {"spec": {"nfs": {"server": "10.0.0.5", "path": "/export"}}}
    state = FakeState(pv_obj)
    manager = NetFSManager(state, root=tmp_path)

    monkeypatch.setattr(manager, "_mount_nfs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_mount_info", lambda _target: None)
    monkeypatch.setattr(manager, "_ensure_nfs_tools", lambda: None)

    seen = {}

    def _fake_selinux(pvc, target, opts, *, recursive=False):
        seen["pvc"] = pvc
        seen["target"] = target
        seen["opts"] = opts
        seen["recursive"] = recursive

    monkeypatch.setattr(manager, "_apply_selinux", _fake_selinux)

    manager.ensure_mount(
        PvcRef(name="pvc", namespace="default"),
        node_id="node1",
        selinux={"type": "container_file_t"},
    )
    assert seen["opts"]["type"] == "container_file_t"
    assert seen["recursive"] is False


def test_netfs_selinux_recursive_for_rwx(tmp_path, monkeypatch) -> None:
    pv_obj = {
        "spec": {
            "nfs": {"server": "10.0.0.6", "path": "/export"},
            "accessModes": ["ReadWriteMany"],
        }
    }
    state = FakeState(pv_obj)
    manager = NetFSManager(state, root=tmp_path)

    monkeypatch.setenv("AE_NETFS_SELINUX_RECURSIVE", "1")
    monkeypatch.setattr(manager, "_mount_nfs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_mount_info", lambda _target: None)
    monkeypatch.setattr(manager, "_ensure_nfs_tools", lambda: None)

    seen = {}

    def _fake_selinux(pvc, target, opts, *, recursive=False):
        seen["recursive"] = recursive

    monkeypatch.setattr(manager, "_apply_selinux", _fake_selinux)

    manager.ensure_mount(
        PvcRef(name="pvc", namespace="default"),
        node_id="node1",
        selinux={"type": "container_file_t"},
    )
    assert seen["recursive"] is True


def test_netfs_block_device_requires_device_path(tmp_path) -> None:
    block_path = tmp_path / "block-dev"
    block_path.write_text("fake", encoding="utf-8")
    pv_obj = {"spec": {"volumeMode": "Block", "hostPath": {"path": str(block_path)}}}
    state = FakeState(pv_obj)
    manager = NetFSManager(state, root=tmp_path)

    with pytest.raises(ValueError):
        manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node1")

    mount = manager.ensure_mount(
        PvcRef(name="pvc", namespace="default"), node_id="node1", for_device=True
    )
    assert mount.host_path == str(block_path)


def test_netfs_hostpath_mount_creates_local_path(tmp_path) -> None:
    host_path = tmp_path / "local-vol"
    pv_obj = {
        "metadata": {"annotations": {"k1s.io/local-host-path": str(host_path)}},
        "spec": {
            "hostPath": {"path": str(host_path)},
            "storageClassName": "k1s-local",
        },
    }
    sc_obj = {"spec": {"provisioner": "k1s.io/local-path"}}
    state = FakeState(pv_obj, sc_obj)
    manager = NetFSManager(state, root=tmp_path / "netfs-root")

    mount = manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node1")
    assert Path(mount.host_path).is_dir()


def test_netfs_hostpath_mount_respects_node_affinity(tmp_path) -> None:
    host_path = tmp_path / "local-vol"
    pv_obj = {
        "spec": {
            "hostPath": {"path": str(host_path)},
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": ["node-2"],
                                }
                            ]
                        }
                    ]
                }
            },
        }
    }
    state = FakeState(pv_obj)
    manager = NetFSManager(state, root=tmp_path / "netfs-root")

    with pytest.raises(RuntimeError):
        manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node-1")
    assert any(reason == "NodeAffinityMismatch" for reason, _msg in state.events)


def test_netfs_hostpath_mount_respects_read_only_modes(tmp_path) -> None:
    host_path = tmp_path / "local-vol"
    host_path.mkdir()
    pv_obj = {
        "spec": {
            "hostPath": {"path": str(host_path)},
            "accessModes": ["ReadOnlyMany"],
        }
    }
    state = FakeState(pv_obj)
    manager = NetFSManager(state, root=tmp_path / "netfs-root")

    mount = manager.ensure_mount(PvcRef(name="pvc", namespace="default"), node_id="node-1")
    assert mount.read_only is True
