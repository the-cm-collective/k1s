from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from ae.controller.spec import AppManifest
from ae.runtime.containerd_runtime import ContainerdRuntime
from ae.runtime.podman_runtime import PodmanRuntime


class _FakeLogProc:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.stdout = iter(lines or ["hello\n"])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _assert_containerd_global_args(argv: list[str]) -> None:
    assert "--address" in argv
    assert "--namespace" in argv
    assert "--data-root" in argv
    assert "--cni-netconfpath" in argv


def _manifest_with_service() -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "store", "namespace": "workerbee-poc"},
            "spec": {
                "image": "localhost/workerbee-poc-store:test",
                "replicas": 1,
                "ports": [{"name": "http", "containerPort": 8080}],
                "service": {"port": 19080, "targetPort": 8080},
                "health": {"readiness": {"httpGet": {"path": "/healthz", "port": 8080}}},
            },
        }
    )


def _container_with_ports() -> dict:
    return {
        "Id": "container-1",
        "Name": "/ae-workerbee-poc-store-rev1-0",
        "Config": {
            "Labels": {
                ContainerdRuntime.APP_LABEL: "workerbee-poc/store",
                ContainerdRuntime.POD_LABEL: "workerbee-poc--store-rev1-0",
                ContainerdRuntime.REVISION_LABEL: "1",
            }
        },
        "State": {"Status": "running"},
        "NetworkSettings": {
            "IPAddress": "10.210.227.3",
            "Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19080"}]},
            "Networks": {"ae-net": {"IPAddress": "10.210.227.3"}},
        },
    }


def test_containerd_runtime_run_ok_injects_global_flags(monkeypatch) -> None:
    runtime = ContainerdRuntime(
        address="unix:///run/test-containerd.sock",
        namespace="ae-test",
        data_root="/var/lib/ae/nerdctl-test",
        cni_path="/opt/cni/bin",
        cni_netconfpath="/etc/cni/net.d",
    )
    captured: dict[str, object] = {}

    def fake_run(argv, check, stdout, stderr, text):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runtime._run_ok([runtime._bin, "ps", "-a"])

    assert captured["argv"] == [
        runtime._bin,
        "--address",
        "unix:///run/test-containerd.sock",
        "--namespace",
        "ae-test",
        "--data-root",
        "/var/lib/ae/nerdctl-test",
        "--cni-path",
        "/opt/cni/bin",
        "--cni-netconfpath",
        "/etc/cni/net.d",
        "ps",
        "-a",
    ]


def test_podman_runtime_cmd_is_identity() -> None:
    runtime = PodmanRuntime()
    cmd = [runtime._bin, "logs", "cid"]

    assert runtime._runtime_cmd(cmd) is cmd


def test_containerd_runtime_read_logs_follow_wraps_runtime_command(monkeypatch) -> None:
    runtime = ContainerdRuntime(
        address="unix:///run/test-containerd.sock",
        namespace="ae-test",
        data_root="/var/lib/ae/nerdctl-test",
        cni_path="/opt/cni/bin",
        cni_netconfpath="/etc/cni/net.d",
    )
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(runtime, "_find_by_label", lambda _key, _value: "cid123")

    def fake_popen(argv, **_kwargs):  # noqa: ANN001
        captured["argv"] = list(argv)
        return _FakeLogProc()

    monkeypatch.setattr("ae.runtime.podman_runtime.subprocess.Popen", fake_popen)

    assert list(runtime.read_logs("workerbee-poc--api-rev1-0", follow=True, tail=5)) == ["hello"]
    argv = captured["argv"]
    _assert_containerd_global_args(argv)
    logs_idx = argv.index("logs")
    assert argv[logs_idx : logs_idx + 4] == ["logs", "--tail", "5", "-f"]
    assert argv[-1] == "cid123"


def test_containerd_runtime_read_logs_for_container_follow_wraps_runtime_command(
    monkeypatch,
) -> None:
    runtime = ContainerdRuntime(
        address="unix:///run/test-containerd.sock",
        namespace="ae-test",
        data_root="/var/lib/ae/nerdctl-test",
        cni_path="/opt/cni/bin",
        cni_netconfpath="/etc/cni/net.d",
    )
    captured: dict[str, list[str]] = {}

    def fake_run_ok(_argv, *, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        return SimpleNamespace(code=0, out="cid456\n", err="")

    def fake_popen(argv, **_kwargs):  # noqa: ANN001
        captured["argv"] = list(argv)
        return _FakeLogProc()

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)
    monkeypatch.setattr("ae.runtime.podman_runtime.subprocess.Popen", fake_popen)

    lines = list(runtime.read_logs_for_container("workerbee-poc--api", "main", follow=True))

    assert lines == ["hello"]
    argv = captured["argv"]
    _assert_containerd_global_args(argv)
    assert argv[-3:] == ["logs", "-f", "cid456"]


def test_containerd_runtime_exec_for_container_wraps_runtime_command(monkeypatch) -> None:
    runtime = ContainerdRuntime(
        address="unix:///run/test-containerd.sock",
        namespace="ae-test",
        data_root="/var/lib/ae/nerdctl-test",
        cni_path="/opt/cni/bin",
        cni_netconfpath="/etc/cni/net.d",
    )
    captured: dict[str, list[str]] = {}

    def fake_run_ok(_argv, *, allow_fail=False):  # noqa: ANN001
        _ = allow_fail
        return SimpleNamespace(code=0, out="cid789\n", err="")

    def fake_run(argv, check, stdout, stderr, text, timeout):  # noqa: ANN001
        _ = (check, stdout, stderr, text, timeout)
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)
    monkeypatch.setattr("ae.runtime.podman_runtime.subprocess.run", fake_run)

    assert runtime.exec_for_container("workerbee-poc--api", "main", ["sh", "-lc", "true"]) == 0
    argv = captured["argv"]
    _assert_containerd_global_args(argv)
    assert argv[-5:] == ["exec", "cid789", "sh", "-lc", "true"]


def test_containerd_runtime_create_container_preserves_hostnet_runtime_and_gpu(monkeypatch) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "gpu-smoke", "namespace": "k1s-dev-a"},
            "spec": {
                "image": "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1",
                "replicas": 1,
                "runtimeClassName": "nvidia",
                "hostNetwork": True,
                "ports": [{"name": "http", "containerPort": 8080}],
                "resources": {
                    "requests": {"memory": "256Mi", "nvidia.com/gpu": 1},
                    "limits": {"memory": "512Mi", "nvidia.com/gpu": 1},
                },
                "command": ["/bin/sh", "-lc"],
                "args": ["nvidia-smi && sleep 60"],
            },
        }
    )
    monkeypatch.setenv(
        "AE_NVIDIA_CONTAINER_RUNTIME_BIN", "/usr/local/nvidia/toolkit/nvidia-container-runtime"
    )
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[list[str]] = []

    def fake_run_ok(argv, *, allow_fail=False):
        calls.append(list(argv))
        return SimpleNamespace(code=0, out="", err="")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)
    monkeypatch.setattr(runtime, "_container_exists", lambda _name: False)

    runtime._create_container(manifest, "k1s-dev-a--gpu-smoke-rev1-0", 1, node_id="c3rb3rus")

    run_argv = calls[-1]
    assert run_argv[0:3] == [runtime._bin, "run", "-d"]
    assert "--net" in run_argv
    assert run_argv[run_argv.index("--net") + 1] == "host"
    assert "--runtime" in run_argv
    assert (
        run_argv[run_argv.index("--runtime") + 1]
        == "/usr/local/nvidia/toolkit/nvidia-container-runtime"
    )
    assert "--gpus" in run_argv
    assert run_argv[run_argv.index("--gpus") + 1] == "all"
    assert "--restart" in run_argv
    assert run_argv[run_argv.index("--restart") + 1] == "always"
    assert "-p" not in run_argv


def test_containerd_runtime_job_omits_restart_flag(monkeypatch) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "gpu-job", "namespace": "k1s-dev-a"},
            "spec": {
                "image": "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1",
                "replicas": 1,
                "workload": "job",
                "command": ["/bin/sh", "-lc"],
                "args": ["echo ok"],
            },
        }
    )
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[list[str]] = []

    def fake_run_ok(argv, *, allow_fail=False):
        calls.append(list(argv))
        return SimpleNamespace(code=0, out="", err="")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)
    monkeypatch.setattr(runtime, "_container_exists", lambda _name: False)

    runtime._create_container(manifest, "k1s-dev-a--gpu-job-rev1-0", 1, node_id="c3rb3rus")

    run_argv = calls[-1]
    assert "--restart" not in run_argv


def test_containerd_runtime_create_container_uses_configured_network_for_non_hostnet(
    monkeypatch,
) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "http-smoke", "namespace": "k1s-dev-a"},
            "spec": {
                "image": "docker.io/library/busybox:latest",
                "replicas": 1,
                "command": ["/bin/sh", "-lc"],
                "args": ["echo ok && sleep 60"],
            },
        }
    )
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[list[str]] = []

    def fake_run_ok(argv, *, allow_fail=False):
        calls.append(list(argv))
        return SimpleNamespace(code=0, out="", err="")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)
    monkeypatch.setattr(runtime, "_container_exists", lambda _name: False)

    runtime._create_container(manifest, "k1s-dev-a--http-smoke-rev1-0", 1, node_id="c3rb3rus")

    run_argv = calls[-1]
    assert "--net" in run_argv
    assert run_argv[run_argv.index("--net") + 1] == "ae-net"


def test_containerd_runtime_sanitizes_namespaced_runtime_object_names(monkeypatch) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "store", "namespace": "workerbee-poc"},
            "spec": {
                "image": "localhost/workerbee-poc-store:test",
                "replicas": 1,
                "command": ["/bin/sh", "-lc"],
                "args": ["sleep 60"],
            },
        }
    )
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[list[str]] = []

    def fake_run_ok(argv, *, allow_fail=False):
        _ = allow_fail
        calls.append(list(argv))
        return SimpleNamespace(code=0, out="", err="")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)
    monkeypatch.setattr(runtime, "_container_exists", lambda _name: False)

    runtime._create_container(manifest, "workerbee-poc--store-rev1-0", 1)

    run_argv = calls[-1]
    container_name = run_argv[run_argv.index("--name") + 1]
    assert container_name.startswith("ae-workerbee-poc-store-rev1-0-")
    assert "--" not in container_name
    assert ".." not in container_name
    assert runtime._storage_volume_name("api", "data") == "ae-api-data"
    assert runtime._storage_volume_name("workerbee-poc--store", "data").startswith(
        "ae-workerbee-poc-store-data-"
    )


def test_containerd_runtime_uses_localhost_image_ref_when_only_that_exists(monkeypatch) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "workerbee-poc"},
            "spec": {
                "image": "workerbee-poc-api:test",
                "replicas": 1,
                "command": ["/bin/sh", "-lc"],
                "args": ["sleep 60"],
            },
        }
    )
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[list[str]] = []

    def fake_run_ok(argv, *, allow_fail=False):
        _ = allow_fail
        calls.append(list(argv))
        if argv[1:3] == ["image", "inspect"]:
            image = argv[-1]
            code = 0 if image == "localhost/workerbee-poc-api:test" else 1
            return SimpleNamespace(code=code, out="", err="")
        return SimpleNamespace(code=0, out="", err="")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)
    monkeypatch.setattr(runtime, "_container_exists", lambda _name: False)

    runtime._create_container(manifest, "workerbee-poc--api-rev1-0", 1)

    run_argv = calls[-1]
    assert "localhost/workerbee-poc-api:test" in run_argv
    assert "workerbee-poc-api:test" not in run_argv


def test_containerd_runtime_non_hostnet_declared_ports_do_not_publish_all(monkeypatch) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "bridge-http", "namespace": "k1s-dev-a"},
            "spec": {
                "image": "docker.io/library/busybox:latest",
                "replicas": 1,
                "ports": [{"name": "http", "containerPort": 8080}],
                "command": ["/bin/sh", "-lc"],
                "args": ["httpd -f -p 8080"],
            },
        }
    )
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[list[str]] = []

    def fake_run_ok(argv, *, allow_fail=False):
        calls.append(list(argv))
        return SimpleNamespace(code=0, out="", err="")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)
    monkeypatch.setattr(runtime, "_container_exists", lambda _name: False)

    runtime._create_container(manifest, "k1s-dev-a--bridge-http-rev1-0", 1, node_id="c3rb3rus")

    run_argv = calls[-1]
    assert "--net" in run_argv
    assert run_argv[run_argv.index("--net") + 1] == "ae-net"
    assert "-P" not in run_argv
    assert "-p" not in run_argv


def test_containerd_runtime_prefers_direct_endpoint_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AE_CONTAINERD_ENDPOINT_PREFER_DIRECT", raising=False)
    runtime = ContainerdRuntime(namespace="ae-test")

    endpoint = runtime._endpoint_from_container(
        _manifest_with_service(),
        _container_with_ports(),
        preferred=8080,
    )

    assert endpoint == "10.210.227.3:8080"


def test_containerd_runtime_can_prefer_published_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("AE_CONTAINERD_ENDPOINT_PREFER_DIRECT", "false")
    runtime = ContainerdRuntime(namespace="ae-test")

    endpoint = runtime._endpoint_from_container(
        _manifest_with_service(),
        _container_with_ports(),
        preferred=8080,
    )

    assert endpoint == "127.0.0.1:19080"


def test_containerd_runtime_explicit_true_prefers_direct_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("AE_CONTAINERD_ENDPOINT_PREFER_DIRECT", "on")
    runtime = ContainerdRuntime(namespace="ae-test")

    endpoint = runtime._endpoint_from_container(
        _manifest_with_service(),
        _container_with_ports(),
        preferred=8080,
    )

    assert endpoint == "10.210.227.3:8080"


def test_containerd_runtime_ensure_network_uses_configured_subnet(monkeypatch) -> None:
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[tuple[list[str], bool]] = []

    monkeypatch.setenv("AE_CONTAINERD_NETWORK_SUBNET", "10.241.0.0/16")

    def fake_run_ok(argv, *, allow_fail=False):
        calls.append((list(argv), allow_fail))
        if argv[1:3] == ["network", "inspect"]:
            return SimpleNamespace(code=1, out="", err="missing")
        return SimpleNamespace(code=0, out="", err="")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)

    runtime._ensure_network()

    assert calls == [
        ([runtime._bin, "network", "inspect", "ae-net"], True),
        ([runtime._bin, "network", "create", "--subnet", "10.241.0.0/16", "ae-net"], False),
    ]


def test_containerd_runtime_volume_create_handles_stale_existing_path(monkeypatch) -> None:
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[list[str]] = []

    def fake_run_ok(argv, *, allow_fail=False):
        _ = allow_fail
        calls.append(list(argv))
        if argv[1:3] == ["volume", "create"]:
            return SimpleNamespace(code=1, out="", err="failed to create volume: file exists")
        return SimpleNamespace(code=1, out="", err="missing")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)

    runtime.ensure_storage_volumes("workerbee-poc--store", [{"name": "data"}])

    assert any(call[1:3] == ["volume", "create"] for call in calls)


def test_containerd_runtime_volume_exists_parses_ndjson(monkeypatch) -> None:
    runtime = ContainerdRuntime(namespace="ae-test")
    volume = runtime._storage_volume_name("workerbee-poc--store", "data")

    def fake_run_ok(argv, *, allow_fail=False):
        _ = allow_fail
        if argv[1:3] == ["volume", "inspect"]:
            return SimpleNamespace(code=1, out="", err="missing")
        if argv[1:3] == ["volume", "ls"]:
            return SimpleNamespace(code=0, out=f'{{"Name":"{volume}"}}\n', err="")
        return SimpleNamespace(code=1, out="", err="unexpected")

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)

    assert runtime._volume_exists(volume)


def test_containerd_runtime_validates_exactly_one_gpu_request() -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "gpu-smoke", "namespace": "k1s-dev-a"},
            "spec": {
                "image": "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1",
                "replicas": 1,
                "runtimeClassName": "nvidia",
                "resources": {
                    "requests": {"nvidia.com/gpu": 1},
                    "limits": {"nvidia.com/gpu": 1},
                },
            },
        }
    )
    runtime = ContainerdRuntime(namespace="ae-test")

    assert runtime._validated_gpu_request_count(manifest) == 1


@pytest.mark.parametrize(
    ("spec", "pattern"),
    [
        (
            {
                "runtimeClassName": "nvidia",
                "resources": {"requests": {"nvidia.com/gpu": 1}},
            },
            "both requests and limits",
        ),
        (
            {
                "runtimeClassName": "nvidia",
                "resources": {
                    "requests": {"nvidia.com/gpu": 1},
                    "limits": {"nvidia.com/gpu": 2},
                },
            },
            "matching requests/limits",
        ),
        (
            {
                "runtimeClassName": "nvidia",
                "resources": {
                    "requests": {"nvidia.com/gpu": 2},
                    "limits": {"nvidia.com/gpu": 2},
                },
            },
            "exactly nvidia.com/gpu=1",
        ),
        (
            {
                "resources": {
                    "requests": {"nvidia.com/gpu": 1},
                    "limits": {"nvidia.com/gpu": 1},
                },
            },
            "requires runtimeClassName=nvidia",
        ),
        (
            {
                "runtimeClassName": "nvidia",
            },
            "requires matching requests/limits",
        ),
    ],
)
def test_containerd_runtime_rejects_invalid_gpu_specs(spec, pattern) -> None:
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "gpu-smoke", "namespace": "k1s-dev-a"},
            "spec": {
                "image": "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1",
                "replicas": 1,
                **spec,
            },
        }
    )
    runtime = ContainerdRuntime(namespace="ae-test")

    with pytest.raises(RuntimeError, match=pattern):
        runtime._validated_gpu_request_count(manifest)


def test_containerd_runtime_gpu_preflight_checks_host_tools(monkeypatch) -> None:
    runtime = ContainerdRuntime(namespace="ae-test")
    calls: list[list[str]] = []

    monkeypatch.setenv("AE_NVIDIA_TOOLKIT_DIR", "/usr/local/nvidia/toolkit")
    monkeypatch.setenv("AE_NVIDIA_CONTAINER_CLI_BIN", "/usr/local/nvidia/toolkit/nvidia-container-cli")
    monkeypatch.setenv("AE_NVIDIA_CONTAINER_RUNTIME_BIN", "/usr/local/nvidia/toolkit/nvidia-container-runtime")
    monkeypatch.setenv("AE_NVIDIA_RUNTIME_CONFIG_DIR", "/etc/nvidia-container-runtime")
    monkeypatch.setenv("AE_NVIDIA_SMI_BIN", "/usr/local/nvidia/toolkit/nvidia-smi")
    monkeypatch.setattr(
        "ae.runtime.containerd_runtime.os.path.isdir",
        lambda path: path in {"/usr/local/nvidia/toolkit", "/etc/nvidia-container-runtime"},
    )

    def fake_access(path, mode):  # noqa: ANN001
        return str(path).startswith("/usr/local/nvidia/toolkit/")

    def fake_run(argv, check, stdout, stderr, text):  # noqa: ANN001
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("ae.runtime.containerd_runtime.os.access", fake_access)
    monkeypatch.setattr(subprocess, "run", fake_run)

    runtime._ensure_gpu_runtime_ready()

    assert os.environ["PATH"].startswith("/usr/local/nvidia/toolkit:")
    assert os.environ["LD_LIBRARY_PATH"].startswith("/usr/local/nvidia/toolkit")
    assert calls == [
        ["/usr/local/nvidia/toolkit/nvidia-container-cli", "--version"],
        ["/usr/local/nvidia/toolkit/nvidia-container-runtime", "--version"],
        ["/usr/local/nvidia/toolkit/nvidia-smi", "-L"],
    ]
