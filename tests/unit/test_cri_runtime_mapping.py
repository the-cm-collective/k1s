"""CRI runtime config mapping tests."""

from types import SimpleNamespace

import pytest

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
    assert str(main_cfg.log_path) == "main/0.log"
    assert not str(main_cfg.log_path).startswith("/")
    assert main_cfg.labels.get("ae.container") == "main"
    assert main_cfg.image.image == "docker.io/library/alpine:3.20"

    retry_cfg = runtime._container_config(manifest, "demo-rev1-0", 1, attempt=2)
    assert str(retry_cfg.log_path) == "main/2.log"

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
    assert str(side_cfg.log_path) == "sidecar/0.log"
    assert not str(side_cfg.log_path).startswith("/")
    assert side_cfg.labels.get("ae.container") == "sidecar"
    assert side_cfg.image.image == "docker.io/library/alpine:3.20"

    mounts = list(side_cfg.mounts or [])
    assert any(
        m.host_path == "/tmp/ae-proj/config/db" and m.container_path == "/etc/db" for m in mounts
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
    assert str(getattr(req.config, "log_directory", "")) == runtime._pod_log_dir(
        "default",
        "gpu-demo-rev1-0",
        runtime._pod_uid("gpu-demo-rev1-0", "default"),
    )


def test_run_pod_sets_host_network_namespace_mode(monkeypatch) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "hostnet-demo", "namespace": "default"},
            "spec": {
                "image": "busybox:1.36",
                "replicas": 1,
                "hostNetwork": True,
                "ports": [{"name": "http", "containerPort": 8080}],
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

    runtime._run_pod(manifest, "hostnet-demo-rev1-0", 1)

    req = captured.get("req")
    assert req is not None
    assert int(req.config.linux.security_context.namespace_options.network) == 2
    assert (
        runtime._endpoint_for_manifest(
            manifest,
            None,
            replica_id="hostnet-demo-rev1-0",
        )
        == "127.0.0.1:8080"
    )


def test_cri_container_security_context_maps_profiles_and_existing_fields() -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "secure-demo", "namespace": "default"},
            "spec": {
                "image": "busybox:1.36",
                "replicas": 1,
                "security": {
                    "runAsUser": 1000,
                    "runAsGroup": 2000,
                    "readOnlyRootFilesystem": True,
                    "dropCapabilities": ["NET_RAW", "SYS_PTRACE"],
                    "seccompProfileType": "Localhost",
                    "seccompLocalhostProfile": "profiles/k1s-seccomp.json",
                    "apparmorProfile": "localhost/k1s-secure-demo",
                },
            },
        }
    )

    config = CRIRuntime()._container_config(  # type: ignore[abstract]
        manifest, "secure-demo-rev1-0", 1
    )
    context = config.linux.security_context

    assert context.run_as_user.value == 1000
    assert context.run_as_group.value == 2000
    assert context.readonly_rootfs is True
    assert list(context.capabilities.drop_capabilities) == ["NET_RAW", "SYS_PTRACE"]
    assert context.seccomp.profile_type == context.seccomp.Localhost
    assert context.seccomp.localhost_ref == "profiles/k1s-seccomp.json"
    assert context.apparmor.profile_type == context.apparmor.Localhost
    assert context.apparmor.localhost_ref == "k1s-secure-demo"


@pytest.mark.parametrize(
    ("profile", "expected_type"),
    [
        ("runtime/default", "RuntimeDefault"),
        ("unconfined", "Unconfined"),
    ],
)
def test_cri_container_apparmor_named_profiles(profile: str, expected_type: str) -> None:
    runtime = CRIRuntime()  # type: ignore[abstract]
    context = runtime._build_security_context_from_spec({"apparmorProfile": profile})

    assert context is not None
    assert context.apparmor.profile_type == getattr(context.apparmor, expected_type)
    assert context.apparmor.localhost_ref == ""


@pytest.mark.parametrize("profile_type", ["RuntimeDefault", "Unconfined"])
def test_cri_container_seccomp_named_profiles(profile_type: str) -> None:
    runtime = CRIRuntime()  # type: ignore[abstract]
    context = runtime._build_security_context_from_spec({"seccompProfileType": profile_type})

    assert context is not None
    assert context.seccomp.profile_type == getattr(context.seccomp, profile_type)
    assert context.seccomp.localhost_ref == ""


def test_cri_container_rejects_local_profiles_without_references() -> None:
    runtime = CRIRuntime()  # type: ignore[abstract]

    with pytest.raises(ValueError, match="requires a localhost reference"):
        runtime._build_security_context_from_spec({"seccompProfileType": "Localhost"})
    with pytest.raises(ValueError, match="unsupported AppArmor profile"):
        runtime._build_security_context_from_spec({"apparmorProfile": "localhost/"})


def test_cri_pod_sandbox_maps_fs_group_seccomp_and_selinux_without_host_ns() -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "secure-pod", "namespace": "default"},
            "spec": {
                "image": "busybox:1.36",
                "replicas": 1,
                "podSecurity": {
                    "fsGroup": 3000,
                    "seccompProfileType": "Localhost",
                    "seccompLocalhostProfile": "profiles/pod-seccomp.json",
                    "seLinuxUser": "system_u",
                    "seLinuxRole": "system_r",
                    "seLinuxType": "container_t",
                    "seLinuxLevel": "s0:c1,c2",
                },
            },
        }
    )

    config = CRIRuntime()._pod_sandbox_linux_config(manifest)  # type: ignore[abstract]

    assert config is not None
    context = config.security_context
    assert list(context.supplemental_groups) == [3000]
    assert context.seccomp.profile_type == context.seccomp.Localhost
    assert context.seccomp.localhost_ref == "profiles/pod-seccomp.json"
    assert context.selinux_options.user == "system_u"
    assert context.selinux_options.role == "system_r"
    assert context.selinux_options.type == "container_t"
    assert context.selinux_options.level == "s0:c1,c2"
    assert not context.HasField("namespace_options")


def test_cri_pod_sandbox_preserves_host_namespace_with_security_context() -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "secure-host-pod", "namespace": "default"},
            "spec": {
                "image": "busybox:1.36",
                "replicas": 1,
                "hostNetwork": True,
                "podSecurity": {
                    "fsGroup": 4000,
                    "seccompProfileType": "RuntimeDefault",
                },
            },
        }
    )

    runtime = CRIRuntime()  # type: ignore[abstract]
    config = runtime._pod_sandbox_linux_config(manifest)

    assert config is not None
    context = config.security_context

    assert int(context.namespace_options.network) == 2
    assert list(context.supplemental_groups) == [4000]
    assert context.seccomp.profile_type == context.seccomp.RuntimeDefault
    assert context.seccomp.localhost_ref == ""
