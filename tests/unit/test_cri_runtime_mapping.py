"""CRI runtime config mapping tests."""

import base64
from pathlib import Path

from ae.controller.spec import AppManifest, VolumeSpec
from ae.apishim.store import ObjectStore
from ae.runtime.cri_runtime import CRIRuntime


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


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
    assert any(
        m.host_path == "/tmp/ae-proj" and m.container_path == "/var/run/ae/config/demo"
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


def test_cri_stop_pod_clears_port_assignments(monkeypatch):
    class _Resp:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    runtime = CRIRuntime()
    runtime._port_assignments["demo-rev1-0"] = {8080: 32000}

    def fake_runtime_call(method, _req):  # noqa: ANN001
        if method == "ListContainers":
            return _Resp(containers=[])
        return _Resp()

    monkeypatch.setattr(runtime, "_runtime_call", fake_runtime_call)

    class Pod:
        id = "pod1"
        labels = {"ae.app": "demo", "ae.pod_name": "demo-rev1-0"}

    runtime._stop_and_remove_pod(None, Pod())

    assert "demo-rev1-0" not in runtime._port_assignments


def test_cri_build_states_includes_sidecars(monkeypatch):
    runtime = CRIRuntime()

    class Pod:
        id = "pod1"
        labels = {"ae.app": "demo", "ae.pod_name": "demo-rev1-0", "ae.revision": "1"}

    class Net:
        ip = "10.1.2.3"

    class PodStatus:
        network = Net()

    class Container:
        def __init__(self, cid, labels):  # noqa: ANN001
            self.id = cid
            self.labels = labels

    class Status:
        def __init__(self, state, exit_code=0):  # noqa: ANN001
            self.state = state
            self.exit_code = exit_code
            self.started_at = None
            self.finished_at = None

    runtime._port_assignments["demo-rev1-0"] = {8080: 32000}
    monkeypatch.setattr(runtime, "_list_pods", lambda *_a, **_k: [Pod()])
    monkeypatch.setattr(runtime, "_pod_labels", lambda *_a, **_k: Pod.labels)
    monkeypatch.setattr(runtime, "_pod_name", lambda *_a, **_k: "demo-rev1-0")
    monkeypatch.setattr(runtime, "_pod_status", lambda *_a, **_k: PodStatus())

    containers = [
        Container(
            "c1",
            {
                "ae.container": "main",
                "ae.revision": "1",
            },
        ),
        Container(
            "c2",
            {
                "ae.container": "sidecar",
                "ae.revision": "1",
            },
        ),
    ]

    class _Resp:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def fake_runtime_call(method, _req):  # noqa: ANN001
        if method == "ListContainers":
            return _Resp(containers=containers)
        return _Resp()

    monkeypatch.setattr(runtime, "_runtime_call", fake_runtime_call)
    monkeypatch.setattr(
        runtime, "_container_status", lambda *_a, **_k: Status(runtime._pb2().ContainerState.CONTAINER_RUNNING)
    )

    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "ports": [{"name": "http", "containerPort": 8080}],
            },
        }
    )
    states = runtime._build_states(manifest, revision=1)
    assert len(states) == 2
    assert sum(1 for s in states if s.endpoint) == 1
    assert all(s.status == "running" for s in states)


def test_cri_list_containers_info_includes_sidecars(monkeypatch):
    runtime = CRIRuntime()

    class Pod:
        id = "pod1"
        labels = {"ae.app": "demo", "ae.pod_name": "demo-rev1-0"}

    class Net:
        ip = "10.1.2.3"

    class PodStatus:
        network = Net()

    class Container:
        def __init__(self, cid, labels):  # noqa: ANN001
            self.id = cid
            self.labels = labels

    class Status:
        def __init__(self, state, restart_count=0):  # noqa: ANN001
            self.state = state
            self.restart_count = restart_count
            self.started_at = None

    containers = [
        Container(
            "c1",
            {
                "ae.container": "main",
                "ae.revision": "1",
            },
        ),
        Container(
            "c2",
            {
                "ae.container": "sidecar",
                "ae.revision": "1",
            },
        ),
    ]

    class _Resp:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def fake_runtime_call(method, _req):  # noqa: ANN001
        if method == "ListContainers":
            return _Resp(containers=containers)
        return _Resp()

    runtime._port_assignments["demo-rev1-0"] = {8080: 32000}
    monkeypatch.setattr(runtime, "_list_pods", lambda *_a, **_k: [Pod()])
    monkeypatch.setattr(runtime, "_pod_labels", lambda *_a, **_k: Pod.labels)
    monkeypatch.setattr(runtime, "_pod_name", lambda *_a, **_k: "demo-rev1-0")
    monkeypatch.setattr(runtime, "_pod_status", lambda *_a, **_k: PodStatus())
    monkeypatch.setattr(runtime, "_runtime_call", fake_runtime_call)
    monkeypatch.setattr(
        runtime,
        "_container_status",
        lambda *_a, **_k: Status(runtime._pb2().ContainerState.CONTAINER_RUNNING, 2),
    )

    info = runtime.list_containers_info()
    assert len(info) == 2
    names = {entry.get("name") for entry in info}
    assert names == {"demo-rev1-0/main", "demo-rev1-0/sidecar"}
    assert all(entry.get("host_ports") == [32000] for entry in info)
    assert all(entry.get("port_map") == {8080: 32000} for entry in info)
    assert all(entry.get("pod_ip") == "10.1.2.3" for entry in info)
    assert all(entry.get("running") for entry in info)


def test_cri_read_logs_for_container(tmp_path, monkeypatch):
    runtime = CRIRuntime()
    log_path = tmp_path / "sidecar.log"
    log_path.write_text("hello\nworld\n", encoding="utf-8")

    class Container:
        def __init__(self, cid, labels):  # noqa: ANN001
            self.id = cid
            self.labels = labels

    class Status:
        def __init__(self, path):  # noqa: ANN001
            self.log_path = str(path)

    containers = [
        Container("c1", {"ae.app": "demo", "ae.container": "sidecar"}),
    ]

    class _Resp:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def fake_runtime_call(method, _req):  # noqa: ANN001
        if method == "ListContainers":
            return _Resp(containers=containers)
        return _Resp()

    monkeypatch.setattr(runtime, "_runtime_call", fake_runtime_call)
    monkeypatch.setattr(runtime, "_container_status", lambda *_a, **_k: Status(log_path))

    lines = list(runtime.read_logs_for_container("demo", "sidecar"))
    assert lines == ["hello", "world"]


def test_cri_injects_pvc_mounts(monkeypatch):
    runtime = CRIRuntime()
    captured: dict[str, AppManifest] = {}

    class StubVolumeManager:
        def inject_pvc_mounts(self, manifest, node_id=None):  # noqa: ANN001
            _ = node_id
            vols = list(getattr(manifest.spec, "volumes", []) or [])
            vols.append(VolumeSpec(host_path="/tmp/netfs", mount_path="/data"))
            updated = manifest.spec.model_copy(update={"volumes": vols})
            return manifest.model_copy(update={"spec": updated})

    def fake_run_pod(manifest, *_a, **_k):  # noqa: ANN001
        captured["manifest"] = manifest

    monkeypatch.setattr(runtime, "_get_volume_manager", lambda: StubVolumeManager())
    monkeypatch.setattr(runtime, "_ensure_clients", lambda: None)
    monkeypatch.setattr(runtime, "_list_pods", lambda *_a, **_k: [])
    monkeypatch.setattr(runtime, "_ensure_image", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_run_pod", fake_run_pod)

    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {"image": "alpine:3.20", "replicas": 1},
        }
    )
    runtime.ensure_app(manifest, revision=1)
    injected = captured.get("manifest")
    assert injected is not None
    vols = list(getattr(injected.spec, "volumes", []) or [])
    assert any(v.host_path == "/tmp/netfs" and v.mount_path == "/data" for v in vols)


def test_cri_env_valuefrom_resolution():
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "demo"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "resources": {
                    "limits": {"cpu": 0.5},
                    "requests": {"memory": "128Mi"},
                },
                "env": [
                    {"name": "APP_NAME", "value": "demo"},
                    {
                        "name": "POD_NAME",
                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                    },
                    {
                        "name": "POD_NAMESPACE",
                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                    },
                    {
                        "name": "CPU_UNITS",
                        "valueFrom": {
                            "resourceFieldRef": {"resource": "limits.cpu", "divisor": "100m"}
                        },
                    },
                    {
                        "name": "MEM_UNITS",
                        "valueFrom": {
                            "resourceFieldRef": {"resource": "requests.memory", "divisor": "1Mi"}
                        },
                    },
                ],
            },
        }
    )
    runtime = CRIRuntime()
    cfg = runtime._container_config(manifest, "demo-rev1-0", 1, attempt=0)
    envs = {e.key: e.value for e in (cfg.envs or [])}
    assert envs["APP_NAME"] == "demo"
    assert envs["POD_NAME"] == "demo"
    assert envs["POD_NAMESPACE"] == "demo"
    assert envs["CPU_UNITS"] == "5"
    assert envs["MEM_UNITS"] == "128"


def test_cri_env_valuefrom_configmap_and_secret(tmp_path, monkeypatch):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "demo",
        "app-config",
        {"name": "app-config", "namespace": "demo"},
        {"MODE": "auto", "LOG_LEVEL": "debug"},
        status={},
    )
    store.upsert(
        "",
        "v1",
        "secrets",
        "demo",
        "app-secret",
        {"name": "app-secret", "namespace": "demo"},
        {"PASSWORD": _b64("s3cr3t")},
        status={},
    )
    monkeypatch.setenv("AE_APISHIM_DB", str(tmp_path / "apishim.db"))

    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "demo"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "env": [
                    {
                        "name": "",
                        "valueFrom": {"configMapKeyRef": {"name": "app-config", "key": ""}},
                    },
                    {
                        "name": "",
                        "valueFrom": {"secretKeyRef": {"name": "app-secret", "key": ""}},
                    },
                    {
                        "name": "LOG_LEVEL",
                        "valueFrom": {
                            "configMapKeyRef": {"name": "app-config", "key": "LOG_LEVEL"}
                        },
                    },
                    {
                        "name": "PASSWORD",
                        "valueFrom": {
                            "secretKeyRef": {"name": "app-secret", "key": "PASSWORD"}
                        },
                    },
                    {"name": "LOG_LEVEL", "value": "info"},
                ],
            },
        }
    )
    runtime = CRIRuntime()
    cfg = runtime._container_config(manifest, "demo-rev1-0", 1, attempt=0)
    envs = {e.key: e.value for e in (cfg.envs or [])}
    assert envs["MODE"] == "auto"
    assert envs["PASSWORD"] == "s3cr3t"
    assert envs["LOG_LEVEL"] == "info"


def test_cri_envfrom_prefix(tmp_path, monkeypatch):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "demo",
        "app-config",
        {"name": "app-config", "namespace": "demo"},
        {"MODE": "auto"},
        status={},
    )
    monkeypatch.setenv("AE_APISHIM_DB", str(tmp_path / "apishim.db"))

    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "demo"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "env": [
                    {
                        "name": "",
                        "valueFrom": {
                            "configMapKeyRef": {
                                "name": "app-config",
                                "key": "",
                                "prefix": "CFG_",
                            }
                        },
                    }
                ],
            },
        }
    )
    runtime = CRIRuntime()
    cfg = runtime._container_config(manifest, "demo-rev1-0", 1, attempt=0)
    envs = {e.key: e.value for e in (cfg.envs or [])}
    assert envs["CFG_MODE"] == "auto"


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


def test_cri_emptydir_calls_tmpfs_helper(monkeypatch, tmp_path):
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
    monkeypatch.setenv("AE_CRI_EMPTYDIR_TMPFS_ROOT", str(tmp_path))
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


def test_cri_init_containers_for_pod_keeps_sandbox(monkeypatch):
    class _Resp:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    runtime = CRIRuntime()
    calls: list[str] = []
    replica_ids: list[str] = []

    def fake_runtime_call(method, req):  # noqa: ANN001
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
    runtime.run_init_containers_for_pod(manifest, "demo-rev1-0", 1)
    assert calls.count("RunPodSandbox") == 1
    assert "StopPodSandbox" not in calls
    assert "RemovePodSandbox" not in calls
    assert set(replica_ids) == {"demo-rev1-0"}


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


def test_cri_container_security_profiles():
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "security": {
                    "seccompProfileType": "RuntimeDefault",
                    "apparmorProfile": "localhost/k1s-profile",
                },
            },
        }
    )
    runtime = CRIRuntime()
    cfg = runtime._container_config(manifest, "demo-rev1-0", 1, attempt=0)
    sec = cfg.linux.security_context
    pb2 = runtime._pb2()
    assert sec.seccomp.profile_type == pb2.SecurityProfile.ProfileType.Value("RuntimeDefault")
    assert sec.apparmor.profile_type == pb2.SecurityProfile.ProfileType.Value("Localhost")
    assert sec.apparmor.localhost_ref == "k1s-profile"


def test_cri_pod_security_context_mapping():
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "podSecurity": {
                    "fsGroup": 1234,
                    "seccompProfileType": "Unconfined",
                    "seLinuxUser": "system_u",
                    "seLinuxRole": "system_r",
                    "seLinuxType": "container_t",
                    "seLinuxLevel": "s0:c123,c456",
                },
            },
        }
    )
    runtime = CRIRuntime()
    ctx = runtime._build_sandbox_security_context(manifest)
    assert ctx is not None
    pb2 = runtime._pb2()
    assert 1234 in list(ctx.supplemental_groups)
    assert ctx.seccomp.profile_type == pb2.SecurityProfile.ProfileType.Value("Unconfined")
    assert ctx.selinux_options.user == "system_u"
    assert ctx.selinux_options.role == "system_r"
    assert ctx.selinux_options.type == "container_t"
    assert ctx.selinux_options.level == "s0:c123,c456"


def test_cri_image_pull_policy_defaults():
    runtime = CRIRuntime()
    assert runtime._resolve_image_pull_policy("nginx") == "Always"
    assert runtime._resolve_image_pull_policy("nginx:latest") == "Always"
    assert runtime._resolve_image_pull_policy("nginx:1.2") == "IfNotPresent"
    assert (
        runtime._resolve_image_pull_policy("ghcr.io/org/app@sha256:deadbeef")
        == "IfNotPresent"
    )


def test_cri_image_pull_policy_override():
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "nginx:1.2",
                "replicas": 1,
                "imagePullPolicy": "Never",
            },
        }
    )
    runtime = CRIRuntime()
    assert (
        runtime._resolve_image_pull_policy("nginx:1.2", manifest=manifest) == "Never"
    )


def test_cri_image_pull_auth_prefers_secrets(monkeypatch):
    runtime = CRIRuntime()
    monkeypatch.setattr(
        runtime._registry,
        "list_registries",
        lambda: {"registry.example.com": {"username": "u", "password": "p"}},
    )
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "registry.example.com/app:1",
                "replicas": 1,
                "imagePullSecrets": ["registry.example.com"],
            },
        }
    )
    auth = runtime._image_pull_auth("registry.example.com/app:1", manifest=manifest)
    assert auth is not None
    assert auth.username == "u"
    assert auth.server_address == "registry.example.com"


def test_cri_image_pull_auth_from_apishim_secret(monkeypatch):
    runtime = CRIRuntime()

    class _State:
        def get_secret(self, _ns, _name):  # noqa: ANN001
            return {
                ".dockerconfigjson": (
                    '{"auths":{"registry.example.com":{"username":"suser","password":"spass"}}}'
                )
            }

    monkeypatch.setattr(runtime, "_get_apishim_state", lambda: _State())
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "registry.example.com/app:1",
                "replicas": 1,
                "imagePullSecrets": ["regcred"],
            },
        }
    )
    auth = runtime._image_pull_auth("registry.example.com/app:1", manifest=manifest)
    assert auth is not None
    assert auth.username == "suser"
    assert auth.password == "spass"


def test_cri_image_pull_auth_from_service_account(monkeypatch):
    runtime = CRIRuntime()

    class _Store:
        def get(self, group, version, resource, namespace, name):  # noqa: ANN001
            if resource == "deployments":
                return type("Obj", (), {"spec": {"template": {"spec": {"serviceAccountName": "sa"}}}})
            if resource == "serviceaccounts":
                return type(
                    "Obj", (), {"spec": {"imagePullSecrets": [{"name": "regcred"}]}}
                )
            return None

    class _State:
        def get_secret(self, _ns, _name):  # noqa: ANN001
            return {
                ".dockerconfigjson": (
                    '{"auths":{"registry.example.com":{"username":"sauser","password":"sapass"}}}'
                )
            }

    monkeypatch.setattr(runtime, "_get_apishim_store", lambda: _Store())
    monkeypatch.setattr(runtime, "_get_apishim_state", lambda: _State())

    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "registry.example.com/app:1",
                "replicas": 1,
            },
        }
    )
    auth = runtime._image_pull_auth("registry.example.com/app:1", manifest=manifest)
    assert auth is not None
    assert auth.username == "sauser"
    assert auth.password == "sapass"


def test_cri_dns_config_mapping():
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "dnsConfig": {
                    "nameservers": ["1.1.1.1"],
                    "searches": ["svc.local"],
                    "options": [{"name": "ndots", "value": "2"}, {"name": "single-request"}],
                },
            },
        }
    )
    runtime = CRIRuntime()
    cfg = runtime._dns_config(manifest)
    assert cfg is not None
    assert list(cfg.servers) == ["1.1.1.1"]
    assert list(cfg.searches) == ["svc.local"]
    assert "ndots:2" in list(cfg.options)
    assert "single-request" in list(cfg.options)


def test_cri_dns_policy_defaults(monkeypatch):
    runtime = CRIRuntime()
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {"image": "alpine:3.20", "replicas": 1},
        }
    )
    monkeypatch.setenv("AE_CRI_CLUSTER_DNS", "10.96.0.10")
    cfg = runtime._dns_config(manifest)
    assert cfg is not None
    assert list(cfg.servers) == ["10.96.0.10"]


def test_cri_dns_policy_none_requires_config():
    runtime = CRIRuntime()
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {"image": "alpine:3.20", "replicas": 1, "dnsPolicy": "None"},
        }
    )
    try:
        runtime._dns_config(manifest)
    except RuntimeError as exc:
        assert "dnsPolicy=None" in str(exc)
    else:  # pragma: no cover - should not happen
        raise AssertionError("expected dnsPolicy=None to require dnsConfig")


def test_cri_hostname_fqdn(monkeypatch):
    runtime = CRIRuntime()
    monkeypatch.setenv("AE_CRI_CLUSTER_DOMAIN", "cluster.local")
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "demo"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "hostname": "web",
                "subdomain": "svcname",
                "setHostnameAsFQDN": True,
            },
        }
    )
    assert (
        runtime._resolve_hostname(manifest, "demo-rev1-0")
        == "web.svcname.demo.svc.cluster.local"
    )


def test_cri_host_aliases_mount(monkeypatch, tmp_path):
    monkeypatch.setenv("AE_CRI_HOSTS_ROOT", str(tmp_path))
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "hostAliases": [{"ip": "10.0.0.10", "hostnames": ["db.local", "cache.local"]}],
            },
        }
    )
    runtime = CRIRuntime()
    mounts = runtime._build_mounts_for_container(manifest, "demo", manifest.spec, "demo-rev1-0")
    host_mount = next((m for m in mounts if m.container_path == "/etc/hosts"), None)
    assert host_mount is not None
    content = Path(str(host_mount.host_path)).read_text()
    assert "10.0.0.10 db.local cache.local" in content
