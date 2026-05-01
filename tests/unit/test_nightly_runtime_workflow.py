from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NIGHTLY_RUNTIME = ROOT / ".github" / "workflows" / "nightly-runtime.yml"
K8S_CONFORMANCE = ROOT / "scripts" / "ci" / "k8s-conformance.sh"
K3S_CONFORMANCE = ROOT / "scripts" / "ci" / "k3s-conformance.sh"


def test_nightly_runtime_cri_smoke_preserves_selected_python_under_sudo() -> None:
    text = NIGHTLY_RUNTIME.read_text(encoding="utf-8")

    assert 'sudo -E env "PATH=$PATH" "$(command -v python)" -m pytest \\' in text


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
