#!/usr/bin/env python3
"""Build and validate the deterministic, offline K1s Q1 guest input bundle.

The bundle is an uncompressed canonical USTAR archive. It contains one
canonical manifest and the exact K1s 1.0.0a3 candidate, offline dependency
closure, installed-artifact validator, and qualification guest helper. This
module performs no install, network, privilege, service, runtime, or host
action.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

API_VERSION = "k1s.dev/q1-bundle/v1"
SCHEMA_VERSION = 1
BUNDLE_KIND = "K1sQ1SealedInputBundle"
GATE_B_KIND = "K1sQ1GateBAttestation"
GATE_B_ENDPOINT_KIND = "K1sQ1GateBEndpoint"
RECEIPT_KIND = "K1sQ1BundleAcceptanceReceipt"
CANDIDATE_DISTRIBUTION = "k1s-core"
CANDIDATE_VERSION = "1.0.0a3"
SOURCE_DATE_EPOCH = 1_793_664_000
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MEMBER_COUNT = 64
DEPENDENCY_WHEEL_COUNT = 24
GATE_B_TIMEOUT_SECONDS = 30
GATE_B_MAX_REQUESTS = 64
GATE_B_MAX_PROTOCOL_BYTES = 256 * 1024
GATE_B_TTL_SECONDS = 5 * 60

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
RUN_ID_RE = re.compile(r"q1-[0-9a-f]{32}")
TRANSACTION_RE = re.compile(r"ht1-[0-9a-f]{32}")
TARGET_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
OBJECT_RE = re.compile(r"[0-9a-f]{40}")
WHEEL_RE = re.compile(r"[A-Za-z0-9_.]+-[A-Za-z0-9_.!+]+-[A-Za-z0-9_.-]+\.whl")

PROTECTED_LEAVES = {
    ".env",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    ".envrc",
    "ae-ha-core.env",
    "ae.env",
}

ALLOWED_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "TZ": "UTC",
}

ROLE_COUNTS = {
    "bundle-validator": 1,
    "dependency-wheel": DEPENDENCY_WHEEL_COUNT,
    "guest-helper": 1,
    "installed-validator": 1,
    "k1s-sdist": 1,
    "k1s-wheel": 1,
    "requirements-lock": 1,
    "wheelhouse-manifest": 1,
}


class Q1BundleError(RuntimeError):
    """The sealed bundle or Gate B authority is unsafe or divergent."""


def _fail() -> Q1BundleError:
    return Q1BundleError("K1s Q1 sealed input bundle failed closed")


def canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    return (rendered + "\n").encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _canonical_json(raw: bytes, *, maximum: int) -> dict[str, object]:
    if not 1 <= len(raw) <= maximum:
        raise _fail()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return value


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    return value


def _member_path(value: object) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 240:
        raise _fail()
    if unicodedata.normalize("NFC", value) != value or "\\" in value or "\x00" in value:
        raise _fail()
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise _fail()
    if any(part in {"", ".", ".."} for part in path.parts) or path.parts[0] != "payload":
        raise _fail()
    if path.name in PROTECTED_LEAVES:
        raise _fail()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _fail()
    return value


def _absolute_path(value: object) -> Path:
    if type(value) is not str or not value.startswith("/") or value in {"/", "//"}:
        raise _fail()
    if value.endswith("/") or "\\" in value or "\x00" in value:
        raise _fail()
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail()
    if path.name in PROTECTED_LEAVES:
        raise _fail()
    return Path(value)


def _path_chain_is_not_symlink(path: Path) -> None:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise _fail() from exc
        if stat.S_ISLNK(info.st_mode):
            raise _fail()


@dataclass(frozen=True, slots=True)
class BundleInput:
    source_path: Path
    bundle_path: str
    role: str
    sha256: str
    size: int
    mode: str
    uid: int
    gid: int
    device: int
    inode: int

    def manifest_row(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "path": self.bundle_path,
            "role": self.role,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class BundleAuthority:
    bundle_id: str
    manifest_sha256: str
    plan_digest: str
    run_id: str
    endpoint_sha256: str
    raw_sha256: str
    size: int
    manifest: dict[str, object]


def bundle_input(
    source_path: Path,
    *,
    bundle_path: str,
    role: str,
    expected_sha256: str,
) -> BundleInput:
    path = _absolute_path(str(source_path))
    _path_chain_is_not_symlink(path)
    expected_sha256 = _digest(expected_sha256)
    try:
        before = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid != os.getuid()
            or mode not in {0o644, 0o755}
            or not 1 <= opened.st_size <= MAX_MEMBER_BYTES
        ):
            raise _fail()
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 1024 * 1024))
            if not block:
                raise _fail()
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise _fail()
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise _fail()
    finally:
        os.close(descriptor)
    actual = f"sha256:{digest.hexdigest()}"
    if actual != expected_sha256:
        raise _fail()
    if role not in ROLE_COUNTS:
        raise _fail()
    return BundleInput(
        source_path=path,
        bundle_path=_member_path(bundle_path),
        role=role,
        sha256=actual,
        size=opened.st_size,
        mode=f"0{mode:03o}",
        uid=opened.st_uid,
        gid=opened.st_gid,
        device=opened.st_dev,
        inode=opened.st_ino,
    )


def _read_input(value: BundleInput) -> bytes:
    current = bundle_input(
        value.source_path,
        bundle_path=value.bundle_path,
        role=value.role,
        expected_sha256=value.sha256,
    )
    if current != value:
        raise _fail()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            value.source_path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        raw = os.read(descriptor, value.size)
        if len(raw) != value.size or os.read(descriptor, 1):
            raise _fail()
    except OSError as exc:
        raise _fail() from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if _digest_bytes(raw) != value.sha256:
        raise _fail()
    return raw


def build_gate_b_endpoint(
    *,
    bundle_validator_sha256: str,
    helper_sha256: str,
    python_sha256: str,
    setfacl_sha256: str,
    getfacl_sha256: str,
) -> dict[str, object]:
    endpoint: dict[str, object] = {
        "ambientSudo": False,
        "apiVersion": API_VERSION,
        "bundleValidator": {
            "path": "/opt/k1s-q1/kp1_q1_bundle.py",
            "sha256": _digest(bundle_validator_sha256),
        },
        "cwd": "/",
        "environment": dict(ALLOWED_ENVIRONMENT),
        "gid": 0,
        "getfacl": {"path": "/usr/bin/getfacl", "sha256": _digest(getfacl_sha256)},
        "helper": {
            "path": "/opt/k1s-q1/kp1_q1_guest_helper.py",
            "sha256": _digest(helper_sha256),
        },
        "kind": GATE_B_ENDPOINT_KIND,
        "maxProtocolBytes": GATE_B_MAX_PROTOCOL_BYTES,
        "maxRequests": GATE_B_MAX_REQUESTS,
        "python": {"path": "/usr/bin/python3.11", "sha256": _digest(python_sha256)},
        "requestFd": 3,
        "responseFd": 4,
        "schemaVersion": SCHEMA_VERSION,
        "service": {
            "gid": 0,
            "mode": "single-transaction-exec",
            "name": "k1s-q1-gate-b",
            "persistent": False,
            "uid": 0,
        },
        "setfacl": {"path": "/usr/bin/setfacl", "sha256": _digest(setfacl_sha256)},
        "shell": False,
        "timeoutSeconds": GATE_B_TIMEOUT_SECONDS,
        "transactions": 1,
        "uid": 0,
        "umask": "0077",
    }
    endpoint["endpointSha256"] = _digest_bytes(canonical_json_bytes(endpoint))
    return endpoint


def _validate_endpoint(value: object, helper_sha256: str) -> dict[str, object]:
    endpoint = _closed(
        value,
        {
            "ambientSudo",
            "apiVersion",
            "bundleValidator",
            "cwd",
            "endpointSha256",
            "environment",
            "gid",
            "getfacl",
            "helper",
            "kind",
            "maxProtocolBytes",
            "maxRequests",
            "python",
            "requestFd",
            "responseFd",
            "schemaVersion",
            "service",
            "setfacl",
            "shell",
            "timeoutSeconds",
            "transactions",
            "uid",
            "umask",
        },
    )
    unsigned = dict(endpoint)
    endpoint_digest = unsigned.pop("endpointSha256")
    integer_values = {
        "gid": 0,
        "maxProtocolBytes": GATE_B_MAX_PROTOCOL_BYTES,
        "maxRequests": GATE_B_MAX_REQUESTS,
        "requestFd": 3,
        "responseFd": 4,
        "schemaVersion": SCHEMA_VERSION,
        "timeoutSeconds": GATE_B_TIMEOUT_SECONDS,
        "transactions": 1,
        "uid": 0,
    }
    string_values = {
        "apiVersion": API_VERSION,
        "cwd": "/",
        "kind": GATE_B_ENDPOINT_KIND,
        "umask": "0077",
    }
    if (
        _digest(endpoint_digest) != _digest_bytes(canonical_json_bytes(unsigned))
        or any(
            type(endpoint[name]) is not int or endpoint[name] != expected
            for name, expected in integer_values.items()
        )
        or any(
            type(endpoint[name]) is not str or endpoint[name] != expected
            for name, expected in string_values.items()
        )
        or type(endpoint["shell"]) is not bool
        or endpoint["shell"] is not False
        or type(endpoint["ambientSudo"]) is not bool
        or endpoint["ambientSudo"] is not False
        or type(endpoint["environment"]) is not dict
        or any(
            type(name) is not str or type(setting) is not str
            for name, setting in cast(dict[object, object], endpoint["environment"]).items()
        )
        or endpoint["environment"] != ALLOWED_ENVIRONMENT
    ):
        raise _fail()
    service = _closed(endpoint["service"], {"gid", "mode", "name", "persistent", "uid"})
    if (
        type(service["uid"]) is not int
        or service["uid"] != 0
        or type(service["gid"]) is not int
        or service["gid"] != 0
        or type(service["persistent"]) is not bool
        or service["persistent"] is not False
        or type(service["mode"]) is not str
        or service["mode"] != "single-transaction-exec"
        or type(service["name"]) is not str
        or service["name"] != "k1s-q1-gate-b"
    ):
        raise _fail()
    tools = {
        "bundleValidator": (
            "/opt/k1s-q1/kp1_q1_bundle.py",
            None,
        ),
        "helper": ("/opt/k1s-q1/kp1_q1_guest_helper.py", helper_sha256),
        "python": ("/usr/bin/python3.11", None),
        "setfacl": ("/usr/bin/setfacl", None),
        "getfacl": ("/usr/bin/getfacl", None),
    }
    for name, (expected_path, expected_digest) in tools.items():
        tool = _closed(endpoint[name], {"path", "sha256"})
        if (
            type(tool["path"]) is not str
            or tool["path"] != expected_path
            or (expected_digest is not None and tool["sha256"] != expected_digest)
        ):
            raise _fail()
        _digest(tool["sha256"])
    return endpoint


def _validate_member_rows(value: object) -> list[dict[str, object]]:
    if type(value) is not list or not 1 <= len(value) <= MAX_MEMBER_COUNT:
        raise _fail()
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    folded: set[str] = set()
    roles: Counter[str] = Counter()
    for raw in value:
        row = _closed(raw, {"mode", "path", "role", "sha256", "size"})
        path = _member_path(row["path"])
        fold = unicodedata.normalize("NFC", path).casefold()
        if path in seen or fold in folded:
            raise _fail()
        seen.add(path)
        folded.add(fold)
        if type(row["role"]) is not str or row["role"] not in ROLE_COUNTS:
            raise _fail()
        roles[row["role"]] += 1
        if (
            type(row["mode"]) is not str
            or row["mode"] not in {"0644", "0755"}
            or type(row["size"]) is not int
            or not 1 <= row["size"] <= MAX_MEMBER_BYTES
        ):
            raise _fail()
        _digest(row["sha256"])
        rows.append(dict(row))
    if roles != Counter(ROLE_COUNTS):
        raise _fail()
    if [row["path"] for row in rows] != sorted(seen, key=lambda item: item.encode("utf-8")):
        raise _fail()
    role_rows = {role: [row for row in rows if row["role"] == role] for role in ROLE_COUNTS}
    if role_rows["k1s-wheel"][0]["path"] != ("payload/k1s/k1s_core-1.0.0a3-py3-none-any.whl"):
        raise _fail()
    if role_rows["k1s-sdist"][0]["path"] != "payload/k1s/k1s_core-1.0.0a3.tar.gz":
        raise _fail()
    if role_rows["requirements-lock"][0]["path"] != ("payload/offline/requirements-release.lock"):
        raise _fail()
    if role_rows["wheelhouse-manifest"][0]["path"] != ("payload/offline/wheelhouse-manifest.json"):
        raise _fail()
    if role_rows["guest-helper"][0]["path"] != ("payload/helpers/kp1_q1_guest_helper.py"):
        raise _fail()
    if role_rows["bundle-validator"][0]["path"] != ("payload/helpers/kp1_q1_bundle.py"):
        raise _fail()
    if role_rows["installed-validator"][0]["path"] != (
        "payload/helpers/validate_installed_python_artifact.py"
    ):
        raise _fail()
    dependencies = role_rows["dependency-wheel"]
    if any(
        not str(row["path"]).startswith("payload/offline/wheels/")
        or WHEEL_RE.fullmatch(PurePosixPath(cast(str, row["path"])).name) is None
        for row in dependencies
    ):
        raise _fail()
    return rows


def build_manifest(
    *,
    run_id: str,
    plan_digest: str,
    source_commit: str,
    source_tree: str,
    members: Sequence[BundleInput],
    gate_b_endpoint: dict[str, object],
) -> dict[str, object]:
    if RUN_ID_RE.fullmatch(run_id) is None or OBJECT_RE.fullmatch(source_commit) is None:
        raise _fail()
    if OBJECT_RE.fullmatch(source_tree) is None:
        raise _fail()
    _digest(plan_digest)
    rows = sorted(
        (member.manifest_row() for member in members),
        key=lambda row: cast(str, row["path"]).encode("utf-8"),
    )
    rows = _validate_member_rows(rows)
    helper_rows = [row for row in rows if row["role"] == "guest-helper"]
    validator_rows = [row for row in rows if row["role"] == "bundle-validator"]
    endpoint = _validate_endpoint(gate_b_endpoint, cast(str, helper_rows[0]["sha256"]))
    validator = cast(dict[str, object], endpoint["bundleValidator"])
    if validator["sha256"] != validator_rows[0]["sha256"]:
        raise _fail()
    manifest: dict[str, object] = {
        "apiVersion": API_VERSION,
        "distribution": {"name": CANDIDATE_DISTRIBUTION, "version": CANDIDATE_VERSION},
        "gateB": endpoint,
        "kind": BUNDLE_KIND,
        "members": rows,
        "offlineInstall": {
            "indexAccess": "denied",
            "network": "denied",
            "pipConfigFile": "/dev/null",
            "pipNoIndex": True,
            "requireHashes": True,
            "wheelCount": DEPENDENCY_WHEEL_COUNT,
        },
        "planDigest": plan_digest,
        "runId": run_id,
        "schemaVersion": SCHEMA_VERSION,
        "source": {"commit": source_commit, "tree": source_tree},
        "sourceDateEpoch": SOURCE_DATE_EPOCH,
    }
    manifest["bundleId"] = _digest_bytes(
        b"k1s-q1-sealed-bundle-v1\0" + canonical_json_bytes(manifest)
    )
    return validate_manifest(manifest)


def validate_manifest(value: object) -> dict[str, object]:
    manifest = _closed(
        value,
        {
            "apiVersion",
            "bundleId",
            "distribution",
            "gateB",
            "kind",
            "members",
            "offlineInstall",
            "planDigest",
            "runId",
            "schemaVersion",
            "source",
            "sourceDateEpoch",
        },
    )
    if (
        type(manifest["apiVersion"]) is not str
        or manifest["apiVersion"] != API_VERSION
        or type(manifest["kind"]) is not str
        or manifest["kind"] != BUNDLE_KIND
        or type(manifest["schemaVersion"]) is not int
        or manifest["schemaVersion"] != SCHEMA_VERSION
        or type(manifest["sourceDateEpoch"]) is not int
        or manifest["sourceDateEpoch"] != SOURCE_DATE_EPOCH
        or type(manifest["runId"]) is not str
        or RUN_ID_RE.fullmatch(manifest["runId"]) is None
    ):
        raise _fail()
    _digest(manifest["planDigest"])
    distribution = _closed(manifest["distribution"], {"name", "version"})
    if (
        type(distribution["name"]) is not str
        or distribution["name"] != CANDIDATE_DISTRIBUTION
        or type(distribution["version"]) is not str
        or distribution["version"] != CANDIDATE_VERSION
    ):
        raise _fail()
    source = _closed(manifest["source"], {"commit", "tree"})
    if (
        type(source["commit"]) is not str
        or OBJECT_RE.fullmatch(source["commit"]) is None
        or type(source["tree"]) is not str
        or OBJECT_RE.fullmatch(source["tree"]) is None
    ):
        raise _fail()
    offline = _closed(
        manifest["offlineInstall"],
        {
            "indexAccess",
            "network",
            "pipConfigFile",
            "pipNoIndex",
            "requireHashes",
            "wheelCount",
        },
    )
    if (
        type(offline["indexAccess"]) is not str
        or offline["indexAccess"] != "denied"
        or type(offline["network"]) is not str
        or offline["network"] != "denied"
        or type(offline["pipConfigFile"]) is not str
        or offline["pipConfigFile"] != "/dev/null"
        or type(offline["pipNoIndex"]) is not bool
        or offline["pipNoIndex"] is not True
        or type(offline["requireHashes"]) is not bool
        or offline["requireHashes"] is not True
        or type(offline["wheelCount"]) is not int
        or offline["wheelCount"] != DEPENDENCY_WHEEL_COUNT
    ):
        raise _fail()
    rows = _validate_member_rows(manifest["members"])
    helper = next(row for row in rows if row["role"] == "guest-helper")
    validator = next(row for row in rows if row["role"] == "bundle-validator")
    endpoint = _validate_endpoint(manifest["gateB"], cast(str, helper["sha256"]))
    if cast(dict[str, object], endpoint["bundleValidator"])["sha256"] != validator["sha256"]:
        raise _fail()
    unsigned = dict(manifest)
    bundle_id = unsigned.pop("bundleId")
    if _digest(bundle_id) != _digest_bytes(
        b"k1s-q1-sealed-bundle-v1\0" + canonical_json_bytes(unsigned)
    ):
        raise _fail()
    return manifest


def _tar_bytes(manifest: dict[str, object], payloads: dict[str, bytes]) -> bytes:
    manifest_raw = canonical_json_bytes(manifest)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_raw)
        info.mode = 0o644
        info.mtime = SOURCE_DATE_EPOCH
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        archive.addfile(info, io.BytesIO(manifest_raw))
        rows = cast(list[dict[str, object]], manifest["members"])
        for row in rows:
            path = cast(str, row["path"])
            raw = payloads[path]
            info = tarfile.TarInfo(path)
            info.size = len(raw)
            info.mode = int(cast(str, row["mode"]), 8)
            info.mtime = SOURCE_DATE_EPOCH
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(raw))
    raw = output.getvalue()
    if len(raw) > MAX_BUNDLE_BYTES:
        raise _fail()
    return raw


def build_bundle_bytes(
    manifest: dict[str, object],
    members: Sequence[BundleInput],
) -> bytes:
    manifest = validate_manifest(manifest)
    by_path = {member.bundle_path: member for member in members}
    rows = cast(list[dict[str, object]], manifest["members"])
    if len(by_path) != len(members) or set(by_path) != {row["path"] for row in rows}:
        raise _fail()
    payloads: dict[str, bytes] = {}
    for row in rows:
        member = by_path[cast(str, row["path"])]
        if member.manifest_row() != row:
            raise _fail()
        payloads[member.bundle_path] = _read_input(member)
    return _tar_bytes(manifest, payloads)


def validate_bundle_bytes(
    raw: bytes,
    *,
    expected_run_id: str,
    expected_plan_digest: str,
    expected_bundle_id: str | None = None,
    consumed_bundle_ids: frozenset[str] = frozenset(),
) -> BundleAuthority:
    if not 1 <= len(raw) <= MAX_BUNDLE_BYTES:
        raise _fail()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            items = archive.getmembers()
            names = [item.name for item in items]
            if not items or names[0] != "manifest.json" or len(items) > MAX_MEMBER_COUNT + 1:
                raise _fail()
            if names != [
                "manifest.json",
                *sorted(names[1:], key=lambda item: item.encode("utf-8")),
            ]:
                raise _fail()
            if len(names) != len(set(names)):
                raise _fail()
            payloads: dict[str, bytes] = {}
            manifest_raw: bytes | None = None
            for index, item in enumerate(items):
                if (
                    not item.isreg()
                    or item.uid != 0
                    or item.gid != 0
                    or item.uname != ""
                    or item.gname != ""
                    or item.mtime != SOURCE_DATE_EPOCH
                    or item.pax_headers
                    or item.size > MAX_MEMBER_BYTES
                ):
                    raise _fail()
                expected_mode = 0o644 if index == 0 else None
                if expected_mode is not None and item.mode != expected_mode:
                    raise _fail()
                extracted = archive.extractfile(item)
                if extracted is None:
                    raise _fail()
                member_raw = extracted.read(item.size + 1)
                if len(member_raw) != item.size:
                    raise _fail()
                if index == 0:
                    manifest_raw = member_raw
                else:
                    _member_path(item.name)
                    payloads[item.name] = member_raw
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise _fail() from exc
    if manifest_raw is None:
        raise _fail()
    manifest = validate_manifest(_canonical_json(manifest_raw, maximum=MAX_MANIFEST_BYTES))
    rows = cast(list[dict[str, object]], manifest["members"])
    if [row["path"] for row in rows] != list(payloads):
        raise _fail()
    for item, row in zip(items[1:], rows, strict=True):
        member_raw = payloads[cast(str, row["path"])]
        if (
            item.mode != int(cast(str, row["mode"]), 8)
            or item.size != row["size"]
            or _digest_bytes(member_raw) != row["sha256"]
        ):
            raise _fail()
    if _tar_bytes(manifest, payloads) != raw:
        raise _fail()
    if manifest["runId"] != expected_run_id or manifest["planDigest"] != _digest(
        expected_plan_digest
    ):
        raise _fail()
    bundle_id = _digest(manifest["bundleId"])
    if (expected_bundle_id is not None and bundle_id != _digest(expected_bundle_id)) or (
        bundle_id in consumed_bundle_ids
    ):
        raise _fail()
    endpoint = cast(dict[str, object], manifest["gateB"])
    return BundleAuthority(
        bundle_id=bundle_id,
        manifest_sha256=_digest_bytes(manifest_raw),
        plan_digest=manifest["planDigest"],
        run_id=manifest["runId"],
        endpoint_sha256=cast(str, endpoint["endpointSha256"]),
        raw_sha256=_digest_bytes(raw),
        size=len(raw),
        manifest=manifest,
    )


def validate_bundle_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_run_id: str,
    expected_plan_digest: str,
    expected_bundle_id: str | None = None,
    consumed_bundle_ids: frozenset[str] = frozenset(),
) -> BundleAuthority:
    path = _absolute_path(str(path))
    _path_chain_is_not_symlink(path)
    try:
        info = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or not 1 <= opened.st_size <= MAX_BUNDLE_BYTES
        ):
            raise _fail()
        raw = os.read(descriptor, opened.st_size)
        if len(raw) != opened.st_size or os.read(descriptor, 1):
            raise _fail()
    finally:
        os.close(descriptor)
    if _digest_bytes(raw) != _digest(expected_sha256):
        raise _fail()
    return validate_bundle_bytes(
        raw,
        expected_run_id=expected_run_id,
        expected_plan_digest=expected_plan_digest,
        expected_bundle_id=expected_bundle_id,
        consumed_bundle_ids=consumed_bundle_ids,
    )


def build_gate_b_attestation(
    authority: BundleAuthority,
    *,
    access_uid: int,
    access_gid: int,
    transaction_id: str,
    target_id: str,
    request_count: int,
    rollback_receipt_digest: str,
    issued_at_unix: int,
    expires_at_unix: int,
) -> dict[str, object]:
    if (
        any(type(value) is not int or value < 0 for value in (access_uid, access_gid))
        or type(transaction_id) is not str
        or TRANSACTION_RE.fullmatch(transaction_id) is None
        or type(target_id) is not str
        or TARGET_RE.fullmatch(target_id) is None
        or type(request_count) is not int
        or not 1 <= request_count <= GATE_B_MAX_REQUESTS
        or type(issued_at_unix) is not int
        or type(expires_at_unix) is not int
        or expires_at_unix - issued_at_unix != GATE_B_TTL_SECONDS
    ):
        raise _fail()
    endpoint = cast(dict[str, object], authority.manifest["gateB"])
    for name in ("bundleValidator", "helper", "python", "setfacl", "getfacl"):
        _closed(endpoint[name], {"path", "sha256"})
    value: dict[str, object] = {
        "accessGid": access_gid,
        "accessUid": access_uid,
        "apiVersion": API_VERSION,
        "bundleId": authority.bundle_id,
        "bundleValidatorSha256": cast(dict[str, object], endpoint["bundleValidator"])["sha256"],
        "bundleManifestSha256": authority.manifest_sha256,
        "endpointSha256": authority.endpoint_sha256,
        "expiresAtUnix": expires_at_unix,
        "getfaclSha256": cast(dict[str, object], endpoint["getfacl"])["sha256"],
        "helperSha256": cast(dict[str, object], endpoint["helper"])["sha256"],
        "issuedAtUnix": issued_at_unix,
        "kind": GATE_B_KIND,
        "planDigest": authority.plan_digest,
        "pythonSha256": cast(dict[str, object], endpoint["python"])["sha256"],
        "requestCount": request_count,
        "rollbackReceiptSha256": _digest(rollback_receipt_digest),
        "runId": authority.run_id,
        "schemaVersion": SCHEMA_VERSION,
        "setfaclSha256": cast(dict[str, object], endpoint["setfacl"])["sha256"],
        "targetId": target_id,
        "transactionId": transaction_id,
    }
    value["attestationDigest"] = _digest_bytes(b"k1s-q1-gate-b-v1\0" + canonical_json_bytes(value))
    return value


def validate_gate_b_attestation(
    value: object,
    authority: BundleAuthority,
    *,
    now_unix: int,
) -> dict[str, object]:
    attestation = _closed(
        value,
        {
            "accessGid",
            "accessUid",
            "apiVersion",
            "attestationDigest",
            "bundleId",
            "bundleManifestSha256",
            "bundleValidatorSha256",
            "endpointSha256",
            "expiresAtUnix",
            "getfaclSha256",
            "helperSha256",
            "issuedAtUnix",
            "kind",
            "planDigest",
            "pythonSha256",
            "requestCount",
            "rollbackReceiptSha256",
            "runId",
            "schemaVersion",
            "setfaclSha256",
            "targetId",
            "transactionId",
        },
    )
    unsigned = dict(attestation)
    attestation_digest = unsigned.pop("attestationDigest")
    endpoint = cast(dict[str, object], authority.manifest["gateB"])
    integer_fields = (
        "accessUid",
        "accessGid",
        "expiresAtUnix",
        "issuedAtUnix",
        "requestCount",
        "schemaVersion",
    )
    if any(type(attestation[field]) is not int for field in integer_fields):
        raise _fail()
    access_uid = cast(int, attestation["accessUid"])
    access_gid = cast(int, attestation["accessGid"])
    issued_at = cast(int, attestation["issuedAtUnix"])
    expires_at = cast(int, attestation["expiresAtUnix"])
    request_count = cast(int, attestation["requestCount"])
    if (
        _digest(attestation_digest)
        != _digest_bytes(b"k1s-q1-gate-b-v1\0" + canonical_json_bytes(unsigned))
        or type(now_unix) is not int
        or now_unix < 0
        or type(attestation["apiVersion"]) is not str
        or attestation["apiVersion"] != API_VERSION
        or type(attestation["kind"]) is not str
        or attestation["kind"] != GATE_B_KIND
        or attestation["schemaVersion"] != SCHEMA_VERSION
        or _digest(attestation["bundleId"]) != authority.bundle_id
        or _digest(attestation["bundleManifestSha256"]) != authority.manifest_sha256
        or _digest(attestation["bundleValidatorSha256"])
        != cast(dict[str, object], endpoint["bundleValidator"])["sha256"]
        or _digest(attestation["endpointSha256"]) != authority.endpoint_sha256
        or _digest(attestation["planDigest"]) != authority.plan_digest
        or type(attestation["runId"]) is not str
        or attestation["runId"] != authority.run_id
        or type(attestation["transactionId"]) is not str
        or TRANSACTION_RE.fullmatch(attestation["transactionId"]) is None
        or type(attestation["targetId"]) is not str
        or TARGET_RE.fullmatch(attestation["targetId"]) is None
        or not 1 <= request_count <= GATE_B_MAX_REQUESTS
        or any(value < 0 for value in (access_uid, access_gid, issued_at, expires_at))
        or not issued_at <= now_unix <= expires_at
        or expires_at - issued_at != GATE_B_TTL_SECONDS
    ):
        raise _fail()
    for name in ("bundleValidator", "helper", "python", "setfacl", "getfacl"):
        tool = cast(dict[str, object], endpoint[name])
        if _digest(attestation[f"{name}Sha256"]) != tool["sha256"]:
            raise _fail()
    _digest(attestation["rollbackReceiptSha256"])
    return attestation


def validate_endpoint_context(
    *,
    euid: int,
    egid: int,
    cwd: str,
    umask: int,
    request_fd_mode: int,
    response_fd_mode: int,
    request_fd_device: int,
    request_fd_inode: int,
    response_fd_device: int,
    response_fd_inode: int,
    request_fd: int,
    response_fd: int,
    environment: dict[str, str],
) -> None:
    if (
        any(
            type(value) is not int or value < 0
            for value in (
                euid,
                egid,
                request_fd,
                request_fd_device,
                request_fd_inode,
                request_fd_mode,
                response_fd,
                response_fd_device,
                response_fd_inode,
                response_fd_mode,
                umask,
            )
        )
        or euid != 0
        or egid != 0
        or type(cwd) is not str
        or cwd != "/"
        or umask != 0o077
        or request_fd != 3
        or response_fd != 4
        or request_fd == response_fd
        or min(
            request_fd_device,
            request_fd_inode,
            response_fd_device,
            response_fd_inode,
        )
        <= 0
        or (request_fd_device, request_fd_inode) == (response_fd_device, response_fd_inode)
        or not stat.S_ISFIFO(request_fd_mode)
        or not stat.S_ISFIFO(response_fd_mode)
        or type(environment) is not dict
        or any(
            type(name) is not str or type(value) is not str
            for name, value in cast(dict[object, object], environment).items()
        )
        or environment != ALLOWED_ENVIRONMENT
    ):
        raise _fail()


def build_acceptance_receipt(
    authority: BundleAuthority,
    *,
    transaction_id: str,
) -> dict[str, object]:
    if type(transaction_id) is not str or TRANSACTION_RE.fullmatch(transaction_id) is None:
        raise _fail()
    value: dict[str, object] = {
        "apiVersion": API_VERSION,
        "bundleId": authority.bundle_id,
        "bundleManifestSha256": authority.manifest_sha256,
        "kind": RECEIPT_KIND,
        "planDigest": authority.plan_digest,
        "runId": authority.run_id,
        "schemaVersion": SCHEMA_VERSION,
        "state": "accepted-once",
        "transactionId": transaction_id,
    }
    value["receiptDigest"] = _digest_bytes(canonical_json_bytes(value))
    return value


def expected_acceptance_receipt_path(authority: BundleAuthority) -> Path:
    bundle_id = _digest(authority.bundle_id)
    return Path("/run/k1s-q1/private/acceptance") / f"{bundle_id.removeprefix('sha256:')}.json"


def validate_acceptance_receipt_output_path(
    path: Path,
    authority: BundleAuthority,
) -> Path:
    canonical = _absolute_path(str(path))
    if canonical != expected_acceptance_receipt_path(authority):
        raise _fail()
    return canonical


def validate_acceptance_receipt(
    value: object,
    authority: BundleAuthority,
    *,
    transaction_id: str,
) -> dict[str, object]:
    receipt = _closed(
        value,
        {
            "apiVersion",
            "bundleId",
            "bundleManifestSha256",
            "kind",
            "planDigest",
            "receiptDigest",
            "runId",
            "schemaVersion",
            "state",
            "transactionId",
        },
    )
    if (
        type(transaction_id) is not str
        or TRANSACTION_RE.fullmatch(transaction_id) is None
        or type(receipt["apiVersion"]) is not str
        or receipt["apiVersion"] != API_VERSION
        or type(receipt["kind"]) is not str
        or receipt["kind"] != RECEIPT_KIND
        or type(receipt["schemaVersion"]) is not int
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or type(receipt["state"]) is not str
        or receipt["state"] != "accepted-once"
        or type(receipt["runId"]) is not str
        or receipt["runId"] != authority.run_id
        or type(receipt["transactionId"]) is not str
        or receipt["transactionId"] != transaction_id
        or _digest(receipt["bundleId"]) != authority.bundle_id
        or _digest(receipt["bundleManifestSha256"]) != authority.manifest_sha256
        or _digest(receipt["planDigest"]) != authority.plan_digest
    ):
        raise _fail()
    unsigned = dict(receipt)
    receipt_digest = unsigned.pop("receiptDigest")
    if _digest(receipt_digest) != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    return receipt


def _validate_acceptance_parent(
    parent: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, os.stat_result]:
    _path_chain_is_not_symlink(parent)
    try:
        before = os.lstat(parent)
        descriptor = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise _fail() from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or opened.st_uid != expected_uid
        or opened.st_gid != expected_gid
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_nlink < 2
    ):
        os.close(descriptor)
        raise _fail()
    return descriptor, opened


def _load_acceptance_receipt_at(
    parent_fd: int,
    parent_info: os.stat_result,
    name: str,
    authority: BundleAuthority,
    *,
    transaction_id: str,
    expected_uid: int,
    expected_gid: int,
) -> dict[str, object]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != parent_info.st_dev
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= MAX_MANIFEST_BYTES
        ):
            raise _fail()
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 64 * 1024))
            if not block:
                raise _fail()
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise _fail()
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise _fail()
        raw = b"".join(chunks)
        receipt = _canonical_json(raw, maximum=MAX_MANIFEST_BYTES)
        return validate_acceptance_receipt(
            receipt,
            authority,
            transaction_id=transaction_id,
        )
    except OSError as exc:
        raise _fail() from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def validate_acceptance_receipt_file(
    path: Path,
    authority: BundleAuthority,
    *,
    transaction_id: str,
    _expected_uid: int = 0,
    _expected_gid: int = 0,
) -> dict[str, object]:
    path = _absolute_path(str(path))
    parent_fd, parent_info = _validate_acceptance_parent(
        path.parent,
        expected_uid=_expected_uid,
        expected_gid=_expected_gid,
    )
    try:
        return _load_acceptance_receipt_at(
            parent_fd,
            parent_info,
            path.name,
            authority,
            transaction_id=transaction_id,
            expected_uid=_expected_uid,
            expected_gid=_expected_gid,
        )
    finally:
        os.close(parent_fd)


def create_acceptance_receipt(
    path: Path,
    authority: BundleAuthority,
    *,
    transaction_id: str,
    _expected_uid: int = 0,
    _expected_gid: int = 0,
) -> dict[str, object]:
    path = _absolute_path(str(path))
    receipt = build_acceptance_receipt(authority, transaction_id=transaction_id)
    raw = canonical_json_bytes(receipt)
    parent_fd, parent_info = _validate_acceptance_parent(
        path.parent,
        expected_uid=_expected_uid,
        expected_gid=_expected_gid,
    )
    pending_name = f".{path.name}.pending-{os.getpid()}"
    pending_created = False
    descriptor: int | None = None
    try:
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise _fail()
        descriptor = os.open(
            pending_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        pending_created = True
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise _fail()
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(
            pending_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.unlink(pending_name, dir_fd=parent_fd)
        pending_created = False
        os.fsync(parent_fd)
        return _load_acceptance_receipt_at(
            parent_fd,
            parent_info,
            path.name,
            authority,
            transaction_id=transaction_id,
            expected_uid=_expected_uid,
            expected_gid=_expected_gid,
        )
    except (OSError, TypeError) as exc:
        raise _fail() from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if pending_created:
            try:
                os.unlink(pending_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def _atomic_output(path: Path, raw: bytes) -> None:
    path = _absolute_path(str(path))
    parent = path.parent
    info = os.lstat(parent)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or path.exists()
        or path.is_symlink()
    ):
        raise _fail()
    pending = parent / f".{path.name}.pending-{os.getpid()}"
    descriptor = os.open(
        pending,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise _fail()
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(pending, path)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _input_from_spec(value: object) -> BundleInput:
    row = _closed(value, {"bundlePath", "expectedSha256", "role", "sourcePath"})
    if any(type(row[field]) is not str for field in row):
        raise _fail()
    return bundle_input(
        _absolute_path(row["sourcePath"]),
        bundle_path=cast(str, row["bundlePath"]),
        role=cast(str, row["role"]),
        expected_sha256=cast(str, row["expectedSha256"]),
    )


def _load_spec(path: Path, expected_sha256: str) -> dict[str, object]:
    path = _absolute_path(str(path))
    _path_chain_is_not_symlink(path)
    descriptor: int | None = None
    try:
        info = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or not 1 <= opened.st_size <= MAX_MANIFEST_BYTES
        ):
            raise _fail()
        raw = os.read(descriptor, opened.st_size)
        if len(raw) != opened.st_size or os.read(descriptor, 1):
            raise _fail()
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise _fail()
    except OSError as exc:
        raise _fail() from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if _digest_bytes(raw) != _digest(expected_sha256):
        raise _fail()
    return _canonical_json(raw, maximum=MAX_MANIFEST_BYTES)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--spec", type=Path, required=True)
    build.add_argument("--expect-spec-sha256", required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--expect-bundle-sha256", required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--plan-digest", required=True)
    verify.add_argument("--bundle-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    ns = _parser().parse_args(argv)
    try:
        if ns.command == "build":
            spec = _load_spec(ns.spec, ns.expect_spec_sha256)
            spec = _closed(
                spec,
                {
                    "endpoint",
                    "inputs",
                    "planDigest",
                    "runId",
                    "sourceCommit",
                    "sourceTree",
                },
            )
            if type(spec["inputs"]) is not list:
                raise _fail()
            inputs = [_input_from_spec(value) for value in spec["inputs"]]
            endpoint_spec = _closed(
                spec["endpoint"],
                {"getfaclSha256", "pythonSha256", "setfaclSha256"},
            )
            helper = next(value for value in inputs if value.role == "guest-helper")
            validator = next(value for value in inputs if value.role == "bundle-validator")
            endpoint = build_gate_b_endpoint(
                bundle_validator_sha256=validator.sha256,
                helper_sha256=helper.sha256,
                python_sha256=cast(str, endpoint_spec["pythonSha256"]),
                setfacl_sha256=cast(str, endpoint_spec["setfaclSha256"]),
                getfacl_sha256=cast(str, endpoint_spec["getfaclSha256"]),
            )
            manifest = build_manifest(
                run_id=cast(str, spec["runId"]),
                plan_digest=cast(str, spec["planDigest"]),
                source_commit=cast(str, spec["sourceCommit"]),
                source_tree=cast(str, spec["sourceTree"]),
                members=inputs,
                gate_b_endpoint=endpoint,
            )
            raw = build_bundle_bytes(manifest, inputs)
            _atomic_output(ns.output, raw)
            sys.stdout.buffer.write(
                canonical_json_bytes(
                    {
                        "bundleId": manifest["bundleId"],
                        "sha256": _digest_bytes(raw),
                        "size": len(raw),
                    }
                )
            )
            return 0
        authority = validate_bundle_file(
            ns.bundle,
            expected_sha256=ns.expect_bundle_sha256,
            expected_run_id=ns.run_id,
            expected_plan_digest=ns.plan_digest,
            expected_bundle_id=ns.bundle_id,
        )
        sys.stdout.buffer.write(
            canonical_json_bytes(
                {
                    "bundleId": authority.bundle_id,
                    "manifestSha256": authority.manifest_sha256,
                    "sha256": authority.raw_sha256,
                    "size": authority.size,
                }
            )
        )
        return 0
    except (Q1BundleError, OSError, StopIteration, TypeError, ValueError):
        print("K1s Q1 sealed input bundle failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
