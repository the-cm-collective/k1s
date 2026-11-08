#!/usr/bin/env python3
"""Build, execute, and audit one isolated K1s Q1 qualification capsule.

This module deliberately separates a deterministic, non-mutating plan from the
later privileged/runtime gates.  Importing it or running ``plan`` never starts
QEMU, creates an overlay, inspects a runtime socket, or changes host state.
Live execution is permitted only after an exact plan, Gate A attestation, and
confirmation string are supplied.  The implementation is dependency-free so
its safety properties can be exercised with synthetic command/process
boundaries before any live qualification is authorized.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

API_VERSION = "k1s.dev/q1-capsule/v1"
PLAN_KIND = "K1sQ1QualificationPlan"
GUEST_MANIFEST_KIND = "K1sQ1GuestManifest"
EVIDENCE_KIND = "K1sQ1EvidenceManifest"
SEAL_KIND = "K1sQ1EvidenceSeal"
SCHEMA_VERSION = 1
QUALIFICATION_POSTURE = "source-validated/unqualified"

RUN_ID_RE = re.compile(r"q1-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
FULL_OBJECT_RE = re.compile(r"[0-9a-f]{40}")
IDENTIFIER_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?")

LAYOUT_NAMES = ("input", "vm", "guest", "evidence", "cleanup")
PRIVATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_INPUT_BYTES = 1024 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PUBLIC_LOG_BYTES = 512 * 1024 * 1024
MIN_FREE_START_BYTES = 64 * 1024 * 1024 * 1024
MIN_FREE_ABORT_BYTES = 32 * 1024 * 1024 * 1024
WALL_TIME_SECONDS = 45 * 60
PER_COMMAND_SECONDS = 10 * 60
GUEST_PROCESS_LIMIT = 512
HOST_NOFILE_LIMIT = 1024
VCPU_COUNT = 4
MEMORY_MIB = 8192
OVERLAY_SIZE = "24G"
CAPSULE_PARENT = Path("/tmp")  # noqa: S108 - fixed, policy-bound capsule parent

ACCEPTANCE_STAGES = (
    "boot-containment",
    "offline-install",
    "pure-contracts",
    "strict-cri-prepare-rollback",
    "core-podman",
    "core-docker",
    "edge-gateway",
    "node-strict-cri",
    "node-fencing",
    "security-context",
    "workload-lifecycle",
    "workerbee-evidence",
    "crash-recovery",
    "teardown",
)

PROTECTED_NAMES = {
    ".env",
    ".envrc",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    "ops/systemd/ae-ha-core.env",
    "ops/systemd/ae.env",
}

_GUEST_TOP_LEVEL = {
    "apiVersion",
    "cni",
    "helper",
    "image",
    "images",
    "kernel",
    "kind",
    "os",
    "python",
    "registry",
    "runtime",
    "schemaVersion",
    "wheelhouse",
}
_RUNTIME_NAMES = ("containerd", "crictl", "docker", "podman", "runc")
_CNI_NAMES = ("bridge", "portmap", "firewall", "tuning", "loopback")


class CapsulePolicyError(RuntimeError):
    """The capsule request is unsafe, ambiguous, divergent, or incomplete."""


def _fail() -> CapsulePolicyError:
    return CapsulePolicyError("K1s Q1 qualification capsule failed closed")


def canonical_json_bytes(value: object) -> bytes:
    """Return the single canonical JSON representation used by the capsule."""

    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    return (text + "\n").encode("utf-8")


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _bind_receipt(value: dict[str, object]) -> dict[str, object]:
    if "receiptDigest" in value:
        raise _fail()
    result = dict(value)
    result["receiptDigest"] = _digest_bytes(canonical_json_bytes(value))
    return result


def _verify_bound_receipt(value: object, expected_keys: set[str]) -> dict[str, object]:
    receipt = _require_closed(value, {*expected_keys, "receiptDigest"})
    unsigned = dict(receipt)
    actual = unsigned.pop("receiptDigest")
    if _digest(actual) != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    return receipt


def _identifier(value: object) -> str:
    if type(value) is not str or IDENTIFIER_RE.fullmatch(value) is None:
        raise _fail()
    return value


def validate_run_id(value: object) -> str:
    if type(value) is not str or RUN_ID_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _full_object(value: object) -> str:
    if type(value) is not str or FULL_OBJECT_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _canonical_absolute_path(value: object) -> Path:
    if type(value) is not str or not value.startswith("/") or value == "/":
        raise _fail()
    if value.startswith("//") or value.endswith("/") or "\\" in value or "\x00" in value:
        raise _fail()
    if unicodedata.normalize("NFC", value) != value:
        raise _fail()
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _fail()
    relative = value.removeprefix("/")
    if path.name in PROTECTED_NAMES or any(
        relative == protected or relative.endswith(f"/{protected}") for protected in PROTECTED_NAMES
    ):
        raise _fail()
    return Path(value)


def _path_parts_are_not_links(path: Path, *, include_leaf: bool) -> None:
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

    def to_payload(self) -> dict[str, object]:
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
    raw_path: object,
    *,
    expected_digest: object,
    executable: bool = False,
    maximum: int = MAX_INPUT_BYTES,
) -> FileIdentity:
    """Validate one immutable regular input without accepting link substitution."""

    path = _canonical_absolute_path(raw_path)
    expected = _digest(expected_digest)
    _path_parts_are_not_links(path, include_leaf=False)
    try:
        before = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise _fail()
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise _fail()
        mode = stat.S_IMODE(opened.st_mode)
        if (
            opened.st_uid not in {0, os.getuid()}
            or mode & 0o022
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= maximum
            or (executable and not mode & 0o111)
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
        stable = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        actual = f"sha256:{digest.hexdigest()}"
        if not stable or actual != expected:
            raise _fail()
        return FileIdentity(
            path=str(path),
            sha256=actual,
            size=opened.st_size,
            mode=f"0{mode:03o}",
            uid=opened.st_uid,
            gid=opened.st_gid,
            device=opened.st_dev,
            inode=opened.st_ino,
        )
    finally:
        os.close(descriptor)


def _read_identity_bytes(identity: FileIdentity, *, maximum: int) -> bytes:
    current = file_identity(
        identity.path,
        expected_digest=identity.sha256,
        maximum=maximum,
    )
    if current != identity:
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
        chunks: list[bytes] = []
        remaining = identity.size
        while remaining:
            block = os.read(descriptor, min(remaining, 1024 * 1024))
            if not block:
                raise _fail()
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise _fail()
        raw = b"".join(chunks)
        if _digest_bytes(raw) != identity.sha256:
            raise _fail()
        return raw
    finally:
        os.close(descriptor)


def _load_canonical_json(identity: FileIdentity) -> dict[str, object]:
    try:
        raw = _read_identity_bytes(identity, maximum=MAX_JSON_BYTES)
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return value


def _require_closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    return value


def _identity_from_payload(value: object) -> FileIdentity:
    item = _require_closed(
        value,
        {"device", "gid", "inode", "mode", "path", "sha256", "size", "uid"},
    )
    path = _canonical_absolute_path(item["path"])
    mode = item["mode"]
    size = item["size"]
    integers = [item[field] for field in ("device", "gid", "inode", "uid")]
    if type(mode) is not str or re.fullmatch(r"0[0-7]{3}", mode) is None:
        raise _fail()
    if type(size) is not int or not 1 <= size <= MAX_INPUT_BYTES:
        raise _fail()
    if any(type(number) is not int or number < 0 for number in integers):
        raise _fail()
    return FileIdentity(
        path=str(path),
        sha256=_digest(item["sha256"]),
        size=size,
        mode=mode,
        uid=cast(int, item["uid"]),
        gid=cast(int, item["gid"]),
        device=cast(int, item["device"]),
        inode=cast(int, item["inode"]),
    )


def _component_identity(value: object) -> dict[str, object]:
    item = _require_closed(value, {"sha256", "version"})
    if type(item["version"]) is not str or not item["version"] or len(item["version"]) > 128:
        raise _fail()
    return {"sha256": _digest(item["sha256"]), "version": item["version"]}


def validate_guest_manifest(
    value: object,
    *,
    image_digest: str,
    helper_digest: str,
    wheelhouse_manifest_digest: str,
) -> dict[str, object]:
    manifest = _require_closed(value, _GUEST_TOP_LEVEL)
    if (
        manifest["apiVersion"] != API_VERSION
        or manifest["kind"] != GUEST_MANIFEST_KIND
        or manifest["schemaVersion"] != SCHEMA_VERSION
    ):
        raise _fail()
    image = _require_closed(manifest["image"], {"format", "sha256"})
    if image != {"format": "qcow2", "sha256": image_digest}:
        raise _fail()
    os_value = _require_closed(manifest["os"], {"id", "releaseSha256", "version"})
    if os_value["id"] != "ubuntu" or os_value["version"] != "22.04":
        raise _fail()
    _digest(os_value["releaseSha256"])
    kernel = _require_closed(manifest["kernel"], {"imageSha256", "package", "release"})
    _digest(kernel["imageSha256"])
    for field in ("package", "release"):
        text = kernel[field]
        if type(text) is not str or not text or len(text) > 128:
            raise _fail()
    python = _require_closed(manifest["python"], {"executableSha256", "version"})
    _digest(python["executableSha256"])
    if type(python["version"]) is not str or not python["version"].startswith("3.11."):
        raise _fail()
    runtime = _require_closed(manifest["runtime"], set(_RUNTIME_NAMES))
    normalized_runtime = {name: _component_identity(runtime[name]) for name in _RUNTIME_NAMES}
    cni = _require_closed(manifest["cni"], {"configTemplateSha256", "plugins", "version"})
    if cni["version"] != "1.0.0":
        raise _fail()
    _digest(cni["configTemplateSha256"])
    plugins = cni["plugins"]
    if type(plugins) is not list or len(plugins) != len(_CNI_NAMES):
        raise _fail()
    normalized_plugins: list[dict[str, object]] = []
    for expected, raw in zip(_CNI_NAMES, plugins, strict=True):
        item = _require_closed(raw, {"name", "sha256", "version"})
        if item["name"] != expected:
            raise _fail()
        normalized_plugins.append(
            {
                "name": expected,
                "sha256": _digest(item["sha256"]),
                "version": _component_identity(
                    {"sha256": item["sha256"], "version": item["version"]}
                )["version"],
            }
        )
    registry = _require_closed(manifest["registry"], {"archiveSha256", "imageDigest"})
    for field in ("archiveSha256", "imageDigest"):
        _digest(registry[field])
    images = manifest["images"]
    if type(images) is not list or not 1 <= len(images) <= 64:
        raise _fail()
    normalized_images: list[dict[str, str]] = []
    for raw in images:
        item = _require_closed(raw, {"archiveSha256", "manifestDigest", "name"})
        name = _identifier(item["name"])
        normalized_images.append(
            {
                "archiveSha256": _digest(item["archiveSha256"]),
                "manifestDigest": _digest(item["manifestDigest"]),
                "name": name,
            }
        )
    if [item["name"] for item in normalized_images] != sorted(
        {item["name"] for item in normalized_images}, key=lambda item: item.encode("utf-8")
    ):
        raise _fail()
    wheelhouse = _require_closed(
        manifest["wheelhouse"], {"manifestSha256", "requirementsLockSha256"}
    )
    if wheelhouse["manifestSha256"] != wheelhouse_manifest_digest:
        raise _fail()
    _digest(wheelhouse["requirementsLockSha256"])
    helper = _require_closed(manifest["helper"], {"sha256"})
    if helper["sha256"] != helper_digest:
        raise _fail()
    return {
        **manifest,
        "runtime": normalized_runtime,
        "cni": {**cni, "plugins": normalized_plugins},
        "images": normalized_images,
    }


def _layout(run_id: str) -> dict[str, str]:
    root = CAPSULE_PARENT / f"k1s-{run_id}"
    return {
        "root": str(root),
        **{name: str(root / name) for name in LAYOUT_NAMES},
    }


def _qemu_argv(plan: dict[str, object]) -> list[str]:
    layout = plan["layout"]
    tools = plan["tools"]
    network = plan["network"]
    if type(layout) is not dict or type(tools) is not dict or type(network) is not dict:
        raise _fail()
    run_id = validate_run_id(plan["runId"])
    return [
        str(cast(dict[str, object], tools["qemu"])["path"]),
        "-name",
        f"k1s-{run_id}",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-smp",
        str(VCPU_COUNT),
        "-m",
        str(MEMORY_MIB),
        "-display",
        "none",
        "-nodefaults",
        "-no-reboot",
        "-sandbox",
        "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        "-drive",
        f"file={layout['vm']}/overlay.qcow2,if=virtio,format=qcow2,cache=none,aio=threads",
        "-drive",
        f"file={layout['vm']}/seed.iso,media=cdrom,readonly=on,format=raw",
        "-device",
        "virtio-rng-pci",
        "-netdev",
        f"user,id=q1net,restrict=on,hostfwd=tcp:127.0.0.1:{network['sshHostPort']}-:22",
        "-device",
        "virtio-net-pci,netdev=q1net",
        "-qmp",
        f"unix:{layout['vm']}/qmp.sock,server=on,wait=off",
        "-serial",
        f"file:{layout['vm']}/serial.log",
        "-pidfile",
        f"{layout['vm']}/qemu.pid",
        "-daemonize",
    ]


def _tool_payload(identity: FileIdentity) -> dict[str, object]:
    return identity.to_payload()


def build_plan(
    *,
    run_id: str,
    source_commit: str,
    source_tree: str,
    base_image: FileIdentity,
    guest_manifest_identity: FileIdentity,
    wheel: FileIdentity,
    sdist: FileIdentity,
    wheelhouse_manifest: FileIdentity,
    helper: FileIdentity,
    ssh_public_key: FileIdentity,
    qemu: FileIdentity,
    qemu_img: FileIdentity,
    seed_builder: FileIdentity,
    ssh_host_port: int,
) -> dict[str, object]:
    """Build one deterministic plan from already validated immutable identities."""

    run_id = validate_run_id(run_id)
    _full_object(source_commit)
    _full_object(source_tree)
    if type(ssh_host_port) is not int or not 1024 <= ssh_host_port <= 65535:
        raise _fail()
    manifest = validate_guest_manifest(
        _load_canonical_json(guest_manifest_identity),
        image_digest=base_image.sha256,
        helper_digest=helper.sha256,
        wheelhouse_manifest_digest=wheelhouse_manifest.sha256,
    )
    plan: dict[str, object] = {
        "acceptanceStages": list(ACCEPTANCE_STAGES),
        "apiVersion": API_VERSION,
        "artifacts": {
            "baseImage": base_image.to_payload(),
            "guestHelper": helper.to_payload(),
            "guestManifest": guest_manifest_identity.to_payload(),
            "sdist": sdist.to_payload(),
            "sshPublicKey": ssh_public_key.to_payload(),
            "wheel": wheel.to_payload(),
            "wheelhouseManifest": wheelhouse_manifest.to_payload(),
        },
        "guestIdentity": manifest,
        "kind": PLAN_KIND,
        "layout": _layout(run_id),
        "limits": {
            "guestProcessCount": GUEST_PROCESS_LIMIT,
            "hostNoFile": HOST_NOFILE_LIMIT,
            "maxCommandOutputBytes": MAX_COMMAND_OUTPUT_BYTES,
            "maxEvidenceBytes": MAX_EVIDENCE_BYTES,
            "maxPublicLogBytes": MAX_PUBLIC_LOG_BYTES,
            "memoryMiB": MEMORY_MIB,
            "minFreeAbortBytes": MIN_FREE_ABORT_BYTES,
            "minFreeStartBytes": MIN_FREE_START_BYTES,
            "overlayVirtualSize": OVERLAY_SIZE,
            "perCommandSeconds": PER_COMMAND_SECONDS,
            "vcpus": VCPU_COUNT,
            "wallTimeSeconds": WALL_TIME_SECONDS,
        },
        "mutating": False,
        "network": {
            "dns": "denied",
            "egress": "denied",
            "hostForward": "ssh-only",
            "mode": "qemu-user-restrict",
            "sshHost": "127.0.0.1",
            "sshHostPort": ssh_host_port,
        },
        "qualificationPosture": QUALIFICATION_POSTURE,
        "runId": run_id,
        "schemaVersion": SCHEMA_VERSION,
        "source": {"commit": source_commit, "tree": source_tree},
        "tools": {
            "qemu": _tool_payload(qemu),
            "qemuImg": _tool_payload(qemu_img),
            "seedBuilder": _tool_payload(seed_builder),
        },
        "warnings": [
            "This plan is not live qualification evidence.",
            "Gate A and Gate B require separate paired human approval.",
        ],
    }
    plan["qemuArgv"] = _qemu_argv(plan)
    plan["planDigest"] = _digest_bytes(canonical_json_bytes(plan))
    return plan


def verify_plan(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _fail()
    required = {
        "acceptanceStages",
        "apiVersion",
        "artifacts",
        "guestIdentity",
        "kind",
        "layout",
        "limits",
        "mutating",
        "network",
        "planDigest",
        "qemuArgv",
        "qualificationPosture",
        "runId",
        "schemaVersion",
        "source",
        "tools",
        "warnings",
    }
    if (
        set(value) != required
        or value["apiVersion"] != API_VERSION
        or value["kind"] != PLAN_KIND
        or value["schemaVersion"] != SCHEMA_VERSION
        or value["mutating"] is not False
        or value["qualificationPosture"] != QUALIFICATION_POSTURE
        or value["acceptanceStages"] != list(ACCEPTANCE_STAGES)
    ):
        raise _fail()
    validate_run_id(value["runId"])
    source = _require_closed(value["source"], {"commit", "tree"})
    _full_object(source["commit"])
    _full_object(source["tree"])
    artifacts = _require_closed(
        value["artifacts"],
        {
            "baseImage",
            "guestHelper",
            "guestManifest",
            "sdist",
            "sshPublicKey",
            "wheel",
            "wheelhouseManifest",
        },
    )
    artifact_identities = {
        name: _identity_from_payload(payload) for name, payload in artifacts.items()
    }
    tools = _require_closed(value["tools"], {"qemu", "qemuImg", "seedBuilder"})
    for payload in tools.values():
        _identity_from_payload(payload)
    normalized_guest = validate_guest_manifest(
        value["guestIdentity"],
        image_digest=artifact_identities["baseImage"].sha256,
        helper_digest=artifact_identities["guestHelper"].sha256,
        wheelhouse_manifest_digest=artifact_identities["wheelhouseManifest"].sha256,
    )
    if normalized_guest != value["guestIdentity"]:
        raise _fail()
    expected_layout = _layout(str(value["runId"]))
    expected_limits = {
        "guestProcessCount": GUEST_PROCESS_LIMIT,
        "hostNoFile": HOST_NOFILE_LIMIT,
        "maxCommandOutputBytes": MAX_COMMAND_OUTPUT_BYTES,
        "maxEvidenceBytes": MAX_EVIDENCE_BYTES,
        "maxPublicLogBytes": MAX_PUBLIC_LOG_BYTES,
        "memoryMiB": MEMORY_MIB,
        "minFreeAbortBytes": MIN_FREE_ABORT_BYTES,
        "minFreeStartBytes": MIN_FREE_START_BYTES,
        "overlayVirtualSize": OVERLAY_SIZE,
        "perCommandSeconds": PER_COMMAND_SECONDS,
        "vcpus": VCPU_COUNT,
        "wallTimeSeconds": WALL_TIME_SECONDS,
    }
    network = _require_closed(
        value["network"],
        {"dns", "egress", "hostForward", "mode", "sshHost", "sshHostPort"},
    )
    if (
        network["dns"] != "denied"
        or network["egress"] != "denied"
        or network["hostForward"] != "ssh-only"
        or network["mode"] != "qemu-user-restrict"
        or network["sshHost"] != "127.0.0.1"
        or type(network["sshHostPort"]) is not int
        or not 1024 <= network["sshHostPort"] <= 65535
    ):
        raise _fail()
    if (
        value["layout"] != expected_layout
        or value["limits"] != expected_limits
        or value["warnings"]
        != [
            "This plan is not live qualification evidence.",
            "Gate A and Gate B require separate paired human approval.",
        ]
        or value["qemuArgv"] != _qemu_argv(value)
    ):
        raise _fail()
    digest = value["planDigest"]
    unsigned = dict(value)
    unsigned.pop("planDigest")
    if _digest(digest) != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    return value


def _write_private(path: Path, raw: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise _fail()
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        PRIVATE_FILE_MODE,
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
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_private_json(path: Path, value: object) -> None:
    _write_private(path, canonical_json_bytes(value))


def _make_layout(plan: dict[str, object]) -> dict[str, Path]:
    layout = _require_closed(
        plan["layout"], {"cleanup", "evidence", "guest", "input", "root", "vm"}
    )
    root = _canonical_absolute_path(layout["root"])
    if root != CAPSULE_PARENT / f"k1s-{validate_run_id(plan['runId'])}":
        raise _fail()
    if root.exists() or root.is_symlink():
        raise _fail()
    root.mkdir(mode=PRIVATE_MODE)
    root.chmod(PRIVATE_MODE)
    result = {"root": root}
    for name in LAYOUT_NAMES:
        path = _canonical_absolute_path(layout[name])
        if path.parent != root:
            raise _fail()
        path.mkdir(mode=PRIVATE_MODE)
        path.chmod(PRIVATE_MODE)
        result[name] = path
    return result


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int,
        environment: dict[str, str],
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Execute one already validated argv without a shell or ambient environment."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int,
        environment: dict[str, str],
    ) -> CommandResult:
        def child_limits() -> None:
            nofile_hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
            if nofile_hard != resource.RLIM_INFINITY and nofile_hard < HOST_NOFILE_LIMIT:
                raise _fail()
            resource.setrlimit(resource.RLIMIT_NOFILE, (HOST_NOFILE_LIMIT, HOST_NOFILE_LIMIT))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            os.umask(0o077)

        try:
            result = subprocess.run(  # noqa: S603 - exact executable identity is plan-bound
                list(argv),
                cwd=cwd,
                env=environment,
                capture_output=True,
                timeout=timeout,
                check=False,
                close_fds=True,
                start_new_session=True,
                preexec_fn=child_limits,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _fail() from exc
        if (
            len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or len(result.stderr) > MAX_COMMAND_OUTPUT_BYTES
        ):
            raise _fail()
        return CommandResult(result.returncode, result.stdout, result.stderr)


def _clean_environment() -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
    }


def _record_step(
    evidence: Path,
    *,
    sequence: int,
    name: str,
    argv: Sequence[str],
    result: CommandResult,
) -> None:
    if (
        result.returncode != 0
        or len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES
        or len(result.stderr) > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise _fail()
    receipt = {
        "apiVersion": API_VERSION,
        "argvDigest": _digest_bytes(canonical_json_bytes(list(argv))),
        "kind": "K1sQ1StepReceipt",
        "name": name,
        "output": {
            "stderrBytes": len(result.stderr),
            "stderrSha256": _digest_bytes(result.stderr),
            "stdoutBytes": len(result.stdout),
            "stdoutSha256": _digest_bytes(result.stdout),
            "truncated": False,
        },
        "returnCode": result.returncode,
        "schemaVersion": SCHEMA_VERSION,
        "sequence": sequence,
    }
    _write_private_json(evidence / f"{sequence:03d}-{name}.json", receipt)


def _revalidate_plan_inputs(plan: dict[str, object]) -> None:
    artifacts = plan["artifacts"]
    tools = plan["tools"]
    if type(artifacts) is not dict or type(tools) is not dict:
        raise _fail()
    for raw in artifacts.values():
        identity = _identity_from_payload(raw)
        current = file_identity(
            identity.path,
            expected_digest=identity.sha256,
        )
        if current != identity:
            raise _fail()
    for raw in tools.values():
        identity = _identity_from_payload(raw)
        current = file_identity(
            identity.path,
            expected_digest=identity.sha256,
            executable=True,
        )
        if current != identity:
            raise _fail()


def _validate_gate_a(path: Path, expected_digest: str, run_id: str) -> dict[str, object]:
    identity = file_identity(str(path), expected_digest=expected_digest, maximum=MAX_JSON_BYTES)
    value = _load_canonical_json(identity)
    expected_keys = {
        "apiVersion",
        "device",
        "gid",
        "inode",
        "kind",
        "major",
        "minor",
        "mode",
        "runId",
        "schemaVersion",
        "uid",
        "userUid",
    }
    if (
        set(value) != expected_keys
        or value["apiVersion"] != API_VERSION
        or value["kind"] != "K1sQ1GateAAttestation"
        or value["schemaVersion"] != SCHEMA_VERSION
        or value["runId"] != run_id
        or value["major"] != 10
        or value["minor"] != 232
        or value["uid"] != 0
        or value["mode"] != "0660"
        or value["userUid"] != os.getuid()
        or any(
            type(number) is not int or number < 0
            for number in (value["device"], value["gid"], value["inode"])
        )
    ):
        raise _fail()
    try:
        current = os.stat("/dev/kvm", follow_symlinks=False)
    except OSError as exc:
        raise _fail() from exc
    if (
        not stat.S_ISCHR(current.st_mode)
        or os.major(current.st_rdev) != 10
        or os.minor(current.st_rdev) != 232
        or current.st_dev != value["device"]
        or current.st_ino != value["inode"]
        or current.st_uid != value["uid"]
        or current.st_gid != value["gid"]
        or f"0{stat.S_IMODE(current.st_mode):03o}" != value["mode"]
        or not os.access("/dev/kvm", os.R_OK | os.W_OK)
    ):
        raise _fail()
    return value


def execute_plan(
    plan: dict[str, object],
    *,
    expected_plan_digest: str,
    confirmation: str,
    gate_a_path: Path,
    expected_gate_a_digest: str,
    runner: CommandRunner | None = None,
) -> Path:
    """Cross the explicit run boundary after all immutable checks succeed."""

    plan = verify_plan(plan)
    plan_digest = _digest(expected_plan_digest)
    if plan["planDigest"] != plan_digest:
        raise _fail()
    run_id = validate_run_id(plan["runId"])
    if confirmation != f"{run_id}:{plan_digest}":
        raise _fail()
    _revalidate_plan_inputs(plan)
    _validate_gate_a(gate_a_path, expected_gate_a_digest, run_id)
    if shutil.disk_usage(CAPSULE_PARENT).free < MIN_FREE_START_BYTES:
        raise _fail()
    started = time.monotonic()

    def check_limits() -> None:
        if (
            time.monotonic() - started > WALL_TIME_SECONDS
            or shutil.disk_usage(CAPSULE_PARENT).free < MIN_FREE_ABORT_BYTES
        ):
            raise _fail()

    paths = _make_layout(plan)
    runner = runner or SubprocessCommandRunner()
    environment = _clean_environment()
    tools = plan["tools"]
    artifacts = plan["artifacts"]
    assert isinstance(tools, dict) and isinstance(artifacts, dict)
    base = str(cast(dict[str, object], artifacts["baseImage"])["path"])
    overlay = paths["vm"] / "overlay.qcow2"
    qemu_img_argv = [
        str(cast(dict[str, object], tools["qemuImg"])["path"]),
        "create",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        base,
        str(overlay),
        OVERLAY_SIZE,
    ]
    result = runner.run(
        qemu_img_argv,
        cwd=paths["vm"],
        timeout=PER_COMMAND_SECONDS,
        environment=environment,
    )
    _record_step(paths["evidence"], sequence=1, name="overlay", argv=qemu_img_argv, result=result)
    check_limits()

    meta_data = b"instance-id: " + run_id.encode("ascii") + b"\nlocal-hostname: k1s-q1\n"
    ssh_key_identity = _identity_from_payload(artifacts["sshPublicKey"])
    public_key = _read_identity_bytes(
        ssh_key_identity,
        maximum=64 * 1024,
    )
    user_data = (
        b"#cloud-config\nusers:\n  - name: q1\n    lock_passwd: true\n"
        b"    sudo: false\n    ssh_authorized_keys:\n      - "
        + public_key.strip()
        + b"\nwrite_files:\n  - path: /etc/k1s-q1-egress-denied\n"
        b"    permissions: '0600'\n    content: enabled\n"
        b"  - path: /etc/security/limits.d/90-k1s-q1.conf\n"
        b"    permissions: '0644'\n"
        b"    content: |\n      q1 hard nproc 512\n      q1 hard nofile 1024\n"
    )
    _write_private(paths["input"] / "meta-data", meta_data)
    _write_private(paths["input"] / "user-data", user_data)
    seed_argv = [
        str(cast(dict[str, object], tools["seedBuilder"])["path"]),
        "-output",
        str(paths["vm"] / "seed.iso"),
        "-volid",
        "cidata",
        "-joliet",
        "-rock",
        str(paths["input"] / "user-data"),
        str(paths["input"] / "meta-data"),
    ]
    result = runner.run(
        seed_argv,
        cwd=paths["vm"],
        timeout=PER_COMMAND_SECONDS,
        environment=environment,
    )
    _record_step(paths["evidence"], sequence=2, name="seed", argv=seed_argv, result=result)
    check_limits()

    qemu_argv = [str(item) for item in cast(list[object], plan["qemuArgv"])]
    result = runner.run(
        qemu_argv,
        cwd=paths["vm"],
        timeout=PER_COMMAND_SECONDS,
        environment=environment,
    )
    _record_step(paths["evidence"], sequence=3, name="qemu-launch", argv=qemu_argv, result=result)
    check_limits()
    run_receipt = _bind_receipt(
        {
            "apiVersion": API_VERSION,
            "kind": "K1sQ1RunReceipt",
            "planDigest": plan_digest,
            "qualificationPosture": QUALIFICATION_POSTURE,
            "runId": run_id,
            "schemaVersion": SCHEMA_VERSION,
            "state": "launched-unqualified",
        }
    )
    _write_private_json(paths["guest"] / "run.json", run_receipt)
    return paths["root"]


def _safe_tree_bytes(root: Path) -> int:
    total = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            path = current_path / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise _fail()
            if info.st_uid != os.getuid() or info.st_dev != root.stat().st_dev:
                raise _fail()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_EVIDENCE_BYTES:
                    raise _fail()
    return total


def seal_evidence(
    root: Path,
    *,
    plan_digest: str,
    source_commit: str,
    source_tree: str,
) -> dict[str, object]:
    """Seal retained metadata-safe evidence without including private inputs."""

    evidence = root / "evidence"
    if not evidence.is_dir() or evidence.is_symlink():
        raise _fail()
    _safe_tree_bytes(evidence)
    excluded = {"Q1-EVIDENCE-MANIFEST.json", "SHA256SUMS", "Q1-EVIDENCE-SEAL.json"}
    members: list[dict[str, object]] = []
    for path in sorted(evidence.iterdir(), key=lambda item: item.name.encode("utf-8")):
        if path.name in excluded:
            continue
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _fail()
        members.append(
            {
                "classification": "metadata-safe",
                "path": path.name,
                "sha256": _file_digest(path),
                "size": info.st_size,
                "truncated": False,
            }
        )
    manifest = {
        "apiVersion": API_VERSION,
        "kind": EVIDENCE_KIND,
        "members": members,
        "planDigest": _digest(plan_digest),
        "qualificationPosture": QUALIFICATION_POSTURE,
        "schemaVersion": SCHEMA_VERSION,
        "source": {"commit": _full_object(source_commit), "tree": _full_object(source_tree)},
    }
    manifest_path = evidence / "Q1-EVIDENCE-MANIFEST.json"
    _write_private_json(manifest_path, manifest)
    sum_members: list[dict[str, object]] = [
        *members,
        {"path": manifest_path.name, "sha256": _file_digest(manifest_path)},
    ]
    sums = "".join(
        f"{str(item['sha256']).removeprefix('sha256:')}  {item['path']}\n"
        for item in sorted(sum_members, key=lambda item: str(item["path"]).encode("utf-8"))
    ).encode("utf-8")
    sums_path = evidence / "SHA256SUMS"
    _write_private(sums_path, sums)
    seal = {
        "apiVersion": API_VERSION,
        "evidenceManifestSha256": _file_digest(manifest_path),
        "kind": SEAL_KIND,
        "planDigest": _digest(plan_digest),
        "qualificationPosture": QUALIFICATION_POSTURE,
        "schemaVersion": SCHEMA_VERSION,
        "sha256sumsSha256": _file_digest(sums_path),
        "source": {"commit": source_commit, "tree": source_tree},
    }
    _write_private_json(evidence / "Q1-EVIDENCE-SEAL.json", seal)
    return seal


def _remove_exact_tree(path: Path, *, expected_parent: Path) -> None:
    if path.parent != expected_parent:
        raise _fail()
    info = os.lstat(path)
    parent_info = os.lstat(expected_parent)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_dev != parent_info.st_dev
        or stat.S_IMODE(info.st_mode) != PRIVATE_MODE
    ):
        raise _fail()
    for child in path.iterdir():
        child_info = os.lstat(child)
        if child_info.st_dev != info.st_dev or child_info.st_uid != os.getuid():
            raise _fail()
        if stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(child_info.st_mode):
            _remove_exact_tree(child, expected_parent=path)
        elif stat.S_ISREG(child_info.st_mode) and child_info.st_nlink == 1:
            child.unlink()
        else:
            raise _fail()
    path.rmdir()


def _load_run_receipt(root: Path, expected_file_digest: str) -> dict[str, object]:
    path = root / "guest" / "run.json"
    identity = file_identity(
        str(path),
        expected_digest=expected_file_digest,
        maximum=MAX_JSON_BYTES,
    )
    receipt = _verify_bound_receipt(
        _load_canonical_json(identity),
        {
            "apiVersion",
            "kind",
            "planDigest",
            "qualificationPosture",
            "runId",
            "schemaVersion",
            "state",
        },
    )
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != "K1sQ1RunReceipt"
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or receipt["qualificationPosture"] != QUALIFICATION_POSTURE
        or receipt["state"] != "launched-unqualified"
    ):
        raise _fail()
    _digest(receipt["planDigest"])
    validate_run_id(receipt["runId"])
    return receipt


def teardown_private_layout(
    root: Path,
    *,
    expect_run_id: str,
    expected_run_receipt_file_digest: str,
    confirmation: str,
    process_absent: bool,
) -> Path:
    """Remove only exact private capsule objects after the QEMU identity is absent.

    A live executor must first use QMP and exact PID/start-time/executable/argv
    evidence to stop QEMU.  This filesystem boundary intentionally refuses to
    discover or signal a process by name or pattern.
    """

    run_id = validate_run_id(expect_run_id)
    expected = CAPSULE_PARENT / f"k1s-{run_id}"
    if root != expected or not process_absent:
        raise _fail()
    run_receipt = _load_run_receipt(root, expected_run_receipt_file_digest)
    if run_receipt["runId"] != run_id or confirmation != f"{run_id}:{run_receipt['receiptDigest']}":
        raise _fail()
    root_info = os.lstat(root)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_IMODE(root_info.st_mode) != PRIVATE_MODE
        or root_info.st_uid != os.getuid()
    ):
        raise _fail()
    for name in ("input", "vm", "guest"):
        path = root / name
        if path.exists():
            _remove_exact_tree(path, expected_parent=root)
    cleanup = root / "cleanup"
    receipt = _bind_receipt(
        {
            "apiVersion": API_VERSION,
            "kind": "K1sQ1CleanupReceipt",
            "privateObjectsRemoved": ["guest", "input", "vm"],
            "qualificationPosture": QUALIFICATION_POSTURE,
            "runId": run_id,
            "runReceiptDigest": run_receipt["receiptDigest"],
            "schemaVersion": SCHEMA_VERSION,
            "zeroResiduePendingIndependentVerification": True,
        }
    )
    receipt_path = cleanup / "cleanup.json"
    _write_private_json(receipt_path, receipt)
    return receipt_path


def verify_clean(
    root: Path,
    *,
    expect_run_id: str,
    receipt_path: Path,
    expected_receipt_file_digest: str,
) -> None:
    run_id = validate_run_id(expect_run_id)
    if root != CAPSULE_PARENT / f"k1s-{run_id}":
        raise _fail()
    if receipt_path != root / "cleanup" / "cleanup.json":
        raise _fail()
    names = sorted(item.name for item in root.iterdir())
    if names != ["cleanup", "evidence"]:
        raise _fail()
    for name in names:
        info = os.lstat(root / name)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != PRIVATE_MODE
        ):
            raise _fail()
    _safe_tree_bytes(root / "evidence")
    _safe_tree_bytes(root / "cleanup")
    receipt_identity = file_identity(
        str(receipt_path),
        expected_digest=expected_receipt_file_digest,
        maximum=MAX_JSON_BYTES,
    )
    receipt = _verify_bound_receipt(
        _load_canonical_json(receipt_identity),
        {
            "apiVersion",
            "kind",
            "privateObjectsRemoved",
            "qualificationPosture",
            "runId",
            "runReceiptDigest",
            "schemaVersion",
            "zeroResiduePendingIndependentVerification",
        },
    )
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != "K1sQ1CleanupReceipt"
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or receipt["qualificationPosture"] != QUALIFICATION_POSTURE
        or receipt["runId"] != run_id
        or receipt["privateObjectsRemoved"] != ["guest", "input", "vm"]
        or receipt["zeroResiduePendingIndependentVerification"] is not True
    ):
        raise _fail()
    _digest(receipt["runReceiptDigest"])


def _read_plan(path: Path, expected_file_digest: str) -> dict[str, object]:
    identity = file_identity(
        str(path), expected_digest=expected_file_digest, maximum=MAX_JSON_BYTES
    )
    return verify_plan(_load_canonical_json(identity))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Build a pure digest-bound capsule plan")
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--source-commit", required=True)
    plan.add_argument("--source-tree", required=True)
    for name in (
        "base-image",
        "guest-manifest",
        "wheel",
        "sdist",
        "wheelhouse-manifest",
        "guest-helper",
        "ssh-public-key",
        "qemu",
        "qemu-img",
        "seed-builder",
    ):
        plan.add_argument(f"--{name}", type=Path, required=True)
        plan.add_argument(f"--expect-{name}-sha256", required=True)
    plan.add_argument("--ssh-host-port", type=int, required=True)
    plan.add_argument("--output-root", type=Path, required=True)
    plan.add_argument("--output", type=Path)

    run = sub.add_parser("run", help="Cross the explicit Gate A capsule boundary")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--expect-plan-sha256", required=True)
    run.add_argument("--expect-plan-digest", required=True)
    run.add_argument("--confirm", required=True)
    run.add_argument("--gate-a-attestation", type=Path, required=True)
    run.add_argument("--expect-gate-a-sha256", required=True)

    teardown = sub.add_parser("teardown", help="Remove exact private state after QEMU stops")
    teardown.add_argument("--run-root", type=Path, required=True)
    teardown.add_argument("--run-id", required=True)
    teardown.add_argument("--expect-run-receipt-sha256", required=True)
    teardown.add_argument("--confirm", required=True)
    teardown.add_argument("--confirm-process-absent", action="store_true")

    clean = sub.add_parser("verify-clean", help="Verify only evidence and cleanup remain")
    clean.add_argument("--run-id", required=True)
    clean.add_argument("--receipt", type=Path, required=True)
    clean.add_argument("--expect-receipt-sha256", required=True)
    return parser


def _plan_from_namespace(ns: argparse.Namespace) -> dict[str, object]:
    def identity(name: str, *, executable: bool = False) -> FileIdentity:
        key = name.replace("-", "_")
        return file_identity(
            str(getattr(ns, key)),
            expected_digest=getattr(ns, f"expect_{key}_sha256"),
            executable=executable,
        )

    run_id = validate_run_id(ns.run_id)
    if _canonical_absolute_path(str(ns.output_root)) != CAPSULE_PARENT / f"k1s-{run_id}":
        raise _fail()
    return build_plan(
        run_id=run_id,
        source_commit=ns.source_commit,
        source_tree=ns.source_tree,
        base_image=identity("base-image"),
        guest_manifest_identity=identity("guest-manifest"),
        wheel=identity("wheel"),
        sdist=identity("sdist"),
        wheelhouse_manifest=identity("wheelhouse-manifest"),
        helper=identity("guest-helper"),
        ssh_public_key=identity("ssh-public-key"),
        qemu=identity("qemu", executable=True),
        qemu_img=identity("qemu-img", executable=True),
        seed_builder=identity("seed-builder", executable=True),
        ssh_host_port=ns.ssh_host_port,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    try:
        if ns.command == "plan":
            plan = _plan_from_namespace(ns)
            raw = canonical_json_bytes(plan)
            if ns.output is None:
                sys.stdout.buffer.write(raw)
            else:
                output = _canonical_absolute_path(str(ns.output))
                parent = output.parent
                parent_info = os.lstat(parent)
                if (
                    not stat.S_ISDIR(parent_info.st_mode)
                    or parent_info.st_uid != os.getuid()
                    or stat.S_IMODE(parent_info.st_mode) != PRIVATE_MODE
                ):
                    raise _fail()
                _write_private(output, raw)
            return 0
        if ns.command == "run":
            plan = _read_plan(ns.plan, ns.expect_plan_sha256)
            execute_plan(
                plan,
                expected_plan_digest=ns.expect_plan_digest,
                confirmation=ns.confirm,
                gate_a_path=ns.gate_a_attestation,
                expected_gate_a_digest=ns.expect_gate_a_sha256,
            )
            return 0
        if ns.command == "teardown":
            teardown_private_layout(
                ns.run_root,
                expect_run_id=ns.run_id,
                expected_run_receipt_file_digest=ns.expect_run_receipt_sha256,
                confirmation=ns.confirm,
                process_absent=ns.confirm_process_absent,
            )
            return 0
        if ns.command == "verify-clean":
            verify_clean(
                CAPSULE_PARENT / f"k1s-{validate_run_id(ns.run_id)}",
                expect_run_id=ns.run_id,
                receipt_path=ns.receipt,
                expected_receipt_file_digest=ns.expect_receipt_sha256,
            )
            return 0
        raise _fail()
    except CapsulePolicyError:
        print("K1s Q1 qualification capsule failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
