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


def test_cri_container_config_maps_empty_dirs(tmp_path, monkeypatch):
    root = tmp_path / "emptydirs"
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "emptyDirs": [{"name": "cache", "mountPath": "/cache"}],
            },
        }
    )
    runtime = CRIRuntime()
    runtime._endpoint = "unix:///run/containerd/containerd.sock"
    runtime._sandbox_image = "registry.k8s.io/pause:3.9"
    monkeypatch.setenv("AE_CRI_EMPTYDIR_ROOT", str(root))
    cfg = runtime._container_config(manifest, "demo-rev1-0", 1, attempt=0)
    mounts = list(cfg.mounts or [])
    host_path = root / "demo" / "demo-rev1-0" / "cache"
    assert any(
        m.host_path == str(host_path) and m.container_path == "/cache" for m in mounts
    )


def test_cri_emptydir_calls_tmpfs_helper(monkeypatch):
    calls = []

    def record(self, host_path, *, medium, size_limit):
        calls.append((str(host_path), medium, size_limit))

    monkeypatch.setattr(CRIRuntime, "_ensure_emptydir_mount", record, raising=True)
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "emptyDirs": [
                    {"name": "cache", "mountPath": "/cache", "medium": "Memory", "sizeLimit": "64Mi"}
                ],
            },
        }
    )
    runtime = CRIRuntime()
    runtime._container_config(manifest, "demo-rev1-0", 1, attempt=0)
    assert calls
    _, medium, size_limit = calls[0]
    assert medium == "Memory"
    assert size_limit == "64Mi"


def test_cri_init_containers_share_sandbox(monkeypatch):
    class _Resp:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    runtime = CRIRuntime()
    calls: list[str] = []
    replica_ids: list[str] = []

    def fake_runtime_call(method, _req):  # noqa: ANN001
        calls.append(method)
        if method == "RunPodSandbox":
            return _Resp(pod_sandbox_id="pod1")
        if method == "CreateContainer":
            return _Resp(container_id=f"c{calls.count('CreateContainer')}")
        return _Resp()

    def fake_wait(_cid, _timeout=None):  # noqa: ANN001
        return 0

    orig_cfg = runtime._container_config_for_spec

    def wrapped_cfg(*args, **kwargs):  # noqa: ANN001
        replica_ids.append(str(kwargs.get("replica_id")))
        return orig_cfg(*args, **kwargs)

    monkeypatch.setattr(runtime, "_runtime_call", fake_runtime_call)
    monkeypatch.setattr(runtime, "_wait_container_exit", fake_wait)
    monkeypatch.setattr(runtime, "_ensure_clients", lambda: None)
    monkeypatch.setattr(runtime, "_ensure_image", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_container_config_for_spec", wrapped_cfg)

    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "initContainers": [
                    {"name": "init-a", "image": "alpine:3.20", "command": ["true"]},
                    {"name": "init-b", "image": "alpine:3.20", "command": ["true"]},
                ],
            },
        }
    )
    runtime.run_init_containers(manifest)
    assert calls.count("RunPodSandbox") == 1
    assert len(set(replica_ids)) == 1


def test_cri_namespace_options_gate(monkeypatch):
    runtime = CRIRuntime()
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "hostNetwork": True,
                "hostPID": True,
                "hostIPC": True,
                "shareProcessNamespace": True,
            },
        }
    )
    monkeypatch.delenv("AE_CRI_ALLOW_HOST_NS", raising=False)
    assert runtime._sandbox_namespace_options(manifest) is None
    monkeypatch.setenv("AE_CRI_ALLOW_HOST_NS", "1")
    opts = runtime._sandbox_namespace_options(manifest)
    assert opts is not None
