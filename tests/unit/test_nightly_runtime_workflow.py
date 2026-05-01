from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NIGHTLY_RUNTIME = ROOT / ".github" / "workflows" / "nightly-runtime.yml"
K8S_CONFORMANCE = ROOT / "scripts" / "ci" / "k8s-conformance.sh"
K3S_CONFORMANCE = ROOT / "scripts" / "ci" / "k3s-conformance.sh"
CI_LIB = ROOT / "scripts" / "ci" / "lib.sh"
CORE_EDGE = ROOT / "tests" / "e2e" / "core_edge.py"


def test_nightly_runtime_cri_smoke_preserves_selected_python_under_sudo() -> None:
    text = NIGHTLY_RUNTIME.read_text(encoding="utf-8")

    assert 'sudo -E env "PATH=$PATH" "$(command -v python)" -m pytest \\' in text


def test_podman_job_targets_cli_tooling_compat_not_strict_policy() -> None:
    text = NIGHTLY_RUNTIME.read_text(encoding="utf-8")
    podman_block = text.split("  podman-compat:\n", 1)[1].split("\n  cri-smoke:\n", 1)[0]

    assert "Podman CLI tooling compatibility checks" in podman_block
    assert "podman info >/dev/null 2>&1" in podman_block
    assert (
        "python -m ae.cli k8s-check -f specs/examples/echo.yaml --policy baseline --summary >/dev/null"
        in podman_block
    )
    assert "python -m ae.cli k8s-check -f specs/examples/echo.yaml --policy strict" not in podman_block


def test_nightly_runtime_svc_portforward_disables_ingress_apply_validation() -> None:
    text = NIGHTLY_RUNTIME.read_text(encoding="utf-8")

    assert '$K --validate=false apply -f /tmp/echo-svc.yaml' in text
    assert '$K --validate=false apply -f /tmp/echo-ing.yaml' in text


def test_kind_conformance_creates_demo_namespace_before_dry_run() -> None:
    text = K8S_CONFORMANCE.read_text(encoding="utf-8")

    assert 'echo "Ensuring demo namespace exists..."' in text
    assert (
        '${KUBECTL_BIN} create namespace demo --dry-run=client -o yaml | '
        '${KUBECTL_BIN} apply --validate=false -f -'
    ) in text


def test_k3s_conformance_creates_demo_namespace_before_dry_run() -> None:
    text = K3S_CONFORMANCE.read_text(encoding="utf-8")

    assert 'echo "Ensuring demo namespace exists..."' in text
    assert (
        'kubectl create namespace demo --dry-run=client -o yaml | '
        'kubectl apply --validate=false -f -'
    ) in text


def test_ci_lib_supports_remote_docker_hosts_and_insecure_tls_toggle() -> None:
    text = CI_LIB.read_text(encoding="utf-8")

    assert "ci_disable_docker_tls_verify()" in text
    assert "ci_docker_published_host()" in text
    assert "ci_append_no_proxy()" in text
    assert 'docker-py treats unset/empty DOCKER_TLS_VERIFY as verify=false.' in text


def test_kind_conformance_rewrites_kubeconfig_for_remote_docker_topology() -> None:
    text = K8S_CONFORMANCE.read_text(encoding="utf-8")

    assert '${KIND_BIN} get kubeconfig --name "${CLUSTER_NAME}" >"${KUBECONFIG_PATH}"' in text
    assert 'REACHABLE_HOST="${K1S_DOCKER_PUBLISHED_HOST:-$(ci_docker_published_host)}"' in text
    assert 'cluster["insecure-skip-tls-verify"] = True' in text


def test_nightly_runtime_exports_reachable_host_for_kind_and_core_edge() -> None:
    text = NIGHTLY_RUNTIME.read_text(encoding="utf-8")

    assert 'ci_disable_docker_tls_verify' in text
    assert 'ci_export_env K1S_DOCKER_PUBLISHED_HOST "$(ci_docker_published_host)"' in text
    assert 'ci_append_no_proxy "${K1S_DOCKER_PUBLISHED_HOST}"' in text


def test_core_edge_e2e_uses_configurable_docker_published_host() -> None:
    text = CORE_EDGE.read_text(encoding="utf-8")

    assert 'def _docker_published_host() -> str:' in text
    assert 'published_host = _docker_published_host()' in text
    assert 'etcd_url = f"http://{published_host}:2379"' in text
    assert 'hub_nats_url = f"nats://hub-controller:dev@{published_host}:4222"' in text
