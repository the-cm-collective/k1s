"""CRI runtime config mapping tests."""

from types import SimpleNamespace

from ae.controller.spec import AppManifest
from ae.runtime.cri_runtime import CRIRuntime


def _manifest_with_sidecar() -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
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
    assert main_cfg.image.image == "docker.io/library/alpine:3.20"

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
    assert side_cfg.image.image == "docker.io/library/alpine:3.20"

    mounts = list(side_cfg.mounts or [])
    assert any(
        m.host_path == "/tmp/ae-proj/config/db" and m.container_path == "/etc/db"
        for m in mounts
    )


def test_cri_normalize_image_ref_handles_short_and_registry_refs() -> None:
    runtime = CRIRuntime()

    assert runtime._normalize_image_ref("busybox") == "docker.io/library/busybox"
    assert runtime._normalize_image_ref("busybox:1.36") == "docker.io/library/busybox:1.36"
    assert runtime._normalize_image_ref("library/busybox:1.36") == "docker.io/library/busybox:1.36"
    assert runtime._normalize_image_ref("ghcr.io/acme/demo:1") == "ghcr.io/acme/demo:1"
    assert runtime._normalize_image_ref("localhost:5001/demo:1") == "localhost:5001/demo:1"


def test_run_pod_sets_runtime_handler_from_runtime_class_name(monkeypatch) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "gpu-demo", "namespace": "default"},
            "spec": {
                "image": "nvidia/cuda:12.4.1-base-ubuntu22.04",
                "replicas": 1,
                "runtimeClassName": "nvidia",
            },
        }
    )
    runtime = CRIRuntime()
    captured: dict[str, object] = {}

    def fake_runtime_call(method: str, req):
        captured["method"] = method
        captured["req"] = req
        return SimpleNamespace(pod_sandbox_id="pod-1")

    monkeypatch.setattr(runtime, "_runtime_call", fake_runtime_call)
    monkeypatch.setattr(runtime, "_create_main_container", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)

    runtime._run_pod(manifest, "gpu-demo-rev1-0", 1)

    assert captured.get("method") == "RunPodSandbox"
    req = captured.get("req")
    assert req is not None
    assert str(getattr(req, "runtime_handler", "")) == "nvidia"
