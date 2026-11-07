from __future__ import annotations

import hashlib
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.check_public_markers import scan_archive
from scripts.validate_installed_python_artifact import (
    offline_environment,
    verify_wheelhouse,
)

ROOT = Path(__file__).resolve().parents[2]


def test_release_wheel_lock_is_complete_and_content_addressed() -> None:
    lines = [
        line
        for line in (ROOT / "requirements-release.lock").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(lines) == 24
    assert all(
        " --hash=sha256:" in line and "  # " in line and line.endswith(".whl") for line in lines
    )
    assert any(line.startswith("nkeys==0.2.1 ") for line in lines)
    assert not any(line.lower().startswith("k1s-core") for line in lines)


def test_wheelhouse_verification_rejects_missing_extra_and_changed_wheels(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "example-1.0-py3-none-any.whl"
    wheel.write_bytes(b"reviewed wheel")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = tmp_path / "requirements-release.lock"
    lock.write_text(
        f"example==1.0 --hash=sha256:{digest}  # {wheel.name}\n",
        encoding="utf-8",
    )
    verify_wheelhouse(wheelhouse, lock)

    wheel.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        verify_wheelhouse(wheelhouse, lock)


def test_offline_environment_removes_index_proxy_and_checkout_paths(tmp_path: Path) -> None:
    env = offline_environment(
        home=tmp_path / "home",
        temporary=tmp_path / "tmp",
        guard=tmp_path / "guard",
    )
    assert env["PIP_NO_INDEX"] == "1"
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert env["AE_RUNTIME_BACKEND"] == "stub"
    assert env["PYTHONPATH"] == str(tmp_path / "guard")
    assert not any("proxy" in key.lower() for key in env)
    assert "PIP_INDEX_URL" not in env
    assert "PIP_EXTRA_INDEX_URL" not in env


def test_archive_scanner_rejects_private_markers_in_wheel_and_sdist(tmp_path: Path) -> None:
    wheel = tmp_path / "k1s_core-0.1.8a1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ae/leak.py", 'SOURCE = "/home/alice/git/private"\n')
    assert {finding.rule for finding in scan_archive(wheel)} == {"private-home"}

    payload = tmp_path / "README.md"
    payload.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    sdist = tmp_path / "k1s_core-0.1.8a1.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(payload, arcname="k1s_core-0.1.8a1/README.md")
    assert {finding.rule for finding in scan_archive(sdist)} == {"credential-material"}
