#!/usr/bin/env python3
"""Acquire and authenticate one immutable Canonical Q1 base image.

The B1 CLI is deliberately narrow.  It accepts a digest-bound QH5-B
transaction, three digest-bound host tools, and one absent owner-private root
below ``/srv/storage/k1s``.  It downloads four exact dated HTTPS objects,
verifies the signed checksum set with the pinned Canonical fingerprint, hashes
the requested image and package manifest, inspects the qcow2 without starting
it, and writes a canonical evidence seal.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.parse import urlsplit

API_VERSION = "k1s.dev/q1-base-acquisition/v1"
SCHEMA_VERSION = 1
SPEC_KIND = "K1sQ1BaseAcquisitionSpec"
PLAN_KIND = "K1sQ1BaseAcquisitionPlan"
RECEIPT_KIND = "K1sQ1BaseAcquisitionReceipt"
SEAL_KIND = "K1sQ1BaseAcquisitionSeal"
TRANSACTION_SCHEMA = "k1s-qh5b-guest-image-transaction/v1"
QUALIFICATION_POSTURE = "source-validated/unqualified"

DEFAULT_OUTPUT_PARENT = Path("/srv/storage/k1s")
OUTPUT_RE = re.compile(r"qh5b-b1-1\.0\.0rc2-[0-9a-f]{8}-20[0-9]{6}T[0-9]{6}Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
OBJECT_RE = re.compile(r"[0-9a-f]{40}")
SERIAL_RE = re.compile(r"20[0-9]{6}")
FINGERPRINT_RE = re.compile(r"[0-9A-F]{40}")

IMAGE_FILENAME = "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img"
MANIFEST_FILENAME = "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.manifest"
METADATA_FILENAMES = ("SHA256SUMS", "SHA256SUMS.gpg")
DOWNLOAD_ORDER = (*METADATA_FILENAMES, MANIFEST_FILENAME, IMAGE_FILENAME)
TOOL_NAMES = ("curl", "gpgv", "qemuImg")
MAX_TRANSACTION_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 128 * 1024
METADATA_TIMEOUT_SECONDS = 5 * 60
IMAGE_TIMEOUT_SECONDS = 60 * 60
COMMAND_TIMEOUT_SECONDS = 2 * 60
MIN_VIRTUAL_SIZE = 1024 * 1024 * 1024
MAX_VIRTUAL_SIZE = 64 * 1024 * 1024 * 1024

PROTECTED_NAMES = {
    ".env",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    ".envrc",
    "ae-ha-core.env",
    "ae.env",
}


class BaseAcquisitionPolicyError(RuntimeError):
    """The B1 request or evidence failed the closed acquisition contract."""


def _fail() -> BaseAcquisitionPolicyError:
    return BaseAcquisitionPolicyError("K1s Q1 base acquisition failed closed")


def canonical_json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    return (text + "\n").encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _json(raw: bytes, *, maximum: int, canonical: bool) -> dict[str, object]:
    if not 1 <= len(raw) <= maximum:
        raise _fail()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or (canonical and canonical_json_bytes(value) != raw):
        raise _fail()
    return cast(dict[str, object], value)


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    return cast(dict[str, object], value)


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _hex_digest(value: object) -> str:
    if type(value) is not str or HEX_DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return f"sha256:{value}"


def _object_id(value: object) -> str:
    if type(value) is not str or OBJECT_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _bind(value: Mapping[str, object], field: str) -> dict[str, object]:
    if field in value:
        raise _fail()
    result = dict(value)
    result[field] = _digest_bytes(canonical_json_bytes(result))
    return result


def _verify_bound(value: object, keys: set[str], field: str) -> dict[str, object]:
    result = _closed(value, {*keys, field})
    unsigned = dict(result)
    actual = _digest(unsigned.pop(field))
    if actual != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    return result


def _absolute_path(value: object) -> Path:
    if type(value) is not str or not value.startswith("/") or value in {"/", "//"}:
        raise _fail()
    if value.endswith("/") or "\\" in value or "\x00" in value:
        raise _fail()
    pure = PurePosixPath(value)
    if pure.as_posix() != value or any(part in {"", ".", ".."} for part in pure.parts):
        raise _fail()
    if pure.name in PROTECTED_NAMES:
        raise _fail()
    return Path(value)


def _path_chain_no_symlinks(path: Path, *, include_leaf: bool = True) -> None:
    current = Path("/")
    parts = path.parts[1:] if include_leaf else path.parts[1:-1]
    for part in parts:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise _fail() from exc
        if stat.S_ISLNK(info.st_mode):
            raise _fail()


def _hash_descriptor(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = os.read(descriptor, min(remaining, 1024 * 1024))
        if not block:
            raise _fail()
        digest.update(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise _fail()
    return f"sha256:{digest.hexdigest()}"


def _read_descriptor(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        block = os.read(descriptor, min(remaining, 1024 * 1024))
        if not block:
            raise _fail()
        chunks.append(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise _fail()
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: str
    sha256: str
    size: int
    mode: str
    uid: int
    gid: int
    device: int
    inode: int

    def as_dict(self) -> dict[str, object]:
        return {
            "device": self.device,
            "gid": self.gid,
            "inode": self.inode,
            "mode": self.mode,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "uid": self.uid,
        }


def file_identity(
    path_value: object,
    *,
    expected_digest: object | None = None,
    maximum: int,
    executable: bool = False,
    allowed_modes: set[int] | None = None,
) -> FileIdentity:
    path = _absolute_path(path_value)
    _path_chain_no_symlinks(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= maximum
            or mode & 0o022
            or (allowed_modes is not None and mode not in allowed_modes)
        ):
            raise _fail()
        if executable and not mode & 0o100:
            raise _fail()
        if not executable and mode & 0o111:
            raise _fail()
        actual = _hash_descriptor(descriptor, opened.st_size)
        if expected_digest is not None and actual != _digest(expected_digest):
            raise _fail()
        return FileIdentity(
            path=str(path),
            sha256=actual,
            size=opened.st_size,
            mode=f"{mode:04o}",
            uid=opened.st_uid,
            gid=opened.st_gid,
            device=opened.st_dev,
            inode=opened.st_ino,
        )
    finally:
        os.close(descriptor)


def _identity(value: object, *, maximum: int) -> FileIdentity:
    item = _closed(
        value,
        {"device", "gid", "inode", "mode", "path", "sha256", "size", "uid"},
    )
    mode = item["mode"]
    if type(mode) is not str or re.fullmatch(r"0[0-7]{3}", mode) is None:
        raise _fail()
    for key in ("device", "gid", "inode", "size", "uid"):
        if type(item[key]) is not int or cast(int, item[key]) < 0:
            raise _fail()
    if not 1 <= cast(int, item["size"]) <= maximum:
        raise _fail()
    return FileIdentity(
        path=str(_absolute_path(item["path"])),
        sha256=_digest(item["sha256"]),
        size=cast(int, item["size"]),
        mode=mode,
        uid=cast(int, item["uid"]),
        gid=cast(int, item["gid"]),
        device=cast(int, item["device"]),
        inode=cast(int, item["inode"]),
    )


def _read_identity(identity: FileIdentity, *, maximum: int) -> bytes:
    if identity.size > maximum:
        raise _fail()
    current = file_identity(
        identity.path,
        expected_digest=identity.sha256,
        maximum=maximum,
        executable=bool(int(identity.mode, 8) & 0o100),
    )
    if current != identity:
        raise _fail()
    try:
        descriptor = os.open(identity.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        return _read_descriptor(descriptor, identity.size)
    finally:
        os.close(descriptor)


def _tool(
    value: object,
    *,
    name: str,
    expected_paths: Mapping[str, str],
) -> FileIdentity:
    item = _closed(value, {"expectedSha256", "path"})
    if item["path"] != expected_paths[name]:
        raise _fail()
    return file_identity(
        item["path"],
        expected_digest=item["expectedSha256"],
        maximum=64 * 1024 * 1024,
        executable=True,
    )


def _dated_url(value: object, *, serial: str, filename: str) -> str:
    if type(value) is not str or len(value) > 2048:
        raise _fail()
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "cloud-images.ubuntu.com"
        or parsed.path != f"/releases/jammy/release-{serial}/{filename}"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _fail()
    return value


def _transaction(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != TRANSACTION_SCHEMA:
        raise _fail()
    candidate = _closed(
        value.get("canonicalBaseCandidate"),
        {
            "architecture",
            "filename",
            "format",
            "headObservedAt",
            "headStatus",
            "imageContentLength",
            "imageSha256",
            "imageUrl",
            "immutableDirectory",
            "packageManifestSha256",
            "packageManifestUrl",
            "serial",
            "sha256sumsUrl",
            "signatureUrl",
            "status",
        },
    )
    serial = candidate["serial"]
    if type(serial) is not str or SERIAL_RE.fullmatch(serial) is None:
        raise _fail()
    if (
        candidate["architecture"] != "amd64"
        or candidate["filename"] != IMAGE_FILENAME
        or candidate["format"] != "qcow2"
        or candidate["headStatus"] != 200
        or type(candidate["imageContentLength"]) is not int
        or not 1 <= candidate["imageContentLength"] <= MAX_IMAGE_BYTES
    ):
        raise _fail()
    image_url = _dated_url(candidate["imageUrl"], serial=serial, filename=IMAGE_FILENAME)
    manifest_url = _dated_url(
        candidate["packageManifestUrl"],
        serial=serial,
        filename=MANIFEST_FILENAME,
    )
    sums_url = _dated_url(candidate["sha256sumsUrl"], serial=serial, filename="SHA256SUMS")
    signature_url = _dated_url(
        candidate["signatureUrl"],
        serial=serial,
        filename="SHA256SUMS.gpg",
    )
    signer = _closed(
        value.get("canonicalSigner"),
        {"fingerprint", "keyring", "keyringSha256", "locallyPresentFingerprints"},
    )
    fingerprint = signer["fingerprint"]
    if type(fingerprint) is not str or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise _fail()
    keyring_path = _absolute_path(signer["keyring"])
    keyring_digest = _hex_digest(signer["keyringSha256"])
    return {
        "candidate": {
            "image": {
                "filename": IMAGE_FILENAME,
                "sha256": _hex_digest(candidate["imageSha256"]),
                "size": candidate["imageContentLength"],
                "url": image_url,
            },
            "packageManifest": {
                "filename": MANIFEST_FILENAME,
                "sha256": _hex_digest(candidate["packageManifestSha256"]),
                "url": manifest_url,
            },
            "serial": serial,
            "sha256sumsUrl": sums_url,
            "signatureUrl": signature_url,
        },
        "signer": {
            "fingerprint": fingerprint,
            "keyring": str(keyring_path),
            "keyringSha256": keyring_digest,
        },
    }


def build_plan(
    spec_value: object,
    *,
    allowed_output_parent: Path = DEFAULT_OUTPUT_PARENT,
    expected_tool_paths: Mapping[str, str] | None = None,
) -> dict[str, object]:
    spec = _closed(
        spec_value,
        {
            "apiVersion",
            "kind",
            "outputName",
            "outputParent",
            "schemaVersion",
            "source",
            "tools",
            "transaction",
        },
    )
    if (
        spec["apiVersion"] != API_VERSION
        or spec["kind"] != SPEC_KIND
        or spec["schemaVersion"] != SCHEMA_VERSION
    ):
        raise _fail()
    source = _closed(spec["source"], {"commit", "tree", "version"})
    _object_id(source["commit"])
    _object_id(source["tree"])
    if source["version"] != "1.0.0rc2":
        raise _fail()
    parent = _absolute_path(spec["outputParent"])
    if parent != allowed_output_parent:
        raise _fail()
    _path_chain_no_symlinks(parent)
    info = os.stat(parent, follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o002
        or not os.access(parent, os.W_OK | os.X_OK)
    ):
        raise _fail()
    output_name = spec["outputName"]
    if (
        type(output_name) is not str
        or OUTPUT_RE.fullmatch(output_name) is None
        or f"-{cast(str, source['commit'])[:8]}-" not in output_name
    ):
        raise _fail()
    output_root = parent / output_name
    failure_path = parent / f"{output_name}.failure.json"
    if (
        output_root.exists()
        or output_root.is_symlink()
        or failure_path.exists()
        or failure_path.is_symlink()
    ):
        raise _fail()
    transaction_spec = _closed(spec["transaction"], {"expectedSha256", "path"})
    transaction_identity = file_identity(
        transaction_spec["path"],
        expected_digest=transaction_spec["expectedSha256"],
        maximum=MAX_TRANSACTION_BYTES,
    )
    transaction = _transaction(
        _json(
            _read_identity(transaction_identity, maximum=MAX_TRANSACTION_BYTES),
            maximum=MAX_TRANSACTION_BYTES,
            canonical=False,
        )
    )
    signer = cast(dict[str, object], transaction["signer"])
    keyring = file_identity(
        signer["keyring"],
        expected_digest=signer["keyringSha256"],
        maximum=64 * 1024 * 1024,
        allowed_modes={0o644},
    )
    tools_raw = _closed(spec["tools"], set(TOOL_NAMES))
    tool_paths = expected_tool_paths or {
        "curl": "/usr/bin/curl",
        "gpgv": "/usr/bin/gpgv",
        "qemuImg": "/usr/bin/qemu-img",
    }
    if set(tool_paths) != set(TOOL_NAMES):
        raise _fail()
    tools = {
        name: _tool(tools_raw[name], name=name, expected_paths=tool_paths).as_dict()
        for name in TOOL_NAMES
    }
    candidate = cast(dict[str, object], transaction["candidate"])
    image = cast(dict[str, object], candidate["image"])
    package_manifest = cast(dict[str, object], candidate["packageManifest"])
    downloads = [
        {
            "filename": "SHA256SUMS",
            "maximumBytes": 64 * 1024,
            "url": candidate["sha256sumsUrl"],
        },
        {
            "filename": "SHA256SUMS.gpg",
            "maximumBytes": 64 * 1024,
            "url": candidate["signatureUrl"],
        },
        {
            "expectedSha256": package_manifest["sha256"],
            "filename": MANIFEST_FILENAME,
            "maximumBytes": MAX_METADATA_BYTES,
            "url": package_manifest["url"],
        },
        {
            "expectedSha256": image["sha256"],
            "expectedSize": image["size"],
            "filename": IMAGE_FILENAME,
            "maximumBytes": MAX_IMAGE_BYTES,
            "url": image["url"],
        },
    ]
    plan: dict[str, object] = {
        "apiVersion": API_VERSION,
        "candidate": candidate,
        "downloads": downloads,
        "failurePath": str(failure_path),
        "keyring": keyring.as_dict(),
        "kind": PLAN_KIND,
        "limits": {
            "commandOutputBytes": MAX_COMMAND_OUTPUT_BYTES,
            "commandTimeoutSeconds": COMMAND_TIMEOUT_SECONDS,
            "imageTimeoutSeconds": IMAGE_TIMEOUT_SECONDS,
            "metadataTimeoutSeconds": METADATA_TIMEOUT_SECONDS,
        },
        "mutating": True,
        "outputRoot": str(output_root),
        "qualificationPosture": QUALIFICATION_POSTURE,
        "schemaVersion": SCHEMA_VERSION,
        "signerFingerprint": signer["fingerprint"],
        "source": source,
        "tools": tools,
        "transaction": transaction_identity.as_dict(),
    }
    return _bind(plan, "planDigest")


def verify_plan(
    value: object,
    *,
    allowed_output_parent: Path = DEFAULT_OUTPUT_PARENT,
) -> dict[str, object]:
    keys = {
        "apiVersion",
        "candidate",
        "downloads",
        "failurePath",
        "keyring",
        "kind",
        "limits",
        "mutating",
        "outputRoot",
        "qualificationPosture",
        "schemaVersion",
        "signerFingerprint",
        "source",
        "tools",
        "transaction",
    }
    plan = _verify_bound(value, keys, "planDigest")
    if (
        plan["apiVersion"] != API_VERSION
        or plan["kind"] != PLAN_KIND
        or plan["schemaVersion"] != SCHEMA_VERSION
        or plan["mutating"] is not True
        or plan["qualificationPosture"] != QUALIFICATION_POSTURE
    ):
        raise _fail()
    source = _closed(plan["source"], {"commit", "tree", "version"})
    _object_id(source["commit"])
    _object_id(source["tree"])
    if source["version"] != "1.0.0rc2":
        raise _fail()
    fingerprint = plan["signerFingerprint"]
    if type(fingerprint) is not str or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise _fail()
    candidate = _closed(
        plan["candidate"],
        {"image", "packageManifest", "serial", "sha256sumsUrl", "signatureUrl"},
    )
    serial = candidate["serial"]
    if type(serial) is not str or SERIAL_RE.fullmatch(serial) is None:
        raise _fail()
    image = _closed(candidate["image"], {"filename", "sha256", "size", "url"})
    package_manifest = _closed(
        candidate["packageManifest"],
        {"filename", "sha256", "url"},
    )
    if image["filename"] != IMAGE_FILENAME or package_manifest["filename"] != MANIFEST_FILENAME:
        raise _fail()
    _digest(image["sha256"])
    _digest(package_manifest["sha256"])
    if type(image["size"]) is not int or not 1 <= image["size"] <= MAX_IMAGE_BYTES:
        raise _fail()
    _dated_url(image["url"], serial=serial, filename=IMAGE_FILENAME)
    _dated_url(package_manifest["url"], serial=serial, filename=MANIFEST_FILENAME)
    _dated_url(candidate["sha256sumsUrl"], serial=serial, filename="SHA256SUMS")
    _dated_url(candidate["signatureUrl"], serial=serial, filename="SHA256SUMS.gpg")
    _identity(plan["keyring"], maximum=64 * 1024 * 1024)
    _identity(plan["transaction"], maximum=MAX_TRANSACTION_BYTES)
    tools = _closed(plan["tools"], set(TOOL_NAMES))
    for value_item in tools.values():
        _identity(value_item, maximum=64 * 1024 * 1024)
    downloads = plan["downloads"]
    if type(downloads) is not list or len(downloads) != 4:
        raise _fail()
    if [cast(dict[str, object], row).get("filename") for row in downloads] != list(DOWNLOAD_ORDER):
        raise _fail()
    for row in downloads:
        if type(row) is not dict:
            raise _fail()
        keys_for_row = {"filename", "maximumBytes", "url"}
        if row["filename"] == MANIFEST_FILENAME:
            keys_for_row.add("expectedSha256")
        if row["filename"] == IMAGE_FILENAME:
            keys_for_row.update({"expectedSha256", "expectedSize"})
        item = _closed(row, keys_for_row)
        filename = cast(str, item["filename"])
        _dated_url(item["url"], serial=serial, filename=filename)
        if (
            type(item["maximumBytes"]) is not int
            or not 1 <= item["maximumBytes"] <= MAX_IMAGE_BYTES
        ):
            raise _fail()
        if "expectedSha256" in item:
            _digest(item["expectedSha256"])
        if "expectedSize" in item and (
            type(item["expectedSize"]) is not int
            or not 1 <= item["expectedSize"] <= item["maximumBytes"]
        ):
            raise _fail()
    limits = _closed(
        plan["limits"],
        {
            "commandOutputBytes",
            "commandTimeoutSeconds",
            "imageTimeoutSeconds",
            "metadataTimeoutSeconds",
        },
    )
    if limits != {
        "commandOutputBytes": MAX_COMMAND_OUTPUT_BYTES,
        "commandTimeoutSeconds": COMMAND_TIMEOUT_SECONDS,
        "imageTimeoutSeconds": IMAGE_TIMEOUT_SECONDS,
        "metadataTimeoutSeconds": METADATA_TIMEOUT_SECONDS,
    }:
        raise _fail()
    root = _absolute_path(plan["outputRoot"])
    failure = _absolute_path(plan["failurePath"])
    if (
        root.parent != allowed_output_parent
        or OUTPUT_RE.fullmatch(root.name) is None
        or f"-{cast(str, source['commit'])[:8]}-" not in root.name
        or failure != root.parent / f"{root.name}.failure.json"
    ):
        raise _fail()
    return plan


@dataclass(frozen=True, slots=True)
class CommandObservation:
    argv: tuple[str, ...]
    exit_code: int
    timed_out: bool
    stdout: bytes = b""
    stderr: bytes = b""


class CommandAdapter(Protocol):
    def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> CommandObservation: ...


class Clock(Protocol):
    def time(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    def time(self) -> int:
        return int(time.time())


@dataclass(frozen=True, slots=True)
class HostCommandAdapter:
    def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> CommandObservation:
        environment = {
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
        process = subprocess.Popen(  # noqa: S603 - argv and executable are closed by the plan.
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        if len(stdout) + len(stderr) > maximum_output_bytes:
            raise _fail()
        return CommandObservation(argv, process.returncode, timed_out, stdout, stderr)


def _execute(
    adapter: CommandAdapter,
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout: int,
) -> CommandObservation:
    if not argv or any(not item or "\x00" in item for item in argv):
        raise _fail()
    try:
        observation = adapter.execute(
            argv,
            cwd=str(cwd),
            timeout_seconds=timeout,
            maximum_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
        )
    except BaseAcquisitionPolicyError:
        raise
    except Exception as exc:
        raise _fail() from exc
    if (
        type(observation) is not CommandObservation
        or type(observation.argv) is not tuple
        or observation.argv != argv
        or type(observation.exit_code) is not int
        or observation.exit_code != 0
        or type(observation.timed_out) is not bool
        or observation.timed_out
        or type(observation.stdout) is not bytes
        or type(observation.stderr) is not bytes
        or len(observation.stdout) + len(observation.stderr) > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise _fail()
    return observation


def _write_absent(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    if path.name in PROTECTED_NAMES or path.exists() or path.is_symlink():
        raise _fail()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
    try:
        written = 0
        while written < len(raw):
            count = os.write(descriptor, raw[written:])
            if count <= 0:
                raise _fail()
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_failed_root(plan: Mapping[str, object]) -> None:
    """Remove only the closed set of files B1 may create in its new root."""

    root = _absolute_path(plan["outputRoot"])
    failure = _absolute_path(plan["failurePath"])
    if (
        root.parent != failure.parent
        or failure != root.parent / f"{root.name}.failure.json"
        or OUTPUT_RE.fullmatch(root.name) is None
    ):
        raise _fail()
    _path_chain_no_symlinks(root)
    parent_info = os.stat(root.parent, follow_symlinks=False)
    root_info = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
        or root_info.st_dev != parent_info.st_dev
    ):
        raise _fail()
    allowed_names = {
        *DOWNLOAD_ORDER,
        *(f".{name}.partial" for name in DOWNLOAD_ORDER),
        "B1-ACQUISITION-RECEIPT.json",
        "B1-EVIDENCE-SEAL.json",
        "B1-LOCAL-SHA256SUMS",
    }
    descriptor = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        names = sorted(entry.name for entry in os.scandir(descriptor))
        if any(name not in allowed_names for name in names):
            raise _fail()
        for name in names:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_dev != root_info.st_dev
                or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            ):
                raise _fail()
        for name in names:
            os.unlink(name, dir_fd=descriptor)
        os.fsync(descriptor)
    except OSError as exc:
        raise _fail() from exc
    finally:
        os.close(descriptor)
    try:
        os.rmdir(root)
    except OSError as exc:
        raise _fail() from exc


def _record_failure(path: Path, failure: Mapping[str, object]) -> bool:
    try:
        _write_absent(path, canonical_json_bytes(failure))
    except Exception:
        return False
    return True


def _download(
    plan: Mapping[str, object],
    item: Mapping[str, object],
    *,
    root: Path,
    adapter: CommandAdapter,
) -> FileIdentity:
    filename = cast(str, item["filename"])
    maximum = cast(int, item["maximumBytes"])
    temporary = root / f".{filename}.partial"
    final = root / filename
    _write_absent(temporary, b"")
    curl = _identity(cast(dict[str, object], plan["tools"])["curl"], maximum=64 * 1024 * 1024)
    timeout = IMAGE_TIMEOUT_SECONDS if filename == IMAGE_FILENAME else METADATA_TIMEOUT_SECONDS
    argv = (
        curl.path,
        "-q",
        "--proto",
        "=https",
        "--tlsv1.2",
        "--fail",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "30",
        "--max-time",
        str(timeout),
        "--max-filesize",
        str(maximum),
        "--output",
        str(temporary),
        cast(str, item["url"]),
    )
    _execute(adapter, argv, cwd=root, timeout=timeout + 10)
    expected = item.get("expectedSha256")
    identity = file_identity(
        str(temporary),
        expected_digest=expected,
        maximum=maximum,
        allowed_modes={0o600},
    )
    if "expectedSize" in item and identity.size != item["expectedSize"]:
        raise _fail()
    os.rename(temporary, final)
    os.chmod(final, 0o400, follow_symlinks=False)
    return file_identity(
        str(final),
        expected_digest=identity.sha256,
        maximum=maximum,
        allowed_modes={0o400},
    )


def _signed_checksums(raw: bytes, requested: Mapping[str, str]) -> None:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise _fail() from exc
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if match is None:
            raise _fail()
        filename = match.group(2)
        if filename in rows:
            raise _fail()
        rows[filename] = f"sha256:{match.group(1)}"
    for filename, expected in requested.items():
        if rows.get(filename) != expected:
            raise _fail()


def _signature_fingerprint(stdout: bytes, expected: str) -> str:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail() from exc
    fingerprints: list[str] = []
    for line in text.splitlines():
        prefix = "[GNUPG:] VALIDSIG "
        if line.startswith(prefix):
            fields = line.removeprefix(prefix).split()
            if not fields or FINGERPRINT_RE.fullmatch(fields[0]) is None:
                raise _fail()
            fingerprints.append(fields[0])
    if fingerprints != [expected]:
        raise _fail()
    return expected


def _inspect_image(
    plan: Mapping[str, object],
    image: FileIdentity,
    *,
    root: Path,
    adapter: CommandAdapter,
) -> dict[str, object]:
    qemu = _identity(cast(dict[str, object], plan["tools"])["qemuImg"], maximum=64 * 1024 * 1024)
    argv = (qemu.path, "info", "--output=json", image.path)
    observed = _execute(adapter, argv, cwd=root, timeout=COMMAND_TIMEOUT_SECONDS)
    value = _json(observed.stdout, maximum=MAX_COMMAND_OUTPUT_BYTES, canonical=False)
    if value.get("format") != "qcow2" or value.get("filename") != image.path:
        raise _fail()
    if value.get("backing-filename") not in {None, ""} or value.get(
        "full-backing-filename"
    ) not in {
        None,
        "",
    }:
        raise _fail()
    virtual_size = value.get("virtual-size")
    actual_size = value.get("actual-size")
    if (
        type(virtual_size) is not int
        or not MIN_VIRTUAL_SIZE <= virtual_size <= MAX_VIRTUAL_SIZE
        or type(actual_size) is not int
        or not 0 <= actual_size <= MAX_IMAGE_BYTES
    ):
        raise _fail()
    current = file_identity(
        image.path,
        expected_digest=image.sha256,
        maximum=MAX_IMAGE_BYTES,
        allowed_modes={0o400},
    )
    if current != image:
        raise _fail()
    return {
        "actualSize": actual_size,
        "format": "qcow2",
        "hasBackingFile": False,
        "virtualSize": virtual_size,
    }


def _artifact(identity: FileIdentity) -> dict[str, object]:
    return {
        "filename": Path(identity.path).name,
        "mode": identity.mode,
        "sha256": identity.sha256,
        "size": identity.size,
    }


def run_acquisition(
    plan_value: object,
    *,
    adapter: CommandAdapter,
    clock: Clock | None = None,
    allowed_output_parent: Path = DEFAULT_OUTPUT_PARENT,
) -> dict[str, object]:
    plan = verify_plan(plan_value, allowed_output_parent=allowed_output_parent)
    root = _absolute_path(plan["outputRoot"])
    if root.exists() or root.is_symlink():
        raise _fail()
    os.mkdir(root, 0o700)
    os.chmod(root, 0o700, follow_symlinks=False)
    active_clock = clock or SystemClock()
    started = active_clock.time()
    try:
        downloads = cast(list[dict[str, object]], plan["downloads"])
        artifacts: dict[str, FileIdentity] = {}
        for item in downloads[:2]:
            artifacts[cast(str, item["filename"])] = _download(
                plan,
                item,
                root=root,
                adapter=adapter,
            )
            verify_plan(plan, allowed_output_parent=allowed_output_parent)
        gpgv = _identity(cast(dict[str, object], plan["tools"])["gpgv"], maximum=64 * 1024 * 1024)
        keyring = _identity(plan["keyring"], maximum=64 * 1024 * 1024)
        signature_argv = (
            gpgv.path,
            "--status-fd",
            "1",
            "--keyring",
            keyring.path,
            artifacts["SHA256SUMS.gpg"].path,
            artifacts["SHA256SUMS"].path,
        )
        signature = _execute(
            adapter,
            signature_argv,
            cwd=root,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        fingerprint = _signature_fingerprint(
            signature.stdout,
            cast(str, plan["signerFingerprint"]),
        )
        requested = {
            cast(str, row["filename"]): cast(str, row["expectedSha256"]) for row in downloads[2:]
        }
        sums_raw = _read_identity(artifacts["SHA256SUMS"], maximum=64 * 1024)
        _signed_checksums(sums_raw, requested)
        for item in downloads[2:]:
            artifacts[cast(str, item["filename"])] = _download(
                plan,
                item,
                root=root,
                adapter=adapter,
            )
            verify_plan(plan, allowed_output_parent=allowed_output_parent)
        image_info = _inspect_image(
            plan,
            artifacts[IMAGE_FILENAME],
            root=root,
            adapter=adapter,
        )
        rows = [_artifact(artifacts[name]) for name in sorted(artifacts)]
        receipt: dict[str, object] = {
            "apiVersion": API_VERSION,
            "artifacts": rows,
            "completedAtUnix": active_clock.time(),
            "image": image_info,
            "kind": RECEIPT_KIND,
            "planDigest": plan["planDigest"],
            "qualificationPosture": QUALIFICATION_POSTURE,
            "schemaVersion": SCHEMA_VERSION,
            "signature": {
                "fingerprint": fingerprint,
                "stderrSha256": _digest_bytes(signature.stderr),
                "stderrSize": len(signature.stderr),
                "stdoutSha256": _digest_bytes(signature.stdout),
                "stdoutSize": len(signature.stdout),
            },
            "source": plan["source"],
            "startedAtUnix": started,
            "status": "acquired-and-authenticated",
        }
        receipt = _bind(receipt, "receiptDigest")
        receipt_path = root / "B1-ACQUISITION-RECEIPT.json"
        _write_absent(receipt_path, canonical_json_bytes(receipt))
        os.chmod(receipt_path, 0o400, follow_symlinks=False)
        receipt_identity = file_identity(
            str(receipt_path),
            maximum=MAX_METADATA_BYTES,
            allowed_modes={0o400},
        )
        sum_rows = [*rows, _artifact(receipt_identity)]
        sums = "".join(
            f"{cast(str, row['sha256']).removeprefix('sha256:')}  {row['filename']}\n"
            for row in sorted(sum_rows, key=lambda row: cast(str, row["filename"]).encode())
        ).encode("ascii")
        local_sums_path = root / "B1-LOCAL-SHA256SUMS"
        _write_absent(local_sums_path, sums)
        os.chmod(local_sums_path, 0o400, follow_symlinks=False)
        sums_identity = file_identity(
            str(local_sums_path),
            maximum=MAX_METADATA_BYTES,
            allowed_modes={0o400},
        )
        seal = _bind(
            {
                "apiVersion": API_VERSION,
                "kind": SEAL_KIND,
                "localSumsSha256": sums_identity.sha256,
                "memberCount": len(sum_rows),
                "planDigest": plan["planDigest"],
                "qualificationPosture": QUALIFICATION_POSTURE,
                "receiptFileSha256": receipt_identity.sha256,
                "receiptId": receipt["receiptDigest"],
                "schemaVersion": SCHEMA_VERSION,
            },
            "sealDigest",
        )
        seal_path = root / "B1-EVIDENCE-SEAL.json"
        _write_absent(seal_path, canonical_json_bytes(seal))
        os.chmod(seal_path, 0o400, follow_symlinks=False)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {"receipt": receipt, "seal": seal}
    except Exception as exc:
        failure = {
            "apiVersion": API_VERSION,
            "completedAtUnix": active_clock.time(),
            "kind": "K1sQ1BaseAcquisitionFailure",
            "outputName": root.name,
            "planDigest": plan["planDigest"],
            "schemaVersion": SCHEMA_VERSION,
            "status": "failed-closed",
        }
        failure_recorded = _record_failure(
            _absolute_path(plan["failurePath"]),
            failure,
        )
        if failure_recorded:
            with contextlib.suppress(Exception):
                _remove_failed_root(plan)
        if isinstance(exc, BaseAcquisitionPolicyError):
            raise
        raise _fail() from exc


def verify_receipt(value: object, *, expected_plan_digest: object) -> dict[str, object]:
    keys = {
        "apiVersion",
        "artifacts",
        "completedAtUnix",
        "image",
        "kind",
        "planDigest",
        "qualificationPosture",
        "schemaVersion",
        "signature",
        "source",
        "startedAtUnix",
        "status",
    }
    receipt = _verify_bound(value, keys, "receiptDigest")
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != RECEIPT_KIND
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or receipt["qualificationPosture"] != QUALIFICATION_POSTURE
        or receipt["status"] != "acquired-and-authenticated"
        or receipt["planDigest"] != _digest(expected_plan_digest)
    ):
        raise _fail()
    artifacts = receipt["artifacts"]
    if type(artifacts) is not list or len(artifacts) != 4:
        raise _fail()
    if [cast(dict[str, object], row).get("filename") for row in artifacts] != sorted(
        DOWNLOAD_ORDER
    ):
        raise _fail()
    for raw in artifacts:
        row = _closed(raw, {"filename", "mode", "sha256", "size"})
        if (
            row["mode"] != "0400"
            or type(row["size"]) is not int
            or not 1 <= row["size"] <= MAX_IMAGE_BYTES
        ):
            raise _fail()
        _digest(row["sha256"])
    signature = _closed(
        receipt["signature"],
        {"fingerprint", "stderrSha256", "stderrSize", "stdoutSha256", "stdoutSize"},
    )
    if (
        type(signature["fingerprint"]) is not str
        or FINGERPRINT_RE.fullmatch(signature["fingerprint"]) is None
    ):
        raise _fail()
    for name in ("stderrSha256", "stdoutSha256"):
        _digest(signature[name])
    for name in ("stderrSize", "stdoutSize"):
        size = signature[name]
        if type(size) is not int or not 0 <= size <= MAX_COMMAND_OUTPUT_BYTES:
            raise _fail()
    image = _closed(receipt["image"], {"actualSize", "format", "hasBackingFile", "virtualSize"})
    if (
        image["format"] != "qcow2"
        or image["hasBackingFile"] is not False
        or type(image["actualSize"]) is not int
        or not 0 <= image["actualSize"] <= MAX_IMAGE_BYTES
        or type(image["virtualSize"]) is not int
        or not MIN_VIRTUAL_SIZE <= image["virtualSize"] <= MAX_VIRTUAL_SIZE
    ):
        raise _fail()
    source = _closed(receipt["source"], {"commit", "tree", "version"})
    _object_id(source["commit"])
    _object_id(source["tree"])
    for name in ("startedAtUnix", "completedAtUnix"):
        timestamp = receipt[name]
        if type(timestamp) is not int or timestamp < 0:
            raise _fail()
    if cast(int, receipt["completedAtUnix"]) < cast(int, receipt["startedAtUnix"]):
        raise _fail()
    return receipt


def verify_acquisition_root(
    plan_value: object,
    *,
    expected_seal_file_digest: object,
    allowed_output_parent: Path = DEFAULT_OUTPUT_PARENT,
) -> dict[str, object]:
    plan = verify_plan(plan_value, allowed_output_parent=allowed_output_parent)
    root = _absolute_path(plan["outputRoot"])
    _path_chain_no_symlinks(root)
    root_info = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise _fail()
    expected_names = {
        *DOWNLOAD_ORDER,
        "B1-ACQUISITION-RECEIPT.json",
        "B1-EVIDENCE-SEAL.json",
        "B1-LOCAL-SHA256SUMS",
    }
    try:
        names = {entry.name for entry in os.scandir(root)}
    except OSError as exc:
        raise _fail() from exc
    if names != expected_names:
        raise _fail()
    downloads = cast(list[dict[str, object]], plan["downloads"])
    actual_artifacts: list[dict[str, object]] = []
    for row in downloads:
        identity = file_identity(
            str(root / cast(str, row["filename"])),
            expected_digest=row.get("expectedSha256"),
            maximum=cast(int, row["maximumBytes"]),
            allowed_modes={0o400},
        )
        if "expectedSize" in row and identity.size != row["expectedSize"]:
            raise _fail()
        actual_artifacts.append(_artifact(identity))
    actual_artifacts.sort(key=lambda row: cast(str, row["filename"]).encode())
    receipt_path = root / "B1-ACQUISITION-RECEIPT.json"
    receipt_identity = file_identity(
        str(receipt_path),
        maximum=MAX_METADATA_BYTES,
        allowed_modes={0o400},
    )
    receipt = _json(
        _read_identity(receipt_identity, maximum=MAX_METADATA_BYTES),
        maximum=MAX_METADATA_BYTES,
        canonical=True,
    )
    verify_receipt(receipt, expected_plan_digest=plan["planDigest"])
    if receipt["artifacts"] != actual_artifacts or receipt["source"] != plan["source"]:
        raise _fail()
    expected_sum_rows = [*actual_artifacts, _artifact(receipt_identity)]
    expected_sums = "".join(
        f"{cast(str, row['sha256']).removeprefix('sha256:')}  {row['filename']}\n"
        for row in sorted(
            expected_sum_rows,
            key=lambda row: cast(str, row["filename"]).encode(),
        )
    ).encode("ascii")
    sums_identity = file_identity(
        str(root / "B1-LOCAL-SHA256SUMS"),
        maximum=MAX_METADATA_BYTES,
        allowed_modes={0o400},
    )
    if _read_identity(sums_identity, maximum=MAX_METADATA_BYTES) != expected_sums:
        raise _fail()
    seal_path = root / "B1-EVIDENCE-SEAL.json"
    seal_identity = file_identity(
        str(seal_path),
        expected_digest=expected_seal_file_digest,
        maximum=MAX_METADATA_BYTES,
        allowed_modes={0o400},
    )
    seal_value = _json(
        _read_identity(seal_identity, maximum=MAX_METADATA_BYTES),
        maximum=MAX_METADATA_BYTES,
        canonical=True,
    )
    seal = _verify_bound(
        seal_value,
        {
            "apiVersion",
            "kind",
            "localSumsSha256",
            "memberCount",
            "planDigest",
            "qualificationPosture",
            "receiptFileSha256",
            "receiptId",
            "schemaVersion",
        },
        "sealDigest",
    )
    if (
        seal["apiVersion"] != API_VERSION
        or seal["kind"] != SEAL_KIND
        or seal["schemaVersion"] != SCHEMA_VERSION
        or seal["qualificationPosture"] != QUALIFICATION_POSTURE
        or seal["planDigest"] != plan["planDigest"]
        or seal["localSumsSha256"] != sums_identity.sha256
        or seal["receiptFileSha256"] != receipt_identity.sha256
        or seal["receiptId"] != receipt["receiptDigest"]
        or seal["memberCount"] != len(expected_sum_rows)
    ):
        raise _fail()
    return {
        "artifacts": actual_artifacts,
        "receipt": receipt,
        "seal": seal,
        "sealFileSha256": seal_identity.sha256,
    }


def _read_json_file(path: Path, *, expected_digest: object, maximum: int) -> dict[str, object]:
    identity = file_identity(
        str(path),
        expected_digest=expected_digest,
        maximum=maximum,
    )
    return _json(_read_identity(identity, maximum=maximum), maximum=maximum, canonical=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="create a canonical B1 acquisition plan")
    plan.add_argument("--spec", type=Path, required=True)
    plan.add_argument("--expect-spec-sha256", required=True)
    plan.add_argument("--output", type=Path, required=True)
    acquire = sub.add_parser("acquire", help="execute one exact B1 acquisition plan")
    acquire.add_argument("--plan", type=Path, required=True)
    acquire.add_argument("--expect-plan-sha256", required=True)
    acquire.add_argument("--confirm", required=True)
    verify = sub.add_parser("verify-receipt", help="verify a canonical B1 receipt")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--expect-receipt-sha256", required=True)
    verify.add_argument("--expect-plan-digest", required=True)
    root = sub.add_parser("verify-root", help="independently replay a retained B1 root")
    root.add_argument("--plan", type=Path, required=True)
    root.add_argument("--expect-plan-sha256", required=True)
    root.add_argument("--expect-seal-file-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            spec = _read_json_file(
                args.spec,
                expected_digest=args.expect_spec_sha256,
                maximum=MAX_TRANSACTION_BYTES,
            )
            plan = build_plan(spec)
            _write_absent(args.output, canonical_json_bytes(plan))
            print(
                json.dumps({"planDigest": plan["planDigest"], "status": "planned"}, sort_keys=True)
            )
            return 0
        if args.command == "acquire":
            plan_value = _read_json_file(
                args.plan,
                expected_digest=args.expect_plan_sha256,
                maximum=MAX_TRANSACTION_BYTES,
            )
            plan = verify_plan(plan_value)
            if args.confirm != f"ACQUIRE-Q1-BASE:{plan['planDigest']}":
                raise _fail()
            result = run_acquisition(plan, adapter=HostCommandAdapter())
            seal = cast(dict[str, object], result["seal"])
            print(
                json.dumps({"sealDigest": seal["sealDigest"], "status": "acquired"}, sort_keys=True)
            )
            return 0
        if args.command == "verify-receipt":
            receipt = _read_json_file(
                args.receipt,
                expected_digest=args.expect_receipt_sha256,
                maximum=MAX_METADATA_BYTES,
            )
            verified = verify_receipt(receipt, expected_plan_digest=args.expect_plan_digest)
            print(
                json.dumps(
                    {"receiptDigest": verified["receiptDigest"], "status": "verified"},
                    sort_keys=True,
                )
            )
            return 0
        plan_value = _read_json_file(
            args.plan,
            expected_digest=args.expect_plan_sha256,
            maximum=MAX_TRANSACTION_BYTES,
        )
        replay = verify_acquisition_root(
            plan_value,
            expected_seal_file_digest=args.expect_seal_file_sha256,
        )
        print(
            json.dumps(
                {
                    "sealDigest": cast(dict[str, object], replay["seal"])["sealDigest"],
                    "status": "replayed",
                },
                sort_keys=True,
            )
        )
        return 0
    except BaseAcquisitionPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
