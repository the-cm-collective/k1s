from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GITHUB_WORKFLOWS = ROOT / ".github" / "workflows"
GITEA_WORKFLOWS = ROOT / ".gitea" / "workflows"
SAFE_BUILDER_GATE = ROOT / "scripts" / "release" / "require_safe_builder.sh"
OCI_BUILDER = ROOT / "scripts" / "release" / "build_oci_artifacts.sh"


def _github_workflow(name: str) -> str:
    return (GITHUB_WORKFLOWS / name).read_text(encoding="utf-8")


def _gitea_workflow(name: str) -> str:
    return (GITEA_WORKFLOWS / name).read_text(encoding="utf-8")


def _job_block(workflow: str, job: str) -> str:
    match = re.search(
        rf"^  {re.escape(job)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, f"missing workflow job: {job}"
    return match.group(0)


def _run_builder_gate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command_env = {"PATH": os.environ["PATH"], **env}
    return subprocess.run(  # noqa: S603 - fixed local script and shell paths
        ["/usr/bin/bash", str(SAFE_BUILDER_GATE)],
        check=False,
        capture_output=True,
        env=command_env,
        text=True,
    )


def test_public_python_publish_jobs_are_github_only() -> None:
    testpypi = _github_workflow("publish-testpypi.yml")
    pypi = _github_workflow("publish-pypi.yml")

    for workflow, jobs in (
        (testpypi, ("build", "publish-testpypi")),
        (pypi, ("publish-pypi",)),
    ):
        for job in jobs:
            assert "if: ${{ github.server_url == 'https://github.com' }}" in _job_block(
                workflow, job
            )
        assert "GITEA_" not in workflow
        assert "gitea.core.home.arpa" not in workflow


def test_gitea_integration_workflows_have_no_python_publish_authority() -> None:
    publication_markers = (
        "id-token: write",
        "gh-action-pypi-publish",
        "twine upload",
        "repository-url: https://pypi.org",
        "repository-url: https://test.pypi.org",
    )
    integration_workflows = sorted(GITEA_WORKFLOWS.glob("*.yml"))
    assert {path.name for path in integration_workflows} == {
        "ci-core.yml",
        "ci-docs.yml",
        "nightly-apishim.yml",
        "nightly-runtime.yml",
        "release.yml",
    }

    for path in integration_workflows:
        text = path.read_text(encoding="utf-8")
        for marker in publication_markers:
            assert marker not in text, f"{path.name} contains publication marker {marker!r}"

    release = _gitea_workflow("release.yml")
    assert release.startswith("name: gitea-release-artifacts\n")
    assert "server-guard:" not in release
    assert "permissions:\n  contents: read" in release


def test_github_workflow_directory_contains_only_supported_github_authority() -> None:
    assert {path.name for path in GITHUB_WORKFLOWS.glob("*.yml")} == {
        "publish-pypi.yml",
        "publish-testpypi.yml",
        "python-artifact-reproducibility.yml",
    }


def test_release_paths_refuse_tls_bypasses() -> None:
    release = _gitea_workflow("release.yml")
    ci_release = _job_block(_gitea_workflow("ci-core.yml"), "release-image-smoke")
    builder = OCI_BUILDER.read_text(encoding="utf-8")

    for text in (release, ci_release, builder):
        assert "sslVerify false" not in text
        assert "GIT_SSL_NO_VERIFY=true" not in text
        assert "skip-tls-verify=true" not in text

    assert release.count("GITEA_CA_B64 is required for release checkout") == 4
    assert "GITEA_CA_B64 is required for release-image smoke" in ci_release
    assert "K1S_RELEASE_BUILDER_SCOPE: isolated-ci" in release
    assert "K1S_RELEASE_BUILDER_SCOPE: isolated-ci" in ci_release
    assert 'source "$ROOT_DIR/scripts/release/require_safe_builder.sh"' in builder
    assert "k1s_release_require_safe_builder" in builder
    assert builder.index("k1s_release_require_safe_builder") < builder.index('mkdir -p "$OUT_DIR"')


def test_release_builder_gate_rejects_host_and_insecure_daemons(tmp_path: Path) -> None:
    unscoped = _run_builder_gate({})
    assert unscoped.returncode == 2
    assert "refusing an unscoped release builder" in unscoped.stderr

    host_socket = _run_builder_gate(
        {
            "K1S_RELEASE_BUILDER_SCOPE": "isolated-ci",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
        }
    )
    assert host_socket.returncode == 2
    assert "host-local Docker sockets are forbidden" in host_socket.stderr

    insecure_remote = _run_builder_gate(
        {
            "K1S_RELEASE_BUILDER_SCOPE": "isolated-ci",
            "DOCKER_HOST": "tcp://builder.invalid:2376",
            "DOCKER_TLS_VERIFY": "0",
        }
    )
    assert insecure_remote.returncode == 2
    assert "DOCKER_TLS_VERIFY=1 is required" in insecure_remote.stderr

    certs = tmp_path / "client"
    certs.mkdir()
    for name in ("ca.pem", "cert.pem", "key.pem"):
        (certs / name).write_text("fixture only\n", encoding="utf-8")
    isolated = _run_builder_gate(
        {
            "K1S_RELEASE_BUILDER_SCOPE": "isolated-ci",
            "DOCKER_HOST": "tcp://builder.invalid:2376",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": str(certs),
        }
    )
    assert isolated.returncode == 0, isolated.stderr


def test_release_artifact_commands_do_not_target_managed_runtimes() -> None:
    release = _gitea_workflow("release.yml")
    builder = OCI_BUILDER.read_text(encoding="utf-8")

    for marker in (
        "microk8s",
        "systemctl",
        "containerd",
        "crictl",
        "iptables",
        "docker push",
        "--push",
        "--load",
    ):
        assert marker not in release.lower()
        assert marker not in builder.lower()
    assert '--output "type=oci,dest=$archive_path,name=$image_ref"' in builder


def test_workerbee_evidence_remains_advisory_to_k1s() -> None:
    design = (ROOT / "docs" / "design" / "fabric-control-plane.md").read_text(encoding="utf-8")
    assert "## WorkerBee Complementary Evidence Boundary" in design
    assert "external lifecycle evidence" in design
    assert "do not replace controller-owned" in design
    assert "must still pass k1s-native gates" in design
