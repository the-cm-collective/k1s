from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _helm_template(chart: Path, values: Path, release: str, namespace: str) -> list[dict]:
    helm = shutil.which("helm")
    assert helm, "helm binary is required for chart contract tests"
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
    assert env["AE_CONTROLLER_URL"] == "http://k1s-dev-a-k1s-core-ha-controller.k1s-dev-a.svc.cluster.local:9110"
    assert env["AE_CRI_ENDPOINT"] == "unix:///run/containerd/containerd.sock"
    assert mounts["containerd-sock"] == "/run/containerd/containerd.sock"
    assert volumes["containerd-sock"] == "/var/snap/microk8s/common/run/containerd.sock"
    assert env["AE_NODE_LABELS"]


def test_node_chart_guard_uses_lookup_and_fail() -> None:
    text = _read_text(ROOT / "ops" / "helm" / "k1s-node-local" / "templates" / "guard.yaml")
    assert 'lookup "apps/v1" "DaemonSet" "" ""' in text
    assert "k1s-node-local is cluster-exclusive" in text
    assert "fail" in text
