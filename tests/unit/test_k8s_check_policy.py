from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.check import k8s_portability_issues, apply_policy
from ae.cli.__main__ import main


def test_policy_escalation_to_strict() -> None:
    # echo manifest lacks requests and PDB (replicas=1 so PDB not applicable; force replicas=2)
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(
        update={"spec": man.spec.model_copy(update={"replicas": 2, "resources": None})}
    )
    issues = k8s_portability_issues(man)
    # Baseline: only warnings
    assert all(i.level == "warn" for i in issues)
    strict = apply_policy(issues, "strict")
    # Expect at least one error escalated (PDB_MISSING or REQS_NONE)
    assert any(i.level == "error" for i in strict)


def test_canary_single_replica_warns() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Ensure single replica and canary strategy
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(update={"replicas": 1, "rollout": {"strategy": "canary"}})
        }
    )
    issues = k8s_portability_issues(man)
    codes = {i.code for i in issues}
    assert "CANARY_SINGLE_REPLICA" in codes


def test_hpa_assumptions_validation(tmp_path, monkeypatch, capsys) -> None:
    # manifest without CPU/Memory requests
    man_path = tmp_path / "echo.yaml"
    man_path.write_text(
        """
apiVersion: ae.dev/v1alpha1
kind: App
metadata:
  name: echo
spec:
  image: alpine:3.20
  replicas: 2
        """.strip()
    )
    exit_code = main(
        [
            "k8s-check",
            "-f",
            str(man_path),
            "--assume-hpa",
            "cpu-util",
            "--assume-hpa",
            "mem-value=200Mi",
            "--policy",
            "strict",
        ]
    )
    assert exit_code != 0


def test_hpa_mem_value_invalid(tmp_path) -> None:
    man_path = tmp_path / "echo.yaml"
    content = "\n".join(
        [
            "apiVersion: ae.dev/v1alpha1",
            "kind: App",
            "metadata:",
            "  name: echo",
            "spec:",
            "  image: alpine:3.20",
            "  replicas: 2",
        ]
    )
    man_path.write_text(content)
    from ae.cli.__main__ import main as _main

    exit_code = _main(
        ["k8s-check", "-f", str(man_path), "--assume-hpa", "mem-value=20XYZ", "--policy", "strict"]
    )
    assert exit_code != 0
