"""CRI runtime config mapping tests."""

from ae.controller.spec import AppManifest
from ae.runtime.cri_runtime import CRIRuntime


def _manifest_with_sidecar() -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "App",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "command": ["/bin/app"],
                "args": ["--main"],
                "volumes": [
                    {
                        "hostPath": "/tmp/ae-proj",
                        "mountPath": "/var/run/ae/config/demo",
                        "readOnly": True,
                    }
                ],
                "containers": [
                    {
                        "name": "sidecar",
                        "image": "alpine:3.20",
                        "command": ["/bin/side"],
                        "args": ["--flag"],
                        "projectionMounts": [
                            {"path": "config/db", "mountPath": "/etc/db", "readOnly": True}
                        ],
                    }
                ],
            },
        }
    )


def test_cri_container_config_separates_command_args_and_mounts():
    manifest = _manifest_with_sidecar()
    runtime = CRIRuntime()

    main_cfg = runtime._container_config(manifest, "demo-rev1-0", 1, attempt=0)
    assert list(main_cfg.command) == ["/bin/app"]
    assert list(main_cfg.args) == ["--main"]
    assert main_cfg.labels.get("ae.container") == "main"

    sidecar = manifest.spec.containers[0]
    side_cfg = runtime._container_config_for_spec(
        manifest,
        sidecar,
        name="sidecar",
        replica_id="demo-rev1-0",
        revision=1,
        attempt=0,
        is_main=False,
    )
    assert list(side_cfg.command) == ["/bin/side"]
    assert list(side_cfg.args) == ["--flag"]
    assert side_cfg.labels.get("ae.container") == "sidecar"

    mounts = list(side_cfg.mounts or [])
    assert any(
        m.host_path == "/tmp/ae-proj/config/db" and m.container_path == "/etc/db"
        for m in mounts
    )


def test_cri_container_config_maps_volume_devices():
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "volumeDevices": [
                    {"hostPath": "/dev/sdb", "devicePath": "/dev/xvdb", "readOnly": True}
                ],
            },
        }
    )
    runtime = CRIRuntime()
    cfg = runtime._container_config(manifest, "demo-rev1-0", 1, attempt=0)
    devices = list(cfg.devices or [])
    assert len(devices) == 1
    dev = devices[0]
    assert dev.host_path == "/dev/sdb"
    assert dev.container_path == "/dev/xvdb"
    assert dev.permissions == "r"
