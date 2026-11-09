#!/usr/bin/env python3
"""Build and independently replay the closed K1s Q1 guest-input BOM.

QH5-B B2 uses this module after network acquisition has finished.  It accepts
only an explicit list of regular files below one private staging directory,
writes a deterministic uncompressed USTAR payload, and derives the canonical
``K1sQ1GuestBuildBOM`` already consumed by the B0 image builder.

The replay path reads only the retained tar bytes.  It validates the embedded
member manifest, rehashes every member, reconstructs the BOM, and then invokes
the pre-existing B0 validator.  Agreement between the externally retained BOM
and replayed BOM is the B2 two-validator gate.  This module performs no
network, package installation, image build, QEMU, KVM, or guest action.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

try:
    from scripts.qualification import kp1_q1_guest_image as guest_image
except ModuleNotFoundError:  # Direct execution from scripts/qualification.
    import kp1_q1_guest_image as guest_image  # type: ignore[import-not-found,no-redef]

API_VERSION = "k1s.dev/q1-guest-bom-payload/v1"
SCHEMA_VERSION = 1
MANIFEST_KIND = "K1sQ1GuestBOMPayloadManifest"
MANIFEST_NAME = "B2-PAYLOAD-MANIFEST.json"
SOURCE_DATE_EPOCH = guest_image.SOURCE_DATE_EPOCH

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024 * 1024
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024 * 1024
MAX_MEMBERS = 4096
COPY_BLOCK_BYTES = 1024 * 1024

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
OBJECT_RE = re.compile(r"[0-9a-f]{40}")
ROLE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
PATH_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")

SINGLETON_ROLES = frozenset(
    {
        "accepted-sdist",
        "accepted-wheel",
        "apt-package-ledger",
        "base-acquisition-receipt",
        "base-package-manifest",
        "cni-config",
        "crictl-archive",
        "crictl-checksum",
        "deadsnakes-keyring",
        "guest-helper",
        "offline-evidence",
        "oci-resolution-ledger",
        "qh5a-completion",
        "requirements-lock",
        "ubuntu-keyring",
        "wheelhouse-manifest",
    }
)
MULTI_ROLES = frozenset({"apt-metadata", "apt-package", "oci-image", "wheel"})
ALL_ROLES = SINGLETON_ROLES | MULTI_ROLES
REQUIRED_MINIMUMS = {
    "apt-metadata": 8,
    "apt-package": 1,
    "oci-image": 2,
    "wheel": 1,
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


class GuestBOMPolicyError(RuntimeError):
    """The B2 payload or its evidence failed the closed-input policy."""


def _fail() -> GuestBOMPolicyError:
    return GuestBOMPolicyError("K1s Q1 guest BOM payload failed closed")


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


def canonical_json(raw: bytes, *, maximum: int = MAX_JSON_BYTES) -> dict[str, object]:
    if not 1 <= len(raw) <= maximum:
        raise _fail()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return cast(dict[str, object], value)


def digest_bytes(raw: bytes) -> str:
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


def _relative_path(value: object) -> str:
    if type(value) is not str or not value or value.startswith("/"):
        raise _fail()
    if "\\" in value or "\x00" in value or value == MANIFEST_NAME:
        raise _fail()
    pure = PurePosixPath(value)
    if pure.as_posix() != value or any(part in {"", ".", ".."} for part in pure.parts):
        raise _fail()
    if len(value.encode("utf-8")) > 255:
        raise _fail()
    for part in pure.parts:
        if part in PROTECTED_NAMES or PATH_PART_RE.fullmatch(part) is None:
            raise _fail()
    return value


def _role(value: object) -> str:
    if type(value) is not str or ROLE_RE.fullmatch(value) is None or value not in ALL_ROLES:
        raise _fail()
    return value


def _source(value: object) -> dict[str, object]:
    item = _closed(value, {"commit", "sourceDateEpoch", "tree", "version"})
    _object_id(item["commit"])
    _object_id(item["tree"])
    if item["version"] != guest_image.CANDIDATE_VERSION:
        raise _fail()
    if item["sourceDateEpoch"] != SOURCE_DATE_EPOCH:
        raise _fail()
    return item


def _hash_stream(stream: BinaryIO, size: int) -> str:
    if not 1 <= size <= MAX_MEMBER_BYTES:
        raise _fail()
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = stream.read(min(remaining, COPY_BLOCK_BYTES))
        if not block:
            raise _fail()
        digest.update(block)
        remaining -= len(block)
    if stream.read(1):
        raise _fail()
    return f"sha256:{digest.hexdigest()}"


def _path_chain_no_links(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise _fail() from exc
        if stat.S_ISLNK(info.st_mode):
            raise _fail()
    return current


def _private_directory(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
        info = os.lstat(path)
    except (OSError, RuntimeError) as exc:
        raise _fail() from exc
    if (
        resolved != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_nlink < 2
    ):
        raise _fail()


@dataclass(frozen=True, slots=True)
class PayloadMember:
    path: str
    role: str
    sha256: str
    size: int
    mode: str

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "path": self.path,
            "role": self.role,
            "sha256": self.sha256,
            "size": self.size,
        }


def observe_member(staging_root: Path, *, path: object, role: object) -> PayloadMember:
    relative = _relative_path(path)
    selected_role = _role(role)
    target = _path_chain_no_links(staging_root, relative)
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or mode & 0o022
            or not 1 <= opened.st_size <= MAX_MEMBER_BYTES
        ):
            raise _fail()
        selected_mode = 0o755 if mode & 0o111 else 0o644
        with os.fdopen(os.dup(descriptor), "rb") as selected:
            digest = _hash_stream(selected, opened.st_size)
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise _fail()
        return PayloadMember(
            path=relative,
            role=selected_role,
            sha256=digest,
            size=opened.st_size,
            mode=f"{selected_mode:04o}",
        )
    finally:
        os.close(descriptor)


def _validate_role_counts(members: Sequence[PayloadMember]) -> None:
    counts = Counter(member.role for member in members)
    if set(counts) != ALL_ROLES:
        raise _fail()
    if any(counts[role] != 1 for role in SINGLETON_ROLES):
        raise _fail()
    if any(counts[role] < minimum for role, minimum in REQUIRED_MINIMUMS.items()):
        raise _fail()


def _validate_guest_bindings(
    guest: Mapping[str, object],
    members: Sequence[PayloadMember],
) -> None:
    by_role: dict[str, list[PayloadMember]] = {}
    for member in members:
        by_role.setdefault(member.role, []).append(member)
    helper = cast(dict[str, object], guest["helper"])
    wheelhouse = cast(dict[str, object], guest["wheelhouse"])
    cni = cast(dict[str, object], guest["cni"])
    if (
        by_role["guest-helper"][0].sha256 != helper["sha256"]
        or by_role["wheelhouse-manifest"][0].sha256 != wheelhouse["manifestSha256"]
        or by_role["requirements-lock"][0].sha256 != wheelhouse["requirementsLockSha256"]
        or by_role["cni-config"][0].sha256 != cni["configTemplateSha256"]
    ):
        raise _fail()
    archives: dict[str, str] = {}
    for member in by_role["oci-image"]:
        pure = PurePosixPath(member.path)
        if len(pure.parts) != 2 or pure.parts[0] != "oci" or not pure.name.endswith(".oci.tar"):
            raise _fail()
        name = pure.name.removesuffix(".oci.tar")
        if name in archives:
            raise _fail()
        archives[name] = member.sha256
    expected: dict[str, str] = {}
    for image in cast(list[dict[str, object]], guest["images"]):
        expected[cast(str, image["name"])] = cast(str, image["archiveSha256"])
    registry = cast(dict[str, object], guest["registry"])
    expected["registry"] = cast(str, registry["archiveSha256"])
    if archives != expected:
        raise _fail()


def _read_staged_member(staging_root: Path, member: PayloadMember) -> bytes:
    target = _path_chain_no_links(staging_root, member.path)
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != member.size
            or opened.st_size > MAX_JSON_BYTES
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


def _validate_staged_semantics(
    staging_root: Path,
    guest: Mapping[str, object],
    members: Sequence[PayloadMember],
) -> None:
    by_role = {member.role: member for member in members if member.role in SINGLETON_ROLES}
    cni_value = canonical_json(_read_staged_member(staging_root, by_role["cni-config"]))
    if cni_value.get("cniVersion") != "1.0.0":
        raise _fail()
    plugins = cni_value.get("plugins")
    if type(plugins) is not list or [
        value.get("type") if type(value) is dict else None for value in plugins
    ] != ["bridge", "portmap", "firewall", "tuning"]:
        raise _fail()
    offline = _closed(
        canonical_json(_read_staged_member(staging_root, by_role["offline-evidence"])),
        {"apiVersion", "helper", "images", "kind", "registry", "schemaVersion", "wheelhouse"},
    )
    if (
        offline["apiVersion"] != "k1s.dev/q1-guest-offline-evidence/v1"
        or offline["kind"] != "K1sQ1GuestOfflineEvidence"
        or offline["schemaVersion"] != 1
        or offline["helper"] != guest["helper"]
        or offline["images"] != guest["images"]
        or offline["registry"] != guest["registry"]
        or offline["wheelhouse"] != guest["wheelhouse"]
    ):
        raise _fail()


def build_member_manifest(
    *,
    source: object,
    guest: object,
    members: Sequence[PayloadMember],
) -> dict[str, object]:
    normalized_source = _source(source)
    try:
        normalized_guest = guest_image.validate_guest_template(guest)
    except Exception as exc:
        raise _fail() from exc
    if not 1 <= len(members) <= MAX_MEMBERS - 1:
        raise _fail()
    ordered = sorted(members, key=lambda item: item.path.encode("utf-8"))
    if list(members) != ordered or len({item.path for item in ordered}) != len(ordered):
        raise _fail()
    _validate_role_counts(ordered)
    _validate_guest_bindings(normalized_guest, ordered)
    return {
        "apiVersion": API_VERSION,
        "guest": normalized_guest,
        "kind": MANIFEST_KIND,
        "members": [item.as_dict() for item in ordered],
        "schemaVersion": SCHEMA_VERSION,
        "source": normalized_source,
    }


def validate_member_manifest(value: object) -> dict[str, object]:
    item = _closed(value, {"apiVersion", "guest", "kind", "members", "schemaVersion", "source"})
    if (
        item["apiVersion"] != API_VERSION
        or item["kind"] != MANIFEST_KIND
        or item["schemaVersion"] != SCHEMA_VERSION
    ):
        raise _fail()
    source = _source(item["source"])
    try:
        guest = guest_image.validate_guest_template(item["guest"])
    except Exception as exc:
        raise _fail() from exc
    raw_members = item["members"]
    if type(raw_members) is not list or not 1 <= len(raw_members) <= MAX_MEMBERS - 1:
        raise _fail()
    members: list[PayloadMember] = []
    for raw in raw_members:
        row = _closed(raw, {"mode", "path", "role", "sha256", "size"})
        mode = row["mode"]
        size = row["size"]
        if (
            mode not in {"0644", "0755"}
            or type(size) is not int
            or not 1 <= size <= MAX_MEMBER_BYTES
        ):
            raise _fail()
        members.append(
            PayloadMember(
                path=_relative_path(row["path"]),
                role=_role(row["role"]),
                sha256=_digest(row["sha256"]),
                size=size,
                mode=cast(str, mode),
            )
        )
    if members != sorted(members, key=lambda member: member.path.encode("utf-8")):
        raise _fail()
    if len({member.path for member in members}) != len(members):
        raise _fail()
    _validate_role_counts(members)
    _validate_guest_bindings(guest, members)
    return {
        **item,
        "guest": guest,
        "members": [member.as_dict() for member in members],
        "source": source,
    }


def bom_from_manifest(
    manifest_value: object,
    *,
    payload_sha256: object,
    payload_size: object,
) -> dict[str, object]:
    manifest = validate_member_manifest(manifest_value)
    if type(payload_size) is not int or not 1 <= payload_size <= MAX_PAYLOAD_BYTES:
        raise _fail()
    source = cast(dict[str, object], manifest["source"])
    members = cast(list[object], manifest["members"])
    bom = {
        "apiVersion": guest_image.BOM_API_VERSION,
        "guest": manifest["guest"],
        "kind": guest_image.BOM_KIND,
        "payload": {
            "memberCount": len(members) + 1,
            "sha256": _digest(payload_sha256),
            "size": payload_size,
        },
        "schemaVersion": guest_image.BOM_SCHEMA_VERSION,
        "sourceCommit": source["commit"],
        "sourceDateEpoch": source["sourceDateEpoch"],
        "sourceTree": source["tree"],
    }
    try:
        return guest_image.validate_bom(bom)
    except Exception as exc:
        raise _fail() from exc


class _DigestingWriter:
    def __init__(self, raw: BinaryIO) -> None:
        self.raw = raw
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        count = self.raw.write(data)
        if count != len(data):
            raise _fail()
        self.digest.update(data)
        self.size += count
        if self.size > MAX_PAYLOAD_BYTES:
            raise _fail()
        return count

    def tell(self) -> int:
        return self.raw.tell()

    def flush(self) -> None:
        self.raw.flush()


def _tar_info(name: str, *, size: int, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.size = size
    info.mode = mode
    info.mtime = SOURCE_DATE_EPOCH
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.pax_headers = _expected_pax_headers(name)
    return info


def _expected_pax_headers(name: str) -> dict[str, str]:
    return {"path": name} if len(name.encode("utf-8")) > 100 else {}


def _open_member(staging_root: Path, member: PayloadMember) -> BinaryIO:
    target = _path_chain_no_links(staging_root, member.path)
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size != member.size
        or opened.st_uid != os.getuid()
    ):
        os.close(descriptor)
        raise _fail()
    return os.fdopen(descriptor, "rb")


def write_payload(
    *,
    staging_root: Path,
    destination: Path,
    source: object,
    guest: object,
    member_roles: Mapping[str, str],
) -> tuple[dict[str, object], dict[str, object]]:
    """Write one deterministic payload and return its manifest and BOM."""

    _private_directory(staging_root)
    if (
        destination.exists()
        or destination.is_symlink()
        or destination.parent != staging_root.parent
    ):
        raise _fail()
    ordered_paths = sorted(member_roles, key=lambda value: value.encode("utf-8"))
    if ordered_paths != list(member_roles) or len(ordered_paths) != len(member_roles):
        raise _fail()
    members = [
        observe_member(staging_root, path=path, role=member_roles[path]) for path in ordered_paths
    ]
    try:
        normalized_guest = guest_image.validate_guest_template(guest)
    except Exception as exc:
        raise _fail() from exc
    _validate_staged_semantics(staging_root, normalized_guest, members)
    manifest = build_member_manifest(source=source, guest=guest, members=members)
    manifest_raw = canonical_json_bytes(manifest)
    temporary = destination.with_name(f".{destination.name}.partial")
    if temporary.exists() or temporary.is_symlink():
        raise _fail()
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as raw:
            output = _DigestingWriter(raw)
            with tarfile.open(
                fileobj=cast(BinaryIO, output), mode="w|", format=tarfile.PAX_FORMAT
            ) as archive:
                archive.addfile(
                    _tar_info(MANIFEST_NAME, size=len(manifest_raw), mode=0o644),
                    io.BytesIO(manifest_raw),
                )
                for member in members:
                    with _open_member(staging_root, member) as selected:
                        archive.addfile(
                            _tar_info(member.path, size=member.size, mode=int(member.mode, 8)),
                            selected,
                        )
            output.flush()
            os.fsync(raw.fileno())
            payload_digest = f"sha256:{output.digest.hexdigest()}"
            payload_size = output.size
        os.chmod(temporary, 0o400)
        os.replace(temporary, destination)
    except Exception:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    bom = bom_from_manifest(
        manifest,
        payload_sha256=payload_digest,
        payload_size=payload_size,
    )
    return manifest, bom


def _hash_path(path: Path) -> tuple[str, int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise _fail() from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _fail()
        return _hash_stream(os.fdopen(os.dup(descriptor), "rb"), opened.st_size), opened.st_size
    finally:
        os.close(descriptor)


def replay_payload(
    payload_path: Path,
    *,
    expected_payload_sha256: object | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Re-enumerate one retained payload and reconstruct its canonical BOM."""

    payload_digest, payload_size = _hash_path(payload_path)
    if expected_payload_sha256 is not None and payload_digest != _digest(expected_payload_sha256):
        raise _fail()
    if not 1 <= payload_size <= MAX_PAYLOAD_BYTES:
        raise _fail()
    try:
        with payload_path.open("rb") as raw, tarfile.open(fileobj=raw, mode="r:") as archive:
            infos = archive.getmembers()
            if not 2 <= len(infos) <= MAX_MEMBERS or infos[0].name != MANIFEST_NAME:
                raise _fail()
            if [info.name for info in infos[1:]] != sorted(
                (info.name for info in infos[1:]), key=lambda value: value.encode("utf-8")
            ):
                raise _fail()
            manifest_info = infos[0]
            if (
                not manifest_info.isreg()
                or manifest_info.mode != 0o644
                or manifest_info.mtime != SOURCE_DATE_EPOCH
                or manifest_info.uid != 0
                or manifest_info.gid != 0
                or manifest_info.uname != "root"
                or manifest_info.gname != "root"
                or manifest_info.pax_headers != _expected_pax_headers(manifest_info.name)
            ):
                raise _fail()
            manifest_stream = archive.extractfile(manifest_info)
            if manifest_stream is None:
                raise _fail()
            manifest = validate_member_manifest(
                canonical_json(manifest_stream.read(MAX_JSON_BYTES + 1))
            )
            rows = cast(list[dict[str, object]], manifest["members"])
            if len(infos) != len(rows) + 1:
                raise _fail()
            for info, row in zip(infos[1:], rows, strict=True):
                expected_mode = int(cast(str, row["mode"]), 8)
                if (
                    info.name != row["path"]
                    or not info.isreg()
                    or info.size != row["size"]
                    or info.mode != expected_mode
                    or info.mtime != SOURCE_DATE_EPOCH
                    or info.uid != 0
                    or info.gid != 0
                    or info.uname != "root"
                    or info.gname != "root"
                    or info.pax_headers != _expected_pax_headers(info.name)
                ):
                    raise _fail()
                selected = archive.extractfile(info)
                if selected is None or _hash_stream(selected, info.size) != row["sha256"]:
                    raise _fail()
    except (OSError, tarfile.TarError) as exc:
        raise _fail() from exc
    bom = bom_from_manifest(
        manifest,
        payload_sha256=payload_digest,
        payload_size=payload_size,
    )
    return manifest, bom


def verify_two_validator_gate(
    *,
    bom_raw: bytes,
    payload_path: Path,
    expected_bom_sha256: object | None = None,
    expected_payload_sha256: object | None = None,
) -> dict[str, object]:
    external_value = canonical_json(bom_raw)
    try:
        external = guest_image.validate_bom(external_value)
    except Exception as exc:
        raise _fail() from exc
    external_raw = guest_image.canonical_json_bytes(external)
    external_digest = digest_bytes(external_raw)
    if expected_bom_sha256 is not None and external_digest != _digest(expected_bom_sha256):
        raise _fail()
    manifest, replayed = replay_payload(
        payload_path,
        expected_payload_sha256=expected_payload_sha256,
    )
    replayed_raw = guest_image.canonical_json_bytes(replayed)
    replayed_digest = digest_bytes(replayed_raw)
    if external_raw != replayed_raw or external_digest != replayed_digest:
        raise _fail()
    return {
        "bomSha256": external_digest,
        "memberManifestSha256": digest_bytes(canonical_json_bytes(manifest)),
        "payloadSha256": cast(dict[str, object], external["payload"])["sha256"],
        "replayValidatorSha256": replayed_digest,
        "schemaValidatorSha256": external_digest,
        "status": "two-validator-match",
    }


def _read(path: Path, maximum: int) -> bytes:
    try:
        info = os.lstat(path)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or not 1 <= info.st_size <= maximum
        ):
            raise _fail()
        return path.read_bytes()
    except OSError as exc:
        raise _fail() from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="replay a B2 payload and compare its canonical BOM")
    verify.add_argument("--bom", required=True, type=Path)
    verify.add_argument("--payload", required=True, type=Path)
    verify.add_argument("--expect-bom-sha256")
    verify.add_argument("--expect-payload-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_two_validator_gate(
            bom_raw=_read(args.bom, MAX_JSON_BYTES),
            payload_path=args.payload,
            expected_bom_sha256=args.expect_bom_sha256,
            expected_payload_sha256=args.expect_payload_sha256,
        )
    except GuestBOMPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
