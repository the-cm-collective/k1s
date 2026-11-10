from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tomllib
import zipfile
from importlib import import_module
from pathlib import Path

import pytest
from scripts.build_python_artifacts import (
    BuildPolicyError,
    GitFile,
    _source_rows,
    _validate_path,
    export_source,
)
from scripts.normalize_python_sdist import SdistNormalizationError
from scripts.normalize_python_sdist import normalize as normalize_sdist
from scripts.normalize_python_wheel import (
    WheelNormalizationError,
)
from scripts.normalize_python_wheel import (
    normalize as normalize_wheel,
)
from scripts.project_version import read_project_version
from scripts.validate_python_artifacts import verify_bundle_metadata, write_bundle_metadata

from ae import __version__ as ae_version
from ae import build_info
from ae.apishim.server import _openapi_v3_stub, _swagger_doc
from ae.kctl import __version__ as kctl_version
from ae.kctl.profile_adapter import (
    ADAPTER_API_VERSION,
    EVIDENCE_API_VERSION,
    load_workerbee_adapter_contract,
)
from ae.runtime.cri.api.runtime.v1 import api_pb2, gogo_pb2
from ae.storage.csi.api import csi_pb2, csi_pb2_grpc

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
PROTECTED_PATHS = {
    ".env",
    ".envrc",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    "ops/systemd/ae-ha-core.env",
    "ops/systemd/ae.env",
}
FIXTURE_COMMIT = "a" * 40
FIXTURE_TREE = "b" * 40


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def _workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_distribution_identity_preserves_import_and_commands() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["project"]["name"] == "k1s-core"
    assert "version" not in data["project"]
    assert "version" in data["project"]["dynamic"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "ae.__version__"}
    assert read_project_version(PYPROJECT) == "1.0.0a2"
    assert data["project"]["scripts"] == {
        "ae": "ae.cli.__main__:main",
        "ae-rotate-certs": "ae.cli.rotate_certs:main",
        "k1s": "ae.kctl.__main__:main",
        "ae-node": "ae.node.server:main",
        "ae-gateway": "ae.gateway.__main__:main",
        "ae-worker-stub": "ae.worker_stub.__main__:main",
    }
    packages = data["tool"]["setuptools"]["packages"]["find"]
    assert packages == {
        "where": ["src"],
        "include": ["ae", "ae.*"],
        "namespaces": True,
    }
    assert data["tool"]["setuptools"]["package-data"] == {
        "ae.resources": [
            "ingress/*.txt",
            "observability/*.html",
            "profiles/*.json",
            "sql/apishim/*.sql",
            "sql/controller/*.sql",
        ],
        "ae.observability": ["static/vendor/*.css", "static/vendor/*.js"],
    }


def test_workerbee_adapter_is_one_package_resource_not_a_distribution_or_console() -> None:
    resource = ROOT / "src" / "ae" / "resources" / "profiles" / "workerbee-adapter-v1.json"
    raw = resource.read_bytes()
    value = json.loads(raw)
    assert raw == (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    contract = load_workerbee_adapter_contract()
    assert value["apiVersion"] == ADAPTER_API_VERSION
    assert value["evidenceApiVersion"] == EVIDENCE_API_VERSION
    assert [descriptor.name for descriptor in contract.operations] == [
        "list",
        "doctor",
        "plan",
        "status",
        "logs",
        "bootstrap",
    ]
    project = _project()
    assert set(project["scripts"]) == {
        "ae",
        "ae-rotate-certs",
        "k1s",
        "ae-node",
        "ae-gateway",
        "ae-worker-stub",
    }
    assert project["name"] == "k1s-core"


def test_documented_compatibility_entrypoints_remain_importable() -> None:
    scripts = _project()["scripts"]
    for target in scripts.values():
        module_name, attribute = target.split(":", 1)
        module = import_module(module_name)
        assert callable(getattr(module, attribute))
    assert build_info()["version"] == ae_version


def test_version_sources_share_the_canonical_runtime_version() -> None:
    expected = read_project_version(PYPROJECT)
    assert expected == ae_version == kctl_version
    assert _swagger_doc()["info"]["version"] == expected
    assert _openapi_v3_stub()["info"]["version"] == expected
    controller_source = (ROOT / "src" / "ae" / "observability" / "http_api.py").read_text(
        encoding="utf-8"
    )
    assert '"info": {"title": "k1s Controller API", "version": AE_VERSION}' in controller_source


def test_cri_gogo_dependency_is_package_local() -> None:
    github_source = ROOT / "src" / "github"
    assert not github_source.exists() or not list(github_source.rglob("*.py"))
    assert gogo_pb2.DESCRIPTOR.name == "github.com/gogo/protobuf/gogoproto/gogo.proto"
    assert api_pb2.DESCRIPTOR.dependencies[0] is gogo_pb2.DESCRIPTOR
    api_source = (
        ROOT / "src" / "ae" / "runtime" / "cri" / "api" / "runtime" / "v1" / "api_pb2.py"
    ).read_text(encoding="utf-8")
    assert "from . import gogo_pb2 as" in api_source
    assert "from github." not in api_source
    codegen = (ROOT / "scripts" / "cri_codegen.sh").read_text(encoding="utf-8")
    assert '"$OUT_DIR/gogo_pb2.py"' in codegen
    assert '"$ROOT/src/github"' not in codegen


def test_csi_generated_dependencies_are_package_local() -> None:
    assert csi_pb2_grpc is not None
    assert csi_pb2_grpc.csi__pb2 is csi_pb2
    grpc_source = (ROOT / "src" / "ae" / "storage" / "csi" / "api" / "csi_pb2_grpc.py").read_text(
        encoding="utf-8"
    )
    assert "from . import csi_pb2 as csi__pb2" in grpc_source
    assert "\nimport csi_pb2 as csi__pb2" not in grpc_source


def test_obsolete_workerbee_runtime_distribution_cannot_be_built() -> None:
    assert not (ROOT / "scripts" / "build_workerbee_runtime_wheel.py").exists()
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert project["name"] == "k1s-core"
    assert "k1s-workerbee-runtime" not in PYPROJECT.read_text(encoding="utf-8")


def test_publish_workflows_pin_actions_and_preserve_exact_bytes() -> None:
    testpypi = _workflow("publish-testpypi.yml")
    pypi = _workflow("publish-pypi.yml")
    for workflow in (testpypi, pypi):
        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)
        assert uses
        assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in uses)
        assert "id-token: write" in workflow
        assert "packages-dir:" in workflow
        assert "--extra-index-url" not in workflow
        assert "--index-url" not in workflow

    assert "repository-url: https://test.pypi.org/legacy/" in testpypi
    assert '--requirements-lock "$GITHUB_WORKSPACE/requirements-release.lock"' in testpypi
    assert '--source-contract "$GITHUB_WORKSPACE/requirements-release-sources.json"' in testpypi
    assert "scripts/prepare_release_wheelhouse.py" in testpypi
    assert 'python-version: "3.11.18"' in testpypi
    assert "SHA256SUMS" in testpypi
    assert testpypi.count("scripts/build_python_artifacts.py") == 2
    assert "python -m build" not in testpypi
    assert "normalize_python_sdist.py" not in testpypi
    assert "normalize_python_wheel.py" not in testpypi
    assert "validate_python_artifacts.py" not in testpypi
    assert "artifact-manifest.json" in testpypi
    assert "manifests/*.jsonl" in testpypi
    assert "actions/attest-build-provenance@1c608d11d69870c2092266b3f9a6f3abbf17002c" in testpypi
    assert "attestations: write" in testpypi
    assert testpypi.count("attestations: read") == 1
    assert testpypi.count("gh attestation verify") == 5
    assert "source-run-id" in pypi
    assert '"success"' in pypi
    assert '".github/workflows/publish-testpypi.yml"' in pypi
    assert "run-id: ${{ inputs.source-run-id }}" in pypi
    assert "name: k1s-core-python-${{ inputs.source-sha }}" in pypi
    assert "python -m build" not in pypi
    assert "--verify-manifest bundle/SHA256SUMS" in pypi
    assert pypi.count("attestations: read") == 1
    assert pypi.count("gh attestation verify") == 3


def test_ci_artifact_job_is_nonpublishing_and_uses_only_the_helper() -> None:
    ci = _workflow("ci-core.yml")
    assert "python-artifact-reproducibility:" in ci
    job = ci.split("  python-artifact-reproducibility:", 1)[1].split("  release-image-smoke:", 1)[0]
    assert job.count("scripts/build_python_artifacts.py build") == 2
    assert "umask 022" in job
    assert "umask 077" in job
    assert "diff -qr" in job
    assert "id-token: write" not in job
    assert "attestations: write" not in job
    assert "gh-action-pypi-publish" not in job
    assert "python -m build" not in job
    assert "normalize_python_sdist.py" not in job
    assert "normalize_python_wheel.py" not in job


def test_release_documentation_records_v1_alpha_compatibility() -> None:
    versioning = (ROOT / "VERSIONING.md").read_text(encoding="utf-8")
    distribution = (ROOT / "docs" / "reference" / "python-distribution.md").read_text(
        encoding="utf-8"
    )
    assert "0.1.8a1" in versioning
    assert "1.0.0a2" in versioning
    assert "distribution name to `k1s-core`" in versioning
    assert "1.0 alpha compatibility contract" in versioning
    assert "Python imports remain under `ae`" in distribution
    assert "current\nPEP 440 candidate" in distribution
    assert "Production never rebuilds" in distribution
    assert PROTECTED_PATHS.isdisjoint(distribution.splitlines())


def test_workerbee_adapter_documentation_preserves_experimental_release_holds() -> None:
    adapter = (ROOT / "docs" / "reference" / "workerbee-profile-adapter.md").read_text(
        encoding="utf-8"
    )
    public = (ROOT / "docs" / "reference" / "public-contracts.md").read_text(encoding="utf-8")
    assert "source-validated/unqualified" in adapter
    assert "W-P0-1 and Q1" in adapter
    assert "adds no distribution" in adapter
    assert "No line, substring" in adapter
    assert "k1s-workerbee-run-ref-v1" in adapter
    assert "k1s-workerbee-profile-evidence-v1" in adapter
    assert "WorkerBee profile evidence adapter | experimental" in public
    assert PROTECTED_PATHS.isdisjoint(adapter.splitlines())


def test_sdist_normalization_is_exact_byte_reproducible(tmp_path: Path) -> None:
    payload = b"same payload\n"
    source_manifest = tmp_path / "source-members.jsonl"
    source_manifest.write_text(
        json.dumps(
            {
                "gitMode": "100644",
                "mode": "0644",
                "path": "payload.txt",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "type": "file",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    outputs = []
    for index, mtime in enumerate((100, 200)):
        lane = tmp_path / f"lane-{index}"
        lane.mkdir()
        archive = lane / "k1s_core-1.0.0a2.tar.gz"
        with tarfile.open(archive, "w:gz") as target:
            info = tarfile.TarInfo("k1s_core-1.0.0a2/payload.txt")
            info.mtime = mtime
            info.uid = index + 1000
            info.gid = index + 1000
            info.mode = 0o600 if index else 0o666
            info.size = len(payload)
            target.addfile(info, io.BytesIO(payload))
        output = lane / "normalized.tar.gz"
        normalize_sdist(
            archive,
            output,
            epoch=1793664000,
            distribution="k1s-core",
            version="1.0.0a2",
            source_members=source_manifest,
        )
        with tarfile.open(output, "r:gz") as normalized:
            member = normalized.getmembers()[0]
            assert stat.S_IMODE(member.mode) == 0o644
            assert member.mtime == 1793664000
            assert member.uid == member.gid == 0
        outputs.append(output.read_bytes())
    assert outputs[0] == outputs[1]


def _wheel_record(payloads: dict[str, bytes], record: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(payloads):
        digest = hashlib.sha256(payloads[name]).digest()
        import base64

        writer.writerow(
            (
                name,
                "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
                str(len(payloads[name])),
            )
        )
    writer.writerow((record, "", ""))
    return output.getvalue().encode()


def _write_wheel(path: Path, *, unsafe_name: str | None = None) -> None:
    dist_info = "k1s_core-1.0.0a2.dist-info"
    record = f"{dist_info}/RECORD"
    payloads = {
        "ae/__init__.py": b'__version__ = "1.0.0a2"\n',
        f"{dist_info}/METADATA": b"Metadata-Version: 2.4\nName: k1s-core\nVersion: 1.0.0a2\n\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\nRoot-Is-Purelib: true\n\n",
    }
    if unsafe_name is not None:
        payloads[unsafe_name] = b"unsafe"
    payloads[record] = _wheel_record(payloads, record)
    with zipfile.ZipFile(path, "w") as archive:
        for name in reversed(list(payloads)):
            archive.writestr(name, payloads[name])


def test_wheel_normalization_rebuilds_record_modes_order_and_timestamps(tmp_path: Path) -> None:
    input_wheel = tmp_path / "k1s_core-1.0.0a2-py3-none-any.whl"
    output_wheel = tmp_path / "canonical.whl"
    _write_wheel(input_wheel)
    members = normalize_wheel(
        input_wheel,
        output_wheel,
        epoch=1793664000,
        distribution="k1s-core",
        version="1.0.0a2",
    )
    assert [member.path for member in members] == sorted(
        (member.path for member in members), key=str.encode
    )
    with zipfile.ZipFile(output_wheel) as archive:
        assert archive.namelist() == sorted(archive.namelist(), key=str.encode)
        assert all(stat.S_IMODE(info.external_attr >> 16) == 0o644 for info in archive.infolist())
        assert all(info.create_system == 3 for info in archive.infolist())
        record = archive.read("k1s_core-1.0.0a2.dist-info/RECORD").decode().splitlines()
        assert record[-1] == "k1s_core-1.0.0a2.dist-info/RECORD,,"


@pytest.mark.parametrize("unsafe", ["../escape", "/absolute", "ae//double", "ae\\bad.py"])
def test_wheel_normalization_rejects_unsafe_members(tmp_path: Path, unsafe: str) -> None:
    wheel = tmp_path / "k1s_core-1.0.0a2-py3-none-any.whl"
    _write_wheel(wheel, unsafe_name=unsafe)
    with pytest.raises(WheelNormalizationError):
        normalize_wheel(
            wheel,
            tmp_path / "output.whl",
            epoch=1793664000,
            distribution="k1s-core",
            version="1.0.0a2",
        )


@pytest.mark.parametrize(
    "name",
    ["/absolute", "../escape", "a/../escape", "a//b", "a\\b", "C:/drive", "a\x00b"],
)
def test_git_export_path_policy_rejects_noncanonical_names(name: str) -> None:
    with pytest.raises(BuildPolicyError):
        _validate_path(name)


def test_source_manifest_uses_only_git_modes() -> None:
    rows = _source_rows(
        [
            GitFile("bin/tool", "100755", "a" * 40, b"tool"),
            GitFile("pkg/data", "100644", "b" * 40, b"data"),
        ]
    )
    by_path = {str(row["path"]): row for row in rows}
    assert by_path["bin"]["mode"] == "0755"
    assert by_path["bin/tool"]["mode"] == "0755"
    assert by_path["pkg/data"]["mode"] == "0644"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed test-only Git executable
        ["/usr/bin/git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_fixture(repo: Path) -> tuple[str, str]:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    files = [
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    ]
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-qm", "fixture")
    return _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")


def test_exact_git_object_export_ignores_umask_and_workspace_modes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    regular = repo / "pkg/data.txt"
    executable = repo / "pkg/tool"
    regular.write_text("data\n", encoding="utf-8")
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    commit, tree = _commit_fixture(repo)
    regular.chmod(0o600)
    executable.chmod(0o600)
    manifests = []
    for name, mask in (("a", 0o022), ("b", 0o077)):
        lane = tmp_path / name
        lane.mkdir()
        source = lane / "source"
        manifest = lane / "source-members.jsonl"
        previous = os.umask(mask)
        try:
            export_source(
                git=Path(shutil.which("git") or "/usr/bin/git"),
                repository=repo,
                commit=commit,
                tree=tree,
                source_root=source,
                source_manifest=manifest,
            )
        finally:
            os.umask(previous)
        assert stat.S_IMODE((source / "pkg/data.txt").stat().st_mode) == 0o644
        assert stat.S_IMODE((source / "pkg/tool").stat().st_mode) == 0o755
        manifests.append(manifest.read_bytes())
    assert manifests[0] == manifests[1]


def test_git_export_rejects_symlinks_before_materialization(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "target").write_text("target\n", encoding="utf-8")
    (repo / "link").symlink_to("target")
    commit, tree = _commit_fixture(repo)
    with pytest.raises(BuildPolicyError, match="unsupported mode or type"):
        export_source(
            git=Path("/usr/bin/git"),
            repository=repo,
            commit=commit,
            tree=tree,
            source_root=tmp_path / "source",
            source_manifest=tmp_path / "source-members.jsonl",
        )


def test_git_export_rejects_casefold_collisions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Name").write_text("one\n", encoding="utf-8")
    (repo / "name").write_text("two\n", encoding="utf-8")
    commit, tree = _commit_fixture(repo)
    with pytest.raises(BuildPolicyError, match="collision"):
        export_source(
            git=Path("/usr/bin/git"),
            repository=repo,
            commit=commit,
            tree=tree,
            source_root=tmp_path / "source",
            source_manifest=tmp_path / "source-members.jsonl",
        )


@pytest.mark.parametrize("authority", ["replacement", "promisor"])
def test_git_export_rejects_ambiguous_object_authority(tmp_path: Path, authority: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file").write_text("payload\n", encoding="utf-8")
    commit, tree = _commit_fixture(repo)
    if authority == "replacement":
        _git(repo, "update-ref", f"refs/replace/{commit}", commit)
    else:
        _git(repo, "config", "remote.origin.promisor", "true")
    with pytest.raises(BuildPolicyError):
        export_source(
            git=Path("/usr/bin/git"),
            repository=repo,
            commit=commit,
            tree=tree,
            source_root=tmp_path / "source",
            source_manifest=tmp_path / "source-members.jsonl",
        )


@pytest.mark.parametrize("failure", ["symlink", "unknown-pax", "unowned"])
def test_sdist_normalization_rejects_links_metadata_and_unowned_members(
    tmp_path: Path, failure: str
) -> None:
    source_manifest = tmp_path / "source-members.jsonl"
    payload = b"payload\n"
    source_manifest.write_text(
        json.dumps(
            {
                "gitMode": "100644",
                "mode": "0644",
                "path": "owned.txt",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "type": "file",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    archive = tmp_path / "k1s_core-1.0.0a2.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        name = "owned.txt" if failure != "unowned" else "extra.txt"
        info = tarfile.TarInfo(f"k1s_core-1.0.0a2/{name}")
        if failure == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "owned.txt"
            target.addfile(info)
        else:
            info.size = len(payload)
            if failure == "unknown-pax":
                info.pax_headers = {"SCHILY.xattr.user.bad": "value"}
            target.addfile(info, io.BytesIO(payload))
    with pytest.raises(SdistNormalizationError):
        normalize_sdist(
            archive,
            tmp_path / "normalized.tar.gz",
            epoch=1793664000,
            distribution="k1s-core",
            version="1.0.0a2",
            source_members=source_manifest,
        )


def test_normalizers_refuse_existing_outputs(tmp_path: Path) -> None:
    wheel = tmp_path / "k1s_core-1.0.0a2-py3-none-any.whl"
    _write_wheel(wheel)
    existing = tmp_path / "existing.whl"
    existing.write_bytes(b"do not replace")
    with pytest.raises(WheelNormalizationError, match="already exists"):
        normalize_wheel(
            wheel,
            existing,
            epoch=1793664000,
            distribution="k1s-core",
            version="1.0.0a2",
        )


def test_protected_paths_are_exact_and_never_source_members() -> None:
    assert {
        ".env",
        ".envrc",
        ".env.development",
        ".env.local",
        ".env.production",
        ".env.test",
        "ops/systemd/ae-ha-core.env",
        "ops/systemd/ae.env",
    } == PROTECTED_PATHS


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fixture_tool_inputs() -> dict[str, object]:
    wheels = [
        {
            "filename": f"dependency_{index:02}-1-py3-none-any.whl",
            "sha256": hashlib.sha256(f"wheel-{index}".encode()).hexdigest(),
            "size": index + 1,
        }
        for index in range(24)
    ]
    wheel_stream = b"".join(_canonical_json(wheel) for wheel in wheels)
    distributions = {
        name: {"recordDigest": hashlib.sha256(name.encode()).hexdigest(), "version": "1"}
        for name in ("build", "packaging", "pyproject-hooks", "setuptools", "twine", "wheel")
    }
    return {
        "buildDistributions": distributions,
        "git": {"sha256": "1" * 64, "size": 1, "version": "git version fixture"},
        "python": {"sha256": "2" * 64, "size": 1, "version": "Python fixture"},
        "requirementsLockSha256": "3" * 64,
        "runtimeWheelhouseCount": 24,
        "runtimeWheelhouseIdentity": hashlib.sha256(wheel_stream).hexdigest(),
        "runtimeWheels": wheels,
    }


def _create_valid_bundle(root: Path) -> Path:
    bundle = root / "bundle"
    dist = bundle / "dist"
    manifests = bundle / "manifests"
    dist.mkdir(parents=True)
    manifests.mkdir()
    payload = b"payload\n"
    source_members = manifests / "source-members.jsonl"
    source_members.write_bytes(
        _canonical_json(
            {
                "gitMode": "100644",
                "mode": "0644",
                "path": "payload.txt",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "type": "file",
            }
        )
    )

    raw = root / "raw"
    raw.mkdir()
    raw_wheel = raw / "k1s_core-1.0.0a2-py3-none-any.whl"
    _write_wheel(raw_wheel)
    normalize_wheel(
        raw_wheel,
        dist / raw_wheel.name,
        epoch=1793664000,
        distribution="k1s-core",
        version="1.0.0a2",
    )
    raw_sdist = raw / "k1s_core-1.0.0a2.tar.gz"
    with tarfile.open(raw_sdist, "w:gz") as archive:
        info = tarfile.TarInfo("k1s_core-1.0.0a2/payload.txt")
        info.size = len(payload)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(payload))
    normalize_sdist(
        raw_sdist,
        dist / raw_sdist.name,
        epoch=1793664000,
        distribution="k1s-core",
        version="1.0.0a2",
        source_members=source_members,
    )
    write_bundle_metadata(
        dist=dist,
        source_members=source_members,
        wheel_members=manifests / "wheel-members.jsonl",
        sdist_members=manifests / "sdist-members.jsonl",
        artifact_manifest=bundle / "artifact-manifest.json",
        sha256sums=bundle / "SHA256SUMS",
        commit=FIXTURE_COMMIT,
        tree=FIXTURE_TREE,
        tool_inputs=_fixture_tool_inputs(),
        validation={
            "artifactValidator": "passed",
            "installedArtifact": "passed",
            "publicMarkers": "passed",
            "twine": "passed",
        },
    )
    return bundle


def _read_bundle_manifest(bundle: Path) -> dict[str, object]:
    return json.loads((bundle / "artifact-manifest.json").read_text(encoding="utf-8"))


def _rewrite_sha256sums(bundle: Path) -> None:
    relative_files = (
        "artifact-manifest.json",
        "dist/k1s_core-1.0.0a2-py3-none-any.whl",
        "dist/k1s_core-1.0.0a2.tar.gz",
        "manifests/sdist-members.jsonl",
        "manifests/source-members.jsonl",
        "manifests/wheel-members.jsonl",
    )
    lines = [
        f"{hashlib.sha256((bundle / relative).read_bytes()).hexdigest()}  {relative}"
        for relative in relative_files
    ]
    (bundle / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_bundle_manifest(bundle: Path, value: dict[str, object]) -> None:
    (bundle / "artifact-manifest.json").write_bytes(_canonical_json(value))
    _rewrite_sha256sums(bundle)


def _verify_fixture_bundle(bundle: Path) -> dict[str, object]:
    return verify_bundle_metadata(
        bundle=bundle,
        expected_commit=FIXTURE_COMMIT,
        expected_tree=FIXTURE_TREE,
    )


def test_bundle_verifier_accepts_one_exact_valid_bundle(tmp_path: Path) -> None:
    bundle = _create_valid_bundle(tmp_path)
    result = _verify_fixture_bundle(bundle)
    assert result["schema"] == "k1s-python-artifacts/v1"


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("sha256", "0" * 64),
        ("size", 1),
        ("memberCount", 1),
        ("memberManifestSha256", "0" * 64),
    ],
)
def test_bundle_verifier_rejects_false_artifact_claims(
    tmp_path: Path, kind: str, field: str, replacement: object
) -> None:
    bundle = _create_valid_bundle(tmp_path)
    manifest = _read_bundle_manifest(bundle)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    artifact = artifacts[kind]
    assert isinstance(artifact, dict)
    if field in {"size", "memberCount"}:
        replacement = int(artifact[field]) + int(replacement)
    artifact[field] = replacement
    _write_bundle_manifest(bundle, manifest)
    with pytest.raises(ValueError):
        _verify_fixture_bundle(bundle)


@pytest.mark.parametrize("field", ["memberCount", "memberManifestSha256"])
def test_bundle_verifier_rejects_false_source_manifest_claims(tmp_path: Path, field: str) -> None:
    bundle = _create_valid_bundle(tmp_path)
    manifest = _read_bundle_manifest(bundle)
    source = manifest["source"]
    assert isinstance(source, dict)
    source[field] = 2 if field == "memberCount" else "0" * 64
    _write_bundle_manifest(bundle, manifest)
    with pytest.raises(ValueError):
        _verify_fixture_bundle(bundle)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_bundle_verifier_reconstructs_archive_rows_instead_of_trusting_claims(
    tmp_path: Path, kind: str
) -> None:
    bundle = _create_valid_bundle(tmp_path)
    bogus_row = {
        "gitMode": None,
        "mode": "0644",
        "path": "bogus.txt",
        "sha256": hashlib.sha256(b"x").hexdigest(),
        "size": 1,
        "type": "file",
    }
    member_manifest = bundle / f"manifests/{kind}-members.jsonl"
    member_manifest.write_bytes(_canonical_json(bogus_row))
    manifest = _read_bundle_manifest(bundle)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    artifact = artifacts[kind]
    assert isinstance(artifact, dict)
    artifact["memberCount"] = 1
    artifact["memberManifestSha256"] = hashlib.sha256(member_manifest.read_bytes()).hexdigest()
    _write_bundle_manifest(bundle, manifest)
    with pytest.raises(ValueError, match="reconstructed archive rows"):
        _verify_fixture_bundle(bundle)


def test_bundle_verifier_rejects_exact_prior_all_zero_wheel_hash_tamper(
    tmp_path: Path,
) -> None:
    bundle = _create_valid_bundle(tmp_path)
    bogus_row = {
        "gitMode": None,
        "mode": "0644",
        "path": "bogus.txt",
        "sha256": hashlib.sha256(b"x").hexdigest(),
        "size": 1,
        "type": "file",
    }
    wheel_members = bundle / "manifests/wheel-members.jsonl"
    wheel_members.write_bytes(_canonical_json(bogus_row))
    manifest = _read_bundle_manifest(bundle)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    wheel = artifacts["wheel"]
    assert isinstance(wheel, dict)
    wheel["sha256"] = "0" * 64
    wheel["memberCount"] = 1
    wheel["memberManifestSha256"] = hashlib.sha256(wheel_members.read_bytes()).hexdigest()
    _write_bundle_manifest(bundle, manifest)
    with pytest.raises(ValueError, match="wheel artifact claim"):
        _verify_fixture_bundle(bundle)


@pytest.mark.parametrize("entry_type", ["file", "directory"])
def test_bundle_verifier_rejects_extra_filesystem_entries(tmp_path: Path, entry_type: str) -> None:
    bundle = _create_valid_bundle(tmp_path)
    extra = bundle / "unexpected"
    extra.write_bytes(b"extra") if entry_type == "file" else extra.mkdir()
    with pytest.raises(ValueError, match="filesystem membership"):
        _verify_fixture_bundle(bundle)


@pytest.mark.parametrize(
    "relative",
    [
        "artifact-manifest.json",
        "SHA256SUMS",
        "dist/k1s_core-1.0.0a2-py3-none-any.whl",
        "dist/k1s_core-1.0.0a2.tar.gz",
        "manifests/source-members.jsonl",
        "manifests/wheel-members.jsonl",
        "manifests/sdist-members.jsonl",
    ],
)
def test_bundle_verifier_rejects_symlink_substitution(tmp_path: Path, relative: str) -> None:
    bundle = _create_valid_bundle(tmp_path)
    target = bundle / relative
    replacement = tmp_path / (relative.replace("/", "-") + ".replacement")
    replacement.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(replacement)
    with pytest.raises(ValueError, match="regular file"):
        _verify_fixture_bundle(bundle)


@pytest.mark.parametrize("entry_type", ["directory", "fifo"])
def test_bundle_verifier_rejects_nonregular_file_positions(tmp_path: Path, entry_type: str) -> None:
    bundle = _create_valid_bundle(tmp_path)
    target = bundle / "artifact-manifest.json"
    target.unlink()
    target.mkdir() if entry_type == "directory" else os.mkfifo(target)
    with pytest.raises(ValueError, match="regular file"):
        _verify_fixture_bundle(bundle)


def test_bundle_verifier_rejects_duplicate_manifest_json_keys(tmp_path: Path) -> None:
    bundle = _create_valid_bundle(tmp_path)
    (bundle / "artifact-manifest.json").write_bytes(b'{"schema":"one","schema":"two"}\n')
    with pytest.raises(ValueError, match="duplicate key"):
        _verify_fixture_bundle(bundle)


@pytest.mark.parametrize("failure", ["noncanonical-json", "wrong-schema", "extra-schema"])
def test_bundle_verifier_rejects_manifest_canonicality_and_schema(
    tmp_path: Path, failure: str
) -> None:
    bundle = _create_valid_bundle(tmp_path)
    manifest = _read_bundle_manifest(bundle)
    if failure == "noncanonical-json":
        (bundle / "artifact-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        _rewrite_sha256sums(bundle)
    else:
        manifest["schema"] = "wrong" if failure == "wrong-schema" else manifest["schema"]
        if failure == "extra-schema":
            manifest["unexpected"] = True
        _write_bundle_manifest(bundle, manifest)
    with pytest.raises(ValueError):
        _verify_fixture_bundle(bundle)


@pytest.mark.parametrize("failure", ["duplicate-key", "noncanonical", "order", "schema"])
def test_bundle_verifier_rejects_member_manifest_encoding_and_schema(
    tmp_path: Path, failure: str
) -> None:
    bundle = _create_valid_bundle(tmp_path)
    member_path = bundle / "manifests/source-members.jsonl"
    row = json.loads(member_path.read_text(encoding="utf-8"))
    if failure == "duplicate-key":
        member_path.write_bytes(
            b'{"gitMode":"100644","gitMode":"100644","mode":"0644",'
            b'"path":"payload.txt","sha256":"'
            + str(row["sha256"]).encode()
            + b'","size":8,"type":"file"}\n'
        )
    elif failure == "noncanonical":
        member_path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    elif failure == "order":
        second = dict(row)
        second["path"] = "aardvark.txt"
        member_path.write_bytes(_canonical_json(row) + _canonical_json(second))
    else:
        row["unexpected"] = True
        member_path.write_bytes(_canonical_json(row))
    manifest = _read_bundle_manifest(bundle)
    source = manifest["source"]
    assert isinstance(source, dict)
    source["memberCount"] = len(member_path.read_bytes().splitlines())
    source["memberManifestSha256"] = hashlib.sha256(member_path.read_bytes()).hexdigest()
    _write_bundle_manifest(bundle, manifest)
    with pytest.raises(ValueError):
        _verify_fixture_bundle(bundle)


def test_bundle_verifier_rejects_sha256sums_order_and_format(tmp_path: Path) -> None:
    bundle = _create_valid_bundle(tmp_path)
    sha_path = bundle / "SHA256SUMS"
    lines = sha_path.read_text(encoding="utf-8").splitlines()
    sha_path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="membership/order"):
        _verify_fixture_bundle(bundle)
