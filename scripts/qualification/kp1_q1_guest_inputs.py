#!/usr/bin/env python3
"""Acquire, close, and replay the immutable K1s Q1 guest inputs.

This is the QH5-B B2 network boundary.  It creates one absent owner-private
root, uses an isolated APT state bound to a dated Ubuntu snapshot and one
signed deadsnakes PPA release, resolves a fixed linux/amd64 OCI set to expected
manifest digests, verifies the vendor checksum for crictl, and copies the
already accepted QH5-A closure.  It then delegates deterministic payload and
BOM construction to :mod:`kp1_q1_guest_bom`.

No package is installed on the host.  No container runtime, QEMU, KVM, guest,
service, socket, source mount, publication endpoint, tag, or push is used.
"""

# All subprocess paths are digest-bound by the canonical plan.  Every URL is
# independently constrained to HTTPS before urllib is called.
# ruff: noqa: S310, S603

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

try:
    from scripts.qualification import kp1_q1_base_acquisition as base_acquisition
    from scripts.qualification import kp1_q1_guest_bom as guest_bom
    from scripts.qualification import kp1_q1_guest_image as guest_image
except ModuleNotFoundError:  # Direct execution from scripts/qualification.
    import kp1_q1_base_acquisition as base_acquisition  # type: ignore[import-not-found,no-redef]
    import kp1_q1_guest_bom as guest_bom  # type: ignore[import-not-found,no-redef]
    import kp1_q1_guest_image as guest_image  # type: ignore[import-not-found,no-redef]

API_VERSION = "k1s.dev/q1-guest-input-acquisition/v1"
SCHEMA_VERSION = 1
SPEC_KIND = "K1sQ1GuestInputAcquisitionSpec"
PLAN_KIND = "K1sQ1GuestInputAcquisitionPlan"
RECEIPT_KIND = "K1sQ1GuestInputAcquisitionReceipt"
SEAL_KIND = "K1sQ1GuestInputAcquisitionSeal"
QUALIFICATION_POSTURE = "source-validated/unqualified"

CANDIDATE_VERSION = guest_image.CANDIDATE_VERSION
SOURCE_DATE_EPOCH = guest_image.SOURCE_DATE_EPOCH
DEFAULT_OUTPUT_PARENT = Path("/srv/storage/k1s")
OUTPUT_RE = re.compile(r"qh5b-b2-1\.0\.0a1-[0-9a-f]{8}-20[0-9]{6}T[0-9]{6}Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
OBJECT_RE = re.compile(r"[0-9a-f]{40}")
FINGERPRINT_RE = re.compile(r"[0-9A-F]{40}")
APT_PACKAGE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,63}")

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024 * 1024
APT_TIMEOUT_SECONDS = 60 * 60
COMMAND_TIMEOUT_SECONDS = 5 * 60
DOWNLOAD_TIMEOUT_SECONDS = 60 * 60
OCI_BLOB_TIMEOUT_SECONDS = 60 * 60
COPY_BLOCK_BYTES = 1024 * 1024

SNAPSHOT_ID = "20261107T000000Z"
UBUNTU_SNAPSHOT = f"https://snapshot.ubuntu.com/ubuntu/{SNAPSHOT_ID}"
DEADSNAKES_URI = "https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu"
UBUNTU_SIGNER = "F6ECB3762474EDA9D21B7022871920D1991BC93C"
DEADSNAKES_SIGNER = "F23C5A6CF475977595C89F51BA6932366A755776"
UBUNTU_KEYRING_SHA256 = "sha256:1a4dd63e5c76728960a2edddae22e2e0fc53df8e8b87806deb971030ac704eb0"
DEADSNAKES_KEYRING_SHA256 = (
    "sha256:5caefc9c4bb1dadde2e03579a1c9ac1ae0d3ae9c15350de361a89782f814c438"
)

INRELEASE_AUTHORITIES = {
    "ppa.launchpadcontent.net_deadsnakes_ppa_ubuntu_dists_jammy_InRelease": (
        "sha256:45c101cc4fd184e1adb32a943cb38180441d6ded5d6533aa40382c7c21095f92",
        DEADSNAKES_SIGNER,
        "deadsnakes",
    ),
    f"snapshot.ubuntu.com_ubuntu_{SNAPSHOT_ID}_dists_jammy-security_InRelease": (
        "sha256:06adcb08b355d32117d33b1ab948c12a835f822f62133ece84541ef254264b22",
        UBUNTU_SIGNER,
        "ubuntu",
    ),
    f"snapshot.ubuntu.com_ubuntu_{SNAPSHOT_ID}_dists_jammy-updates_InRelease": (
        "sha256:3929a0032c551dc25b0ce106c44547f227cea9cf2551323f1f9cb3e814c1409d",
        UBUNTU_SIGNER,
        "ubuntu",
    ),
    f"snapshot.ubuntu.com_ubuntu_{SNAPSHOT_ID}_dists_jammy_InRelease": (
        "sha256:c14060cd8c6d625874dfcb9523a35a395bf4865c28b6b6a82569ef326fe92dc6",
        UBUNTU_SIGNER,
        "ubuntu",
    ),
}

PRIMARY_PACKAGES = (
    ("acl", None),
    ("base-files", "12ubuntu4.7"),
    ("ca-certificates", None),
    ("containernetworking-plugins", "0.9.1+ds1-1ubuntu0.1"),
    ("containerd", "2.2.1-0ubuntu1~22.04.2"),
    ("dbus-user-session", None),
    ("docker.io", "29.1.3-0ubuntu3~22.04.2"),
    ("fuse-overlayfs", None),
    ("iptables", None),
    ("linux-image-5.15.0-1112-kvm", "5.15.0-1112.117"),
    ("linux-modules-5.15.0-1112-kvm", "5.15.0-1112.117"),
    ("podman", "3.4.4+ds1-1ubuntu1.22.04.3"),
    ("python3.11", "3.11.18-1+jammy1"),
    ("python3.11-venv", "3.11.18-1+jammy1"),
    ("runc", "1.3.4-0ubuntu1~22.04.1"),
    ("slirp4netns", None),
    ("uidmap", None),
)

RUNTIME_BINARY_PATHS = {
    "containerd": ("usr/bin/containerd",),
    "docker": ("usr/bin/docker",),
    "podman": ("usr/bin/podman",),
    "runc": ("usr/bin/runc",),
}

CRICTL_VERSION = "1.30.1"
CRICTL_FILENAME = f"crictl-v{CRICTL_VERSION}-linux-amd64.tar.gz"
CRICTL_SHA256 = "sha256:71873cdeeeb6c9ee0f79c27b45db38066da81f0c30dcda909b4eedc3aff63f59"
CRICTL_BASE_URL = (
    f"https://github.com/kubernetes-sigs/cri-tools/releases/download/v{CRICTL_VERSION}"
)

OCI_SOURCES = (
    (
        "busybox",
        "docker.io/library/busybox:1.36",
        "sha256:b7f3d86d6e84fc17718c48bcde1450807faa2d56704205c697b4bd5df7b9e29f",
        False,
    ),
    (
        "caddy",
        "docker.io/library/caddy:2.8",
        "sha256:b95ed06fbc6d74d24a40902090c8cc6086ce7d08ba60a3a7e8e62bf164a9d7bb",
        False,
    ),
    (
        "envoy",
        "docker.io/envoyproxy/envoy:v1.29-latest",
        "sha256:e47177609f491b9ae9112d02720ed8538679df2c6607a43d5a667b1fe90e800f",
        False,
    ),
    (
        "etcd",
        "quay.io/coreos/etcd:v3.5.13",
        "sha256:218e7e5382a601a0ac5b865b8020ea29308c6a88b2052732be8620365c0211dc",
        False,
    ),
    (
        "nats",
        "docker.io/library/nats:2.10",
        "sha256:c5b47eee3ccfeeeaac9635ea601fe9229dc947871ab36d1f73ca2f08eaa465a6",
        False,
    ),
    (
        "pause",
        "registry.k8s.io/pause:3.9",
        "sha256:8d4106c88ec0bd28001e34c975d65175d994072d65341f62a8ab0754b0fafe10",
        False,
    ),
    (
        "postgres",
        "docker.io/library/postgres:16",
        "sha256:43f9275df91e012c3da3d86bea5d7bb5e67a473296cc906c5f9e80b8176b1a37",
        False,
    ),
    (
        "rathole",
        "docker.io/rapiz1/rathole:v0.5.0",
        "sha256:52f3a087ccb89358705de1f6ac0e923a00d066fe9a8548ab7a79ad21adab93fa",
        False,
    ),
    (
        "registry",
        "docker.io/library/registry:2",
        "sha256:46faa9a1ae6813194b53921a370f2f4f8c5e1aae228a89bceafef5847a6a3278",
        True,
    ),
)

TOOL_NAMES = ("aptCache", "aptGet", "dpkgDeb", "gpgv")
INPUT_NAMES = {
    "acceptedSdist",
    "acceptedWheel",
    "baseAcquisitionReceipt",
    "basePackageManifest",
    "guestHelper",
    "qh5aCompletion",
    "requirementsLock",
    "wheelhouseManifest",
}

PROTECTED_NAMES = frozenset(
    {
        ".env",
        ".env.development",
        ".env.local",
        ".env.production",
        ".env.test",
        ".envrc",
        "ae-ha-core.env",
        "ae.env",
    }
)


class GuestInputPolicyError(RuntimeError):
    """The B2 acquisition, assembly, or replay failed closed."""


def _fail() -> GuestInputPolicyError:
    return GuestInputPolicyError("K1s Q1 guest input acquisition failed closed")


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


def _json(raw: bytes, *, canonical: bool, maximum: int = MAX_JSON_BYTES) -> dict[str, object]:
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


def _object_id(value: object) -> str:
    if type(value) is not str or OBJECT_RE.fullmatch(value) is None:
        raise _fail()
    return value


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


def _path_chain_no_links(path: Path, *, include_leaf: bool = True) -> None:
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
    if not 1 <= size <= MAX_DOWNLOAD_BYTES:
        raise _fail()
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = os.read(descriptor, min(remaining, COPY_BLOCK_BYTES))
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
    expected_sha256: object | None = None,
    executable: bool = False,
) -> FileIdentity:
    path = _absolute_path(path_value)
    _path_chain_no_links(path)
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
            or mode & 0o022
            or opened.st_size < 1
            or opened.st_size > MAX_DOWNLOAD_BYTES
            or (executable and not mode & 0o100)
            or (not executable and mode & 0o111)
        ):
            raise _fail()
        actual = _hash_descriptor(descriptor, opened.st_size)
        if expected_sha256 is not None and actual != _digest(expected_sha256):
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
    item = _closed(value, {"device", "gid", "inode", "mode", "path", "sha256", "size", "uid"})
    if (
        type(item["size"]) is not int
        or type(item["uid"]) is not int
        or type(item["gid"]) is not int
        or type(item["device"]) is not int
        or type(item["inode"]) is not int
        or item["mode"] not in {"0400", "0444", "0600", "0644", "0700", "0755"}
    ):
        raise _fail()
    return FileIdentity(
        path=str(_absolute_path(item["path"])),
        sha256=_digest(item["sha256"]),
        size=cast(int, item["size"]),
        mode=cast(str, item["mode"]),
        uid=cast(int, item["uid"]),
        gid=cast(int, item["gid"]),
        device=cast(int, item["device"]),
        inode=cast(int, item["inode"]),
    )


def _revalidate(identity: FileIdentity, *, executable: bool = False) -> None:
    current = file_identity(
        identity.path,
        expected_sha256=identity.sha256,
        executable=executable,
    )
    if current != identity:
        raise _fail()


def _input(value: object, *, executable: bool = False) -> FileIdentity:
    item = _closed(value, {"expectedSha256", "path"})
    return file_identity(
        item["path"],
        expected_sha256=item["expectedSha256"],
        executable=executable,
    )


def _private_parent(value: object, *, allowed: Path) -> Path:
    path = _absolute_path(value)
    _path_chain_no_links(path)
    try:
        info = os.lstat(path)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _fail() from exc
    if (
        path != allowed
        or resolved != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o002
        or not os.access(path, os.W_OK | os.X_OK, effective_ids=True)
    ):
        raise _fail()
    return path


def _source(value: object) -> dict[str, object]:
    item = _closed(value, {"commit", "tree", "version"})
    _object_id(item["commit"])
    _object_id(item["tree"])
    if item["version"] != CANDIDATE_VERSION:
        raise _fail()
    return item


def _wheelhouse(path_value: object, manifest_identity: FileIdentity) -> list[dict[str, object]]:
    root = _absolute_path(path_value)
    _path_chain_no_links(root)
    try:
        info = os.lstat(root)
        value = _json(Path(manifest_identity.path).read_bytes(), canonical=True)
    except OSError as exc:
        raise _fail() from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise _fail()
    rows = value.get("runtimeWheels")
    if (
        type(rows) is not list
        or value.get("runtimeWheelhouseCount") != len(rows)
        or len(rows) != 24
    ):
        raise _fail()
    result: list[dict[str, object]] = []
    names: list[str] = []
    for raw in rows:
        row = _closed(raw, {"filename", "sha256", "size"})
        filename = row["filename"]
        if type(filename) is not str or PurePosixPath(filename).name != filename:
            raise _fail()
        if type(row["size"]) is not int or not 1 <= row["size"] <= MAX_DOWNLOAD_BYTES:
            raise _fail()
        expected = row["sha256"]
        if type(expected) is not str or HEX_DIGEST_RE.fullmatch(expected) is None:
            raise _fail()
        identity = file_identity(
            str(root / filename),
            expected_sha256=f"sha256:{expected}",
        )
        if identity.size != row["size"]:
            raise _fail()
        names.append(filename)
        result.append(identity.as_dict())
    if names != sorted(set(names), key=lambda item: item.encode("utf-8")):
        raise _fail()
    return result


def _bind(value: Mapping[str, object], field: str) -> dict[str, object]:
    if field in value:
        raise _fail()
    result = dict(value)
    result[field] = _digest_bytes(canonical_json_bytes(result))
    return result


def _verify_bound(value: object, keys: set[str], field: str) -> dict[str, object]:
    item = _closed(value, {*keys, field})
    unsigned = dict(item)
    actual = _digest(unsigned.pop(field))
    if actual != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    return item


def _authority_json(identity: FileIdentity, *, canonical: bool) -> dict[str, object]:
    raw = _read_file(Path(identity.path))
    if len(raw) != identity.size or _digest_bytes(raw) != identity.sha256:
        raise _fail()
    return _json(raw, canonical=canonical)


def _hex_digest(value: object) -> str:
    if type(value) is not str or HEX_DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _validate_base_authority(inputs: Mapping[str, object]) -> None:
    receipt_identity = _identity(inputs["baseAcquisitionReceipt"])
    manifest_identity = _identity(inputs["basePackageManifest"])
    receipt = _authority_json(receipt_identity, canonical=True)
    try:
        verified = base_acquisition.verify_receipt(
            receipt,
            expected_plan_digest=receipt.get("planDigest"),
        )
    except Exception as exc:
        raise _fail() from exc
    source = cast(dict[str, object], verified["source"])
    if source.get("version") != CANDIDATE_VERSION:
        raise _fail()
    rows = [
        cast(dict[str, object], row)
        for row in cast(list[object], verified["artifacts"])
        if cast(dict[str, object], row).get("filename")
        == "ubuntu-22.04-server-cloudimg-amd64-disk-kvm.manifest"
    ]
    if len(rows) != 1:
        raise _fail()
    row = rows[0]
    if row.get("sha256") != manifest_identity.sha256 or row.get("size") != manifest_identity.size:
        raise _fail()


def _validate_qh5a_authority(inputs: Mapping[str, object], *, wheelhouse_count: int) -> None:
    completion = _authority_json(_identity(inputs["qh5aCompletion"]), canonical=False)
    required = {
        "acceptedCandidate",
        "artifactAssemblyStatus",
        "boundaries",
        "completedAt",
        "inputs",
        "installedSmoke",
        "liveQualificationStatus",
        "qualificationPosture",
        "releasePromotionStatus",
        "reproducibility",
        "schema",
        "setupCorrections",
        "source",
        "status",
        "toolchain",
    }
    if set(completion) != required or (
        completion["schema"] != "k1s-qh5a-completion/v1"
        or completion["status"] != "completed"
        or completion["artifactAssemblyStatus"] != "completed"
        or completion["liveQualificationStatus"] != "not-started"
        or completion["releasePromotionStatus"] != "not-started"
        or completion["qualificationPosture"] != QUALIFICATION_POSTURE
    ):
        raise _fail()
    source = _closed(
        completion["source"],
        {"branch", "commit", "fullStatus", "repository", "trackedStatus", "tree", "version"},
    )
    _object_id(source["commit"])
    _object_id(source["tree"])
    if source["version"] != CANDIDATE_VERSION or source["trackedStatus"] != "":
        raise _fail()
    boundaries = _closed(
        completion["boundaries"],
        {
            "artifactPublished",
            "hostServiceChanged",
            "packageIndexUsed",
            "qemuOrKvmInvoked",
            "runtimeWorkloadStarted",
            "sourceCommitCreated",
            "sourceFilesModified",
            "tagOrPushPerformed",
        },
    )
    if any(value is not False for value in boundaries.values()):
        raise _fail()
    reproducibility = cast(dict[str, object], completion["reproducibility"])
    installed_smoke = cast(dict[str, object], completion["installedSmoke"])
    if (
        reproducibility.get("byteIdentical") is not True
        or installed_smoke.get("status") != "passed"
        or installed_smoke.get("pipCheck") != "passed"
        or installed_smoke.get("networkIndexMode") != "--no-index"
    ):
        raise _fail()
    accepted = _closed(
        completion["acceptedCandidate"],
        {
            "artifactManifestSha256",
            "artifacts",
            "derivedFrom",
            "files",
            "independentVerificationReceiptSha256",
            "independentVerificationStatus",
            "path",
            "sha256sumsSha256",
            "sourceManifestSha256",
        },
    )
    if (
        accepted["independentVerificationStatus"] != "verified"
        or accepted["path"] != "accepted-candidate"
    ):
        raise _fail()
    for name in (
        "artifactManifestSha256",
        "independentVerificationReceiptSha256",
        "sha256sumsSha256",
        "sourceManifestSha256",
    ):
        _hex_digest(accepted[name])
    artifacts = _closed(accepted["artifacts"], {"sdist", "wheel"})
    expected_artifacts = {
        "sdist": (
            "acceptedSdist",
            "dist/k1s_core-1.0.0a1.tar.gz",
        ),
        "wheel": (
            "acceptedWheel",
            "dist/k1s_core-1.0.0a1-py3-none-any.whl",
        ),
    }
    for name, (input_name, filename) in expected_artifacts.items():
        row = _closed(
            artifacts[name],
            {"filename", "memberCount", "memberManifestSha256", "sha256", "size"},
        )
        identity = _identity(inputs[input_name])
        if (
            row["filename"] != filename
            or row["sha256"] != identity.sha256.removeprefix("sha256:")
            or row["size"] != identity.size
            or type(row["memberCount"]) is not int
            or cast(int, row["memberCount"]) < 1
        ):
            raise _fail()
        _hex_digest(row["memberManifestSha256"])
    authority_inputs = _closed(
        completion["inputs"],
        {
            "requirementsLockSha256",
            "runtimeWheelhouseCount",
            "runtimeWheelhouseIdentity",
            "wheelhouseManifestContentSha256",
            "wheelhouseManifestFileSha256",
            "wheelhouseReverificationReceiptSha256",
        },
    )
    for name in (
        "requirementsLockSha256",
        "runtimeWheelhouseIdentity",
        "wheelhouseManifestContentSha256",
        "wheelhouseManifestFileSha256",
        "wheelhouseReverificationReceiptSha256",
    ):
        _hex_digest(authority_inputs[name])
    if (
        authority_inputs["runtimeWheelhouseCount"] != wheelhouse_count
        or authority_inputs["requirementsLockSha256"]
        != _identity(inputs["requirementsLock"]).sha256.removeprefix("sha256:")
        or authority_inputs["wheelhouseManifestFileSha256"]
        != _identity(inputs["wheelhouseManifest"]).sha256.removeprefix("sha256:")
    ):
        raise _fail()


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
            "inputs",
            "kind",
            "outputName",
            "outputParent",
            "schemaVersion",
            "source",
            "tools",
            "wheelhouseRoot",
        },
    )
    if (
        spec["apiVersion"] != API_VERSION
        or spec["kind"] != SPEC_KIND
        or spec["schemaVersion"] != SCHEMA_VERSION
    ):
        raise _fail()
    source = _source(spec["source"])
    output_name = spec["outputName"]
    if type(output_name) is not str or OUTPUT_RE.fullmatch(output_name) is None:
        raise _fail()
    if not output_name.startswith(f"qh5b-b2-{CANDIDATE_VERSION}-{str(source['commit'])[:8]}-"):
        raise _fail()
    parent = _private_parent(spec["outputParent"], allowed=allowed_output_parent)
    output_root = parent / output_name
    if output_root.exists() or output_root.is_symlink():
        raise _fail()
    raw_inputs = _closed(spec["inputs"], INPUT_NAMES)
    inputs = {
        name: _input(raw_inputs[name], executable=name == "guestHelper").as_dict()
        for name in sorted(INPUT_NAMES)
    }
    raw_tools = _closed(spec["tools"], set(TOOL_NAMES))
    tools: dict[str, dict[str, object]] = {}
    for name in TOOL_NAMES:
        identity = _input(raw_tools[name], executable=True)
        if expected_tool_paths is not None and identity.path != expected_tool_paths[name]:
            raise _fail()
        tools[name] = identity.as_dict()
    wheelhouse = _wheelhouse(spec["wheelhouseRoot"], _identity(inputs["wheelhouseManifest"]))
    _validate_base_authority(inputs)
    _validate_qh5a_authority(inputs, wheelhouse_count=len(wheelhouse))
    plan: dict[str, object] = {
        "apiVersion": API_VERSION,
        "apt": {
            "deadsnakesSigner": DEADSNAKES_SIGNER,
            "deadsnakesUri": DEADSNAKES_URI,
            "packages": [{"name": name, "version": version} for name, version in PRIMARY_PACKAGES],
            "snapshotId": SNAPSHOT_ID,
            "ubuntuSigner": UBUNTU_SIGNER,
            "ubuntuUri": UBUNTU_SNAPSHOT,
        },
        "crictl": {
            "archiveSha256": CRICTL_SHA256,
            "archiveUrl": f"{CRICTL_BASE_URL}/{CRICTL_FILENAME}",
            "checksumUrl": f"{CRICTL_BASE_URL}/{CRICTL_FILENAME}.sha256",
            "version": CRICTL_VERSION,
        },
        "inputs": inputs,
        "kind": PLAN_KIND,
        "oci": [
            {
                "expectedManifestDigest": digest,
                "name": name,
                "reference": reference,
                "registry": registry,
            }
            for name, reference, digest, registry in OCI_SOURCES
        ],
        "outputRoot": str(output_root),
        "qualificationPosture": QUALIFICATION_POSTURE,
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "sourceDateEpoch": SOURCE_DATE_EPOCH,
        "tools": tools,
        "wheelhouse": wheelhouse,
    }
    return _bind(plan, "planDigest")


PLAN_KEYS = {
    "apiVersion",
    "apt",
    "crictl",
    "inputs",
    "kind",
    "oci",
    "outputRoot",
    "qualificationPosture",
    "schemaVersion",
    "source",
    "sourceDateEpoch",
    "tools",
    "wheelhouse",
}


def verify_plan(
    value: object,
    *,
    allowed_output_parent: Path = DEFAULT_OUTPUT_PARENT,
) -> dict[str, object]:
    plan = _verify_bound(value, PLAN_KEYS, "planDigest")
    if (
        plan["apiVersion"] != API_VERSION
        or plan["kind"] != PLAN_KIND
        or plan["schemaVersion"] != SCHEMA_VERSION
        or plan["qualificationPosture"] != QUALIFICATION_POSTURE
        or plan["sourceDateEpoch"] != SOURCE_DATE_EPOCH
    ):
        raise _fail()
    source = _source(plan["source"])
    output_root = _absolute_path(plan["outputRoot"])
    parent = _private_parent(str(output_root.parent), allowed=allowed_output_parent)
    if (
        output_root.parent != parent
        or OUTPUT_RE.fullmatch(output_root.name) is None
        or not output_root.name.startswith(
            f"qh5b-b2-{CANDIDATE_VERSION}-{str(source['commit'])[:8]}-"
        )
    ):
        raise _fail()
    inputs = _closed(plan["inputs"], INPUT_NAMES)
    tools = _closed(plan["tools"], set(TOOL_NAMES))
    for raw in inputs.values():
        _identity(raw)
    for raw in tools.values():
        _identity(raw)
    wheelhouse = plan["wheelhouse"]
    if type(wheelhouse) is not list or len(wheelhouse) != 24:
        raise _fail()
    for raw in wheelhouse:
        _identity(raw)
    expected_apt = {
        "deadsnakesSigner": DEADSNAKES_SIGNER,
        "deadsnakesUri": DEADSNAKES_URI,
        "packages": [{"name": name, "version": version} for name, version in PRIMARY_PACKAGES],
        "snapshotId": SNAPSHOT_ID,
        "ubuntuSigner": UBUNTU_SIGNER,
        "ubuntuUri": UBUNTU_SNAPSHOT,
    }
    expected_crictl = {
        "archiveSha256": CRICTL_SHA256,
        "archiveUrl": f"{CRICTL_BASE_URL}/{CRICTL_FILENAME}",
        "checksumUrl": f"{CRICTL_BASE_URL}/{CRICTL_FILENAME}.sha256",
        "version": CRICTL_VERSION,
    }
    expected_oci = [
        {
            "expectedManifestDigest": digest,
            "name": name,
            "reference": reference,
            "registry": registry,
        }
        for name, reference, digest, registry in OCI_SOURCES
    ]
    if (
        plan["apt"] != expected_apt
        or plan["crictl"] != expected_crictl
        or plan["oci"] != expected_oci
    ):
        raise _fail()
    return plan


def revalidate_plan_files(
    plan_value: object,
    *,
    allowed_output_parent: Path = DEFAULT_OUTPUT_PARENT,
) -> dict[str, object]:
    plan = verify_plan(plan_value, allowed_output_parent=allowed_output_parent)
    for name, raw in cast(dict[str, object], plan["inputs"]).items():
        _revalidate(_identity(raw), executable=name == "guestHelper")
    for raw in cast(dict[str, object], plan["tools"]).values():
        _revalidate(_identity(raw), executable=True)
    for raw in cast(list[object], plan["wheelhouse"]):
        _revalidate(_identity(raw))
    output = Path(cast(str, plan["outputRoot"]))
    if output.exists() or output.is_symlink():
        raise _fail()
    return plan


def _safe_stage_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        not relative
        or relative.startswith("/")
        or pure.as_posix() != relative
        or any(part in {"", ".", ".."} or part in PROTECTED_NAMES for part in pure.parts)
    ):
        raise _fail()
    current = root
    for part in pure.parts[:-1]:
        current /= part
        with contextlib.suppress(FileExistsError):
            current.mkdir(mode=0o700)
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise _fail() from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise _fail()
        if stat.S_IMODE(info.st_mode) != 0o700:
            current.chmod(0o700)
    target = root.joinpath(*pure.parts)
    _path_chain_no_links(target.parent)
    if target.exists() or target.is_symlink():
        raise _fail()
    return target


def _write_new(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    if not raw or len(raw) > MAX_DOWNLOAD_BYTES:
        raise _fail()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
        )
        with os.fdopen(descriptor, "wb") as selected:
            selected.write(raw)
            selected.flush()
            os.fsync(selected.fileno())
    except OSError as exc:
        raise _fail() from exc


def _copy_identity(identity: FileIdentity, destination: Path, *, executable: bool = False) -> None:
    _revalidate(identity, executable=executable)
    if destination.exists() or destination.is_symlink():
        raise _fail()
    try:
        source_descriptor = os.open(
            identity.path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o700 if executable else 0o600,
        )
        with (
            os.fdopen(source_descriptor, "rb") as source,
            os.fdopen(destination_descriptor, "wb") as target,
        ):
            shutil.copyfileobj(source, target, COPY_BLOCK_BYTES)
            target.flush()
            os.fsync(target.fileno())
        copied = file_identity(
            str(destination),
            expected_sha256=identity.sha256,
            executable=executable,
        )
        if copied.size != identity.size:
            raise _fail()
    except OSError as exc:
        raise _fail() from exc


def _clean_environment(*, home: Path | None = None) -> dict[str, str]:
    return {
        "HOME": str(home or Path("/nonexistent")),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "TZ": "UTC",
    }


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    maximum_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    if not argv or any(not value or "\x00" in value for value in argv):
        raise _fail()
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env or _clean_environment()),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail() from exc
    if len(result.stdout) + len(result.stderr) > maximum_output or result.returncode != 0:
        raise _fail()
    return result


def _download_https(url: str, destination: Path, *, expected_sha256: str | None = None) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise _fail()
    if destination.exists() or destination.is_symlink():
        raise _fail()
    request = urllib.request.Request(url, headers={"User-Agent": "k1s-qh5b-b2/1"})
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with (
            urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            os.fdopen(descriptor, "wb") as target,
        ):
            effective = urllib.parse.urlsplit(response.geturl())
            if effective.scheme != "https" or not effective.netloc:
                raise _fail()
            while True:
                block = response.read(COPY_BLOCK_BYTES)
                if not block:
                    break
                size += len(block)
                if size > MAX_DOWNLOAD_BYTES:
                    raise _fail()
                digest.update(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
    except (OSError, urllib.error.URLError) as exc:
        with contextlib.suppress(OSError):
            destination.unlink(missing_ok=True)
        raise _fail() from exc
    if size < 1:
        raise _fail()
    actual = f"sha256:{digest.hexdigest()}"
    if expected_sha256 is not None and actual != _digest(expected_sha256):
        raise _fail()
    return actual


def _first_version_line(result: subprocess.CompletedProcess[bytes]) -> str:
    selected = result.stdout.strip() or result.stderr.strip()
    try:
        lines = [line.strip() for line in selected.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise _fail() from exc
    if not lines or len(lines[0]) > 128 or "\x00" in lines[0]:
        raise _fail()
    return lines[0]


def _deb_fields(dpkg_deb: str, deb: Path, *, cwd: Path) -> tuple[str, str, str]:
    result = _run(
        (
            dpkg_deb,
            "--show",
            "--showformat=${Package}\n${Version}\n${Architecture}\n",
            str(deb),
        ),
        cwd=cwd,
    )
    try:
        fields = [line.strip() for line in result.stdout.decode("utf-8").splitlines()]
    except UnicodeDecodeError as exc:
        raise _fail() from exc
    if len(fields) != 3 or any(not field for field in fields):
        raise _fail()
    return fields[0], fields[1], fields[2]


def _apt_payload_filename(package: str, sha256: str) -> str:
    digest = _digest(sha256).removeprefix("sha256:")
    if APT_PACKAGE_NAME_RE.fullmatch(package) is None:
        raise _fail()
    return f"{package}-{digest[:32]}.deb"


@dataclass(frozen=True, slots=True)
class StageResult:
    guest: dict[str, object]
    member_roles: dict[str, str]
    evidence: dict[str, object]


class AcquisitionAdapter(Protocol):
    def stage(self, plan: Mapping[str, object], staging_root: Path) -> StageResult: ...


class RegistryClient:
    """Small anonymous OCI distribution client with strict digest checks."""

    ACCEPT = ",".join(
        (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    )
    INDEX_TYPES = {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
    MANIFEST_TYPES = {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }

    def __init__(self) -> None:
        self.tokens: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def _parse_reference(reference: str) -> tuple[str, str, str]:
        if "@" in reference or reference.count("/") < 1:
            raise _fail()
        host, remainder = reference.split("/", 1)
        last = remainder.rsplit("/", 1)[-1]
        if ":" in last:
            repository, selected = remainder.rsplit(":", 1)
        else:
            repository, selected = remainder, "latest"
        if not host or not repository or not selected:
            raise _fail()
        registry = "registry-1.docker.io" if host == "docker.io" else host
        return registry, repository, selected

    @staticmethod
    def _challenge(value: str) -> tuple[str, str, str]:
        if not value.startswith("Bearer "):
            raise _fail()
        fields: dict[str, str] = {}
        for match in re.finditer(r'(realm|service|scope)="([^"]+)"', value[7:]):
            fields[match.group(1)] = match.group(2)
        if "realm" not in fields:
            raise _fail()
        return fields["realm"], fields.get("service", ""), fields.get("scope", "")

    def _token(self, challenge: str) -> str:
        realm, service, scope = self._challenge(challenge)
        parsed = urllib.parse.urlsplit(realm)
        if parsed.scheme != "https" or not parsed.netloc:
            raise _fail()
        key = (realm, service, scope)
        if key in self.tokens:
            return self.tokens[key]
        query = {key: value for key, value in (("service", service), ("scope", scope)) if value}
        url = realm + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "k1s-qh5b-b2/1"}),
                timeout=COMMAND_TIMEOUT_SECONDS,
            ) as response:
                raw = response.read(MAX_JSON_BYTES + 1)
        except urllib.error.URLError as exc:
            raise _fail() from exc
        value = _json(raw, canonical=False)
        token = value.get("token") or value.get("access_token")
        if type(token) is not str or not token or len(token) > 16384:
            raise _fail()
        self.tokens[key] = token
        return token

    def _open(self, url: str, *, accept: str | None = None):  # type: ignore[no-untyped-def]
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise _fail()
        headers = {"User-Agent": "k1s-qh5b-b2/1"}
        if accept:
            headers["Accept"] = accept
        request = urllib.request.Request(url, headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=OCI_BLOB_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise _fail() from error
            challenge = error.headers.get("WWW-Authenticate", "")
            token = self._token(challenge)
            request = urllib.request.Request(
                url,
                headers={**headers, "Authorization": f"Bearer {token}"},
            )
            try:
                return urllib.request.urlopen(request, timeout=OCI_BLOB_TIMEOUT_SECONDS)
            except urllib.error.URLError as exc:
                raise _fail() from exc
        except urllib.error.URLError as exc:
            raise _fail() from exc

    def _manifest(self, registry: str, repository: str, reference: str) -> tuple[bytes, str, str]:
        url = f"https://{registry}/v2/{repository}/manifests/{reference}"
        with self._open(url, accept=self.ACCEPT) as response:
            effective = urllib.parse.urlsplit(response.geturl())
            if effective.scheme != "https" or not effective.netloc:
                raise _fail()
            raw = response.read(MAX_JSON_BYTES + 1)
            media_type = response.headers.get_content_type()
            header_digest = response.headers.get("Docker-Content-Digest")
        if len(raw) > MAX_JSON_BYTES:
            raise _fail()
        digest = _digest_bytes(raw)
        if header_digest is not None and digest != _digest(header_digest):
            raise _fail()
        value = _json(raw, canonical=False)
        declared = value.get("mediaType")
        if type(declared) is str:
            media_type = declared
        return raw, digest, media_type

    @staticmethod
    def _descriptor(value: object) -> tuple[str, int, str]:
        if type(value) is not dict:
            raise _fail()
        digest = _digest(value.get("digest"))
        size = value.get("size")
        media_type = value.get("mediaType")
        if (
            type(size) is not int
            or not 1 <= size <= MAX_DOWNLOAD_BYTES
            or type(media_type) is not str
            or not media_type
            or len(media_type) > 256
        ):
            raise _fail()
        return digest, size, media_type

    def _selected_manifest(
        self,
        registry: str,
        repository: str,
        reference: str,
    ) -> tuple[bytes, str, str, str | None]:
        raw, digest, media_type = self._manifest(registry, repository, reference)
        if media_type in self.MANIFEST_TYPES:
            return raw, digest, media_type, None
        if media_type not in self.INDEX_TYPES:
            raise _fail()
        value = _json(raw, canonical=False)
        manifests = value.get("manifests")
        if type(manifests) is not list:
            raise _fail()
        matches: list[dict[str, object]] = []
        for descriptor in manifests:
            if type(descriptor) is not dict or type(descriptor.get("platform")) is not dict:
                continue
            platform = cast(dict[str, object], descriptor["platform"])
            if (
                platform.get("os") == "linux"
                and platform.get("architecture") == "amd64"
                and platform.get("variant") in {None, ""}
            ):
                matches.append(descriptor)
        if len(matches) != 1:
            raise _fail()
        selected_digest, _size, _type = self._descriptor(matches[0])
        selected_raw, actual, selected_type = self._manifest(registry, repository, selected_digest)
        if actual != selected_digest or selected_type not in self.MANIFEST_TYPES:
            raise _fail()
        return selected_raw, actual, selected_type, digest

    def _blob(
        self,
        registry: str,
        repository: str,
        digest: str,
        size: int,
        destination: Path,
    ) -> None:
        url = f"https://{registry}/v2/{repository}/blobs/{digest}"
        expected = _digest(digest)
        hasher = hashlib.sha256()
        written = 0
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            with self._open(url) as response, os.fdopen(descriptor, "wb") as target:
                effective = urllib.parse.urlsplit(response.geturl())
                if effective.scheme != "https" or not effective.netloc:
                    raise _fail()
                while True:
                    block = response.read(COPY_BLOCK_BYTES)
                    if not block:
                        break
                    written += len(block)
                    if written > size or written > MAX_DOWNLOAD_BYTES:
                        raise _fail()
                    hasher.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
        except (OSError, urllib.error.URLError) as exc:
            with contextlib.suppress(OSError):
                destination.unlink(missing_ok=True)
            raise _fail() from exc
        if written != size or f"sha256:{hasher.hexdigest()}" != expected:
            raise _fail()

    @staticmethod
    def _archive(layout: Path, destination: Path) -> str:
        files = sorted(
            (path for path in layout.rglob("*") if path.is_file()),
            key=lambda item: item.relative_to(layout).as_posix().encode("utf-8"),
        )
        temporary = destination.with_name(f".{destination.name}.partial")
        try:
            with tarfile.open(temporary, "w", format=tarfile.USTAR_FORMAT) as archive:
                for path in files:
                    name = path.relative_to(layout).as_posix()
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.REGTYPE
                    info.mode = 0o644
                    info.mtime = SOURCE_DATE_EPOCH
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.size = path.stat().st_size
                    with path.open("rb") as selected:
                        archive.addfile(info, selected)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        except (OSError, tarfile.TarError) as exc:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise _fail() from exc
        return file_identity(str(destination)).sha256

    def acquire(
        self,
        *,
        name: str,
        reference: str,
        expected_manifest_digest: str,
        destination: Path,
        work_root: Path,
    ) -> dict[str, object]:
        registry, repository, selected = self._parse_reference(reference)
        manifest_raw, manifest_digest, media_type, index_digest = self._selected_manifest(
            registry,
            repository,
            selected,
        )
        if manifest_digest != _digest(expected_manifest_digest):
            raise _fail()
        manifest = _json(manifest_raw, canonical=False)
        if manifest.get("schemaVersion") != 2:
            raise _fail()
        config_digest, config_size, _config_type = self._descriptor(manifest.get("config"))
        raw_layers = manifest.get("layers")
        if type(raw_layers) is not list or not raw_layers:
            raise _fail()
        descriptors = [
            (manifest_digest, len(manifest_raw), media_type),
            (config_digest, config_size, ""),
        ]
        descriptors.extend(self._descriptor(value) for value in raw_layers)
        layout = work_root / name
        blobs = layout / "blobs" / "sha256"
        blobs.mkdir(mode=0o700, parents=True)
        manifest_path = blobs / manifest_digest.removeprefix("sha256:")
        _write_new(manifest_path, manifest_raw)
        seen = {manifest_digest}
        for digest, size, _type in descriptors[1:]:
            if digest in seen:
                continue
            seen.add(digest)
            self._blob(
                registry,
                repository,
                digest,
                size,
                blobs / digest.removeprefix("sha256:"),
            )
        layout_value = {"imageLayoutVersion": "1.0.0"}
        index_value = {
            "manifests": [
                {
                    "annotations": {"org.opencontainers.image.ref.name": reference},
                    "digest": manifest_digest,
                    "mediaType": media_type,
                    "size": len(manifest_raw),
                }
            ],
            "schemaVersion": 2,
        }
        _write_new(layout / "oci-layout", canonical_json_bytes(layout_value))
        _write_new(layout / "index.json", canonical_json_bytes(index_value))
        archive_digest = self._archive(layout, destination)
        return {
            "archiveSha256": archive_digest,
            "indexDigest": index_digest,
            "manifestDigest": manifest_digest,
            "name": name,
            "reference": reference,
        }


class LiveAcquisitionAdapter:
    def __init__(self, *, work_parent: Path | None = None) -> None:
        self.work_parent = work_parent

    @staticmethod
    def _tool(plan: Mapping[str, object], name: str) -> str:
        tools = cast(dict[str, object], plan["tools"])
        identity = _identity(tools[name])
        _revalidate(identity, executable=True)
        return identity.path

    @staticmethod
    def _apt_options(work: Path, sources: Path) -> list[str]:
        return [
            "-o",
            f"Dir::State::lists={work / 'lists'}",
            "-o",
            f"Dir::State::status={work / 'status'}",
            "-o",
            f"Dir::Cache::archives={work / 'archives'}",
            "-o",
            f"Dir::Cache::pkgcache={work / 'pkgcache.bin'}",
            "-o",
            f"Dir::Cache::srcpkgcache={work / 'srcpkgcache.bin'}",
            "-o",
            f"Dir::Etc::sourcelist={sources}",
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "APT::Architecture=amd64",
            "-o",
            "Acquire::Languages=none",
            "-o",
            "Acquire::Check-Valid-Until=false",
            "-o",
            "APT::Get::List-Cleanup=0",
            "-o",
            "Debug::NoLocking=true",
        ]

    @staticmethod
    def _signer(gpgv: str, *, keyring: Path, inrelease: Path, expected: str) -> str:
        result = _run(
            (
                gpgv,
                "--status-fd",
                "1",
                "--keyring",
                str(keyring),
                str(inrelease),
            ),
            cwd=inrelease.parent,
        )
        fingerprints: list[str] = []
        try:
            for line in result.stdout.decode("utf-8").splitlines():
                if line.startswith("[GNUPG:] VALIDSIG "):
                    value = line.split()[2]
                    if FINGERPRINT_RE.fullmatch(value):
                        fingerprints.append(value)
        except UnicodeDecodeError as exc:
            raise _fail() from exc
        if fingerprints != [expected]:
            raise _fail()
        return expected

    @staticmethod
    def _copy_file(source: Path, destination: Path, *, expected: str | None = None) -> str:
        identity = file_identity(str(source), expected_sha256=expected)
        _copy_identity(identity, destination)
        return identity.sha256

    def _apt_stage(
        self,
        plan: Mapping[str, object],
        staging_root: Path,
        work: Path,
        roles: dict[str, str],
    ) -> tuple[dict[str, object], Path]:
        apt_get = self._tool(plan, "aptGet")
        apt_cache = self._tool(plan, "aptCache")
        dpkg_deb = self._tool(plan, "dpkgDeb")
        gpgv = self._tool(plan, "gpgv")
        apt_work = work / "apt"
        lists = apt_work / "lists"
        archives = apt_work / "archives"
        lists_partial = lists / "partial"
        archives_partial = archives / "partial"
        for path in (apt_work, lists, archives, lists_partial, archives_partial):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        status = apt_work / "status"
        status.touch(mode=0o600)
        ubuntu_keyring = apt_work / "ubuntu-archive-keyring.gpg"
        deadsnakes_keyring = apt_work / "deadsnakes-ppa.gpg"
        shutil.copyfile("/usr/share/keyrings/ubuntu-archive-keyring.gpg", ubuntu_keyring)
        shutil.copyfile("/etc/apt/trusted.gpg.d/deadsnakes-ubuntu-ppa.gpg", deadsnakes_keyring)
        ubuntu_keyring.chmod(0o600)
        deadsnakes_keyring.chmod(0o600)
        file_identity(str(ubuntu_keyring), expected_sha256=UBUNTU_KEYRING_SHA256)
        file_identity(str(deadsnakes_keyring), expected_sha256=DEADSNAKES_KEYRING_SHA256)
        sources = apt_work / "sources.list"
        sources_raw = "\n".join(
            (
                f"deb [arch=amd64 signed-by={ubuntu_keyring}] "
                f"{UBUNTU_SNAPSHOT} jammy main universe",
                f"deb [arch=amd64 signed-by={ubuntu_keyring}] "
                f"{UBUNTU_SNAPSHOT} jammy-updates main universe",
                f"deb [arch=amd64 signed-by={ubuntu_keyring}] "
                f"{UBUNTU_SNAPSHOT} jammy-security main universe",
                f"deb [arch=amd64 signed-by={deadsnakes_keyring}] {DEADSNAKES_URI} jammy main",
                "",
            )
        ).encode()
        _write_new(sources, sources_raw)
        options = self._apt_options(apt_work, sources)
        _run(
            (apt_get, *options, "update"),
            cwd=apt_work,
            timeout=APT_TIMEOUT_SECONDS,
        )
        inrelease_evidence: list[dict[str, str]] = []
        for filename, (expected_digest, fingerprint, authority) in sorted(
            INRELEASE_AUTHORITIES.items()
        ):
            path = lists / filename
            file_identity(str(path), expected_sha256=expected_digest)
            signer = self._signer(
                gpgv,
                keyring=deadsnakes_keyring if authority == "deadsnakes" else ubuntu_keyring,
                inrelease=path,
                expected=fingerprint,
            )
            inrelease_evidence.append(
                {"filename": filename, "sha256": expected_digest, "signer": signer}
            )
        packages = [f"{name}={version}" if version else name for name, version in PRIMARY_PACKAGES]
        _run(
            (
                apt_get,
                *options,
                "--yes",
                "--download-only",
                "--no-install-recommends",
                "install",
                *packages,
            ),
            cwd=apt_work,
            timeout=APT_TIMEOUT_SECONDS,
        )
        debs = sorted(archives.glob("*.deb"), key=lambda path: path.name.encode("utf-8"))
        if not 1 <= len(debs) <= 512:
            raise _fail()
        package_rows: list[dict[str, object]] = []
        package_files: dict[str, tuple[Path, str, str]] = {}
        for deb in debs:
            package, version, architecture = _deb_fields(dpkg_deb, deb, cwd=apt_work)
            if architecture not in {"all", "amd64"} or package in package_files:
                raise _fail()
            identity = file_identity(str(deb))
            payload_filename = _apt_payload_filename(package, identity.sha256)
            show = _run(
                (apt_cache, *options, "show", f"{package}={version}"),
                cwd=apt_work,
            )
            try:
                text = show.stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _fail() from exc
            advertised = {
                f"sha256:{match.group(1)}"
                for match in re.finditer(r"^SHA256:\s*([0-9a-f]{64})\s*$", text, re.MULTILINE)
            }
            if identity.sha256 not in advertised:
                raise _fail()
            package_files[package] = (deb, version, payload_filename)
            package_rows.append(
                {
                    "architecture": architecture,
                    "filename": payload_filename,
                    "package": package,
                    "sha256": identity.sha256,
                    "size": identity.size,
                    "version": version,
                }
            )
        for package, version in PRIMARY_PACKAGES:
            if package not in package_files or (
                version is not None and package_files[package][1] != version
            ):
                raise _fail()
        inspect_root = work / "apt-inspect"
        inspect_root.mkdir(mode=0o700)
        for deb in debs:
            _run((dpkg_deb, "--extract", str(deb), str(inspect_root)), cwd=apt_work)
        metadata_root = _safe_stage_path(staging_root, "apt/metadata/.keep").parent
        package_root = _safe_stage_path(staging_root, "apt/packages/.keep").parent
        for marker in (metadata_root / ".keep", package_root / ".keep"):
            with contextlib.suppress(FileNotFoundError):
                marker.unlink()
        source_destination = metadata_root / "sources.list"
        retained_sources = "\n".join(
            (
                "deb [arch=amd64 signed-by=/opt/k1s-q1/apt/metadata/"
                f"ubuntu-archive-keyring.gpg] {UBUNTU_SNAPSHOT} jammy main universe",
                "deb [arch=amd64 signed-by=/opt/k1s-q1/apt/metadata/"
                f"ubuntu-archive-keyring.gpg] {UBUNTU_SNAPSHOT} jammy-updates main universe",
                "deb [arch=amd64 signed-by=/opt/k1s-q1/apt/metadata/"
                f"ubuntu-archive-keyring.gpg] {UBUNTU_SNAPSHOT} jammy-security main universe",
                "deb [arch=amd64 signed-by=/opt/k1s-q1/apt/metadata/"
                f"deadsnakes-ppa.gpg] {DEADSNAKES_URI} jammy main",
                "",
            )
        ).encode()
        _write_new(source_destination, retained_sources)
        roles[source_destination.relative_to(staging_root).as_posix()] = "apt-metadata"
        ubuntu_destination = metadata_root / "ubuntu-archive-keyring.gpg"
        self._copy_file(ubuntu_keyring, ubuntu_destination, expected=UBUNTU_KEYRING_SHA256)
        roles[ubuntu_destination.relative_to(staging_root).as_posix()] = "ubuntu-keyring"
        deadsnakes_destination = metadata_root / "deadsnakes-ppa.gpg"
        self._copy_file(
            deadsnakes_keyring,
            deadsnakes_destination,
            expected=DEADSNAKES_KEYRING_SHA256,
        )
        roles[deadsnakes_destination.relative_to(staging_root).as_posix()] = "deadsnakes-keyring"
        metadata_files = sorted(
            (
                path
                for path in lists.iterdir()
                if path.is_file() and path.name != "lock" and path.stat().st_size > 0
            ),
            key=lambda path: path.name.encode("utf-8"),
        )
        for path in metadata_files:
            destination = metadata_root / path.name
            self._copy_file(path, destination)
            roles[destination.relative_to(staging_root).as_posix()] = "apt-metadata"
        for package in sorted(package_files, key=lambda value: value.encode("utf-8")):
            deb, _version, payload_filename = package_files[package]
            destination = package_root / payload_filename
            self._copy_file(deb, destination)
            roles[destination.relative_to(staging_root).as_posix()] = "apt-package"
        ledger = {
            "apiVersion": "k1s.dev/q1-apt-package-ledger/v1",
            "inRelease": inrelease_evidence,
            "kind": "K1sQ1AptPackageLedger",
            "packages": package_rows,
            "primaryPackages": [
                {"name": name, "version": version} for name, version in PRIMARY_PACKAGES
            ],
            "schemaVersion": 1,
            "snapshotId": SNAPSHOT_ID,
        }
        ledger_path = _safe_stage_path(staging_root, "apt/package-ledger.json")
        _write_new(ledger_path, canonical_json_bytes(ledger))
        roles[ledger_path.relative_to(staging_root).as_posix()] = "apt-package-ledger"
        return {
            "inRelease": inrelease_evidence,
            "packageCount": len(package_rows),
            "packageLedgerSha256": file_identity(str(ledger_path)).sha256,
        }, inspect_root

    def _crictl_stage(
        self,
        staging_root: Path,
        work: Path,
        roles: dict[str, str],
    ) -> dict[str, str]:
        archive = work / CRICTL_FILENAME
        checksum = work / f"{CRICTL_FILENAME}.sha256"
        _download_https(
            f"{CRICTL_BASE_URL}/{CRICTL_FILENAME}", archive, expected_sha256=CRICTL_SHA256
        )
        _download_https(f"{CRICTL_BASE_URL}/{CRICTL_FILENAME}.sha256", checksum)
        try:
            checksum_text = checksum.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise _fail() from exc
        if checksum_text not in {
            CRICTL_SHA256.removeprefix("sha256:"),
            f"{CRICTL_SHA256.removeprefix('sha256:')}  {CRICTL_FILENAME}",
        }:
            raise _fail()
        try:
            with tarfile.open(archive, "r:gz") as selected:
                members = selected.getmembers()
                if len(members) != 1 or members[0].name != "crictl" or not members[0].isreg():
                    raise _fail()
                stream = selected.extractfile(members[0])
                if stream is None:
                    raise _fail()
                binary_raw = stream.read(256 * 1024 * 1024 + 1)
        except (OSError, tarfile.TarError) as exc:
            raise _fail() from exc
        if not 1 <= len(binary_raw) <= 256 * 1024 * 1024:
            raise _fail()
        binary = work / "crictl"
        _write_new(binary, binary_raw, mode=0o700)
        version = _first_version_line(_run((str(binary), "--version"), cwd=work))
        if f"v{CRICTL_VERSION}" not in version:
            raise _fail()
        archive_destination = _safe_stage_path(staging_root, f"crictl/{CRICTL_FILENAME}")
        self._copy_file(archive, archive_destination, expected=CRICTL_SHA256)
        roles[archive_destination.relative_to(staging_root).as_posix()] = "crictl-archive"
        checksum_destination = _safe_stage_path(
            staging_root,
            f"crictl/{CRICTL_FILENAME}.sha256",
        )
        self._copy_file(checksum, checksum_destination)
        roles[checksum_destination.relative_to(staging_root).as_posix()] = "crictl-checksum"
        return {
            "binarySha256": _digest_bytes(binary_raw),
            "version": version,
        }

    @staticmethod
    def _inspect_file(root: Path, candidates: Sequence[str], *, executable: bool) -> Path:
        matches = [root / candidate.lstrip("/") for candidate in candidates]
        selected = [path for path in matches if path.is_file() and not path.is_symlink()]
        if len(selected) != 1:
            raise _fail()
        identity = file_identity(str(selected[0]), executable=executable)
        if executable and not int(identity.mode, 8) & 0o100:
            raise _fail()
        return selected[0]

    @staticmethod
    def _binary_version(binary: Path, inspect_root: Path) -> str:
        libraries = (
            inspect_root / "usr/lib/x86_64-linux-gnu",
            inspect_root / "lib/x86_64-linux-gnu",
            inspect_root / "usr/lib",
            inspect_root / "lib",
        )
        env = _clean_environment(home=inspect_root)
        env["LD_LIBRARY_PATH"] = ":".join(str(path) for path in libraries)
        return _first_version_line(_run((str(binary), "--version"), cwd=inspect_root, env=env))

    def _guest_template(
        self,
        *,
        inspect_root: Path,
        crictl: Mapping[str, str],
        cni_config_sha256: str,
        helper_sha256: str,
        wheelhouse_manifest_sha256: str,
        requirements_lock_sha256: str,
        image_rows: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        os_release = self._inspect_file(
            inspect_root,
            ("usr/lib/os-release", "etc/os-release"),
            executable=False,
        )
        kernel = self._inspect_file(
            inspect_root,
            ("boot/vmlinuz-5.15.0-1112-kvm",),
            executable=False,
        )
        python = self._inspect_file(
            inspect_root,
            ("usr/bin/python3.11",),
            executable=True,
        )
        python_version = self._binary_version(python, inspect_root)
        if python_version != "Python 3.11.18":
            raise _fail()
        runtime: dict[str, dict[str, str]] = {}
        for name, candidates in RUNTIME_BINARY_PATHS.items():
            binary = self._inspect_file(inspect_root, candidates, executable=True)
            runtime[name] = {
                "sha256": file_identity(str(binary), executable=True).sha256,
                "version": self._binary_version(binary, inspect_root),
            }
        runtime["crictl"] = {
            "sha256": _digest(crictl["binarySha256"]),
            "version": crictl["version"],
        }
        plugins: list[dict[str, str]] = []
        for name in ("bridge", "portmap", "firewall", "tuning", "loopback"):
            binary = self._inspect_file(inspect_root, (f"usr/lib/cni/{name}",), executable=True)
            plugins.append(
                {
                    "name": name,
                    "sha256": file_identity(str(binary), executable=True).sha256,
                    "version": self._binary_version(binary, inspect_root),
                }
            )
        registry_rows = [row for row in image_rows if row["name"] == "registry"]
        workload_rows = [row for row in image_rows if row["name"] != "registry"]
        if len(registry_rows) != 1 or not workload_rows:
            raise _fail()
        registry = registry_rows[0]
        guest = {
            "cni": {
                "configTemplateSha256": _digest(cni_config_sha256),
                "plugins": plugins,
                "version": "1.0.0",
            },
            "helper": {"sha256": _digest(helper_sha256)},
            "images": [
                {
                    "archiveSha256": row["archiveSha256"],
                    "manifestDigest": row["manifestDigest"],
                    "name": row["name"],
                }
                for row in workload_rows
            ],
            "kernel": {
                "imageSha256": file_identity(str(kernel)).sha256,
                "package": "linux-image-5.15.0-1112-kvm",
                "release": "5.15.0-1112-kvm",
            },
            "os": {
                "id": "ubuntu",
                "releaseSha256": file_identity(str(os_release)).sha256,
                "version": "22.04",
            },
            "python": {
                "executableSha256": file_identity(str(python), executable=True).sha256,
                "version": python_version.removeprefix("Python "),
            },
            "registry": {
                "archiveSha256": registry["archiveSha256"],
                "imageDigest": registry["manifestDigest"],
            },
            "runtime": {
                name: runtime[name] for name in ("containerd", "crictl", "docker", "podman", "runc")
            },
            "wheelhouse": {
                "manifestSha256": _digest(wheelhouse_manifest_sha256),
                "requirementsLockSha256": _digest(requirements_lock_sha256),
            },
        }
        try:
            return guest_image.validate_guest_template(guest)
        except Exception as exc:
            raise _fail() from exc

    def stage(self, plan: Mapping[str, object], staging_root: Path) -> StageResult:
        verified = verify_plan(plan)
        for name, raw in cast(dict[str, object], verified["inputs"]).items():
            _revalidate(_identity(raw), executable=name == "guestHelper")
        for raw in cast(dict[str, object], verified["tools"]).values():
            _revalidate(_identity(raw), executable=True)
        for raw in cast(list[object], verified["wheelhouse"]):
            _revalidate(_identity(raw))
        roles: dict[str, str] = {}
        inputs = {
            name: _identity(raw)
            for name, raw in cast(dict[str, object], verified["inputs"]).items()
        }
        with tempfile.TemporaryDirectory(
            prefix=".qh5b-b2-work-",
            dir=self.work_parent or staging_root.parent,
        ) as work_name:
            work = Path(work_name)
            work.chmod(0o700)
            apt_evidence, inspect_root = self._apt_stage(verified, staging_root, work, roles)
            crictl = self._crictl_stage(staging_root, work, roles)

            authority_targets = {
                "acceptedSdist": ("q1/accepted/k1s_core-1.0.0a1.tar.gz", "accepted-sdist", False),
                "acceptedWheel": (
                    "q1/accepted/k1s_core-1.0.0a1-py3-none-any.whl",
                    "accepted-wheel",
                    False,
                ),
                "baseAcquisitionReceipt": (
                    "q1/base/B1-ACQUISITION-RECEIPT.json",
                    "base-acquisition-receipt",
                    False,
                ),
                "basePackageManifest": (
                    "q1/base/ubuntu-22.04-server-cloudimg-amd64-disk-kvm.manifest",
                    "base-package-manifest",
                    False,
                ),
                "guestHelper": (
                    "q1/helper/kp1_q1_guest_helper.py",
                    "guest-helper",
                    True,
                ),
                "qh5aCompletion": (
                    "q1/accepted/QH5-A-COMPLETION.json",
                    "qh5a-completion",
                    False,
                ),
                "requirementsLock": (
                    "q1/wheelhouse/requirements-release.lock",
                    "requirements-lock",
                    False,
                ),
                "wheelhouseManifest": (
                    "q1/wheelhouse/wheelhouse-manifest.json",
                    "wheelhouse-manifest",
                    False,
                ),
            }
            for name, (relative, role, executable) in authority_targets.items():
                destination = _safe_stage_path(staging_root, relative)
                _copy_identity(inputs[name], destination, executable=executable)
                roles[relative] = role

            for raw in cast(list[object], verified["wheelhouse"]):
                identity = _identity(raw)
                destination = _safe_stage_path(
                    staging_root,
                    f"q1/wheelhouse/wheels/{Path(identity.path).name}",
                )
                _copy_identity(identity, destination)
                roles[destination.relative_to(staging_root).as_posix()] = "wheel"

            cni_value = {
                "cniVersion": "1.0.0",
                "name": "k1s-q1",
                "plugins": [
                    {
                        "bridge": "k1s-q1",
                        "ipMasq": True,
                        "ipam": {
                            "ranges": [[{"subnet": "10.88.0.0/16"}]],
                            "routes": [{"dst": "0.0.0.0/0"}],
                            "type": "host-local",
                        },
                        "isGateway": True,
                        "type": "bridge",
                    },
                    {"capabilities": {"portMappings": True}, "type": "portmap"},
                    {"type": "firewall"},
                    {"type": "tuning"},
                ],
            }
            cni_raw = canonical_json_bytes(cni_value)
            cni_path = _safe_stage_path(staging_root, "cni/10-k1s-q1.conflist")
            _write_new(cni_path, cni_raw)
            roles[cni_path.relative_to(staging_root).as_posix()] = "cni-config"

            registry_client = RegistryClient()
            image_rows: list[dict[str, object]] = []
            oci_work = work / "oci-layouts"
            oci_work.mkdir(mode=0o700)
            for raw in cast(list[dict[str, object]], verified["oci"]):
                name = cast(str, raw["name"])
                destination = _safe_stage_path(staging_root, f"oci/{name}.oci.tar")
                row = registry_client.acquire(
                    name=name,
                    reference=cast(str, raw["reference"]),
                    expected_manifest_digest=cast(str, raw["expectedManifestDigest"]),
                    destination=destination,
                    work_root=oci_work,
                )
                image_rows.append(row)
                roles[destination.relative_to(staging_root).as_posix()] = "oci-image"
            if [row["name"] for row in image_rows] != sorted(
                (cast(str, row["name"]) for row in image_rows),
                key=lambda value: value.encode("utf-8"),
            ):
                raise _fail()
            oci_ledger = {
                "apiVersion": "k1s.dev/q1-oci-resolution-ledger/v1",
                "images": image_rows,
                "kind": "K1sQ1OCIResolutionLedger",
                "platform": {"architecture": "amd64", "os": "linux"},
                "schemaVersion": 1,
            }
            oci_ledger_path = _safe_stage_path(staging_root, "oci/resolution-ledger.json")
            _write_new(oci_ledger_path, canonical_json_bytes(oci_ledger))
            roles[oci_ledger_path.relative_to(staging_root).as_posix()] = "oci-resolution-ledger"

            guest = self._guest_template(
                inspect_root=inspect_root,
                crictl=crictl,
                cni_config_sha256=_digest_bytes(cni_raw),
                helper_sha256=inputs["guestHelper"].sha256,
                wheelhouse_manifest_sha256=inputs["wheelhouseManifest"].sha256,
                requirements_lock_sha256=inputs["requirementsLock"].sha256,
                image_rows=image_rows,
            )
            offline_evidence = {
                "apiVersion": "k1s.dev/q1-guest-offline-evidence/v1",
                "helper": guest["helper"],
                "images": guest["images"],
                "kind": "K1sQ1GuestOfflineEvidence",
                "registry": guest["registry"],
                "schemaVersion": 1,
                "wheelhouse": guest["wheelhouse"],
            }
            offline_path = _safe_stage_path(staging_root, "q1/offline-evidence.json")
            _write_new(offline_path, canonical_json_bytes(offline_evidence))
            roles[offline_path.relative_to(staging_root).as_posix()] = "offline-evidence"
            evidence = {
                "apt": apt_evidence,
                "crictl": crictl,
                "imageCount": len(image_rows),
                "ociResolutionLedgerSha256": file_identity(str(oci_ledger_path)).sha256,
                "offlineEvidenceSha256": file_identity(str(offline_path)).sha256,
            }
            return StageResult(
                guest=guest, member_roles=dict(sorted(roles.items())), evidence=evidence
            )


def _write_canonical(path: Path, value: object, *, mode: int = 0o400) -> bytes:
    raw = canonical_json_bytes(value)
    _write_new(path, raw, mode=mode)
    return raw


def _read_file(path: Path, *, maximum: int = MAX_JSON_BYTES) -> bytes:
    _path_chain_no_links(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= maximum
        ):
            raise _fail()
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, COPY_BLOCK_BYTES))
            if not block:
                raise _fail()
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise _fail()
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _local_sums(rows: Sequence[tuple[str, str]]) -> bytes:
    names = [name for name, _digest_value in rows]
    if names != sorted(set(names), key=lambda name: name.encode("utf-8")):
        raise _fail()
    return "".join(f"{digest.removeprefix('sha256:')}  {name}\n" for name, digest in rows).encode()


RECEIPT_KEYS = {
    "apiVersion",
    "bomSha256",
    "completedAtUnix",
    "evidence",
    "kind",
    "memberManifestSha256",
    "outputRoot",
    "payloadSha256",
    "payloadSize",
    "planDigest",
    "qualificationPosture",
    "schemaVersion",
    "source",
    "status",
    "twoValidatorGate",
}


def _verify_receipt(value: object) -> dict[str, object]:
    receipt = _verify_bound(value, RECEIPT_KEYS, "receiptDigest")
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != RECEIPT_KIND
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or receipt["qualificationPosture"] != QUALIFICATION_POSTURE
        or receipt["status"] != "bom-closed"
        or type(receipt["completedAtUnix"]) is not int
        or cast(int, receipt["completedAtUnix"]) <= 0
        or type(receipt["payloadSize"]) is not int
        or not 1 <= cast(int, receipt["payloadSize"]) <= guest_bom.MAX_PAYLOAD_BYTES
    ):
        raise _fail()
    _source(receipt["source"])
    for field in (
        "bomSha256",
        "memberManifestSha256",
        "payloadSha256",
        "planDigest",
    ):
        _digest(receipt[field])
    gate = _closed(
        receipt["twoValidatorGate"],
        {
            "bomSha256",
            "memberManifestSha256",
            "payloadSha256",
            "replayValidatorSha256",
            "schemaValidatorSha256",
            "status",
        },
    )
    if gate["status"] != "two-validator-match":
        raise _fail()
    for field in (
        "bomSha256",
        "memberManifestSha256",
        "payloadSha256",
        "replayValidatorSha256",
        "schemaValidatorSha256",
    ):
        _digest(gate[field])
    if (
        gate["bomSha256"] != receipt["bomSha256"]
        or gate["memberManifestSha256"] != receipt["memberManifestSha256"]
        or gate["payloadSha256"] != receipt["payloadSha256"]
    ):
        raise _fail()
    if type(receipt["evidence"]) is not dict:
        raise _fail()
    _absolute_path(receipt["outputRoot"])
    return receipt


SEAL_KEYS = {
    "apiVersion",
    "bomSha256",
    "kind",
    "localSumsSha256",
    "memberManifestSha256",
    "payloadSha256",
    "planDigest",
    "qualificationPosture",
    "receiptDigest",
    "schemaVersion",
    "source",
    "status",
}


def _verify_seal(value: object) -> dict[str, object]:
    seal = _verify_bound(value, SEAL_KEYS, "sealDigest")
    if (
        seal["apiVersion"] != API_VERSION
        or seal["kind"] != SEAL_KIND
        or seal["schemaVersion"] != SCHEMA_VERSION
        or seal["qualificationPosture"] != QUALIFICATION_POSTURE
        or seal["status"] != "retained-b2-evidence"
    ):
        raise _fail()
    _source(seal["source"])
    for field in (
        "bomSha256",
        "localSumsSha256",
        "memberManifestSha256",
        "payloadSha256",
        "planDigest",
        "receiptDigest",
    ):
        _digest(seal[field])
    return seal


def execute_acquisition(
    plan_value: object,
    *,
    confirmation: str,
    adapter: AcquisitionAdapter,
    clock: Callable[[], float] = time.time,
    allowed_output_parent: Path = DEFAULT_OUTPUT_PARENT,
) -> tuple[dict[str, object], dict[str, object]]:
    plan = revalidate_plan_files(
        plan_value,
        allowed_output_parent=allowed_output_parent,
    )
    plan_digest = cast(str, plan["planDigest"])
    if confirmation != f"ACQUIRE-Q1-BOM:{plan_digest}":
        raise _fail()
    output_root = Path(cast(str, plan["outputRoot"]))
    if output_root.parent != allowed_output_parent:
        raise _fail()
    try:
        output_root.mkdir(mode=0o700)
        output_root.chmod(0o700)
        staging_root = output_root / "payload"
        staging_root.mkdir(mode=0o700)
        staged = adapter.stage(plan, staging_root)
        if type(staged) is not StageResult:
            raise _fail()
        source = {
            **cast(dict[str, object], plan["source"]),
            "sourceDateEpoch": SOURCE_DATE_EPOCH,
        }
        payload_path = output_root / "B2-OFFLINE-PAYLOAD.tar"
        member_manifest, bom = guest_bom.write_payload(
            staging_root=staging_root,
            destination=payload_path,
            source=source,
            guest=staged.guest,
            member_roles=staged.member_roles,
        )
        plan_raw = _write_canonical(output_root / "B2-ACQUISITION-PLAN.json", plan)
        manifest_raw = _write_canonical(output_root / "B2-PAYLOAD-MANIFEST.json", member_manifest)
        bom_raw = _write_canonical(output_root / "B2-GUEST-BOM.json", bom)
        gate = guest_bom.verify_two_validator_gate(
            bom_raw=bom_raw,
            payload_path=payload_path,
            expected_bom_sha256=_digest_bytes(bom_raw),
            expected_payload_sha256=cast(dict[str, object], bom["payload"])["sha256"],
        )
        payload_identity = file_identity(
            str(payload_path),
            expected_sha256=gate["payloadSha256"],
        )
        receipt = _bind(
            {
                "apiVersion": API_VERSION,
                "bomSha256": gate["bomSha256"],
                "completedAtUnix": int(clock()),
                "evidence": staged.evidence,
                "kind": RECEIPT_KIND,
                "memberManifestSha256": gate["memberManifestSha256"],
                "outputRoot": str(output_root),
                "payloadSha256": gate["payloadSha256"],
                "payloadSize": payload_identity.size,
                "planDigest": plan_digest,
                "qualificationPosture": QUALIFICATION_POSTURE,
                "schemaVersion": SCHEMA_VERSION,
                "source": plan["source"],
                "status": "bom-closed",
                "twoValidatorGate": gate,
            },
            "receiptDigest",
        )
        receipt_raw = _write_canonical(output_root / "B2-ACQUISITION-RECEIPT.json", receipt)
        sums_rows = sorted(
            (
                ("B2-ACQUISITION-PLAN.json", _digest_bytes(plan_raw)),
                ("B2-ACQUISITION-RECEIPT.json", _digest_bytes(receipt_raw)),
                ("B2-GUEST-BOM.json", _digest_bytes(bom_raw)),
                ("B2-OFFLINE-PAYLOAD.tar", payload_identity.sha256),
                ("B2-PAYLOAD-MANIFEST.json", _digest_bytes(manifest_raw)),
            ),
            key=lambda row: row[0].encode("utf-8"),
        )
        sums_raw = _local_sums(sums_rows)
        _write_new(output_root / "B2-LOCAL-SHA256SUMS", sums_raw, mode=0o400)
        seal = _bind(
            {
                "apiVersion": API_VERSION,
                "bomSha256": gate["bomSha256"],
                "kind": SEAL_KIND,
                "localSumsSha256": _digest_bytes(sums_raw),
                "memberManifestSha256": gate["memberManifestSha256"],
                "payloadSha256": payload_identity.sha256,
                "planDigest": plan_digest,
                "qualificationPosture": QUALIFICATION_POSTURE,
                "receiptDigest": receipt["receiptDigest"],
                "schemaVersion": SCHEMA_VERSION,
                "source": plan["source"],
                "status": "retained-b2-evidence",
            },
            "sealDigest",
        )
        _write_canonical(output_root / "B2-EVIDENCE-SEAL.json", seal)
        return receipt, seal
    except Exception as exc:
        if output_root.is_dir() and not (output_root / "B2-FAILURE.json").exists():
            failure = {
                "apiVersion": API_VERSION,
                "kind": "K1sQ1GuestInputAcquisitionFailure",
                "planDigest": plan_digest,
                "schemaVersion": SCHEMA_VERSION,
                "status": "failed-closed",
            }
            with contextlib.suppress(Exception):
                _write_canonical(output_root / "B2-FAILURE.json", failure)
        if isinstance(exc, GuestInputPolicyError):
            raise
        if isinstance(exc, guest_bom.GuestBOMPolicyError):
            raise _fail() from exc
        raise _fail() from exc


def _payload_stage_matches(root: Path, manifest: Mapping[str, object]) -> None:
    staging_root = root / "payload"
    try:
        info = os.lstat(staging_root)
    except OSError as exc:
        raise _fail() from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise _fail()
    rows = cast(list[dict[str, object]], manifest["members"])
    expected_paths = [cast(str, row["path"]) for row in rows]
    actual_paths: list[str] = []
    for path in staging_root.rglob("*"):
        relative = path.relative_to(staging_root).as_posix()
        path_info = os.lstat(path)
        if stat.S_ISLNK(path_info.st_mode):
            raise _fail()
        if stat.S_ISREG(path_info.st_mode):
            actual_paths.append(relative)
        elif not stat.S_ISDIR(path_info.st_mode):
            raise _fail()
    if sorted(actual_paths, key=lambda value: value.encode("utf-8")) != expected_paths:
        raise _fail()
    for row in rows:
        observed = guest_bom.observe_member(
            staging_root,
            path=row["path"],
            role=row["role"],
        ).as_dict()
        if observed != row:
            raise _fail()


def verify_retained_root(
    plan_value: object,
    *,
    expected_seal_file_sha256: object | None = None,
    allowed_output_parent: Path = DEFAULT_OUTPUT_PARENT,
) -> dict[str, object]:
    plan = verify_plan(plan_value, allowed_output_parent=allowed_output_parent)
    root = Path(cast(str, plan["outputRoot"]))
    _path_chain_no_links(root)
    try:
        info = os.lstat(root)
    except OSError as exc:
        raise _fail() from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise _fail()
    external_names = {
        "B2-ACQUISITION-PLAN.json",
        "B2-ACQUISITION-RECEIPT.json",
        "B2-EVIDENCE-SEAL.json",
        "B2-GUEST-BOM.json",
        "B2-LOCAL-SHA256SUMS",
        "B2-OFFLINE-PAYLOAD.tar",
        "B2-PAYLOAD-MANIFEST.json",
        "payload",
    }
    if {path.name for path in root.iterdir()} != external_names:
        raise _fail()
    retained_plan_raw = _read_file(root / "B2-ACQUISITION-PLAN.json")
    retained_plan = _json(retained_plan_raw, canonical=True)
    if retained_plan != plan:
        raise _fail()
    receipt_raw = _read_file(root / "B2-ACQUISITION-RECEIPT.json")
    receipt = _verify_receipt(_json(receipt_raw, canonical=True))
    seal_raw = _read_file(root / "B2-EVIDENCE-SEAL.json")
    seal = _verify_seal(_json(seal_raw, canonical=True))
    if expected_seal_file_sha256 is not None and _digest_bytes(seal_raw) != _digest(
        expected_seal_file_sha256
    ):
        raise _fail()
    bom_raw = _read_file(root / "B2-GUEST-BOM.json")
    manifest_raw = _read_file(root / "B2-PAYLOAD-MANIFEST.json")
    manifest = guest_bom.validate_member_manifest(guest_bom.canonical_json(manifest_raw))
    apt_package_count = sum(
        row["role"] == "apt-package" for row in cast(list[dict[str, object]], manifest["members"])
    )
    if not 1 <= apt_package_count <= 512:
        raise _fail()
    _payload_stage_matches(root, manifest)
    gate = guest_bom.verify_two_validator_gate(
        bom_raw=bom_raw,
        payload_path=root / "B2-OFFLINE-PAYLOAD.tar",
        expected_bom_sha256=receipt["bomSha256"],
        expected_payload_sha256=receipt["payloadSha256"],
    )
    sums_raw = _read_file(root / "B2-LOCAL-SHA256SUMS")
    payload_identity = file_identity(
        str(root / "B2-OFFLINE-PAYLOAD.tar"),
        expected_sha256=receipt["payloadSha256"],
    )
    expected_rows = sorted(
        (
            ("B2-ACQUISITION-PLAN.json", _digest_bytes(retained_plan_raw)),
            ("B2-ACQUISITION-RECEIPT.json", _digest_bytes(receipt_raw)),
            ("B2-GUEST-BOM.json", _digest_bytes(bom_raw)),
            ("B2-OFFLINE-PAYLOAD.tar", cast(str, receipt["payloadSha256"])),
            ("B2-PAYLOAD-MANIFEST.json", _digest_bytes(manifest_raw)),
        ),
        key=lambda row: row[0].encode("utf-8"),
    )
    if sums_raw != _local_sums(expected_rows):
        raise _fail()
    if (
        receipt["planDigest"] != plan["planDigest"]
        or receipt["outputRoot"] != str(root)
        or receipt["source"] != plan["source"]
        or receipt["payloadSize"] != payload_identity.size
        or receipt["twoValidatorGate"] != gate
        or receipt["memberManifestSha256"] != _digest_bytes(manifest_raw)
        or seal["planDigest"] != plan["planDigest"]
        or seal["source"] != plan["source"]
        or seal["receiptDigest"] != receipt["receiptDigest"]
        or seal["bomSha256"] != receipt["bomSha256"]
        or seal["memberManifestSha256"] != receipt["memberManifestSha256"]
        or seal["payloadSha256"] != receipt["payloadSha256"]
        or seal["localSumsSha256"] != _digest_bytes(sums_raw)
    ):
        raise _fail()
    return {
        "aptPackageCount": apt_package_count,
        "bomSha256": receipt["bomSha256"],
        "memberManifestSha256": receipt["memberManifestSha256"],
        "outputRoot": str(root),
        "payloadSha256": receipt["payloadSha256"],
        "planDigest": plan["planDigest"],
        "qualificationPosture": QUALIFICATION_POSTURE,
        "receiptDigest": receipt["receiptDigest"],
        "sealDigest": seal["sealDigest"],
        "sealFileSha256": _digest_bytes(seal_raw),
        "status": "retained-b2-evidence-verified",
    }


def _write_output(path: Path, raw: bytes) -> None:
    parent = path.parent
    _path_chain_no_links(parent)
    try:
        info = os.lstat(parent)
    except OSError as exc:
        raise _fail() from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or path.exists()
        or path.is_symlink()
    ):
        raise _fail()
    _write_new(path, raw, mode=0o600)


def _expected_tools() -> dict[str, str]:
    names = {
        "aptCache": "apt-cache",
        "aptGet": "apt-get",
        "dpkgDeb": "dpkg-deb",
        "gpgv": "gpgv",
    }
    result: dict[str, str] = {}
    for name, command in names.items():
        selected = shutil.which(command, path="/usr/sbin:/usr/bin:/sbin:/bin")
        if selected is None:
            raise _fail()
        path = Path(selected)
        _path_chain_no_links(path)
        result[name] = str(path)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="create a canonical B2 acquisition plan")
    plan.add_argument("--spec", required=True, type=Path)
    plan.add_argument("--expect-spec-sha256", required=True)
    plan.add_argument("--output", required=True, type=Path)

    acquire = sub.add_parser("acquire", help="execute a confirmed B2 acquisition")
    acquire.add_argument("--plan", required=True, type=Path)
    acquire.add_argument("--expect-plan-sha256", required=True)
    acquire.add_argument("--confirm", required=True)

    verify = sub.add_parser("verify-root", help="independently replay one retained B2 root")
    verify.add_argument("--plan", required=True, type=Path)
    verify.add_argument("--expect-plan-sha256", required=True)
    verify.add_argument("--expect-seal-file-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan_raw = _read_file(
            args.spec if args.command == "plan" else args.plan,
            maximum=MAX_JSON_BYTES,
        )
        expected_file_digest = _digest(
            args.expect_spec_sha256 if args.command == "plan" else args.expect_plan_sha256
        )
        if _digest_bytes(plan_raw) != expected_file_digest:
            raise _fail()
        value = _json(plan_raw, canonical=True)
        if args.command == "plan":
            result = build_plan(
                value,
                expected_tool_paths=_expected_tools(),
            )
            _write_output(args.output, canonical_json_bytes(result))
            rendered: object = result
        elif args.command == "acquire":
            receipt, seal = execute_acquisition(
                value,
                confirmation=args.confirm,
                adapter=LiveAcquisitionAdapter(),
            )
            rendered = {
                "outputRoot": receipt["outputRoot"],
                "receiptDigest": receipt["receiptDigest"],
                "sealDigest": seal["sealDigest"],
                "status": receipt["status"],
            }
        else:
            rendered = verify_retained_root(
                value,
                expected_seal_file_sha256=args.expect_seal_file_sha256,
            )
    except GuestInputPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(rendered))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
