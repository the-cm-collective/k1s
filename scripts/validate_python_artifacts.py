#!/usr/bin/env python3
"""Validate k1s-core Python artifacts and their exact-byte manifest."""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import io
import json
import stat
import tarfile
import tomllib
import zipfile
from collections import Counter
from email.message import Message
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import cast

try:
    from scripts.check_public_markers import scan_archive
    from scripts.project_version import read_project_version
except ModuleNotFoundError:  # direct execution from scripts/
    from check_public_markers import scan_archive  # type: ignore[import-not-found,no-redef]
    from project_version import read_project_version  # type: ignore[import-not-found,no-redef]


DIST_NAME = "k1s-core"
DIST_MODULE = "k1s_core"
LEGACY_DIST_NAME = "k1s-workerbee-runtime"
EXPECTED_CONSOLES = {
    "ae": "ae.cli.__main__:main",
    "ae-rotate-certs": "ae.cli.rotate_certs:main",
    "k1s": "ae.kctl.__main__:main",
    "ae-node": "ae.node.server:main",
    "ae-gateway": "ae.gateway.__main__:main",
    "ae-worker-stub": "ae.worker_stub.__main__:main",
}
RUNTIME_DATA_GLOBS = {
    "ae/resources/sql/apishim": ("*.sql", 13),
    "ae/resources/sql/controller": ("*.sql", 64),
    "ae/resources/observability": ("*.html", 4),
    "ae/resources/ingress": ("*.txt", 1),
    "ae/resources/profiles": ("*.json", 1),
    "ae/observability/static/vendor": ("*.css", 1),
    "ae/observability/static/vendor#js": ("*.js", 2),
}
PROFILE_CATALOG_MEMBER = "ae/resources/profiles/profile-catalog-v1.json"
MAX_PROFILE_CATALOG_BYTES = 64 * 1024
SDIST_ROOT_FILES = {"LICENSE", "README.md", "pyproject.toml", "requirements.in"}
# Setuptools' standard sdist file list includes tests/test*.py. Keep the exact
# source-only member explicit; it is forbidden from the runtime wheel.
SDIST_SOURCE_ONLY_FILES = {"tests/test_apishim_env.py"}
SDIST_SOURCE_ONLY_IDENTITIES = {
    "tests/test_apishim_env.py": (
        823,
        "650f6d4806e5b08f590d1da71ed0b9b38d73c8f05c7945f07e6e4a716d998bbb",
        "2469965183ea9d90e9c0c5c2ba305ccf0068f9d4",
    )
}


class _CaseConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _validated_member_name(name: str) -> PurePosixPath:
    """Return a safe, normalized relative archive path or fail closed."""
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"unsafe archive member path: {name!r}")
    candidate = name[:-1] if name.endswith("/") else name
    if not candidate or candidate.startswith("/") or "//" in candidate:
        raise ValueError(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(candidate)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive member path: {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"unsafe archive member path: {name!r}")
    if path.as_posix() != candidate:
        raise ValueError(f"non-canonical archive member path: {name!r}")
    return path


def _require_unique_names(names: list[str], *, artifact: Path) -> None:
    duplicates = sorted(name for name, count in Counter(names).items() if count != 1)
    if duplicates:
        raise ValueError(f"artifact contains duplicate members in {artifact}: {duplicates}")


def _profile_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"profile catalog contains duplicate key: {key!r}")
        result[key] = value
    return result


def _validate_profile_catalog_bytes(payload: bytes, *, artifact: Path) -> None:
    if not payload or len(payload) > MAX_PROFILE_CATALOG_BYTES:
        raise ValueError(f"profile catalog size is invalid in {artifact}")
    if payload.startswith(b"\xef\xbb\xbf") or b"\x00" in payload or b"\r" in payload:
        raise ValueError(f"profile catalog encoding is not canonical in {artifact}")
    try:
        text = payload.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_profile_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"profile catalog is not canonical JSON in {artifact}") from exc
    canonical = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if text != canonical or type(value) is not dict:
        raise ValueError(f"profile catalog bytes are not canonical in {artifact}")
    catalog = cast(dict[str, object], value)
    if set(catalog) != {"apiVersion", "kind", "mutating", "profiles", "schemaVersion"}:
        raise ValueError(f"profile catalog schema keys differ in {artifact}")
    if (
        catalog["apiVersion"] != "k1s.dev/v1alpha1"
        or catalog["kind"] != "ProfileCatalog"
        or catalog["mutating"] is not False
        or type(catalog["schemaVersion"]) is not int
        or catalog["schemaVersion"] != 1
        or type(catalog["profiles"]) is not list
    ):
        raise ValueError(f"profile catalog identity differs in {artifact}")
    profiles = cast(list[object], catalog["profiles"])
    names = [profile.get("name") if type(profile) is dict else None for profile in profiles]
    if names != ["local", "core", "edge-gateway", "node", "edge-dev", "core-ha"]:
        raise ValueError(f"profile catalog role order differs in {artifact}")


def _validate_exact_source_identity(
    payload: bytes,
    *,
    name: str,
    expected_size: int,
    expected_sha256: str,
    expected_git_blob: str,
) -> None:
    git_header = f"blob {len(payload)}\0".encode()
    git_blob = hashlib.sha1(git_header + payload, usedforsecurity=False).hexdigest()
    sha256 = hashlib.sha256(payload).hexdigest()
    if len(payload) != expected_size or sha256 != expected_sha256 or git_blob != expected_git_blob:
        raise ValueError(
            f"sdist-only source identity mismatch for {name}: "
            f"size={len(payload)} sha256={sha256} git_blob={git_blob}"
        )


def _source_payloads(pyproject: Path) -> tuple[set[str], set[str]]:
    """Return exact wheel payload and runtime-data members from committed source."""
    source_root = pyproject.parent / "src"
    ae_root = source_root / "ae"
    python_members = {
        path.relative_to(source_root).as_posix() for path in ae_root.rglob("*.py") if path.is_file()
    }
    runtime_data: set[str] = set()
    for owner, (pattern, expected_count) in RUNTIME_DATA_GLOBS.items():
        relative_owner = owner.removesuffix("#js")
        directory = source_root / relative_owner
        members = {
            path.relative_to(source_root).as_posix()
            for path in directory.glob(pattern)
            if path.is_file()
        }
        if len(members) != expected_count:
            raise ValueError(
                f"runtime resource inventory mismatch for {relative_owner}/{pattern}: "
                f"expected {expected_count}, found {len(members)}"
            )
        runtime_data.update(members)
    if len(runtime_data) != 86:
        raise ValueError(
            f"runtime resource inventory must contain 86 files, found {len(runtime_data)}"
        )
    profile_catalog = source_root / PROFILE_CATALOG_MEMBER
    _validate_profile_catalog_bytes(
        profile_catalog.read_bytes(),
        artifact=profile_catalog,
    )
    return python_members | runtime_data, runtime_data


def _validate_entry_points(raw: str, *, artifact: Path) -> None:
    parser = _CaseConfigParser(interpolation=None)
    parser.read_string(raw)
    if set(parser.sections()) != {"console_scripts"}:
        raise ValueError(f"unexpected entry-point groups in {artifact}: {parser.sections()}")
    actual = dict(parser.items("console_scripts"))
    if actual != EXPECTED_CONSOLES:
        raise ValueError(f"console entry points mismatch in {artifact}: {actual}")


def _validate_wheel_record(
    archive: zipfile.ZipFile, *, names: list[str], dist_info: str, artifact: Path
) -> None:
    record_name = f"{dist_info}/RECORD"
    if record_name not in names:
        raise ValueError(f"wheel is missing RECORD: {artifact}")
    rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    if any(len(row) != 3 for row in rows):
        raise ValueError(f"wheel RECORD contains a malformed row: {artifact}")
    record_paths = [row[0] for row in rows]
    _require_unique_names(record_paths, artifact=artifact)
    if set(record_paths) != set(names) or len(record_paths) != len(names):
        missing = sorted(set(names) - set(record_paths))
        extra = sorted(set(record_paths) - set(names))
        raise ValueError(f"wheel RECORD coverage mismatch: missing={missing}, extra={extra}")
    for member_name, encoded_hash, encoded_size in rows:
        _validated_member_name(member_name)
        if member_name == record_name:
            if encoded_hash or encoded_size:
                raise ValueError(f"wheel RECORD row must have empty hash and size: {artifact}")
            continue
        payload = archive.read(member_name)
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        if encoded_hash != f"sha256={digest}" or encoded_size != str(len(payload)):
            raise ValueError(f"wheel RECORD hash/size mismatch for {member_name}: {artifact}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_identity(pyproject: Path) -> tuple[str, str]:
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    return str(project["name"]), read_project_version(pyproject)


def _validate_public_markers(path: Path) -> None:
    findings = scan_archive(path)
    if findings:
        rendered = ", ".join(
            f"{finding.rule}:{finding.path}:{finding.line}" for finding in findings[:10]
        )
        raise ValueError(f"artifact contains non-public markers: {rendered}")


def _validate_metadata(metadata: Message, *, version: str, artifact: Path) -> None:
    if metadata["Name"] != DIST_NAME or metadata["Version"] != version:
        identity = f"Name={metadata['Name']!r} Version={metadata['Version']!r}"
        raise ValueError(f"artifact identity mismatch in {artifact}: {identity}")
    if metadata["Requires-Python"] != ">=3.11":
        raise ValueError(f"artifact Python floor mismatch in {artifact}")
    if metadata["License-Expression"] != "Apache-2.0":
        raise ValueError(f"artifact SPDX license mismatch in {artifact}")

    expected_urls = {
        "Homepage, https://github.com/the-cm-collective/k1s",
        "Repository, https://github.com/the-cm-collective/k1s",
        "Documentation, https://github.com/the-cm-collective/k1s/tree/main/docs",
        "Issues, https://github.com/the-cm-collective/k1s/issues",
        "Changelog, https://github.com/the-cm-collective/k1s/blob/main/CHANGELOG.md",
    }
    if set(metadata.get_all("Project-URL", [])) != expected_urls:
        raise ValueError(f"artifact project URLs mismatch in {artifact}")

    classifiers = set(metadata.get_all("Classifier", []))
    required_python = {f"Programming Language :: Python :: 3.{minor}" for minor in (11, 12, 13)}
    if not required_python.issubset(classifiers):
        raise ValueError(f"artifact supported-Python classifiers mismatch in {artifact}")
    if "Programming Language :: Python :: 3.14" in classifiers:
        raise ValueError(f"artifact must not advertise exploratory Python 3.14: {artifact}")


def _validate_wheel(path: Path, *, version: str, pyproject: Path) -> None:
    expected_payload, _ = _source_payloads(pyproject)
    expected_dist_info = f"{DIST_MODULE}-{version}.dist-info"
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _require_unique_names(names, artifact=path)
        for info in archive.infolist():
            member = _validated_member_name(info.filename)
            mode_type = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or mode_type not in {0, stat.S_IFREG}:
                raise ValueError(f"wheel contains a non-regular member: {info.filename}")
            if member.parts[:1] == ("github",):
                raise ValueError(f"wheel contains forbidden top-level github namespace: {path}")

        dist_info_roots = {
            PurePosixPath(name).parts[0]
            for name in names
            if PurePosixPath(name).parts[0].endswith(".dist-info")
        }
        if dist_info_roots != {expected_dist_info}:
            raise ValueError(
                f"wheel must contain only {expected_dist_info}, found {sorted(dist_info_roots)}"
            )
        payload = {name for name in names if not name.startswith(f"{expected_dist_info}/")}
        if payload != expected_payload:
            missing = sorted(expected_payload - payload)
            extra = sorted(payload - expected_payload)
            raise ValueError(f"wheel payload mismatch: missing={missing}, extra={extra}")

        required_metadata = {
            f"{expected_dist_info}/METADATA",
            f"{expected_dist_info}/WHEEL",
            f"{expected_dist_info}/entry_points.txt",
            f"{expected_dist_info}/top_level.txt",
            f"{expected_dist_info}/RECORD",
        }
        missing_metadata = sorted(required_metadata - set(names))
        if missing_metadata:
            raise ValueError(f"wheel is missing canonical dist-info files: {missing_metadata}")
        metadata_name = f"{expected_dist_info}/METADATA"
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        _validate_metadata(metadata, version=version, artifact=path)
        _validate_entry_points(
            archive.read(f"{expected_dist_info}/entry_points.txt").decode("utf-8"),
            artifact=path,
        )
        if archive.read(f"{expected_dist_info}/top_level.txt").decode("utf-8").splitlines() != [
            "ae"
        ]:
            raise ValueError(f"wheel top-level package metadata mismatch: {path}")
        _validate_wheel_record(
            archive,
            names=names,
            dist_info=expected_dist_info,
            artifact=path,
        )
        profile_payload = archive.read(PROFILE_CATALOG_MEMBER)
        source_payload = (pyproject.parent / "src" / PROFILE_CATALOG_MEMBER).read_bytes()
        if profile_payload != source_payload:
            raise ValueError(f"wheel profile catalog differs from source: {path}")
        _validate_profile_catalog_bytes(profile_payload, artifact=path)


def _validate_sdist(path: Path, *, version: str, pyproject: Path) -> None:
    if set(SDIST_SOURCE_ONLY_IDENTITIES) != SDIST_SOURCE_ONLY_FILES:
        raise ValueError("sdist-only source names and identities do not match")
    expected_payload, _ = _source_payloads(pyproject)
    root = f"{DIST_MODULE}-{version}"
    expected_files = (
        SDIST_ROOT_FILES | SDIST_SOURCE_ONLY_FILES | {f"src/{name}" for name in expected_payload}
    )
    for name, identity in SDIST_SOURCE_ONLY_IDENTITIES.items():
        _validate_exact_source_identity(
            (pyproject.parent / name).read_bytes(),
            name=name,
            expected_size=identity[0],
            expected_sha256=identity[1],
            expected_git_blob=identity[2],
        )
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        _require_unique_names(names, artifact=path)
        relative_files: set[str] = set()
        for member in members:
            member_path = _validated_member_name(member.name)
            if member_path.parts[:1] != (root,):
                raise ValueError(f"sdist member is outside canonical root {root}: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"sdist contains a link or special member: {member.name}")
            relative_path = PurePosixPath(*member_path.parts[1:])
            if relative_path.parts[:1] in {("test",), ("tests",)}:
                if member.isdir() and relative_path.parts != ("tests",):
                    raise ValueError(f"sdist contains a forbidden test directory: {member.name}")
                if member.isfile() and relative_path.as_posix() not in SDIST_SOURCE_ONLY_FILES:
                    raise ValueError(f"sdist contains a forbidden test source: {member.name}")
            if member.isfile():
                relative = relative_path.as_posix()
                relative_files.add(relative)
                if PurePosixPath(relative).parts[:2] == ("src", "github"):
                    raise ValueError(f"sdist contains forbidden top-level github namespace: {path}")

        generated_files = {
            name
            for name in relative_files
            if name == "PKG-INFO"
            or name == "setup.cfg"
            or name.startswith(f"src/{DIST_MODULE}.egg-info/")
        }
        actual_payload = relative_files - generated_files
        if actual_payload != expected_files:
            missing = sorted(expected_files - actual_payload)
            extra = sorted(actual_payload - expected_files)
            raise ValueError(f"sdist payload mismatch: missing={missing}, extra={extra}")
        for name, identity in SDIST_SOURCE_ONLY_IDENTITIES.items():
            source_member = archive.extractfile(f"{root}/{name}")
            if source_member is None:
                raise ValueError(f"unable to read sdist-only source member: {name}")
            _validate_exact_source_identity(
                source_member.read(),
                name=name,
                expected_size=identity[0],
                expected_sha256=identity[1],
                expected_git_blob=identity[2],
            )
        profile_member = archive.extractfile(f"{root}/src/{PROFILE_CATALOG_MEMBER}")
        if profile_member is None:
            raise ValueError(f"unable to read sdist profile catalog: {path}")
        profile_payload = profile_member.read()
        source_payload = (pyproject.parent / "src" / PROFILE_CATALOG_MEMBER).read_bytes()
        if profile_payload != source_payload:
            raise ValueError(f"sdist profile catalog differs from source: {path}")
        _validate_profile_catalog_bytes(profile_payload, artifact=path)
        pkg_info_name = f"{root}/PKG-INFO"
        if pkg_info_name not in names:
            raise ValueError(f"sdist is missing root PKG-INFO: {path}")
        payload_handle = archive.extractfile(pkg_info_name)
        if payload_handle is None:
            raise ValueError(f"unable to read sdist PKG-INFO: {path}")
        metadata = Parser().parsestr(payload_handle.read().decode("utf-8"))
        _validate_metadata(metadata, version=version, artifact=path)


def validate(dist: Path, *, pyproject: Path) -> list[Path]:
    name, version = _project_identity(pyproject)
    if name != DIST_NAME:
        raise ValueError(f"unexpected project name: {name}")
    legacy_builder = pyproject.parent / "scripts" / "build_workerbee_runtime_wheel.py"
    if legacy_builder.exists():
        raise ValueError(f"obsolete alternate-distribution builder still exists: {legacy_builder}")
    artifacts = sorted(path for path in dist.iterdir() if path.is_file())
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(artifacts) != 2:
        found = [path.name for path in artifacts]
        raise ValueError(f"expected exactly one wheel and one sdist in {dist}, found {found}")
    _validate_wheel(wheels[0], version=version, pyproject=pyproject)
    _validate_sdist(sdists[0], version=version, pyproject=pyproject)
    for artifact in artifacts:
        _validate_public_markers(artifact)
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
