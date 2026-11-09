#!/usr/bin/env python3
"""Plan and validate deterministic K1s Q1 guest-image construction.

B0 defines the private build authority, two-lane order, adapter boundary, and
canonical receipt used by a later live image builder.  This module deliberately
ships no QEMU/KVM or package-acquisition adapter.  Its CLI can create and verify
plans and verify synthetic/live receipts; tests use an in-memory adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.parse import urlsplit

try:
    from scripts.qualification import kp1_q1_capsule as q1_capsule
except ModuleNotFoundError:  # Direct execution from scripts/qualification.
    import kp1_q1_capsule as q1_capsule  # type: ignore[import-not-found,no-redef]

API_VERSION = "k1s.dev/q1-guest-image-build/v2"
BOM_API_VERSION = "k1s.dev/q1-guest-build-bom/v1"
SCHEMA_VERSION = 2
BOM_SCHEMA_VERSION = 1
SPEC_KIND = "K1sQ1GuestImageBuildSpec"
PLAN_KIND = "K1sQ1GuestImageBuildPlan"
RECEIPT_KIND = "K1sQ1GuestImageBuildReceipt"
BOM_KIND = "K1sQ1GuestBuildBOM"
SOURCE_DATE_EPOCH = 1_793_664_000
CANDIDATE_VERSION = "0.1.8a1"

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_INPUT_BYTES = 8 * 1024 * 1024 * 1024
MAX_STAGE_OUTPUT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024 * 1024
STAGE_TIMEOUT_SECONDS = 20 * 60
TOTAL_TIMEOUT_SECONDS = 2 * 60 * 60

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
OBJECT_RE = re.compile(r"[0-9a-f]{40}")
SERIAL_RE = re.compile(r"20[0-9]{6}")
FINGERPRINT_RE = re.compile(r"[0-9A-F]{40}")
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,127}")

LANES = (("lane-a", "0022"), ("lane-b", "0077"))
STAGES = (
    "authenticate-base",
    "validate-offline-bom",
    "stage-inputs",
    "create-image",
    "provision-offline",
    "finalize-image",
    "extract-inventory",
    "validate-guest-manifest",
)
INPUT_NAMES = {
    "acceptedSdist",
    "acceptedWheel",
    "b1AcquisitionPlan",
    "b1EvidenceSeal",
    "b2AcquisitionPlan",
    "b2EvidenceSeal",
    "baseAcquisitionModule",
    "baseImage",
    "guestBomModule",
    "guestBundle",
    "guestCapsuleModule",
    "guestHelper",
    "guestImageModule",
    "guestInputsModule",
    "filesystemManifestModule",
    "inventoryModule",
    "liveBuilder",
    "offlineBom",
    "offlinePayload",
    "packageManifest",
    "requirementsLock",
    "signedChecksums",
    "signedChecksumsSignature",
    "wheelhouseManifest",
}
TOOL_NAMES = {"gpgv", "python", "qemu", "qemuImg", "seedBuilder"}
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


class GuestImagePolicyError(RuntimeError):
    """The image-build request or evidence failed the closed B0 contract."""


def _fail() -> GuestImagePolicyError:
    return GuestImagePolicyError("K1s Q1 guest-image build failed closed")


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


def _canonical_json(raw: bytes, *, maximum: int = MAX_JSON_BYTES) -> dict[str, object]:
    if not 1 <= len(raw) <= maximum:
        raise _fail()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return cast(dict[str, object], value)


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _object_id(value: object) -> str:
    if type(value) is not str or OBJECT_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    return cast(dict[str, object], value)


def _bind(value: Mapping[str, object], *, digest_field: str) -> dict[str, object]:
    if digest_field in value:
        raise _fail()
    result = dict(value)
    result[digest_field] = _digest_bytes(canonical_json_bytes(result))
    return result


def _verify_bound(value: object, *, keys: set[str], digest_field: str) -> dict[str, object]:
    result = _closed(value, {*keys, digest_field})
    unsigned = dict(result)
    actual = _digest(unsigned.pop(digest_field))
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


def _path_chain_no_symlinks(path: Path) -> None:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise _fail() from exc
        if stat.S_ISLNK(info.st_mode):
            raise _fail()


def _read_fd(descriptor: int, size: int) -> bytes:
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


def _hash_fd(descriptor: int, size: int) -> str:
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
    expected_digest: object,
    executable: bool = False,
) -> FileIdentity:
    path = _absolute_path(path_value)
    expected = _digest(expected_digest)
    _path_chain_no_symlinks(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _fail()
        if not 1 <= opened.st_size <= MAX_INPUT_BYTES or mode & 0o022:
            raise _fail()
        if executable and not mode & 0o100:
            raise _fail()
        if not executable and mode & 0o111:
            raise _fail()
        actual = _hash_fd(descriptor, opened.st_size)
        if actual != expected:
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


def _identity(value: object) -> FileIdentity:
    item = _closed(
        value,
        {"device", "gid", "inode", "mode", "path", "sha256", "size", "uid"},
    )
    path = _absolute_path(item["path"])
    mode = item["mode"]
    integers = [item[key] for key in ("device", "gid", "inode", "size", "uid")]
    if type(mode) is not str or re.fullmatch(r"0[0-7]{3}", mode) is None:
        raise _fail()
    if any(type(number) is not int or number < 0 for number in integers):
        raise _fail()
    if not 1 <= cast(int, item["size"]) <= MAX_INPUT_BYTES:
        raise _fail()
    return FileIdentity(
        path=str(path),
        sha256=_digest(item["sha256"]),
        size=cast(int, item["size"]),
        mode=mode,
        uid=cast(int, item["uid"]),
        gid=cast(int, item["gid"]),
        device=cast(int, item["device"]),
        inode=cast(int, item["inode"]),
    )


def _revalidate(identity: FileIdentity, *, executable: bool = False) -> FileIdentity:
    current = file_identity(
        identity.path,
        expected_digest=identity.sha256,
        executable=executable,
    )
    if current != identity:
        raise _fail()
    return current


def _private_parent(value: object) -> dict[str, object]:
    path = _absolute_path(value)
    _path_chain_no_symlinks(path)
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _fail() from exc
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or mode != 0o700
        or not os.access(path, os.W_OK | os.X_OK)
    ):
        raise _fail()
    return {
        "device": info.st_dev,
        "gid": info.st_gid,
        "inode": info.st_ino,
        "mode": "0700",
        "path": str(path),
        "uid": info.st_uid,
    }


def _validate_parent_identity(value: object) -> dict[str, object]:
    item = _closed(value, {"device", "gid", "inode", "mode", "path", "uid"})
    if item["mode"] != "0700":
        raise _fail()
    _absolute_path(item["path"])
    for key in ("device", "gid", "inode", "uid"):
        if type(item[key]) is not int or cast(int, item[key]) < 0:
            raise _fail()
    return item


def _immutable_url(value: object, *, serial: str, filename: str) -> str:
    if type(value) is not str or len(value) > 2048:
        raise _fail()
    parsed = urlsplit(value)
    expected = f"/releases/jammy/release-{serial}/{filename}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "cloud-images.ubuntu.com"
        or parsed.path != expected
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _fail()
    return value


def validate_base_provenance(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {
            "architecture",
            "imageFilename",
            "imageSha256",
            "imageUrl",
            "packageManifestFilename",
            "packageManifestSha256",
            "packageManifestUrl",
            "serial",
            "sha256sumsUrl",
            "signatureUrl",
            "signerFingerprint",
        },
    )
    serial = item["serial"]
    if type(serial) is not str or SERIAL_RE.fullmatch(serial) is None:
        raise _fail()
    if item["architecture"] != "amd64":
        raise _fail()
    image_filename = item["imageFilename"]
    manifest_filename = item["packageManifestFilename"]
    if image_filename != "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img":
        raise _fail()
    if manifest_filename != "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.manifest":
        raise _fail()
    _digest(item["imageSha256"])
    _digest(item["packageManifestSha256"])
    fingerprint = item["signerFingerprint"]
    if type(fingerprint) is not str or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise _fail()
    _immutable_url(item["imageUrl"], serial=serial, filename=image_filename)
    _immutable_url(item["packageManifestUrl"], serial=serial, filename=manifest_filename)
    _immutable_url(item["sha256sumsUrl"], serial=serial, filename="SHA256SUMS")
    _immutable_url(item["signatureUrl"], serial=serial, filename="SHA256SUMS.gpg")
    return item


def _component(value: object) -> dict[str, object]:
    item = _closed(value, {"sha256", "version"})
    version = item["version"]
    if type(version) is not str or VERSION_RE.fullmatch(version) is None:
        raise _fail()
    return {"sha256": _digest(item["sha256"]), "version": version}


def validate_guest_template(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {"cni", "helper", "images", "kernel", "os", "python", "registry", "runtime", "wheelhouse"},
    )
    helper = _closed(item["helper"], {"sha256"})
    wheelhouse = _closed(item["wheelhouse"], {"manifestSha256", "requirementsLockSha256"})
    dummy_image = "sha256:" + "0" * 64
    manifest = {
        "apiVersion": q1_capsule.API_VERSION,
        "image": {"format": "qcow2", "sha256": dummy_image},
        "kind": q1_capsule.GUEST_MANIFEST_KIND,
        "schemaVersion": q1_capsule.SCHEMA_VERSION,
        **item,
    }
    try:
        normalized = q1_capsule.validate_guest_manifest(
            manifest,
            image_digest=dummy_image,
            helper_digest=_digest(helper["sha256"]),
            wheelhouse_manifest_digest=_digest(wheelhouse["manifestSha256"]),
        )
    except Exception as exc:
        raise _fail() from exc
    normalized.pop("apiVersion")
    normalized.pop("image")
    normalized.pop("kind")
    normalized.pop("schemaVersion")
    return normalized


def validate_bom(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {
            "apiVersion",
            "guest",
            "kind",
            "payload",
            "schemaVersion",
            "sourceCommit",
            "sourceDateEpoch",
            "sourceTree",
        },
    )
    if (
        item["apiVersion"] != BOM_API_VERSION
        or item["kind"] != BOM_KIND
        or item["schemaVersion"] != BOM_SCHEMA_VERSION
        or item["sourceDateEpoch"] != SOURCE_DATE_EPOCH
    ):
        raise _fail()
    _object_id(item["sourceCommit"])
    _object_id(item["sourceTree"])
    payload = _closed(item["payload"], {"memberCount", "sha256", "size"})
    if (
        type(payload["memberCount"]) is not int
        or not 1 <= payload["memberCount"] <= 4096
        or type(payload["size"]) is not int
        or not 1 <= payload["size"] <= MAX_INPUT_BYTES
    ):
        raise _fail()
    _digest(payload["sha256"])
    return {**item, "guest": validate_guest_template(item["guest"])}


def _read_canonical_identity(identity: FileIdentity) -> dict[str, object]:
    _revalidate(identity)
    if identity.size > MAX_JSON_BYTES:
        raise _fail()
    try:
        descriptor = os.open(identity.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            identity.device,
            identity.inode,
            identity.size,
        ):
            raise _fail()
        return _canonical_json(_read_fd(descriptor, identity.size))
    finally:
        os.close(descriptor)


def _input_spec(value: object, *, executable: bool = False) -> FileIdentity:
    item = _closed(value, {"expectedSha256", "path"})
    return file_identity(
        item["path"],
        expected_digest=item["expectedSha256"],
        executable=executable,
    )


def build_plan(spec_value: object) -> dict[str, object]:
    spec = _closed(
        spec_value,
        {
            "apiVersion",
            "base",
            "inputs",
            "kind",
            "outputParent",
            "schemaVersion",
            "source",
            "sourceDateEpoch",
            "tools",
        },
    )
    if (
        spec["apiVersion"] != API_VERSION
        or spec["kind"] != SPEC_KIND
        or spec["schemaVersion"] != SCHEMA_VERSION
        or spec["sourceDateEpoch"] != SOURCE_DATE_EPOCH
    ):
        raise _fail()
    source = _closed(spec["source"], {"commit", "tree", "version"})
    _object_id(source["commit"])
    _object_id(source["tree"])
    if source["version"] != CANDIDATE_VERSION:
        raise _fail()
    base = validate_base_provenance(spec["base"])
    inputs_raw = _closed(spec["inputs"], INPUT_NAMES)
    tools_raw = _closed(spec["tools"], TOOL_NAMES)
    inputs = {name: _input_spec(inputs_raw[name]).as_dict() for name in sorted(INPUT_NAMES)}
    tools = {
        name: _input_spec(tools_raw[name], executable=True).as_dict() for name in sorted(TOOL_NAMES)
    }
    if inputs["baseImage"]["sha256"] != base["imageSha256"]:
        raise _fail()
    if inputs["packageManifest"]["sha256"] != base["packageManifestSha256"]:
        raise _fail()
    bom_identity = _identity(inputs["offlineBom"])
    bom = validate_bom(_read_canonical_identity(bom_identity))
    if bom["sourceCommit"] != source["commit"] or bom["sourceTree"] != source["tree"]:
        raise _fail()
    payload = cast(dict[str, object], bom["payload"])
    if payload["sha256"] != inputs["offlinePayload"]["sha256"]:
        raise _fail()
    if payload["size"] != inputs["offlinePayload"]["size"]:
        raise _fail()
    guest = cast(dict[str, object], bom["guest"])
    helper = _closed(guest["helper"], {"sha256"})
    if helper["sha256"] != inputs["guestHelper"]["sha256"]:
        raise _fail()
    wheelhouse = cast(dict[str, object], guest["wheelhouse"])
    if wheelhouse["manifestSha256"] != inputs["wheelhouseManifest"]["sha256"]:
        raise _fail()
    if wheelhouse["requirementsLockSha256"] != inputs["requirementsLock"]["sha256"]:
        raise _fail()
    parent = _private_parent(spec["outputParent"])
    lanes: list[dict[str, object]] = []
    for lane, umask in LANES:
        root = Path(cast(str, parent["path"])) / lane
        if root.exists() or root.is_symlink():
            raise _fail()
        lanes.append(
            {
                "name": lane,
                "root": str(root),
                "stages": list(STAGES),
                "umask": umask,
            }
        )
    plan: dict[str, object] = {
        "apiVersion": API_VERSION,
        "base": base,
        "bomSha256": bom_identity.sha256,
        "inputs": inputs,
        "kind": PLAN_KIND,
        "lanes": lanes,
        "limits": {
            "imageBytes": MAX_IMAGE_BYTES,
            "stageOutputBytes": MAX_STAGE_OUTPUT_BYTES,
            "stageTimeoutSeconds": STAGE_TIMEOUT_SECONDS,
            "totalTimeoutSeconds": TOTAL_TIMEOUT_SECONDS,
        },
        "mutating": True,
        "outputParent": parent,
        "qualificationPosture": "source-validated/unqualified",
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "sourceDateEpoch": SOURCE_DATE_EPOCH,
        "tools": tools,
    }
    return _bind(plan, digest_field="planDigest")


def verify_plan(value: object) -> dict[str, object]:
    keys = {
        "apiVersion",
        "base",
        "bomSha256",
        "inputs",
        "kind",
        "lanes",
        "limits",
        "mutating",
        "outputParent",
        "qualificationPosture",
        "schemaVersion",
        "source",
        "sourceDateEpoch",
        "tools",
    }
    plan = _verify_bound(value, keys=keys, digest_field="planDigest")
    if (
        plan["apiVersion"] != API_VERSION
        or plan["kind"] != PLAN_KIND
        or plan["schemaVersion"] != SCHEMA_VERSION
        or plan["mutating"] is not True
        or plan["qualificationPosture"] != "source-validated/unqualified"
        or plan["sourceDateEpoch"] != SOURCE_DATE_EPOCH
    ):
        raise _fail()
    validate_base_provenance(plan["base"])
    _digest(plan["bomSha256"])
    source = _closed(plan["source"], {"commit", "tree", "version"})
    _object_id(source["commit"])
    _object_id(source["tree"])
    if source["version"] != CANDIDATE_VERSION:
        raise _fail()
    inputs = _closed(plan["inputs"], INPUT_NAMES)
    tools = _closed(plan["tools"], TOOL_NAMES)
    for value_item in inputs.values():
        _identity(value_item)
    for value_item in tools.values():
        _identity(value_item)
    parent = _validate_parent_identity(plan["outputParent"])
    lanes = plan["lanes"]
    if type(lanes) is not list or len(lanes) != len(LANES):
        raise _fail()
    for raw, expected in zip(lanes, LANES, strict=True):
        lane = _closed(raw, {"name", "root", "stages", "umask"})
        if (
            lane["name"] != expected[0]
            or lane["umask"] != expected[1]
            or lane["stages"] != list(STAGES)
            or lane["root"] != str(Path(cast(str, parent["path"])) / expected[0])
        ):
            raise _fail()
    limits = _closed(
        plan["limits"],
        {"imageBytes", "stageOutputBytes", "stageTimeoutSeconds", "totalTimeoutSeconds"},
    )
    if limits != {
        "imageBytes": MAX_IMAGE_BYTES,
        "stageOutputBytes": MAX_STAGE_OUTPUT_BYTES,
        "stageTimeoutSeconds": STAGE_TIMEOUT_SECONDS,
        "totalTimeoutSeconds": TOTAL_TIMEOUT_SECONDS,
    }:
        raise _fail()
    return plan


def revalidate_plan_files(plan_value: object) -> dict[str, object]:
    plan = verify_plan(plan_value)
    inputs = cast(dict[str, object], plan["inputs"])
    tools = cast(dict[str, object], plan["tools"])
    for value in inputs.values():
        _revalidate(_identity(value))
    for value in tools.values():
        _revalidate(_identity(value), executable=True)
    current_parent = _private_parent(cast(dict[str, object], plan["outputParent"])["path"])
    if current_parent != plan["outputParent"]:
        raise _fail()
    for raw in cast(list[object], plan["lanes"]):
        root = Path(cast(str, cast(dict[str, object], raw)["root"]))
        if root.exists() or root.is_symlink():
            raise _fail()
    return plan


@dataclass(frozen=True, slots=True)
class StageRequest:
    authority: Mapping[str, object]
    plan: Mapping[str, object]
    lane: str
    root: str
    stage: str
    umask: str
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class StageObservation:
    identity: Mapping[str, object]
    exit_code: int
    timed_out: bool
    stdout: bytes = b""
    stderr: bytes = b""
    artifacts: Mapping[str, object] | None = None


class BuildAdapter(Protocol):
    def execute(self, request: StageRequest) -> StageObservation: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_time(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_time(self) -> int:
        return int(time.time())


def stage_identity(plan: Mapping[str, object], *, lane: str, stage: str) -> dict[str, object]:
    source = cast(dict[str, object], plan["source"])
    return {
        "bomSha256": plan["bomSha256"],
        "lane": lane,
        "planDigest": plan["planDigest"],
        "sourceCommit": source["commit"],
        "sourceTree": source["tree"],
        "stage": stage,
    }


def _stage_artifacts(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {
            "filesystemManifestSha256",
            "filesystemPolicySha256",
            "guestManifestSha256",
            "guestSemanticSha256",
            "imageSha256",
            "imageSize",
            "inventorySha256",
        },
    )
    for key in (
        "filesystemManifestSha256",
        "filesystemPolicySha256",
        "guestManifestSha256",
        "guestSemanticSha256",
        "imageSha256",
        "inventorySha256",
    ):
        _digest(item[key])
    if type(item["imageSize"]) is not int or not 1 <= item["imageSize"] <= MAX_IMAGE_BYTES:
        raise _fail()
    return item


def _shared_artifacts(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {
            "filesystemManifestSha256",
            "filesystemPolicySha256",
            "guestSemanticSha256",
        },
    )
    for key in item:
        _digest(item[key])
    return item


def _shared_from_lane(value: Mapping[str, object]) -> dict[str, object]:
    return _shared_artifacts(
        {
            "filesystemManifestSha256": value["filesystemManifestSha256"],
            "filesystemPolicySha256": value["filesystemPolicySha256"],
            "guestSemanticSha256": value["guestSemanticSha256"],
        }
    )


def run_build(
    plan_value: object,
    *,
    adapter: BuildAdapter,
    clock: Clock | None = None,
    revalidate_files: bool = True,
) -> dict[str, object]:
    plan = revalidate_plan_files(plan_value) if revalidate_files else verify_plan(plan_value)
    active_clock = clock or SystemClock()
    started = active_clock.wall_time()
    start_monotonic = active_clock.monotonic()
    lane_receipts: list[dict[str, object]] = []
    for lane_raw in cast(list[object], plan["lanes"]):
        lane_value = cast(dict[str, object], lane_raw)
        lane = cast(str, lane_value["name"])
        stage_receipts: list[dict[str, object]] = []
        final_artifacts: dict[str, object] | None = None
        for stage in STAGES:
            stage_started = active_clock.monotonic()
            if stage_started - start_monotonic >= TOTAL_TIMEOUT_SECONDS:
                raise _fail()
            identity = stage_identity(plan, lane=lane, stage=stage)
            request = StageRequest(
                authority=identity,
                plan=plan,
                lane=lane,
                root=cast(str, lane_value["root"]),
                stage=stage,
                umask=cast(str, lane_value["umask"]),
                deadline_monotonic=stage_started + STAGE_TIMEOUT_SECONDS,
            )
            try:
                observation = adapter.execute(request)
            except Exception as exc:
                raise _fail() from exc
            verify_plan(plan)
            elapsed = active_clock.monotonic() - stage_started
            total_elapsed = active_clock.monotonic() - start_monotonic
            if (
                type(observation) is not StageObservation
                or type(observation.identity) is not dict
                or dict(observation.identity) != identity
                or type(observation.exit_code) is not int
                or observation.exit_code != 0
                or type(observation.timed_out) is not bool
                or observation.timed_out
                or type(observation.stdout) is not bytes
                or type(observation.stderr) is not bytes
                or elapsed < 0
                or elapsed >= STAGE_TIMEOUT_SECONDS
                or total_elapsed < 0
                or total_elapsed >= TOTAL_TIMEOUT_SECONDS
                or len(observation.stdout) + len(observation.stderr) > MAX_STAGE_OUTPUT_BYTES
            ):
                raise _fail()
            if stage == STAGES[-1]:
                final_artifacts = _stage_artifacts(observation.artifacts)
            elif observation.artifacts is not None:
                raise _fail()
            stage_receipts.append(
                {
                    "durationMilliseconds": int(elapsed * 1000),
                    "identity": identity,
                    "outputSha256": _digest_bytes(
                        observation.stdout + b"\x00" + observation.stderr
                    ),
                    "outputSize": len(observation.stdout) + len(observation.stderr),
                    "status": "passed",
                }
            )
        if final_artifacts is None:
            raise _fail()
        lane_receipts.append(
            {
                "artifacts": final_artifacts,
                "name": lane,
                "stages": stage_receipts,
                "umask": lane_value["umask"],
            }
        )
    first = cast(dict[str, object], lane_receipts[0]["artifacts"])
    second = cast(dict[str, object], lane_receipts[1]["artifacts"])
    shared = _shared_from_lane(first)
    if shared != _shared_from_lane(second):
        raise _fail()
    unsigned: dict[str, object] = {
        "apiVersion": API_VERSION,
        "artifacts": shared,
        "completedAtUnix": active_clock.wall_time(),
        "kind": RECEIPT_KIND,
        "lanes": lane_receipts,
        "planDigest": plan["planDigest"],
        "qualificationPosture": "source-validated/unqualified",
        "schemaVersion": SCHEMA_VERSION,
        "startedAtUnix": started,
        "status": "built-and-verified",
    }
    return _bind(unsigned, digest_field="receiptDigest")


def verify_receipt(value: object, *, expected_plan_digest: object) -> dict[str, object]:
    keys = {
        "apiVersion",
        "artifacts",
        "completedAtUnix",
        "kind",
        "lanes",
        "planDigest",
        "qualificationPosture",
        "schemaVersion",
        "startedAtUnix",
        "status",
    }
    receipt = _verify_bound(value, keys=keys, digest_field="receiptDigest")
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != RECEIPT_KIND
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or receipt["status"] != "built-and-verified"
        or receipt["qualificationPosture"] != "source-validated/unqualified"
        or receipt["planDigest"] != _digest(expected_plan_digest)
    ):
        raise _fail()
    for key in ("startedAtUnix", "completedAtUnix"):
        if type(receipt[key]) is not int or cast(int, receipt[key]) < 0:
            raise _fail()
    if cast(int, receipt["completedAtUnix"]) < cast(int, receipt["startedAtUnix"]):
        raise _fail()
    artifacts = _shared_artifacts(receipt["artifacts"])
    lanes = receipt["lanes"]
    if type(lanes) is not list or len(lanes) != len(LANES):
        raise _fail()
    for raw, expected in zip(lanes, LANES, strict=True):
        lane = _closed(raw, {"artifacts", "name", "stages", "umask"})
        if lane["name"] != expected[0] or lane["umask"] != expected[1]:
            raise _fail()
        lane_artifacts = _stage_artifacts(lane["artifacts"])
        if _shared_from_lane(lane_artifacts) != artifacts:
            raise _fail()
        stages = lane["stages"]
        if type(stages) is not list or len(stages) != len(STAGES):
            raise _fail()
        for stage_raw, stage_name in zip(stages, STAGES, strict=True):
            stage = _closed(
                stage_raw,
                {"durationMilliseconds", "identity", "outputSha256", "outputSize", "status"},
            )
            if stage["status"] != "passed":
                raise _fail()
            identity = _closed(
                stage["identity"],
                {"bomSha256", "lane", "planDigest", "sourceCommit", "sourceTree", "stage"},
            )
            if (
                identity["lane"] != expected[0]
                or identity["stage"] != stage_name
                or identity["planDigest"] != receipt["planDigest"]
            ):
                raise _fail()
            _digest(identity["bomSha256"])
            _object_id(identity["sourceCommit"])
            _object_id(identity["sourceTree"])
            _digest(stage["outputSha256"])
            if (
                type(stage["durationMilliseconds"]) is not int
                or not 0 <= stage["durationMilliseconds"] < STAGE_TIMEOUT_SECONDS * 1000
                or type(stage["outputSize"]) is not int
                or not 0 <= stage["outputSize"] <= MAX_STAGE_OUTPUT_BYTES
            ):
                raise _fail()
    return receipt


def _read_canonical_file(path: Path, *, expected_digest: str) -> dict[str, object]:
    identity = file_identity(str(path), expected_digest=expected_digest)
    return _read_canonical_identity(identity)


def _write_absent_private(path: Path, raw: bytes) -> None:
    safe_path = _absolute_path(str(path))
    parent = _private_parent(str(safe_path.parent))
    if parent["path"] != str(safe_path.parent) or safe_path.exists() or safe_path.is_symlink():
        raise _fail()
    descriptor = os.open(
        safe_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="create a private canonical B0 image-build plan")
    plan.add_argument("--spec", type=Path, required=True)
    plan.add_argument("--expect-spec-sha256", required=True)
    plan.add_argument("--output", type=Path, required=True)
    verify_plan_parser = sub.add_parser("verify-plan", help="verify a canonical build plan")
    verify_plan_parser.add_argument("--plan", type=Path, required=True)
    verify_plan_parser.add_argument("--expect-plan-sha256", required=True)
    verify_receipt_parser = sub.add_parser("verify-receipt", help="verify a build receipt")
    verify_receipt_parser.add_argument("--receipt", type=Path, required=True)
    verify_receipt_parser.add_argument("--expect-receipt-sha256", required=True)
    verify_receipt_parser.add_argument("--expect-plan-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            spec = _read_canonical_file(args.spec, expected_digest=args.expect_spec_sha256)
            plan = build_plan(spec)
            _write_absent_private(args.output, canonical_json_bytes(plan))
            print(
                json.dumps({"planDigest": plan["planDigest"], "status": "planned"}, sort_keys=True)
            )
            return 0
        if args.command == "verify-plan":
            plan_value = _read_canonical_file(args.plan, expected_digest=args.expect_plan_sha256)
            plan = verify_plan(plan_value)
            print(
                json.dumps({"planDigest": plan["planDigest"], "status": "verified"}, sort_keys=True)
            )
            return 0
        receipt_value = _read_canonical_file(
            args.receipt,
            expected_digest=args.expect_receipt_sha256,
        )
        receipt = verify_receipt(receipt_value, expected_plan_digest=args.expect_plan_digest)
        print(
            json.dumps(
                {"receiptDigest": receipt["receiptDigest"], "status": "verified"},
                sort_keys=True,
            )
        )
        return 0
    except GuestImagePolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
