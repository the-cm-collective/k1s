from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from ae.controller.spec import AppManifest
from ae.runtime.containerd_runtime import ContainerdRuntime


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
