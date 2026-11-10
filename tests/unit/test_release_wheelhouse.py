from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from scripts.prepare_release_wheelhouse import (
    WheelhousePolicyError,
    load_contract,
    load_lock,
    safe_extract_sdist,
    validate_contract_against_lock,
)

ROOT = Path(__file__).resolve().parents[2]


def test_source_wheel_contract_is_exact_and_matches_release_lock() -> None:
    contract = load_contract(ROOT / "requirements-release-sources.json")
    locked = load_lock(ROOT / "requirements-release.lock")
    ordinary = validate_contract_against_lock(contract, locked)

    assert contract.source_date_epoch == 1_793_664_000
    assert len(contract.packages) == 1
    package = contract.packages[0]
    assert package.name == "nkeys"
    assert package.version == "0.2.1"
    assert package.source_url.startswith("https://files.pythonhosted.org/")
    assert package.source_sha256 == (
        "3a201dcd203d8bb05ba2884d441b2c92918b2a537a10d324e73738887dde9da3"
    )
    assert package.wheel_sha256 == (
        "b2f63a5ca34c36d978eafc71a2f105d2a5a037fc1352e8329269bfcefb294981"
    )
    assert len(ordinary) == 23
    assert not any(item.name == "nkeys" for item in ordinary)


def test_publication_helper_preserves_hash_and_offline_build_boundaries() -> None:
    source = (ROOT / "scripts" / "prepare_release_wheelhouse.py").read_text(encoding="utf-8")
    assert '"--only-binary=:all:"' in source
    assert '"--require-hashes"' in source
    assert '"--no-isolation"' in source
    assert '"PIP_NO_INDEX": "1"' in source
    assert "normalize_wheel(" in source
    assert "verify_wheelhouse(staging, requirements_lock)" in source


def test_contract_rejects_unreviewed_keys(tmp_path: Path) -> None:
    value = json.loads((ROOT / "requirements-release-sources.json").read_text(encoding="utf-8"))
    value["unreviewed"] = True
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(WheelhousePolicyError, match="contract shape differs"):
        load_contract(path)


def _archive(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for info, payload in members:
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_safe_source_extraction_accepts_regular_members(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    info = tarfile.TarInfo("example-1.0/package.py")
    payload = b"VALUE = 1\n"
    _archive(archive, [(info, payload)])

    root = safe_extract_sdist(archive, tmp_path / "out", expected_root="example-1.0")

    extracted = root / "package.py"
    assert extracted.read_bytes() == payload
    assert hashlib.sha256(extracted.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "root/../escape", "C:/escape"])
def test_safe_source_extraction_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "source.tar.gz"
    _archive(archive, [(tarfile.TarInfo(name), b"unsafe")])
    with pytest.raises(WheelhousePolicyError, match="unsafe path"):
        safe_extract_sdist(archive, tmp_path / "out", expected_root="root")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_safe_source_extraction_rejects_links_and_devices(tmp_path: Path, kind: bytes) -> None:
    archive = tmp_path / "source.tar.gz"
    info = tarfile.TarInfo("example-1.0/unsafe")
    info.type = kind
    info.linkname = "target"
    _archive(archive, [(info, b"")])
    with pytest.raises(WheelhousePolicyError, match="link or special"):
        safe_extract_sdist(archive, tmp_path / "out", expected_root="example-1.0")
