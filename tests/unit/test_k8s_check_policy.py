from pathlib import Path

from ae.cli.__main__ import main
from ae.controller.spec import load_manifest
from ae.k8s.check import apply_policy, k8s_portability_issues


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


def test_hpa_assumptions_validation(tmp_path) -> None:
    # manifest without CPU/Memory requests
    man_path = tmp_path / "echo.yaml"
    man_path.write_text(
        """
apiVersion: ae.dev/v1alpha1
kind: Deployment
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
            "kind: Deployment",
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


def test_startup_probe_hint_emitted() -> None:
    from ae.controller.spec import AppManifest, AppSpec, HealthSpec, Metadata, ProbeSpec

    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            health=HealthSpec(
                liveness=ProbeSpec(httpGet={"path": "/live", "port": 8080})  # type: ignore[arg-type]
            ),
        ),
    )
    issues = k8s_portability_issues(man)
    assert any(i.code == "PROBE_STARTUP_RECOMMENDED" and i.level == "warn" for i in issues)


def test_prestop_short_grace_warns() -> None:
    from ae.controller.spec import AppManifest, AppSpec, Metadata

    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            terminationGracePeriodSeconds=1,
            lifecycle={"preStop": {"exec": {"command": ["/bin/sleep", "1"]}}},  # type: ignore[arg-type]
        ),
    )
    issues = k8s_portability_issues(man)
    assert any(i.code == "PRESTOP_SHORT_GRACE" for i in issues)


def test_qos_limits_without_requests_warns() -> None:
    from ae.controller.spec import AppManifest, AppSpec, Metadata, ResourceQuantities, ResourcesSpec

    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            resources=ResourcesSpec(limits=ResourceQuantities(cpu=0.5, memory="256Mi")),
        ),
    )
    issues = k8s_portability_issues(man)
    assert any(i.code == "QOS_LIMITS_NO_REQUESTS" and i.level == "warn" for i in issues)
