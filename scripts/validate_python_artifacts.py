#!/usr/bin/env python3
"""Validate k1s-core Python artifacts and their exact-byte manifest."""

from __future__ import annotations

import argparse
import hashlib
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_identity(pyproject: Path) -> tuple[str, str]:
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    return str(project["name"]), str(project["version"])


def _validate_wheel(path: Path, *, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if any(PurePosixPath(name).parts[:1] == ("github",) for name in names):
            raise ValueError(f"wheel contains forbidden top-level github namespace: {path}")
        required = "ae/runtime/cri/api/runtime/v1/gogo_pb2.py"
        if required not in names:
            raise ValueError(f"wheel is missing package-local CRI dependency: {required}")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError(f"wheel must contain exactly one METADATA file: {path}")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        if metadata["Name"] != "k1s-core" or metadata["Version"] != version:
            identity = f"Name={metadata['Name']!r} Version={metadata['Version']!r}"
            raise ValueError(f"wheel identity mismatch: {identity}")


def _validate_sdist(path: Path, *, version: str) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        relative_parts = [PurePosixPath(name).parts[1:] for name in names]
        if any(parts[:2] == ("src", "github") for parts in relative_parts):
            raise ValueError(f"sdist contains forbidden top-level github namespace: {path}")
        required = ("src", "ae", "runtime", "cri", "api", "runtime", "v1", "gogo_pb2.py")
        if required not in relative_parts:
            raise ValueError(f"sdist is missing package-local CRI dependency: {required}")
        pkg_info_names = [name for name in names if PurePosixPath(name).name == "PKG-INFO"]
        if not pkg_info_names:
            raise ValueError(f"sdist is missing PKG-INFO: {path}")
        member = archive.extractfile(pkg_info_names[0])
        if member is None:
            raise ValueError(f"unable to read sdist PKG-INFO: {path}")
        metadata = Parser().parsestr(member.read().decode("utf-8"))
        if metadata["Name"] != "k1s-core" or metadata["Version"] != version:
            identity = f"Name={metadata['Name']!r} Version={metadata['Version']!r}"
            raise ValueError(f"sdist identity mismatch: {identity}")


def validate(dist: Path, *, pyproject: Path) -> list[Path]:
    name, version = _project_identity(pyproject)
    if name != "k1s-core":
        raise ValueError(f"unexpected project name: {name}")
    artifacts = sorted(path for path in dist.iterdir() if path.is_file())
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(artifacts) != 2:
        found = [path.name for path in artifacts]
        raise ValueError(f"expected exactly one wheel and one sdist in {dist}, found {found}")
    _validate_wheel(wheels[0], version=version)
    _validate_sdist(sdists[0], version=version)
    return artifacts


def _write_manifest(path: Path, artifacts: list[Path]) -> None:
    lines = [f"{_sha256(artifact)}  dist/{artifact.name}" for artifact in artifacts]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _verify_manifest(path: Path, artifacts: list[Path]) -> None:
    expected = {f"dist/{artifact.name}": _sha256(artifact) for artifact in artifacts}
    actual: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, filename = line.split("  ", 1)
        actual[filename] = digest
    if actual != expected:
        raise ValueError(f"artifact manifest mismatch: expected={expected}, actual={actual}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-manifest", type=Path)
    mode.add_argument("--verify-manifest", type=Path)
    args = parser.parse_args()

    artifacts = validate(args.dist, pyproject=args.pyproject)
    if args.write_manifest is not None:
        _write_manifest(args.write_manifest, artifacts)
    else:
        _verify_manifest(args.verify_manifest, artifacts)
    for artifact in artifacts:
        print(f"{_sha256(artifact)}  {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
