from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _helm_template(
    chart: Path,
    values: Path,
    release: str,
    namespace: str,
    extra_args: list[str] | None = None,
) -> list[dict]:
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm binary is required for chart contract tests")
    cmd = [
        helm,
        "template",
        release,
        str(chart),
        "--namespace",
        namespace,
        "-f",
        str(values),
    ]
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(  # noqa: S603 - helm path is resolved by shutil.which; args are fixed.
        cmd,
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
    assert ("Certificate", "k1s-dev-a-ingress-tls") in kinds
    assert ("ServiceMonitor", "k1s-dev-a-k1s-core-ha-controller") in kinds
    assert ("PrometheusRule", "k1s-dev-a-k1s-core-ha") in kinds


def test_core_chart_controller_etcd_retention_env_is_operator_safe() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-core-ha"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-core-ha-values.microk8s.yaml"
    docs = _helm_template(
        chart,
        values,
        "k1s-dev-a",
        "k1s-dev-a",
        extra_args=[
            "--set",
            "controller.etcdMaintenance.quotaBackendBytes=2.147483648e+09",
        ],
    )
    deployment = next(
        item
        for item in docs
        if item["kind"] == "Deployment"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-controller"
    )
    controller = next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == "controller"
    )
    env = {item["name"]: item.get("value") for item in controller["env"] if "value" in item}

    assert env["AE_ETCD_QUOTA_BACKEND_BYTES"] == "2147483648"
    assert env["AE_ETCD_EVENT_RETENTION_PRUNE_ENABLE"] == "1"
    assert env["AE_ETCD_EVENT_RETENTION_PRUNE_BATCH"] == "50000"
    assert env["AE_ETCD_EVENT_RETENTION_PRUNE_MAX_BATCHES"] == "100"
    assert env["AE_ETCD_EVENT_COALESCE_WINDOW_SEC"] == "300"


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
    hosts = [rule["host"] for resource in ingresses for rule in resource["spec"].get("rules", [])]
    assert "*.apps.k1s-dev-a.core.home.arpa" in hosts
    assert "api.k1s-dev-a.core.home.arpa" in hosts
    assert "dash.k1s-dev-a.core.home.arpa" in hosts
    assert "docs.k1s-dev-a.core.home.arpa" in hosts
    assert ingress["spec"]["rules"][0]["host"] == "*.apps.k1s-dev-a.core.home.arpa"
    api_rule = next(
        rule for rule in ingress["spec"]["rules"] if rule["host"] == "api.k1s-dev-a.core.home.arpa"
    )
    api_backend = api_rule["http"]["paths"][0]["backend"]["service"]
    assert api_backend["name"] == "k1s-dev-a-k1s-core-ha-apishim"
    assert api_backend["port"]["name"] == "http"
    assert ingress["spec"]["tls"][0]["secretName"] == "k1s-dev-a-ingress-tls"
    assert dash_ingress["spec"]["tls"][0]["secretName"] == "k1s-dev-a-ingress-tls"
    assert (
        dash_ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/app-root"]
        == "/dashboard"
    )
    assert bootstrap["data"]["dash_url"] == "https://dash.k1s-dev-a.core.home.arpa/"
    assert bootstrap["data"]["docs_url"] == "https://docs.k1s-dev-a.core.home.arpa/"
    assert bootstrap["data"]["api_url"] == "https://api.k1s-dev-a.core.home.arpa/"

    certificate = next(
        item
        for item in docs
        if item["kind"] == "Certificate" and item["metadata"]["name"] == "k1s-dev-a-ingress-tls"
    )
    assert certificate["spec"]["secretName"] == "k1s-dev-a-ingress-tls"
    assert certificate["spec"]["issuerRef"]["name"] == "org-ca"
    assert certificate["spec"]["issuerRef"]["kind"] == "ClusterIssuer"
    assert set(certificate["spec"]["dnsNames"]) == {
        "*.apps.k1s-dev-a.core.home.arpa",
        "api.k1s-dev-a.core.home.arpa",
        "dash.k1s-dev-a.core.home.arpa",
        "docs.k1s-dev-a.core.home.arpa",
    }


def test_core_chart_examples_avoid_shellless_runtime_breakage() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-core-ha"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-core-ha-values.microk8s.yaml"
    docs = _helm_template(chart, values, "k1s-dev-a", "k1s-dev-a")

    etcd = next(
        item
        for item in docs
        if item["kind"] == "StatefulSet"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-etcd"
    )
    etcd_container = etcd["spec"]["template"]["spec"]["containers"][0]
    assert etcd_container["command"] == ["etcd"]

    apishim = next(
        item
        for item in docs
        if item["kind"] == "Deployment"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-apishim"
    )
    apishim_container = apishim["spec"]["template"]["spec"]["containers"][0]
    assert "exec" in apishim_container["readinessProbe"]
    assert "exec" in apishim_container["livenessProbe"]

    controller = next(
        item
        for item in docs
        if item["kind"] == "Deployment"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-controller"
    )
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
    controller_secret_env = {
        item["name"]: item["valueFrom"]["secretKeyRef"]["key"]
        for item in controller_container["env"]
        if "name" in item and "valueFrom" in item and "secretKeyRef" in item["valueFrom"]
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
    assert controller_env["AE_CADDY_SITES"] == ""
    assert controller_env["AE_APISHIM_DB"] == "/var/lib/ae/apishim.db"
    assert controller_env["AE_APISHIM_PUBLIC_BASE"] == "https://api.k1s-dev-a.core.home.arpa"
    assert controller_env["AE_ROUTE_BUNDLE_ENABLED"] == "1"
    assert controller_env["AE_ROUTE_BUNDLE_INTERVAL_S"] == "5"
    assert controller_env["AE_ROUTE_BUNDLE_REPLAY_INTERVAL_S"] == "30"
    assert controller_env["AE_SITE_IDS"] == "host-a,host-b,workerbee-edge"
    assert (
        controller_secret_env["AE_APISHIM_SESSION_SECRET"] == "apishim-session-secret"  # noqa: S105
    )
    assert (
        controller_secret_env["AE_DASHBOARD_BOOTSTRAP_TOKEN"] == "apishim-admin-token"  # noqa: S105
    )
    assert controller_mounts["containerd-sock"] == "/run/containerd/containerd.sock"
    assert controller_volumes["containerd-sock"] == "/var/snap/microk8s/common/run/containerd.sock"
    assert controller_container["readinessProbe"]["httpGet"]["port"] == "agent"
    assert controller_container["livenessProbe"]["httpGet"]["port"] == "agent"
    assert controller_container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "runAsGroup": 1005,
        "runAsNonRoot": True,
        "runAsUser": 10001,
    }
    assert (
        controller_metrics_service["metadata"]["labels"]["k1s.dev/metrics-target"] == "controller"
    )

    etcd_metrics_service = next(
        item
        for item in docs
        if item["kind"] == "Service"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-etcd-metrics"
    )
    assert etcd_metrics_service["metadata"]["labels"]["k1s.dev/metrics-target"] == "etcd"

    nats = next(
        item
        for item in docs
        if item["kind"] == "StatefulSet"
        and item["metadata"]["name"] == "k1s-dev-a-k1s-core-ha-nats"
    )
    for statefulset, component in ((etcd, "etcd"), (nats, "nats")):
        claim_template = statefulset["spec"]["volumeClaimTemplates"][0]
        claim_labels = claim_template["metadata"]["labels"]
        assert claim_labels["app.kubernetes.io/component"] == component
        assert claim_labels["app.kubernetes.io/name"] == "k1s-core-ha"
        assert claim_labels["app.kubernetes.io/instance"] == "k1s-dev-a"
        assert "helm.sh/chart" not in claim_labels
        assert "app.kubernetes.io/version" not in claim_labels
        assert claim_template["spec"]["storageClassName"] == "microk8s-hostpath"
        assert claim_template["spec"]["volumeMode"] == "Filesystem"

    nats_exporter = next(
        container
        for container in nats["spec"]["template"]["spec"]["containers"]
        if container["name"] == "nats-exporter"
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
    assert (
        nats_service_monitor["spec"]["selector"]["matchLabels"]["k1s.dev/metrics-target"] == "nats"
    )
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
    assert (
        env["AE_CONTROLLER_URL"]
        == "http://k1s-dev-a-k1s-core-ha-controller.k1s-dev-a.svc.cluster.local:9110"
    )
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
    assert (
        env["AE_NVIDIA_CONTAINER_RUNTIME_BIN"]
        == "/usr/local/nvidia/toolkit/nvidia-container-runtime"
    )
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


def test_edge_gateway_chart_renders_direct_hub_stack() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-edge-gateway"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-edge-gateway-values.microk8s.yaml"
    docs = _helm_template(chart, values, "k1s-dev-a-edge", "k1s-dev-a")
    kinds = {(item["kind"], item["metadata"]["name"]) for item in docs}
    assert ("Deployment", "k1s-dev-a-edge-k1s-edge-gateway") in kinds
    assert ("PersistentVolumeClaim", "k1s-dev-a-edge-k1s-edge-gateway-state") in kinds

    deployment = next(item for item in docs if item["kind"] == "Deployment")
    containers = {
        item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"]
    }
    assert set(containers) == {"gateway", "edge-caddy", "rathole-client"}

    gateway_env = {
        item["name"]: item.get("value") for item in containers["gateway"]["env"] if "value" in item
    }
    gateway_secret_env = {
        item["name"]: item["valueFrom"]["secretKeyRef"]
        for item in containers["gateway"]["env"]
        if "valueFrom" in item
    }
    assert containers["gateway"]["image"].endswith("/k1s/k1s-edge-core:dev")
    assert gateway_env["K1S_EDGE_TRANSPORT_MODE"] == "directHub"
    assert gateway_env["K1S_NATS_HOST"] == (
        "k1s-dev-a-k1s-core-ha-nats.k1s-dev-a.svc.cluster.local"
    )
    assert gateway_env["AE_TRANSPORT_BACKEND"] == "nats-js"
    assert gateway_env["AE_JS_DOMAIN"] == "K1S"
    assert gateway_env["AE_SITE_ID"] == "host-b"
    assert gateway_env["AE_NODE_ID"] == "edge-gateway-1"
    assert gateway_env["AE_EDGE_LOCAL_INGRESS_SCHEME"] == "http"
    assert gateway_env["AE_EDGE_LOCAL_INGRESS_LISTEN_PORT"] == "18081"
    assert gateway_env["AE_EDGE_LOCAL_UPSTREAM_MODE"] == "bundle-endpoints"
    assert gateway_secret_env["NATS_USER"]["name"] == "k1s-dev-a-k1s-core-ha-auth"
    assert gateway_secret_env["NATS_USER"]["key"] == "nats-controller-user"
    assert gateway_secret_env["NATS_PASSWORD"]["key"] == "nats-controller-password"

    caddy = containers["edge-caddy"]
    rathole = containers["rathole-client"]
    assert caddy["image"].endswith("/docker.io/library/caddy:2.8")
    assert caddy["ports"][0]["containerPort"] == 18081
    assert rathole["image"].endswith("/k1s/k1s-rathole:dev")
    rathole_env = {item["name"]: item.get("value") for item in rathole["env"] if "value" in item}
    assert rathole_env["AE_RATHOLE_SERVER_ADDR"] == (
        "k1s-dev-a-k1s-core-ha-rathole.k1s-dev-a.svc.cluster.local:2333"
    )
    assert rathole_env["AE_EDGE_INGRESS_LOCAL_ADDR"] == "127.0.0.1:18081"
    assert rathole_env["K1S_RATHOLE_CONNECT_ALL_CONTROLLER_PODS"] == "1"
    assert rathole_env["K1S_RATHOLE_DISCOVERY_HOST"] == (
        "k1s-dev-a-k1s-core-ha-controller-headless.k1s-dev-a.svc.cluster.local"
    )
    assert rathole_env["K1S_RATHOLE_DISCOVERY_PORT"] == "2333"
    assert "getent hosts" in "\n".join(rathole["command"])


def test_edge_gateway_chart_renders_edge_nats_leaf_mode() -> None:
    chart = ROOT / "ops" / "helm" / "k1s-edge-gateway"
    values = ROOT / "ops" / "helm" / "examples" / "k1s-edge-gateway-values.microk8s.yaml"
    docs = _helm_template(
        chart,
        values,
        "k1s-dev-a-edge",
        "k1s-dev-a",
        extra_args=["--set", "transport.mode=edgeNatsLeaf"],
    )
    deployment = next(item for item in docs if item["kind"] == "Deployment")
    containers = {
        item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"]
    }
    assert set(containers) == {"edge-nats", "gateway", "edge-caddy", "rathole-client"}
    edge_nats_env = {
        item["name"]: item.get("value")
        for item in containers["edge-nats"]["env"]
        if "value" in item
    }
    assert edge_nats_env["K1S_NATS_LEAF_HOST"] == (
        "k1s-dev-a-k1s-core-ha-nats-leaf.k1s-dev-a.svc.cluster.local"
    )
    assert edge_nats_env["K1S_NATS_LEAF_PORT"] == "7422"
    gateway_env = {
        item["name"]: item.get("value") for item in containers["gateway"]["env"] if "value" in item
    }
    assert gateway_env["K1S_EDGE_TRANSPORT_MODE"] == "edgeNatsLeaf"


def test_node_chart_guard_uses_lookup_and_fail() -> None:
    text = _read_text(ROOT / "ops" / "helm" / "k1s-node-local" / "templates" / "guard.yaml")
    assert 'lookup "apps/v1" "DaemonSet" "" ""' in text
    assert "k1s-node-local is cluster-exclusive" in text
    assert "fail" in text
