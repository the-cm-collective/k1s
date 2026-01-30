from __future__ import annotations

from ae.controller.spec import AppManifest, AppSpec, Metadata, PvcMountSpec
from ae.storage.node_manager import NodeVolumeManager
from ae.storage.types import NetFSMount, PvcRef, PvRef


class _StubNetFS:
    def ensure_mount(  # noqa: D401 - stub
        self, pvc: PvcRef, *, node_id: str, fs_group=None, selinux=None, for_device=False
    ) -> NetFSMount:
        return NetFSMount(pvc=pvc, pv=PvRef(name="pv1"), node_id=node_id, host_path="/dev/loop0")


class _CaptureNetFS:
    def __init__(self) -> None:
        self.last_pvc: PvcRef | None = None

    def ensure_mount(  # noqa: D401 - stub
        self, pvc: PvcRef, *, node_id: str, fs_group=None, selinux=None, for_device=False
    ) -> NetFSMount:
        _ = (node_id, fs_group, selinux, for_device)
        self.last_pvc = pvc
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


def test_node_volume_manager_injects_subpath_mount() -> None:
    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="busybox",
            pvc_mounts=[
                PvcMountSpec(
                    claim_name="data", mount_path="/data", sub_path="cache/subdir"
                )
            ],
        ),
    )
    manager = NodeVolumeManager(_StubNetFS(), node_id="node-a")
    out = manager.inject_pvc_mounts(man)
    volumes = out.spec.volumes
    assert volumes
    vol = volumes[0]
    assert vol.host_path == "/dev/loop0/cache/subdir"
    assert vol.mount_path == "/data"


def test_node_volume_manager_scoped_container_mounts() -> None:
    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="busybox",
            pvc_mounts=[PvcMountSpec(claim_name="data", mount_path="/data")],
            containers=[
                AppSpec.ContainerSpec(
                    name="sidecar",
                    image="busybox",
                    pvc_mounts=[PvcMountSpec(claim_name="cache", mount_path="/cache")],
                )
            ],
            init_containers=[
                AppSpec.ContainerSpec(
                    name="init",
                    image="busybox",
                    pvc_mounts=[PvcMountSpec(claim_name="init", mount_path="/init")],
                )
            ],
        ),
    )
    manager = NodeVolumeManager(_StubNetFS(), node_id="node-a")
    out = manager.inject_pvc_mounts(man)
    assert any(v.mount_path == "/data" for v in out.spec.volumes)
    assert all(v.mount_path != "/cache" for v in out.spec.volumes)
    sidecar = out.spec.containers[0]
    assert any(v.mount_path == "/cache" for v in sidecar.volume_mounts)
    initc = out.spec.init_containers[0]
    assert any(v.mount_path == "/init" for v in initc.volume_mounts)


def test_node_volume_manager_resolves_stateful_claim_template() -> None:
    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="db"),
        spec=AppSpec(
            image="busybox",
            pvc_mounts=[
                PvcMountSpec(
                    claim_name="data",
                    mount_path="/data",
                    claim_template=True,
                )
            ],
        ),
    )
    netfs = _CaptureNetFS()
    manager = NodeVolumeManager(netfs, node_id="node-a")
    out = manager.inject_pvc_mounts(man, replica_id="db-rev3-2")
    assert netfs.last_pvc is not None
    assert netfs.last_pvc.name == "data-db-2"
    assert any(v.mount_path == "/data" for v in out.spec.volumes)
