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
import os
import re
import stat
import tarfile
import tomllib
import unicodedata
import zipfile
from collections import Counter
from datetime import UTC, datetime
from email.message import Message
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import cast

try:
    from scripts.check_public_markers import scan_archive
    from scripts.normalize_python_sdist import GENERATED_FILES, load_source_modes
    from scripts.project_version import read_project_version
except ModuleNotFoundError:  # direct execution from scripts/
    from check_public_markers import scan_archive  # type: ignore[import-not-found,no-redef]
    from normalize_python_sdist import (  # type: ignore[import-not-found,no-redef]
        GENERATED_FILES,
        load_source_modes,
    )
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
    "ae/resources/profiles": ("*.json", 2),
    "ae/observability/static/vendor": ("*.css", 1),
    "ae/observability/static/vendor#js": ("*.js", 2),
}
PROFILE_CATALOG_MEMBER = "ae/resources/profiles/profile-catalog-v1.json"
WORKERBEE_ADAPTER_MEMBER = "ae/resources/profiles/workerbee-adapter-v1.json"
MAX_PROFILE_CATALOG_BYTES = 64 * 1024
MAX_WORKERBEE_ADAPTER_BYTES = 64 * 1024
CANONICAL_SOURCE_DATE_EPOCH = 1793664000
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_ENTRIES = 25_000
MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_ROW_BYTES = 16 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40}")
PROTECTED_SOURCE_PATHS = {
    ".env",
    ".envrc",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    "ops/systemd/ae-ha-core.env",
    "ops/systemd/ae.env",
}
BUILD_DISTRIBUTIONS = {
    "build",
    "packaging",
    "pyproject-hooks",
    "setuptools",
    "twine",
    "wheel",
}
VALIDATION_KEYS = {
    "artifactValidator",
    "installedArtifact",
    "publicMarkers",
    "twine",
}
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


def _validate_workerbee_adapter_bytes(payload: bytes, *, artifact: Path) -> None:
    if not payload or len(payload) > MAX_WORKERBEE_ADAPTER_BYTES:
        raise ValueError(f"WorkerBee adapter contract size is invalid in {artifact}")
    if payload.startswith(b"\xef\xbb\xbf") or b"\x00" in payload or b"\r" in payload:
        raise ValueError(f"WorkerBee adapter contract encoding is not canonical in {artifact}")
    try:
        text = payload.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_profile_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"WorkerBee adapter contract is not canonical JSON in {artifact}") from exc
    canonical = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if text != canonical or type(value) is not dict:
        raise ValueError(f"WorkerBee adapter contract bytes are not canonical in {artifact}")
    contract = cast(dict[str, object], value)
    if set(contract) != {
        "apiVersion",
        "evidenceApiVersion",
        "kind",
        "operations",
        "qualificationPosture",
        "schemaVersion",
    }:
        raise ValueError(f"WorkerBee adapter contract keys differ in {artifact}")
    if (
        contract["apiVersion"] != "k1s.dev/workerbee-profile-adapter/v1"
        or contract["evidenceApiVersion"] != "k1s.dev/workerbee-profile-evidence/v1"
        or contract["kind"] != "WorkerBeeProfileAdapterContract"
        or contract["qualificationPosture"] != "source-validated/unqualified"
        or type(contract["schemaVersion"]) is not int
        or contract["schemaVersion"] != 1
        or type(contract["operations"]) is not list
    ):
        raise ValueError(f"WorkerBee adapter contract identity differs in {artifact}")
    operations = cast(list[object], contract["operations"])
    if len(operations) != 6:
        raise ValueError(f"WorkerBee adapter operation count differs in {artifact}")
    expected = (
        (
            "list",
            [0],
            "forbidden",
            "k1s.dev/v1alpha1",
            "ProfileCatalog",
            "profile-list-json",
            False,
            1,
        ),
        (
            "doctor",
            [0, 1],
            "forbidden",
            "k1s.dev/v1alpha1",
            "ProfileDoctorReport",
            "profile-doctor-catalog-json",
            False,
            1,
        ),
        (
            "plan",
            [0],
            "forbidden",
            "k1s.dev/v1alpha1",
            "ProfilePlan",
            "profile-plan-json",
            False,
            1,
        ),
        (
            "status",
            [0],
            "forbidden",
            None,
            "PortableLifecycleResult",
            "profile-status-portable-json",
            False,
            None,
        ),
        (
            "logs",
            [0],
            "forbidden",
            "k1s.dev/v1alpha1",
            "ProfileLogSnapshot",
            "profile-logs-owned-json",
            False,
            None,
        ),
        (
            "bootstrap",
            [0],
            "optional",
            None,
            "ProfileBootstrapPublicProjection",
            "profile-bootstrap-create-json",
            True,
            None,
        ),
    )
    names: list[object] = []
    for descriptor, expected_identity in zip(operations, expected, strict=True):
        if type(descriptor) is not dict:
            raise ValueError(f"WorkerBee adapter operation is not an object in {artifact}")
        operation = cast(dict[str, object], descriptor)
        if set(operation) != {
            "acceptedExitCodes",
            "adapterMutating",
            "name",
            "nodeObservation",
            "sourceApiVersion",
            "sourceKind",
            "sourceMode",
            "sourceMutating",
            "sourceSchemaVersion",
        }:
            raise ValueError(f"WorkerBee adapter operation keys differ in {artifact}")
        if (
            type(operation["acceptedExitCodes"]) is not list
            or any(
                type(code) is not int for code in cast(list[object], operation["acceptedExitCodes"])
            )
            or operation["adapterMutating"] is not False
            or operation["nodeObservation"] not in {"forbidden", "optional"}
            or type(operation["name"]) is not str
            or type(operation["sourceKind"]) is not str
            or type(operation["sourceMode"]) is not str
            or type(operation["sourceMutating"]) is not bool
            or not (
                operation["sourceApiVersion"] is None or type(operation["sourceApiVersion"]) is str
            )
            or not (
                operation["sourceSchemaVersion"] is None
                or type(operation["sourceSchemaVersion"]) is int
            )
        ):
            raise ValueError(f"WorkerBee adapter operation identity differs in {artifact}")
        identity = (
            operation["name"],
            operation["acceptedExitCodes"],
            operation["nodeObservation"],
            operation["sourceApiVersion"],
            operation["sourceKind"],
            operation["sourceMode"],
            operation["sourceMutating"],
            operation["sourceSchemaVersion"],
        )
        if identity != expected_identity:
            raise ValueError(f"WorkerBee adapter operation literals differ in {artifact}")
        names.append(operation["name"])
    if names != ["list", "doctor", "plan", "status", "logs", "bootstrap"]:
        raise ValueError(f"WorkerBee adapter operation order differs in {artifact}")


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
    if len(runtime_data) != 87:
        raise ValueError(
            f"runtime resource inventory must contain 87 files, found {len(runtime_data)}"
        )
    profile_catalog = source_root / PROFILE_CATALOG_MEMBER
    _validate_profile_catalog_bytes(
        profile_catalog.read_bytes(),
        artifact=profile_catalog,
    )
    workerbee_adapter = source_root / WORKERBEE_ADAPTER_MEMBER
    _validate_workerbee_adapter_bytes(
        workerbee_adapter.read_bytes(),
        artifact=workerbee_adapter,
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


def _zip_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    value = datetime.fromtimestamp(epoch, tz=UTC)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def _validate_wheel(
    path: Path,
    *,
    version: str,
    pyproject: Path,
    epoch: int = CANONICAL_SOURCE_DATE_EPOCH,
) -> None:
    expected_payload, _ = _source_payloads(pyproject)
    expected_dist_info = f"{DIST_MODULE}-{version}.dist-info"
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"wheel exceeds its compressed size ceiling: {path}")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) > MAX_ARCHIVE_MEMBERS or names != sorted(names, key=str.encode):
            raise ValueError(f"wheel member count/order is not canonical: {path}")
        if archive.comment:
            raise ValueError(f"wheel archive comment must be empty: {path}")
        _require_unique_names(names, artifact=path)
        for info in archive.infolist():
            member = _validated_member_name(info.filename)
            mode_type = stat.S_IFMT(info.external_attr >> 16)
            mode = info.external_attr >> 16
            if info.is_dir() or mode_type != stat.S_IFREG:
                raise ValueError(f"wheel contains a non-regular member: {info.filename}")
            if stat.S_IMODE(mode) != 0o644 or info.create_system != 3:
                raise ValueError(f"wheel member mode/system is not canonical: {info.filename}")
            if info.date_time != _zip_datetime(epoch):
                raise ValueError(f"wheel member timestamp is not canonical: {info.filename}")
            if info.extra or info.comment or info.flag_bits & 0x1:
                raise ValueError(f"wheel member metadata is not canonical: {info.filename}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError(f"wheel member compression is not canonical: {info.filename}")
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
        record_rows = list(
            csv.reader(io.StringIO(archive.read(f"{expected_dist_info}/RECORD").decode("utf-8")))
        )
        record_paths = [row[0] for row in record_rows]
        if record_paths[:-1] != sorted(record_paths[:-1], key=str.encode):
            raise ValueError(f"wheel RECORD rows are not canonical: {path}")
        if not record_paths or record_paths[-1] != f"{expected_dist_info}/RECORD":
            raise ValueError(f"wheel RECORD self-row is not last: {path}")
        profile_payload = archive.read(PROFILE_CATALOG_MEMBER)
        source_payload = (pyproject.parent / "src" / PROFILE_CATALOG_MEMBER).read_bytes()
        if profile_payload != source_payload:
            raise ValueError(f"wheel profile catalog differs from source: {path}")
        _validate_profile_catalog_bytes(profile_payload, artifact=path)
        adapter_payload = archive.read(WORKERBEE_ADAPTER_MEMBER)
        adapter_source = (pyproject.parent / "src" / WORKERBEE_ADAPTER_MEMBER).read_bytes()
        if adapter_payload != adapter_source:
            raise ValueError(f"wheel WorkerBee adapter contract differs from source: {path}")
        _validate_workerbee_adapter_bytes(adapter_payload, artifact=path)


def _validate_sdist(
    path: Path,
    *,
    version: str,
    pyproject: Path,
    source_members: Path | None = None,
    epoch: int = CANONICAL_SOURCE_DATE_EPOCH,
) -> None:
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
    source_modes = load_source_modes(source_members) if source_members is not None else None
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"sdist exceeds its compressed size ceiling: {path}")
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"sdist exceeds its member ceiling: {path}")
        names = [member.name for member in members]
        canonical_names = [name[:-1] if name.endswith("/") else name for name in names]
        if canonical_names != sorted(canonical_names, key=str.encode):
            raise ValueError(f"sdist member order is not canonical: {path}")
        _require_unique_names(names, artifact=path)
        relative_files: set[str] = set()
        for member in members:
            member_path = _validated_member_name(member.name)
            if member_path.parts[:1] != (root,):
                raise ValueError(f"sdist member is outside canonical root {root}: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"sdist contains a link or special member: {member.name}")
            if (
                member.mtime != epoch
                or member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
                or member.linkname
                or set(member.pax_headers) - {"path"}
            ):
                raise ValueError(f"sdist member metadata is not canonical: {member.name}")
            if member.isdir() and stat.S_IMODE(member.mode) != 0o755:
                raise ValueError(f"sdist directory mode is not canonical: {member.name}")
            relative_path = PurePosixPath(*member_path.parts[1:])
            if relative_path.parts[:1] in {("test",), ("tests",)}:
                if member.isdir() and relative_path.parts != ("tests",):
                    raise ValueError(f"sdist contains a forbidden test directory: {member.name}")
                if member.isfile() and relative_path.as_posix() not in SDIST_SOURCE_ONLY_FILES:
                    raise ValueError(f"sdist contains a forbidden test source: {member.name}")
            if member.isfile():
                relative = relative_path.as_posix()
                if source_modes is not None:
                    if relative in source_modes:
                        expected_mode = source_modes[relative]
                    elif relative in GENERATED_FILES:
                        expected_mode = 0o644
                    else:
                        raise ValueError(f"sdist contains an unowned member: {member.name}")
                    if stat.S_IMODE(member.mode) != expected_mode:
                        raise ValueError(f"sdist member mode is not Git-canonical: {member.name}")
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
        adapter_member = archive.extractfile(f"{root}/src/{WORKERBEE_ADAPTER_MEMBER}")
        if adapter_member is None:
            raise ValueError(f"unable to read sdist WorkerBee adapter contract: {path}")
        adapter_payload = adapter_member.read()
        adapter_source = (pyproject.parent / "src" / WORKERBEE_ADAPTER_MEMBER).read_bytes()
        if adapter_payload != adapter_source:
            raise ValueError(f"sdist WorkerBee adapter contract differs from source: {path}")
        _validate_workerbee_adapter_bytes(adapter_payload, artifact=path)
        pkg_info_name = f"{root}/PKG-INFO"
        if pkg_info_name not in names:
            raise ValueError(f"sdist is missing root PKG-INFO: {path}")
        payload_handle = archive.extractfile(pkg_info_name)
        if payload_handle is None:
            raise ValueError(f"unable to read sdist PKG-INFO: {path}")
        metadata = Parser().parsestr(payload_handle.read().decode("utf-8"))
        _validate_metadata(metadata, version=version, artifact=path)


def validate(
    dist: Path,
    *,
    pyproject: Path,
    source_members: Path | None = None,
    epoch: int = CANONICAL_SOURCE_DATE_EPOCH,
) -> list[Path]:
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
    _validate_wheel(wheels[0], version=version, pyproject=pyproject, epoch=epoch)
    _validate_sdist(
        sdists[0],
        version=version,
        pyproject=pyproject,
        source_members=source_members,
        epoch=epoch,
    )
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


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"JSON contains duplicate key: {key!r}")
        value[key] = item
    return value


def _read_canonical_json(payload: bytes, *, label: str) -> dict[str, object]:
    if (
        not payload
        or len(payload) > MAX_MANIFEST_BYTES
        or payload.startswith(b"\xef\xbb\xbf")
        or b"\x00" in payload
        or b"\r" in payload
    ):
        raise ValueError(f"{label} encoding is not canonical")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid canonical JSON") from exc
    if type(value) is not dict or payload != _canonical_json(value):
        raise ValueError(f"{label} is not canonical JSON")
    return cast(dict[str, object], value)


def _require_exact_keys(value: dict[str, object], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} schema keys differ")


def _require_sha256(value: object, *, label: str) -> str:
    if type(value) is not str or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical SHA-256")
    return value


def _require_nonnegative_int(value: object, *, label: str, ceiling: int) -> int:
    if type(value) is not int or not 0 <= value <= ceiling:
        raise ValueError(f"{label} is outside its integer range")
    return value


def _validated_manifest_path(name: object) -> str:
    if type(name) is not str:
        raise ValueError("member manifest path is not text")
    encoded = name.encode("utf-8", errors="strict")
    path = _validated_member_name(name)
    if (
        len(encoded) > 4096
        or unicodedata.normalize("NFC", name) != name
        or any(len(part.encode("utf-8")) > 255 for part in path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ValueError("member manifest path is not canonical UTF-8")
    return name


def _parse_member_manifest(payload: bytes, *, kind: str) -> list[dict[str, object]]:
    if (
        not payload
        or len(payload) > MAX_MANIFEST_BYTES
        or not payload.endswith(b"\n")
        or payload.startswith(b"\xef\xbb\xbf")
        or b"\x00" in payload
        or b"\r" in payload
    ):
        raise ValueError(f"{kind} member manifest encoding is not canonical")
    raw_rows = payload.splitlines(keepends=True)
    ceiling = MAX_SOURCE_ENTRIES if kind == "source" else MAX_ARCHIVE_MEMBERS
    if not raw_rows or len(raw_rows) > ceiling:
        raise ValueError(f"{kind} member manifest count exceeds policy")
    rows: list[dict[str, object]] = []
    paths: list[str] = []
    folded: set[str] = set()
    total_file_bytes = 0
    for raw_row in raw_rows:
        if len(raw_row) > MAX_MANIFEST_ROW_BYTES or not raw_row.endswith(b"\n"):
            raise ValueError(f"{kind} member manifest row exceeds policy")
        try:
            text = raw_row.decode("utf-8", errors="strict")
            parsed = json.loads(text, object_pairs_hook=_object_without_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{kind} member manifest contains invalid JSON") from exc
        if type(parsed) is not dict:
            raise ValueError(f"{kind} member manifest row is not an object")
        row = cast(dict[str, object], parsed)
        if raw_row != _canonical_json(row):
            raise ValueError(f"{kind} member manifest row is not canonical JSON")
        _require_exact_keys(
            row,
            {"gitMode", "mode", "path", "sha256", "size", "type"},
            label=f"{kind} member manifest row",
        )
        path = _validated_manifest_path(row["path"])
        folded_path = path.casefold()
        if folded_path in folded:
            raise ValueError(f"{kind} member manifest contains a path collision")
        folded.add(folded_path)
        paths.append(path)
        entry_type = row["type"]
        mode = row["mode"]
        git_mode = row["gitMode"]
        size = _require_nonnegative_int(
            row["size"],
            label=f"{kind} member size",
            ceiling=MAX_SOURCE_FILE_BYTES if kind == "source" else MAX_ARCHIVE_BYTES,
        )
        if entry_type == "directory":
            if kind == "wheel" or git_mode is not None or mode != "0755":
                raise ValueError(f"{kind} member directory schema differs")
            if row["sha256"] is not None or size != 0:
                raise ValueError(f"{kind} member directory identity differs")
        elif entry_type == "file":
            _require_sha256(row["sha256"], label=f"{kind} member digest")
            if kind == "source":
                if git_mode not in {"100644", "100755"}:
                    raise ValueError("source member Git mode differs")
                expected_mode = "0755" if git_mode == "100755" else "0644"
                if mode != expected_mode or path in PROTECTED_SOURCE_PATHS:
                    raise ValueError("source member mode or protected-path policy differs")
            else:
                if git_mode is not None:
                    raise ValueError(f"{kind} member must not claim a Git mode")
                allowed_modes = {"0644"} if kind == "wheel" else {"0644", "0755"}
                if mode not in allowed_modes:
                    raise ValueError(f"{kind} member mode differs")
            total_file_bytes += size
        else:
            raise ValueError(f"{kind} member type differs")
        rows.append(row)
    if paths != sorted(paths, key=str.encode) or len(paths) != len(set(paths)):
        raise ValueError(f"{kind} member manifest path order or uniqueness differs")
    total_ceiling = MAX_SOURCE_BYTES if kind == "source" else MAX_ARCHIVE_BYTES
    if total_file_bytes > total_ceiling:
        raise ValueError(f"{kind} member payload total exceeds policy")
    return rows


def _member_row(*, path: str, kind: str, mode: int, payload: bytes | None) -> dict[str, object]:
    return {
        "gitMode": None,
        "mode": f"{mode:04o}",
        "path": path,
        "sha256": None if payload is None else hashlib.sha256(payload).hexdigest(),
        "size": 0 if payload is None else len(payload),
        "type": kind,
    }


def _read_regular_file(path: Path, *, ceiling: int = MAX_ARCHIVE_BYTES) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("bundle member cannot be opened without following links") from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
            raise ValueError("bundle member is not a sole regular file")
        if not 0 <= identity.st_size <= ceiling:
            raise ValueError("bundle member size exceeds policy")
        chunks: list[bytes] = []
        remaining = identity.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("bundle member changed during its bounded read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("bundle member grew during its bounded read")
        payload = b"".join(chunks)
        if len(payload) != identity.st_size:
            raise ValueError("bundle member size changed during read")
        return payload
    finally:
        os.close(descriptor)


def artifact_member_rows(path: Path) -> list[dict[str, object]]:
    """Return canonical member rows for a validated wheel or sdist."""
    rows: list[dict[str, object]] = []
    payload = _read_regular_file(path)
    total_bytes = 0
    if path.suffix == ".whl":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) > MAX_ARCHIVE_MEMBERS or names != sorted(names, key=str.encode):
                raise ValueError("wheel member count/order differs")
            _require_unique_names(names, artifact=path)
            for info in infos:
                _validated_member_name(info.filename)
                if info.is_dir() or stat.S_IFMT(info.external_attr >> 16) != stat.S_IFREG:
                    raise ValueError("wheel contains a non-regular member")
                member_payload = archive.read(info)
                total_bytes += len(member_payload)
                rows.append(
                    _member_row(
                        path=info.filename,
                        kind="file",
                        mode=stat.S_IMODE(info.external_attr >> 16),
                        payload=member_payload,
                    )
                )
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) > MAX_ARCHIVE_MEMBERS or names != sorted(names, key=str.encode):
                raise ValueError("sdist member count/order differs")
            _require_unique_names(names, artifact=path)
            for member in members:
                _validated_member_name(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError("sdist contains a link or special member")
                handle = archive.extractfile(member) if member.isfile() else None
                tar_payload = None if handle is None else handle.read()
                total_bytes += 0 if tar_payload is None else len(tar_payload)
                rows.append(
                    _member_row(
                        path=member.name,
                        kind="file" if member.isfile() else "directory",
                        mode=stat.S_IMODE(member.mode),
                        payload=tar_payload,
                    )
                )
    else:
        raise ValueError(f"unsupported artifact member manifest input: {path.name}")
    if total_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError("artifact expanded payload exceeds policy")
    return sorted(rows, key=lambda row: str(row["path"]).encode("utf-8"))


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    payload = b"".join(_canonical_json(row) for row in rows)
    path.write_bytes(payload)


def write_bundle_metadata(
    *,
    dist: Path,
    source_members: Path,
    wheel_members: Path,
    sdist_members: Path,
    artifact_manifest: Path,
    sha256sums: Path,
    commit: str,
    tree: str,
    tool_inputs: dict[str, object],
    validation: dict[str, object],
    epoch: int = CANONICAL_SOURCE_DATE_EPOCH,
) -> dict[str, object]:
    artifacts = sorted(path for path in dist.iterdir() if path.is_file())
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    sdist = next(path for path in artifacts if path.name.endswith(".tar.gz"))
    wheel_rows = artifact_member_rows(wheel)
    sdist_rows = artifact_member_rows(sdist)
    write_jsonl(wheel_members, wheel_rows)
    write_jsonl(sdist_members, sdist_rows)
    source_count = len(source_members.read_text(encoding="utf-8").splitlines())
    value: dict[str, object] = {
        "artifacts": {
            "sdist": {
                "filename": f"dist/{sdist.name}",
                "memberCount": len(sdist_rows),
                "memberManifestSha256": _sha256(sdist_members),
                "sha256": _sha256(sdist),
                "size": sdist.stat().st_size,
            },
            "wheel": {
                "filename": f"dist/{wheel.name}",
                "memberCount": len(wheel_rows),
                "memberManifestSha256": _sha256(wheel_members),
                "sha256": _sha256(wheel),
                "size": wheel.stat().st_size,
            },
        },
        "commit": commit,
        "distribution": DIST_NAME,
        "protectedExclusionPolicy": "k1s-eight-path-v1",
        "schema": "k1s-python-artifacts/v1",
        "source": {
            "memberCount": source_count,
            "memberManifestSha256": _sha256(source_members),
        },
        "sourceDateEpoch": epoch,
        "toolInputs": tool_inputs,
        "tree": tree,
        "validation": validation,
        "version": _project_identity(source_members.parents[2] / "source" / "pyproject.toml")[1]
        if (source_members.parents[2] / "source" / "pyproject.toml").exists()
        else "1.0.0a2",
    }
    artifact_manifest.write_bytes(_canonical_json(value))
    files = [artifact_manifest, wheel, sdist, source_members, wheel_members, sdist_members]
    bundle = artifact_manifest.parent
    lines = []
    for file_path in sorted(files, key=lambda item: item.relative_to(bundle).as_posix().encode()):
        lines.append(f"{_sha256(file_path)}  {file_path.relative_to(bundle).as_posix()}")
    sha256sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return value


def _require_bundle_filesystem(bundle: Path) -> dict[str, bytes]:
    version = "1.0.0a2"
    wheel_name = f"k1s_core-{version}-py3-none-any.whl"
    sdist_name = f"k1s_core-{version}.tar.gz"
    expected = {
        bundle: {
            "artifact-manifest.json": "file",
            "SHA256SUMS": "file",
            "dist": "directory",
            "manifests": "directory",
        },
        bundle / "dist": {wheel_name: "file", sdist_name: "file"},
        bundle / "manifests": {
            "source-members.jsonl": "file",
            "wheel-members.jsonl": "file",
            "sdist-members.jsonl": "file",
        },
    }
    try:
        root_identity = bundle.lstat()
    except OSError as exc:
        raise ValueError("artifact bundle root is unavailable") from exc
    if not stat.S_ISDIR(root_identity.st_mode):
        raise ValueError("artifact bundle root is not a directory")
    for directory, expected_entries in expected.items():
        try:
            directory_identity = directory.lstat()
            if not stat.S_ISDIR(directory_identity.st_mode):
                raise ValueError("artifact bundle directory identity differs")
            with os.scandir(directory) as scanner:
                entries = {entry.name: entry for entry in scanner}
        except OSError as exc:
            raise ValueError("artifact bundle cannot be enumerated safely") from exc
        if set(entries) != set(expected_entries):
            raise ValueError("artifact bundle filesystem membership differs")
        for name, expected_type in expected_entries.items():
            try:
                identity = entries[name].stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError("artifact bundle entry identity is unavailable") from exc
            if expected_type == "file":
                if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
                    raise ValueError("artifact bundle entry is not a sole regular file")
            elif not stat.S_ISDIR(identity.st_mode):
                raise ValueError("artifact bundle entry is not a directory")
    relative_files = {
        "artifact-manifest.json",
        "SHA256SUMS",
        f"dist/{wheel_name}",
        f"dist/{sdist_name}",
        "manifests/source-members.jsonl",
        "manifests/wheel-members.jsonl",
        "manifests/sdist-members.jsonl",
    }
    return {
        relative: _read_regular_file(
            bundle / relative,
            ceiling=MAX_ARCHIVE_BYTES if relative.startswith("dist/") else MAX_MANIFEST_BYTES,
        )
        for relative in relative_files
    }


def _validate_artifact_manifest(
    value: dict[str, object], *, expected_commit: str, expected_tree: str
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    _require_exact_keys(
        value,
        {
            "artifacts",
            "commit",
            "distribution",
            "protectedExclusionPolicy",
            "schema",
            "source",
            "sourceDateEpoch",
            "toolInputs",
            "tree",
            "validation",
            "version",
        },
        label="artifact manifest",
    )
    if (
        value["schema"] != "k1s-python-artifacts/v1"
        or value["distribution"] != DIST_NAME
        or value["version"] != "1.0.0a2"
        or value["protectedExclusionPolicy"] != "k1s-eight-path-v1"
        or value["sourceDateEpoch"] != CANONICAL_SOURCE_DATE_EPOCH
        or value["commit"] != expected_commit
        or value["tree"] != expected_tree
        or OBJECT_ID_PATTERN.fullmatch(expected_commit) is None
        or OBJECT_ID_PATTERN.fullmatch(expected_tree) is None
    ):
        raise ValueError("artifact bundle identity differs")

    artifacts_value = value["artifacts"]
    if type(artifacts_value) is not dict:
        raise ValueError("artifact manifest artifacts shape differs")
    artifacts = cast(dict[str, object], artifacts_value)
    _require_exact_keys(artifacts, {"wheel", "sdist"}, label="artifact identities")
    expected_filenames = {
        "wheel": "dist/k1s_core-1.0.0a2-py3-none-any.whl",
        "sdist": "dist/k1s_core-1.0.0a2.tar.gz",
    }
    typed_artifacts: dict[str, dict[str, object]] = {}
    for kind in ("wheel", "sdist"):
        artifact_value = artifacts[kind]
        if type(artifact_value) is not dict:
            raise ValueError(f"{kind} artifact identity shape differs")
        artifact = cast(dict[str, object], artifact_value)
        _require_exact_keys(
            artifact,
            {"filename", "memberCount", "memberManifestSha256", "sha256", "size"},
            label=f"{kind} artifact identity",
        )
        if artifact["filename"] != expected_filenames[kind]:
            raise ValueError(f"{kind} artifact filename differs")
        _require_sha256(artifact["sha256"], label=f"{kind} artifact digest")
        _require_sha256(artifact["memberManifestSha256"], label=f"{kind} member manifest digest")
        _require_nonnegative_int(
            artifact["size"], label=f"{kind} artifact size", ceiling=MAX_ARCHIVE_BYTES
        )
        _require_nonnegative_int(
            artifact["memberCount"],
            label=f"{kind} artifact member count",
            ceiling=MAX_ARCHIVE_MEMBERS,
        )
        typed_artifacts[kind] = artifact

    source_value = value["source"]
    if type(source_value) is not dict:
        raise ValueError("source manifest identity shape differs")
    source = cast(dict[str, object], source_value)
    _require_exact_keys(
        source, {"memberCount", "memberManifestSha256"}, label="source manifest identity"
    )
    _require_nonnegative_int(
        source["memberCount"], label="source member count", ceiling=MAX_SOURCE_ENTRIES
    )
    _require_sha256(source["memberManifestSha256"], label="source member manifest digest")

    validation_value = value["validation"]
    if type(validation_value) is not dict:
        raise ValueError("artifact validation result shape differs")
    validation = cast(dict[str, object], validation_value)
    _require_exact_keys(validation, VALIDATION_KEYS, label="artifact validation results")
    if any(validation[key] != "passed" for key in VALIDATION_KEYS):
        raise ValueError("artifact validation result differs")

    tool_value = value["toolInputs"]
    if type(tool_value) is not dict:
        raise ValueError("tool input identity shape differs")
    tool_inputs = cast(dict[str, object], tool_value)
    _require_exact_keys(
        tool_inputs,
        {
            "buildDistributions",
            "git",
            "python",
            "requirementsLockSha256",
            "runtimeWheelhouseCount",
            "runtimeWheelhouseIdentity",
            "runtimeWheels",
        },
        label="tool input identities",
    )
    _require_sha256(tool_inputs["requirementsLockSha256"], label="requirements lock digest")
    _require_sha256(tool_inputs["runtimeWheelhouseIdentity"], label="wheelhouse identity")
    if tool_inputs["runtimeWheelhouseCount"] != 24:
        raise ValueError("runtime wheelhouse count differs")
    build_distributions_value = tool_inputs["buildDistributions"]
    if type(build_distributions_value) is not dict:
        raise ValueError("build distribution identity shape differs")
    build_distributions = cast(dict[str, object], build_distributions_value)
    _require_exact_keys(
        build_distributions, BUILD_DISTRIBUTIONS, label="build distribution identities"
    )
    for name, identity_value in build_distributions.items():
        if type(identity_value) is not dict:
            raise ValueError(f"build distribution identity differs for {name}")
        identity = cast(dict[str, object], identity_value)
        _require_exact_keys(identity, {"recordDigest", "version"}, label=name)
        _require_sha256(identity["recordDigest"], label=f"{name} RECORD digest")
        if type(identity["version"]) is not str or not identity["version"]:
            raise ValueError(f"build distribution version differs for {name}")
    for name in ("git", "python"):
        executable_value = tool_inputs[name]
        if type(executable_value) is not dict:
            raise ValueError(f"{name} executable identity shape differs")
        executable = cast(dict[str, object], executable_value)
        _require_exact_keys(executable, {"sha256", "size", "version"}, label=name)
        _require_sha256(executable["sha256"], label=f"{name} executable digest")
        _require_nonnegative_int(
            executable["size"], label=f"{name} executable size", ceiling=MAX_ARCHIVE_BYTES
        )
        if type(executable["version"]) is not str or not executable["version"]:
            raise ValueError(f"{name} executable version differs")
    wheels_value = tool_inputs["runtimeWheels"]
    if type(wheels_value) is not list or len(wheels_value) != 24:
        raise ValueError("runtime wheel identities shape differs")
    wheels: list[dict[str, object]] = []
    for item_value in wheels_value:
        if type(item_value) is not dict:
            raise ValueError("runtime wheel identity is not an object")
        item = cast(dict[str, object], item_value)
        _require_exact_keys(item, {"filename", "sha256", "size"}, label="runtime wheel")
        filename = item["filename"]
        if (
            type(filename) is not str
            or "/" in filename
            or "\\" in filename
            or not filename.endswith(".whl")
        ):
            raise ValueError("runtime wheel filename differs")
        _require_sha256(item["sha256"], label="runtime wheel digest")
        _require_nonnegative_int(
            item["size"], label="runtime wheel size", ceiling=MAX_ARCHIVE_BYTES
        )
        wheels.append(item)
    filenames = [cast(str, item["filename"]) for item in wheels]
    if filenames != sorted(filenames, key=str.encode) or len(filenames) != len(set(filenames)):
        raise ValueError("runtime wheel identity order or uniqueness differs")
    wheelhouse_stream = b"".join(_canonical_json(item) for item in wheels)
    if hashlib.sha256(wheelhouse_stream).hexdigest() != tool_inputs["runtimeWheelhouseIdentity"]:
        raise ValueError("runtime wheelhouse identity does not bind its members")
    return typed_artifacts, source


def verify_bundle_metadata(
    *,
    bundle: Path,
    expected_commit: str,
    expected_tree: str,
) -> dict[str, object]:
    files = _require_bundle_filesystem(bundle)
    value = _read_canonical_json(files["artifact-manifest.json"], label="artifact bundle manifest")
    artifacts, source = _validate_artifact_manifest(
        value, expected_commit=expected_commit, expected_tree=expected_tree
    )

    manifest_rows: dict[str, list[dict[str, object]]] = {}
    for kind in ("source", "wheel", "sdist"):
        relative = f"manifests/{kind}-members.jsonl"
        manifest_rows[kind] = _parse_member_manifest(files[relative], kind=kind)
    if (
        source["memberCount"] != len(manifest_rows["source"])
        or source["memberManifestSha256"]
        != hashlib.sha256(files["manifests/source-members.jsonl"]).hexdigest()
    ):
        raise ValueError("source member manifest claim differs from bundle bytes")

    for kind in ("wheel", "sdist"):
        artifact = artifacts[kind]
        artifact_relative = cast(str, artifact["filename"])
        member_relative = f"manifests/{kind}-members.jsonl"
        artifact_payload = files[artifact_relative]
        if (
            artifact["sha256"] != hashlib.sha256(artifact_payload).hexdigest()
            or artifact["size"] != len(artifact_payload)
            or artifact["memberCount"] != len(manifest_rows[kind])
            or artifact["memberManifestSha256"]
            != hashlib.sha256(files[member_relative]).hexdigest()
        ):
            raise ValueError(f"{kind} artifact claim differs from bundle bytes")
        reconstructed = artifact_member_rows(bundle / artifact_relative)
        reconstructed_payload = b"".join(_canonical_json(row) for row in reconstructed)
        if reconstructed != manifest_rows[kind] or reconstructed_payload != files[member_relative]:
            raise ValueError(f"{kind} member manifest differs from reconstructed archive rows")

    sha_payload = files["SHA256SUMS"]
    if (
        not sha_payload
        or len(sha_payload) > MAX_MANIFEST_BYTES
        or not sha_payload.endswith(b"\n")
        or sha_payload.startswith(b"\xef\xbb\xbf")
        or b"\x00" in sha_payload
        or b"\r" in sha_payload
    ):
        raise ValueError("SHA256SUMS encoding is not canonical")
    try:
        sha_lines = sha_payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("SHA256SUMS encoding is not UTF-8") from exc
    actual: dict[str, str] = {}
    for line in sha_lines:
        parts = line.split("  ", 1)
        if (
            len(parts) != 2
            or parts[1] in actual
            or SHA256_PATTERN.fullmatch(parts[0]) is None
            or line != f"{parts[0]}  {parts[1]}"
        ):
            raise ValueError("SHA256SUMS is malformed or duplicated")
        digest, relative = parts
        member = PurePosixPath(relative)
        if (
            member.is_absolute()
            or member.as_posix() != relative
            or any(part in {"", ".", ".."} for part in member.parts)
        ):
            raise ValueError("SHA256SUMS contains an unsafe path")
        actual[relative] = digest
    expected_names = {
        "artifact-manifest.json",
        cast(str, artifacts["wheel"]["filename"]),
        cast(str, artifacts["sdist"]["filename"]),
        "manifests/source-members.jsonl",
        "manifests/wheel-members.jsonl",
        "manifests/sdist-members.jsonl",
    }
    if set(actual) != expected_names or list(actual) != sorted(actual, key=str.encode):
        raise ValueError("SHA256SUMS membership/order differs")
    for relative, digest in actual.items():
        if hashlib.sha256(files[relative]).hexdigest() != digest:
            raise ValueError("SHA256SUMS content differs")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--source-members", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--tree")
    parser.add_argument("--wheel-members-out", type=Path)
    parser.add_argument("--sdist-members-out", type=Path)
    parser.add_argument("--artifact-manifest-out", type=Path)
    parser.add_argument("--sha256sums-out", type=Path)
    parser.add_argument("--verify-bundle", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write-manifest", type=Path)
    mode.add_argument("--verify-manifest", type=Path)
    args = parser.parse_args()

    if args.verify_bundle is not None:
        if not args.commit or not args.tree:
            parser.error("--verify-bundle requires --commit and --tree")
        verify_bundle_metadata(
            bundle=args.verify_bundle,
            expected_commit=args.commit,
            expected_tree=args.tree,
        )
        print("bundle=verified")
        return 0

    artifacts = validate(
        args.dist,
        pyproject=args.pyproject,
        source_members=args.source_members,
    )
    create_outputs = (
        args.wheel_members_out,
        args.sdist_members_out,
        args.artifact_manifest_out,
        args.sha256sums_out,
    )
    if any(create_outputs):
        if (
            not all(create_outputs)
            or args.source_members is None
            or not args.commit
            or not args.tree
        ):
            parser.error(
                "canonical manifest creation requires every output, source, commit, and tree"
            )
        write_bundle_metadata(
            dist=args.dist,
            source_members=args.source_members,
            wheel_members=cast(Path, args.wheel_members_out),
            sdist_members=cast(Path, args.sdist_members_out),
            artifact_manifest=cast(Path, args.artifact_manifest_out),
            sha256sums=cast(Path, args.sha256sums_out),
            commit=args.commit,
            tree=args.tree,
            tool_inputs={},
            validation={"artifactValidator": "passed"},
        )
    if args.write_manifest is not None:
        _write_manifest(args.write_manifest, artifacts)
    elif args.verify_manifest is not None:
        _verify_manifest(args.verify_manifest, artifacts)
    elif not any(create_outputs):
        parser.error("one write, verify, bundle, or canonical output mode is required")
    for artifact in artifacts:
        print(f"{_sha256(artifact)}  {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
