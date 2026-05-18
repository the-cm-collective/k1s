from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def _helm_template(chart: Path, values: Path, release: str, namespace: str) -> list[dict]:
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm binary is required for chart contract tests")
    proc = subprocess.run(
        [
            helm,
            "template",
            release,
            str(chart),
            "--namespace",
            namespace,
            "-f",
            str(values),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    docs = []
    for item in yaml.safe_load_all(proc.stdout):
        if item:
            docs.append(item)
    return docs


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_core_chart_renders_expected_resources() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-core-ha"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-core-ha-values.microk8s.yaml"
    docs = _helm_template(chart, values, "k1s-dev-a", "k1s-dev-a")
    kinds = {(item["kind"], item["metadata"]["name"]) for item in docs}
    assert ("Deployment", "k1s-dev-a-k1s-core-ha-controller") in kinds
    assert ("Deployment", "k1s-dev-a-k1s-core-ha-apishim") in kinds
    assert ("StatefulSet", "k1s-dev-a-k1s-core-ha-etcd") in kinds
    assert ("StatefulSet", "k1s-dev-a-k1s-core-ha-nats") in kinds
    assert ("Service", "k1s-dev-a-k1s-core-ha-controller-external") in kinds
    assert ("Service", "k1s-dev-a-k1s-core-ha-nats-leaf") in kinds
    assert ("Service", "k1s-dev-a-k1s-core-ha-rathole") in kinds
    assert ("Service", "k1s-dev-a-k1s-core-ha-controller-metrics") in kinds
    assert ("Service", "k1s-dev-a-k1s-core-ha-etcd-metrics") in kinds
    assert ("Service", "k1s-dev-a-k1s-core-ha-nats-metrics") in kinds
    assert ("Ingress", "k1s-dev-a-k1s-core-ha") in kinds
    assert ("Ingress", "k1s-dev-a-k1s-core-ha-dash") in kinds
    assert ("ServiceMonitor", "k1s-dev-a-k1s-core-ha-controller") in kinds
    assert ("PrometheusRule", "k1s-dev-a-k1s-core-ha") in kinds


def test_core_chart_ingress_uses_apps_dash_and_docs_hosts() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-core-ha"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-core-ha-values.microk8s.yaml"
    docs = _helm_template(chart, values, "k1s-dev-a", "k1s-dev-a")
    ingresses = [item for item in docs if item["kind"] == "Ingress"]
    ingress = next(
        item for item in ingresses if item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha"
    )
    dash_ingress = next(
        item for item in ingresses if item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-dash"
    )
    bootstrap = next(
        item
        for item in docs
        if item["kind"] == "ConfigMap"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-bootstrap"
    )
    hosts = [
        rule["host"] for resource in ingresses for rule in resource["spec"].get("rules", [])
    ]
    assert "*.apps.k1s-dev-a.home.arpa" in hosts
    assert "dash.k1s-dev-a.home.arpa" in hosts
    assert "docs.k1s-dev-a.home.arpa" in hosts
    assert ingress["spec"]["rules"][0]["host"] == "*.apps.k1s-dev-a.home.arpa"
    assert dash_ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/app-root"] == "/dashboard"
    assert bootstrap["data"]["dash_url"] == "http://dash.k1s-dev-a.home.arpa/"
    assert bootstrap["data"]["docs_url"] == "http://docs.k1s-dev-a.home.arpa/"


def test_core_chart_examples_avoid_shellless_runtime_breakage() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-core-ha"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-core-ha-values.microk8s.yaml"
    docs = _helm_template(chart, values, "k1s-dev-a", "k1s-dev-a")

    etcd = next(item for item in docs if item["kind"] == "StatefulSet" and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-etcd")
    etcd_container = etcd["spec"]["template"]["spec"]["containers"][0]
    assert etcd_container["command"] == ["etcd"]

    apishim = next(item for item in docs if item["kind"] == "Deployment" and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-apishim")
    apishim_container = apishim["spec"]["template"]["spec"]["containers"][0]
    assert "exec" in apishim_container["readinessProbe"]
    assert "exec" in apishim_container["livenessProbe"]

    controller = next(item for item in docs if item["kind"] == "Deployment" and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-controller")
    controller_container = controller["spec"]["template"]["spec"]["containers"][0]
    controller_metrics_service = next(
        item
        for item in docs
        if item["kind"] == "Service"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-controller-metrics"
    )
    controller_env = {
        item["name"]: item.get("value")
        for item in controller_container["env"]
        if "name" in item and "value" in item
    }
    controller_mounts = {
        item["name"]: item["mountPath"] for item in controller_container["volumeMounts"]
    }
    controller_volumes = {
        item["name"]: item["hostPath"]["path"]
        for item in controller["spec"]["template"]["spec"]["volumes"]
        if "hostPath" in item
    }
    assert controller_env["AE_RUNTIME_BACKEND"] == "cri"
    assert controller_env["AE_INFRA_BACKEND"] == "cri"
    assert controller_env["AE_CRI_ENDPOINT"] == "unix:///run/containerd/containerd.sock"
    assert controller_mounts["containerd-sock"] == "/run/containerd/containerd.sock"
    assert (
        controller_volumes["containerd-sock"]
        == "/var/snap/microk8s/common/run/containerd.sock"
    )
    assert controller_container["readinessProbe"]["httpGet"]["port"] == "agent"
    assert controller_container["livenessProbe"]["httpGet"]["port"] == "agent"
    assert controller_metrics_service["metadata"]["labels"]["k1s.dev/metrics-target"] == "controller"

    etcd_metrics_service = next(
        item
        for item in docs
        if item["kind"] == "Service"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-etcd-metrics"
    )
    assert etcd_metrics_service["metadata"]["labels"]["k1s.dev/metrics-target"] == "etcd"

    nats = next(item for item in docs if item["kind"] == "StatefulSet" and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-nats")
    nats_exporter = next(
        container for container in nats["spec"]["template"]["spec"]["containers"] if container["name"] == "nats-exporter"
    )
    nats_metrics_service = next(
        item
        for item in docs
        if item["kind"] == "Service"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-nats-metrics"
    )
    nats_service_monitor = next(
        item
        for item in docs
        if item["kind"] == "ServiceMonitor"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-nats"
    )
    assert nats_exporter["image"].endswith("/docker.io/natsio/prometheus-nats-exporter:0.19.2")
    assert "-varz" in nats_exporter["args"]
    assert "-jsz" in nats_exporter["args"]
    assert "all" in nats_exporter["args"]
    assert nats_metrics_service["metadata"]["labels"]["k1s.dev/metrics-target"] == "nats"
    assert nats_service_monitor["spec"]["selector"]["matchLabels"]["k1s.dev/metrics-target"] == "nats"
    assert nats_service_monitor["spec"]["endpoints"][0]["port"] == "metrics"
    rathole = next(
        container
        for container in controller["spec"]["template"]["spec"]["containers"]
        if container["name"] == "edge-proxy-rathole"
    )
    assert rathole["image"].endswith("/k1s/k1s-rathole:dev")


def test_node_chart_renders_daemonset_against_target_release() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-node-local"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-node-local-values.microk8s.yaml"
    docs = _helm_template(chart, values, "k1s-dev-a-node", "k1s-dev-a")
    daemonset = next(item for item in docs if item["kind"] == "DaemonSet")
    container = daemonset["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"] if "value" in item}
    mounts = {item["name"]: item["mountPath"] for item in container["volumeMounts"]}
    volumes = {
        item["name"]: item["hostPath"]["path"]
        for item in daemonset["spec"]["template"]["spec"]["volumes"]
        if "hostPath" in item
    }
    assert daemonset["spec"]["template"]["spec"]["hostNetwork"] is True
    assert daemonset["spec"]["template"]["spec"]["hostPID"] is True
    assert container["args"][1] == "containerd"
    assert env["AE_RUNTIME_BACKEND"] == "containerd"
    assert env["AE_CONTROLLER_URL"] == "http://k1s-dev-a-k1s-core-ha-controller.k1s-dev-a.svc.cluster.local:9110"
    assert env["AE_CRI_ENDPOINT"] == "unix:///var/snap/microk8s/common/run/containerd.sock"
    assert env["AE_CONTAINERD_ADDRESS"] == "unix:///var/snap/microk8s/common/run/containerd.sock"
    assert env["AE_CONTAINERD_NAMESPACE"] == "ae"
    assert env["AE_CONTAINERD_NETWORK"] == "ae-net"
    assert env["AE_CONTAINERD_NETWORK_SUBNET"] == "10.241.0.0/16"
    assert env["AE_CONTAINERD_DATA_ROOT"] == "/var/lib/ae/nerdctl"
    assert env["AE_NERDCTL_BIN"] == "/var/lib/ae/nerdctl-bin/nerdctl"
    assert env["AE_CONTAINERD_CNI_BIN_DIR"] == "/var/lib/ae/cni/bin"
    assert env["AE_CONTAINERD_CNI_BIN_SOURCE_DIR"] == "/opt/cni/bin"
    assert env["AE_CONTAINERD_CNI_CONF_DIR"] == "/var/lib/ae/cni/net.d"
    assert env["AE_CONTAINERD_CNI_CONF_SOURCE_DIR"] == "/etc/cni/net.d"
    assert env["AE_NVIDIA_TOOLKIT_DIR"] == "/usr/local/nvidia/toolkit"
    assert env["AE_NVIDIA_LIBRARY_DIRS"] == "/usr/local/nvidia/toolkit:/var/lib/ae/nvidia-libs"
    assert env["AE_NVIDIA_HOST_LIB_SOURCE_DIR"] == "/host-libs/x86_64-linux-gnu"
    assert env["AE_NVIDIA_LIBRARY_DIR"] == "/var/lib/ae/nvidia-libs"
    assert env["AE_NVIDIA_CONTAINER_CLI_BIN"] == "/usr/local/nvidia/toolkit/nvidia-container-cli"
    assert env["AE_NVIDIA_CONTAINER_RUNTIME_BIN"] == "/usr/local/nvidia/toolkit/nvidia-container-runtime"
    assert env["AE_NVIDIA_RUNTIME_CONFIG_DIR"] == "/etc/nvidia-container-runtime"
    assert env["AE_NVIDIA_SMI_BIN"] == "/var/lib/ae/nvidia-smi"
    assert mounts["containerd-sock"] == "/var/snap/microk8s/common/run"
    assert mounts["containerd-state"] == "/run/containerd"
    assert mounts["containerd-data"] == "/var/snap/microk8s/common/var/lib/containerd"
    assert mounts["cni-bin"] == "/opt/cni/bin"
    assert mounts["cni-conf"] == "/etc/cni/net.d"
    assert mounts["cni-state"] == "/var/lib/cni"
    assert mounts["nvidia-toolkit"] == "/usr/local/nvidia"
    assert mounts["nvidia-runtime-config"] == "/etc/nvidia-container-runtime"
    assert mounts["nvidia-driver-lib"] == "/host-libs/x86_64-linux-gnu"
    assert mounts["nvidia-smi"] == "/var/lib/ae/nvidia-smi"
    assert volumes["containerd-sock"] == "/var/snap/microk8s/common/run"
    assert volumes["containerd-state"] == "/run/containerd"
    assert volumes["containerd-data"] == "/var/snap/microk8s/common/var/lib/containerd"
    assert volumes["cni-bin"] == "/var/snap/microk8s/current/opt/cni/bin"
    assert volumes["cni-conf"] == "/var/snap/microk8s/current/args/cni-network"
    assert volumes["cni-state"] == "/var/snap/microk8s/current/var/lib/cni"
    assert volumes["nvidia-toolkit"] == "/usr/local/nvidia"
    assert volumes["nvidia-runtime-config"] == "/etc/nvidia-container-runtime"
    assert volumes["nvidia-driver-lib"] == "/usr/lib/x86_64-linux-gnu"
    assert volumes["nvidia-smi"] == "/usr/bin/nvidia-smi"
    assert container["securityContext"]["privileged"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is False
    assert container["securityContext"]["seccompProfile"]["type"] == "Unconfined"
    assert env["AE_NODE_LABELS"]


def test_node_chart_guard_uses_lookup_and_fail() -> None:
    text = _read_text(ROOT / "ops" / "helm" / "k1s-node-local" / "templates" / "guard.yaml")
    assert 'lookup "apps/v1" "DaemonSet" "" ""' in text
    assert "k1s-node-local is cluster-exclusive" in text
    assert "fail" in text
