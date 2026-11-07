from __future__ import annotations

import re
import tarfile
import tomllib
from importlib import import_module
from pathlib import Path

from scripts.normalize_python_sdist import normalize
from scripts.project_version import read_project_version

from ae import __version__ as ae_version
from ae import build_info
from ae.apishim.server import _openapi_v3_stub, _swagger_doc
from ae.kctl import __version__ as kctl_version
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
    assert read_project_version(PYPROJECT) == "0.1.8a1"
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
            "sql/apishim/*.sql",
            "sql/controller/*.sql",
        ],
        "ae.observability": ["static/vendor/*.css", "static/vendor/*.js"],
    }


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
    assert 'PIP_NO_INDEX: "1"' in testpypi
    assert "PIP_CONFIG_FILE: /dev/null" in testpypi
    assert "validate_installed_python_artifact.py" in testpypi
    assert "--requirements-lock requirements-release.lock" in testpypi
    assert "--require-hashes" in testpypi
    assert "SHA256SUMS" in testpypi
    assert "normalize_python_sdist.py" in testpypi
    assert "actions/attest-build-provenance@1c608d11d69870c2092266b3f9a6f3abbf17002c" in testpypi
    assert "attestations: write" in testpypi
    assert testpypi.count("attestations: read") == 1
    assert testpypi.count("gh attestation verify") == 3
    assert "source-run-id" in pypi
    assert '"success"' in pypi
    assert '".github/workflows/publish-testpypi.yml"' in pypi
    assert "run-id: ${{ inputs.source-run-id }}" in pypi
    assert "name: k1s-core-python-${{ inputs.source-sha }}" in pypi
    assert "python -m build" not in pypi
    assert "--verify-manifest bundle/SHA256SUMS" in pypi
    assert pypi.count("attestations: read") == 1
    assert pypi.count("gh attestation verify") == 3


def test_release_documentation_records_pre_v1_compatibility() -> None:
    versioning = (ROOT / "VERSIONING.md").read_text(encoding="utf-8")
    distribution = (ROOT / "docs" / "reference" / "python-distribution.md").read_text(
        encoding="utf-8"
    )
    assert "0.1.8a1" in versioning
    assert "1.0.0a1" in versioning
    assert "distribution name to `k1s-core`" in versioning
    assert "Python imports remain under `ae`" in distribution
    assert "Production never rebuilds" in distribution
    assert PROTECTED_PATHS.isdisjoint(distribution.splitlines())


def test_sdist_normalization_is_exact_byte_reproducible(tmp_path: Path) -> None:
    outputs = []
    for index, mtime in enumerate((100, 200)):
        source = tmp_path / f"payload-{index}.txt"
        source.write_text("same payload\n", encoding="utf-8")
        archive = tmp_path / f"source-{index}.tar.gz"
        with tarfile.open(archive, "w:gz") as target:
            info = target.gettarinfo(str(source), arcname="project/payload.txt")
            info.mtime = mtime
            info.uid = index + 1000
            info.gid = index + 1000
            with source.open("rb") as payload:
                target.addfile(info, payload)
        normalize(archive, epoch=1234567890)
        outputs.append(archive.read_bytes())
    assert outputs[0] == outputs[1]
