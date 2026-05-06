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
    assert ("ServiceMonitor", "k1s-dev-a-k1s-core-ha-controller") in kinds
    assert ("PrometheusRule", "k1s-dev-a-k1s-core-ha") in kinds


def test_core_chart_ingress_uses_apps_dash_and_docs_hosts() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-core-ha"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-core-ha-values.microk8s.yaml"
    docs = _helm_template(chart, values, "k1s-dev-a", "k1s-dev-a")
    ingress = next(item for item in docs if item["kind"] == "Ingress")
    hosts = [rule["host"] for rule in ingress["spec"]["rules"]]
    assert "*.apps.k1s-dev-a.home.arpa" in hosts
    assert "dash.k1s-dev-a.home.arpa" in hosts
    assert "docs.k1s-dev-a.home.arpa" in hosts


def test_node_chart_renders_daemonset_against_target_release() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-node-local"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-node-local-values.microk8s.yaml"
    docs = _helm_template(chart, values, "k1s-dev-a-node", "k1s-dev-a")
    daemonset = next(item for item in docs if item["kind"] == "DaemonSet")
    container = daemonset["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"] if "value" in item}
    assert daemonset["spec"]["template"]["spec"]["hostNetwork"] is True
    assert env["AE_CONTROLLER_URL"] == "http://k1s-dev-a-k1s-core-ha-controller.k1s-dev-a.svc.cluster.local:9110"
    assert env["AE_NODE_LABELS"]


def test_node_chart_guard_uses_lookup_and_fail() -> None:
    text = _read_text(ROOT / "ops" / "helm" / "k1s-node-local" / "templates" / "guard.yaml")
    assert 'lookup "apps/v1" "DaemonSet" "" ""' in text
    assert "k1s-node-local is cluster-exclusive" in text
    assert "fail" in text
