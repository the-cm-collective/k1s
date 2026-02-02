from __future__ import annotations

from ae.controller.spec import AppManifest, AppSpec, Metadata, PvcMountSpec
from ae.storage.node_manager import NodeVolumeManager
from ae.storage.types import NetFSMount, PvcRef, PvRef


class _StubNetFS:
    def ensure_mount(  # noqa: D401 - stub
        self, pvc: PvcRef, *, node_id: str, fs_group=None, selinux=None, for_device=False
    ) -> NetFSMount:
        return NetFSMount(pvc=pvc, pv=PvRef(name="pv1"), node_id=node_id, host_path="/dev/loop0")


def test_node_volume_manager_injects_device_mount() -> None:
    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="busybox",
            pvc_mounts=[
                PvcMountSpec(claim_name="data", mount_path="/data", device_path="/dev/block")
            ],
        ),
    )
    manager = NodeVolumeManager(_StubNetFS(), node_id="node-a")
    out = manager.inject_pvc_mounts(man)
    devices = out.spec.volume_devices
    assert devices
    dev = devices[0]
    assert dev.host_path == "/dev/loop0"
    assert dev.device_path == "/dev/block"
