from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from scripts.check_public_markers import scan_source, scan_text
from scripts.project_version import read_project_version, write_project_version

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"


def test_public_project_metadata_is_canonical_and_supported() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert project["name"] == "k1s-core"
    assert project["requires-python"] == ">=3.11"
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert project["urls"] == {
        "Homepage": "https://github.com/the-cm-collective/k1s",
        "Repository": "https://github.com/the-cm-collective/k1s",
        "Documentation": "https://github.com/the-cm-collective/k1s/tree/main/docs",
        "Issues": "https://github.com/the-cm-collective/k1s/issues",
        "Changelog": "https://github.com/the-cm-collective/k1s/blob/main/CHANGELOG.md",
    }
    classifiers = set(project["classifiers"])
    assert "Development Status :: 3 - Alpha" in classifiers
    assert "Operating System :: POSIX :: Linux" in classifiers
    assert "Programming Language :: Python :: 3.11" in classifiers
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert "Programming Language :: Python :: 3.13" in classifiers
    assert "Programming Language :: Python :: 3.14" not in classifiers

    contracts = (ROOT / "docs" / "reference" / "public-contracts.md").read_text(encoding="utf-8")
    assert "CPython 3.11, 3.12, and 3.13 are blocking" in contracts
    assert "CPython 3.14 is exploratory" in contracts

    ci = (ROOT / ".github" / "workflows" / "ci-core.yml").read_text(encoding="utf-8")
    assert 'python-version: ["3.11", "3.12", "3.13"]' in ci
    assert "python-version: ${{ matrix.python-version }}" in ci
    assert 'python-version: ["3.11", "3.12", "3.13", "3.14"]' not in ci


def test_ae_version_is_the_single_authoritative_literal(tmp_path: Path) -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "version" not in data["project"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "ae.__version__"}
    assert read_project_version(PYPROJECT) == "0.1.8a1"

    root = tmp_path / "project"
    (root / "src" / "ae").mkdir(parents=True)
    copied = root / "pyproject.toml"
    shutil.copy2(PYPROJECT, copied)
    shutil.copy2(ROOT / "src" / "ae" / "__init__.py", root / "src" / "ae" / "__init__.py")
    source = write_project_version(copied, "0.1.8a2.dev0")
    assert source == root / "src" / "ae" / "__init__.py"
    assert read_project_version(copied) == "0.1.8a2.dev0"
    assert "0.1.8a1" not in source.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("marker", "rule"),
    [
        ("https://gitea.core.home.arpa/team/k1s", "internal-gitea"),
        ("/home/alice/git/k1s", "private-home"),
        ("checkout k1s-workerbee-private now", "private-checkout"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "credential-material"),
        ("/tmp/k1s-rc1-deadbeef-evidence", "generated-evidence"),  # noqa: S108
        ("Mirror (Codeberg)", "legacy-codeberg-default"),
    ],
)
def test_public_marker_scanner_rejects_private_markers(marker: str, rule: str) -> None:
    findings = scan_text("docs/bad.md", marker)
    assert [finding.rule for finding in findings] == [rule]


def test_public_marker_scanner_allows_named_integration_and_public_lab_fixtures() -> None:
    assert not scan_text(
        ".github/workflows/ci-core.yml",
        "https://gitea.core.home.arpa/ integration only",
    )
    assert not scan_text("docs/reference/lab.md", "https://api.home.arpa:8443")
    assert not scan_text("scripts/lab/vm/run.sh", "/home/ae/k1s")
    assert not scan_text("CHANGELOG.md", "Mirror (Codeberg) was retired")


def test_current_public_source_has_no_unapproved_markers() -> None:
    assert scan_source(ROOT) == []


def test_public_contract_inventory_uses_tested_status_vocabulary() -> None:
    text = (ROOT / "docs" / "reference" / "public-contracts.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("|") and "---" not in line]
    statuses = {
        columns[1].strip()
        for line in rows[1:]
        if len(columns := line.strip("|").split("|")) >= 3 and columns[1].strip() != "Status"
    }
    assert statuses == {"stable", "experimental"}
    assert "Install releases as `k1s-core`; `ae` is not the distribution name." in text
    assert "`import ae`" in text
    assert "`k1s --help`" in text
