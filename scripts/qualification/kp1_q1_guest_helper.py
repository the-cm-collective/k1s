#!/usr/bin/env python3
"""Qualification-only strict-CRI action boundary for the isolated Q1 guest.

The installed K1s coordinator owns planning, journaling, and protocol framing.
This script supplies the deliberately separate Q1 filesystem/ACL boundary.  It
is not a console, service, shell proxy, or general privileged file operation.
Every action is re-derived by :class:`StrictCRIHostHelperEngine`; this boundary
then verifies exact pre-state, creates a descriptor-relative private backup,
performs one closed operation, and verifies the resulting digest.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import select
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

try:
    from scripts.qualification import kp1_q1_bundle as q1_bundle
except ModuleNotFoundError:  # Direct execution from /opt/k1s-q1.
    import kp1_q1_bundle as q1_bundle  # type: ignore[import-not-found,no-redef]

from ae.kctl.profile_host_helper import (
    MAX_BACKUP_FILE_BYTES,
    HelperOperation,
    HelperRequest,
    HostActionBoundary,
    ProfileHostHelperError,
    StrictCRIHostHelperEngine,
)
from ae.kctl.profile_host_plan import StrictCRIActionMaterial, canonical_json_bytes

API_VERSION = "k1s.dev/q1-capsule/v1"
SCHEMA_VERSION = 1
MAX_TOOL_OUTPUT = 1024 * 1024
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
TRANSACTION_RE = re.compile(r"ht1-[0-9a-f]{32}")
ACTION_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?")
ACL_RE = re.compile(r"(?:user|group):[0-9]*:[r-][w-][x-]|mask::[r-][w-][x-]|other::[r-][w-][x-]")

ALLOWED_ENVIRONMENT = q1_bundle.ALLOWED_ENVIRONMENT
SYSTEM_TEMP = Path("/tmp")  # noqa: S108 - fixed system temporary directory
SYSTEM_TEMP_OWNER_UID = 0
PROTECTED_LEAF_NAMES = {
    ".env",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    ".envrc",
    "ae-ha-core.env",
    "ae.env",
}


class QualificationHelperError(RuntimeError):
    """The Q1 privilege boundary rejected an unsafe or divergent request."""


def _fail() -> QualificationHelperError:
    return QualificationHelperError("K1s Q1 guest helper failed closed")


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest_value(value: object) -> str:
    return _digest_bytes(canonical_json_bytes(value))


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _canonical_path(value: object) -> Path:
    if type(value) is not str or not value.startswith("/") or value == "/":
        raise _fail()
    if value.startswith("//") or value.endswith("/") or "\\" in value or "\x00" in value:
        raise _fail()
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail()
    if path.name in PROTECTED_LEAF_NAMES:
        raise _fail()
    return Path(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _fail()
        value[key] = item
    return value


def validate_clean_environment(environment: dict[str, str] | None = None) -> None:
    """Require the exact Gate B environment; ambient sudo/session state is invalid."""

    value = dict(os.environ if environment is None else environment)
    if value != ALLOWED_ENVIRONMENT:
        raise _fail()


def _wait_for_fd(fd: int, *, readable: bool, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _fail()
    try:
        readers, writers, errors = select.select(
            [fd] if readable else [],
            [] if readable else [fd],
            [fd],
            remaining,
        )
    except (OSError, ValueError) as exc:
        raise _fail() from exc
    if errors or (readable and not readers) or (not readable and not writers):
        raise _fail()


def _read_exact_bounded(fd: int, size: int, *, deadline: float) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        _wait_for_fd(fd, readable=True, deadline=deadline)
        try:
            block = os.read(fd, remaining)
        except BlockingIOError:
            continue
        except OSError as exc:
            raise _fail() from exc
        if not block:
            raise _fail()
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def _read_protocol_bounded(fd: int, *, deadline: float) -> dict[str, object]:
    if type(fd) is not int or fd < 3:
        raise _fail()
    header = _read_exact_bounded(fd, 9, deadline=deadline)
    if header[8:] != b"\n":
        raise _fail()
    try:
        size = int(header[:8], 16)
    except ValueError as exc:
        raise _fail() from exc
    if not 1 <= size <= q1_bundle.GATE_B_MAX_PROTOCOL_BYTES:
        raise _fail()
    raw = _read_exact_bounded(fd, size, deadline=deadline)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return value


def _write_protocol_bounded(fd: int, payload: object, *, deadline: float) -> None:
    raw = canonical_json_bytes(payload)
    if len(raw) > q1_bundle.GATE_B_MAX_PROTOCOL_BYTES or type(fd) is not int or fd < 3:
        raise _fail()
    view = memoryview(f"{len(raw):08x}\n".encode("ascii") + raw)
    while view:
        _wait_for_fd(fd, readable=False, deadline=deadline)
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            continue
        except OSError as exc:
            raise _fail() from exc
        if written <= 0:
            raise _fail()
        view = view[written:]


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    path: Path
    sha256: str
    device: int
    inode: int


def executable_identity(
    path: Path,
    expected_digest: str,
    *,
    require_executable: bool = True,
) -> ExecutableIdentity:
    try:
        return _executable_identity(
            path,
            expected_digest,
            require_executable=require_executable,
        )
    except QualificationHelperError:
        raise
    except OSError as exc:
        raise _fail() from exc


def _executable_identity(
    path: Path,
    expected_digest: str,
    *,
    require_executable: bool,
) -> ExecutableIdentity:
    path = _canonical_path(str(path))
    expected_digest = _digest(expected_digest)
    current = Path("/")
    for part in path.parts[1:-1]:
        current /= part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise _fail()
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_mode & 0o022:
        raise _fail()
    if (require_executable and not before.st_mode & 0o111) or before.st_uid not in {0, os.getuid()}:
        raise _fail()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
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
        actual = f"sha256:{digest.hexdigest()}"
        if actual != expected_digest:
            raise _fail()
        return ExecutableIdentity(path, actual, opened.st_dev, opened.st_ino)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ToolResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ToolRunner(Protocol):
    def run(self, argv: Sequence[str], *, input_bytes: bytes | None = None) -> ToolResult: ...


class SubprocessToolRunner:
    def run(self, argv: Sequence[str], *, input_bytes: bytes | None = None) -> ToolResult:
        try:
            result = subprocess.run(  # noqa: S603 - executables are independently pinned
                list(argv),
                input=input_bytes,
                capture_output=True,
                env=ALLOWED_ENVIRONMENT,
                cwd="/",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _fail() from exc
        if len(result.stdout) > MAX_TOOL_OUTPUT or len(result.stderr) > MAX_TOOL_OUTPUT:
            raise _fail()
        return ToolResult(result.returncode, result.stdout, result.stderr)


def _open_directory_chain(path: Path, *, expected_uid: int) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    current = Path("/")
    try:
        for part in path.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            current /= part
            info = os.fstat(descriptor)
            mode = stat.S_IMODE(info.st_mode)
            sticky_tmp = (
                current == SYSTEM_TEMP and mode == 0o1777 and info.st_uid == SYSTEM_TEMP_OWNER_UID
            )
            if (
                not stat.S_ISDIR(info.st_mode)
                or (info.st_uid not in {0, expected_uid} and not sticky_tmp)
                or (mode & 0o022 and not sticky_tmp)
            ):
                raise _fail()
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _sha256_fd(descriptor: int, size: int) -> str:
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


def _present_fact(path: Path, info: os.stat_result, *, descriptor: int | None) -> dict[str, object]:
    object_type: str
    digest: str | None = None
    size = 0
    if stat.S_ISREG(info.st_mode):
        if descriptor is None or info.st_size > MAX_BACKUP_FILE_BYTES:
            raise _fail()
        object_type = "regular"
        size = info.st_size
        digest = _sha256_fd(descriptor, info.st_size)
    elif stat.S_ISDIR(info.st_mode):
        object_type = "directory"
    elif stat.S_ISSOCK(info.st_mode):
        object_type = "socket"
    else:
        raise _fail()
    return {
        "device": info.st_dev,
        "gid": info.st_gid,
        "inode": info.st_ino,
        "mode": f"0{stat.S_IMODE(info.st_mode):03o}",
        "nlink": info.st_nlink,
        "path": str(path),
        "sha256": digest,
        "size": size,
        "state": "present",
        "type": object_type,
        "uid": info.st_uid,
    }


def observe_exact(path: Path, *, expected_uid: int) -> dict[str, object]:
    """Observe one exact path through a held, no-follow parent descriptor."""

    path = _canonical_path(str(path))
    parent_fd = _open_directory_chain(path.parent, expected_uid=expected_uid)
    try:
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            parent = os.fstat(parent_fd)
            return {
                "parent": {
                    "device": parent.st_dev,
                    "gid": parent.st_gid,
                    "inode": parent.st_ino,
                    "mode": f"0{stat.S_IMODE(parent.st_mode):03o}",
                    "nlink": parent.st_nlink,
                    "path": str(path.parent),
                    "type": "directory",
                    "uid": parent.st_uid,
                },
                "path": str(path),
                "state": "absent",
            }
        descriptor: int | None = None
        if stat.S_ISREG(info.st_mode):
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                os.close(descriptor)
                raise _fail()
            info = opened
        try:
            return _present_fact(path, info, descriptor=descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    finally:
        os.close(parent_fd)


def _read_observed_regular(
    path: Path,
    expected: dict[str, object],
    *,
    expected_uid: int,
) -> bytes:
    path = _canonical_path(str(path))
    parent_fd = _open_directory_chain(path.parent, expected_uid=expected_uid)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            observed = _present_fact(path, opened, descriptor=descriptor)
            if observed != expected or observed.get("type") != "regular":
                raise _fail()
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                block = os.read(descriptor, min(remaining, 1024 * 1024))
                if not block:
                    raise _fail()
                chunks.append(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise _fail()
            raw = b"".join(chunks)
            if _digest_bytes(raw) != expected.get("sha256"):
                raise _fail()
            return raw
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise _fail()
        view = view[written:]
    os.fsync(descriptor)


def _atomic_at(parent_fd: int, name: str, raw: bytes, *, mode: int) -> None:
    if "/" in name or name in {"", ".", ".."}:
        raise _fail()
    pending = f".{name}.pending"
    try:
        descriptor = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
    except FileExistsError:
        descriptor = os.open(pending, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != len(raw):
                raise _fail()
            pending_raw = os.read(descriptor, len(raw))
            if pending_raw != raw or os.read(descriptor, 1):
                raise _fail()
        finally:
            os.close(descriptor)
    else:
        try:
            _write_all(descriptor, raw)
        finally:
            os.close(descriptor)
    try:
        existing_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.replace(pending, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        return
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(existing_info.st_mode)
            or (current.st_dev, current.st_ino) != (existing_info.st_dev, existing_info.st_ino)
            or current.st_size != len(raw)
            or os.read(descriptor, len(raw)) != raw
            or os.read(descriptor, 1)
        ):
            raise _fail()
    finally:
        os.close(descriptor)
    os.unlink(pending, dir_fd=parent_fd)


def _read_at(parent_fd: int, name: str, *, maximum: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise _fail()
        raw = os.read(descriptor, info.st_size)
        if len(raw) != info.st_size or os.read(descriptor, 1):
            raise _fail()
        return raw
    finally:
        os.close(descriptor)


def _backup_directory(transaction_root_fd: int, transaction_id: str) -> int:
    if TRANSACTION_RE.fullmatch(transaction_id) is None:
        raise _fail()
    transactions = os.open(
        "transactions",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=transaction_root_fd,
    )
    try:
        transaction = os.open(
            transaction_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=transactions,
        )
    finally:
        os.close(transactions)
    try:
        return os.open(
            "backups",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=transaction,
        )
    finally:
        os.close(transaction)


def _backup_names(material: StrictCRIActionMaterial) -> tuple[str, str]:
    if ACTION_RE.fullmatch(material.action_id) is None:
        raise _fail()
    stem = f"{material.index:03d}-{material.action_id}"
    return f"{stem}.json", f"{stem}.bin"


class QualificationHostActionBoundary(HostActionBoundary):
    """Closed Q1 filesystem and ACL boundary using exact synthetic-testable tools."""

    def __init__(
        self,
        *,
        access_uid: int,
        access_gid: int,
        setfacl: ExecutableIdentity,
        getfacl: ExecutableIdentity,
        runner: ToolRunner | None = None,
        effective_uid: int = 0,
        effective_gid: int = 0,
    ) -> None:
        if any(type(value) is not int or value < 0 for value in (access_uid, access_gid)):
            raise _fail()
        if os.geteuid() != effective_uid or os.getegid() != effective_gid:
            raise _fail()
        self.access_uid = access_uid
        self.access_gid = access_gid
        self.setfacl = setfacl
        self.getfacl = getfacl
        self.runner = runner or SubprocessToolRunner()
        self.effective_uid = effective_uid
        self.effective_gid = effective_gid

    def _acl(self, target: Path) -> tuple[str, ...]:
        result = self.runner.run([str(self.getfacl.path), "-c", "-p", "-n", "--", str(target)])
        if result.returncode != 0 or result.stderr:
            raise _fail()
        try:
            lines = tuple(
                line for line in result.stdout.decode("utf-8", errors="strict").splitlines() if line
            )
        except UnicodeDecodeError as exc:
            raise _fail() from exc
        if not lines or any(ACL_RE.fullmatch(line) is None for line in lines):
            raise _fail()
        return lines

    def _set_acl(self, target: Path, acl: tuple[str, ...]) -> None:
        if not acl or any(ACL_RE.fullmatch(line) is None for line in acl):
            raise _fail()
        result = self.runner.run(
            [str(self.setfacl.path), f"--set={','.join(acl)}", "--", str(target)]
        )
        if result.returncode != 0 or result.stdout or result.stderr:
            raise _fail()

    def _grant_acl(self, target: Path) -> None:
        result = self.runner.run(
            [
                str(self.setfacl.path),
                "-m",
                f"u:{self.access_uid}:rw",
                "--",
                str(target),
            ]
        )
        if result.returncode != 0 or result.stdout or result.stderr:
            raise _fail()
        expected = f"user:{self.access_uid}:rw-"
        if expected not in self._acl(target):
            raise _fail()

    def _load_backup(
        self,
        backup_fd: int,
        material: StrictCRIActionMaterial,
    ) -> tuple[dict[str, object], bytes | None]:
        meta_name, data_name = _backup_names(material)
        raw = _read_at(backup_fd, meta_name, maximum=MAX_BACKUP_FILE_BYTES)
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail() from exc
        if type(value) is not dict or canonical_json_bytes(value) != raw:
            raise _fail()
        required = {"actionId", "before", "beforeSha256", "bytesSha256", "privateAcl"}
        if (
            set(value) != required
            or value["actionId"] != material.action_id
            or value["before"] != material.expected_before
            or value["beforeSha256"] != material.expected_before_digest
        ):
            raise _fail()
        data: bytes | None = None
        if value["bytesSha256"] is not None:
            digest = _digest(value["bytesSha256"])
            data = _read_at(backup_fd, data_name, maximum=MAX_BACKUP_FILE_BYTES)
            if _digest_bytes(data) != digest:
                raise _fail()
        acl = value["privateAcl"]
        if acl is not None and (
            type(acl) is not list
            or any(type(line) is not str or ACL_RE.fullmatch(line) is None for line in acl)
        ):
            raise _fail()
        return value, data

    def _snapshot(
        self,
        backup_fd: int,
        material: StrictCRIActionMaterial,
    ) -> str:
        observed = observe_exact(Path(material.target), expected_uid=self.effective_uid)
        if (
            observed != material.expected_before
            or _digest_value(observed) != material.expected_before_digest
        ):
            raise _fail()
        raw: bytes | None = None
        if observed["state"] == "present" and observed["type"] == "regular":
            raw = _read_observed_regular(
                Path(material.target),
                observed,
                expected_uid=self.effective_uid,
            )
        private_acl: tuple[str, ...] | None = None
        if material.action_id == "cri-socket-access":
            private_acl = self._acl(Path(material.target))
        meta = {
            "actionId": material.action_id,
            "before": observed,
            "beforeSha256": material.expected_before_digest,
            "bytesSha256": None if raw is None else _digest_bytes(raw),
            "privateAcl": None if private_acl is None else list(private_acl),
        }
        meta_name, data_name = _backup_names(material)
        if raw is not None:
            _atomic_at(backup_fd, data_name, raw, mode=0o600)
        _atomic_at(backup_fd, meta_name, canonical_json_bytes(meta), mode=0o600)
        self._load_backup(backup_fd, material)
        return material.expected_before_digest

    def _desired_file_projection(
        self,
        material: StrictCRIActionMaterial,
        *,
        content_digest: str,
        mode: str,
    ) -> dict[str, object]:
        return {
            "gid": self.effective_gid,
            "mode": mode,
            "path": material.target,
            "sha256": content_digest,
            "type": "regular",
            "uid": self.effective_uid,
        }

    def _replace_regular(self, material: StrictCRIActionMaterial, raw: bytes, *, mode: int) -> None:
        target = Path(material.target)
        parent_fd = _open_directory_chain(target.parent, expected_uid=self.effective_uid)
        temporary = f".{target.name}.k1s-q1-{material.action_id}"
        try:
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError as exc:
                raise _fail() from exc
            try:
                _write_all(descriptor, raw)
                os.fchmod(descriptor, mode)
                os.fchown(descriptor, self.effective_uid, self.effective_gid)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent_fd)
            os.close(parent_fd)

    def _source_bytes(self, material: StrictCRIActionMaterial) -> bytes:
        source = material.source
        if type(source) is not dict or source.get("state") != "present":
            raise _fail()
        path = _canonical_path(source.get("path"))
        observed = observe_exact(path, expected_uid=self.effective_uid)
        if observed != source or observed.get("type") != "regular":
            raise _fail()
        return _read_observed_regular(path, observed, expected_uid=self.effective_uid)

    def _matches_desired(
        self,
        material: StrictCRIActionMaterial,
        observed: dict[str, object],
    ) -> bool:
        if material.kind in {"copy-regular", "write-canonical"}:
            if observed.get("state") != "present" or observed.get("type") != "regular":
                return False
            projection = {
                "gid": observed.get("gid"),
                "mode": observed.get("mode"),
                "path": material.target,
                "sha256": observed.get("sha256"),
                "type": "regular",
                "uid": observed.get("uid"),
            }
            return _digest_value(projection) == material.desired_after_digest
        if material.kind == "grant-exact-acl":
            expected = _digest_value(
                {
                    "gid": self.access_gid,
                    "path": material.target,
                    "permissions": "rw",
                    "uid": self.access_uid,
                }
            )
            return (
                observed == material.expected_before
                and _digest_value(observed) == material.expected_before_digest
                and expected == material.desired_after_digest
                and f"user:{self.access_uid}:rw-" in self._acl(Path(material.target))
            )
        return False

    @staticmethod
    def _matches_exact_before(
        material: StrictCRIActionMaterial,
        observed: dict[str, object],
    ) -> bool:
        return (
            observed == material.expected_before
            and _digest_value(observed) == material.expected_before_digest
        )

    def _apply(self, material: StrictCRIActionMaterial) -> str:
        observed = observe_exact(Path(material.target), expected_uid=self.effective_uid)
        if material.kind in {"validate", "verify"}:
            if _digest_value(observed) != material.expected_before_digest:
                raise _fail()
            return material.desired_after_digest
        if self._matches_desired(material, observed):
            return material.desired_after_digest
        if observed != material.expected_before:
            raise _fail()
        if material.kind == "copy-regular":
            raw = self._source_bytes(material)
            mode = 0o755 if material.action_id.startswith("cni-plugin-") else 0o644
            self._replace_regular(material, raw, mode=mode)
            projection = self._desired_file_projection(
                material,
                content_digest=_digest_bytes(raw),
                mode=f"0{mode:03o}",
            )
            if _digest_value(projection) != material.desired_after_digest:
                raise _fail()
        elif material.kind == "write-canonical":
            if material.content is None:
                raise _fail()
            self._replace_regular(material, material.content, mode=0o644)
            projection = self._desired_file_projection(
                material,
                content_digest=_digest_bytes(material.content),
                mode="0644",
            )
            if _digest_value(projection) != material.desired_after_digest:
                raise _fail()
        elif material.kind == "grant-exact-acl":
            expected = _digest_value(
                {
                    "gid": self.access_gid,
                    "path": material.target,
                    "permissions": "rw",
                    "uid": self.access_uid,
                }
            )
            if expected != material.desired_after_digest:
                raise _fail()
            self._grant_acl(Path(material.target))
        else:
            raise _fail()
        final = observe_exact(Path(material.target), expected_uid=self.effective_uid)
        if not self._matches_desired(material, final):
            raise _fail()
        return material.desired_after_digest

    def _rollback(
        self,
        backup_fd: int,
        material: StrictCRIActionMaterial,
        authorization: str | None,
    ) -> str:
        if authorization is None or DIGEST_RE.fullmatch(authorization) is None:
            raise _fail()
        meta, raw = self._load_backup(backup_fd, material)
        target = Path(material.target)
        if material.rollback == "none":
            raise _fail()
        if material.rollback == "remove-created":
            observed = observe_exact(target, expected_uid=self.effective_uid)
            if self._matches_exact_before(material, observed):
                return material.expected_before_digest
            if not self._matches_desired(material, observed):
                raise _fail()
            parent_fd = _open_directory_chain(target.parent, expected_uid=self.effective_uid)
            try:
                info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_dev != observed.get("device")
                    or info.st_ino != observed.get("inode")
                    or info.st_uid != observed.get("uid")
                    or info.st_gid != observed.get("gid")
                    or f"0{stat.S_IMODE(info.st_mode):03o}" != observed.get("mode")
                    or info.st_nlink != observed.get("nlink")
                    or info.st_size != observed.get("size")
                ):
                    raise _fail()
                os.unlink(target.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        elif material.rollback == "restore-exact-before":
            before = material.expected_before
            if material.action_id == "cri-socket-access":
                acl = meta["privateAcl"]
                if type(acl) is not list:
                    raise _fail()
                private_acl = tuple(str(line) for line in acl)
                observed = observe_exact(target, expected_uid=self.effective_uid)
                if not self._matches_exact_before(material, observed):
                    raise _fail()
                if self._acl(target) == private_acl:
                    return material.expected_before_digest
                if not self._matches_desired(material, observed):
                    raise _fail()
                self._set_acl(target, private_acl)
            elif before.get("state") == "present" and before.get("type") == "regular":
                observed = observe_exact(target, expected_uid=self.effective_uid)
                if self._matches_exact_before(material, observed):
                    return material.expected_before_digest
                if not self._matches_desired(material, observed):
                    raise _fail()
                if raw is None:
                    raise _fail()
                mode = int(str(before["mode"]), 8)
                self._replace_regular(material, raw, mode=mode)
            else:
                raise _fail()
        else:
            raise _fail()
        observed = observe_exact(target, expected_uid=self.effective_uid)
        if (
            observed != material.expected_before
            or _digest_value(observed) != material.expected_before_digest
        ):
            raise _fail()
        if material.action_id == "cri-socket-access":
            acl = meta["privateAcl"]
            if type(acl) is not list or self._acl(target) != tuple(str(line) for line in acl):
                raise _fail()
        return material.expected_before_digest

    def execute(
        self,
        *,
        operation: HelperOperation,
        transaction_id: str,
        transaction_root_fd: int,
        material: StrictCRIActionMaterial,
        rollback_authorization_digest: str | None,
    ) -> str:
        backup_fd = _backup_directory(transaction_root_fd, transaction_id)
        try:
            if operation == "snapshot":
                return self._snapshot(backup_fd, material)
            self._load_backup(backup_fd, material)
            if operation in {"apply", "verify"}:
                return self._apply(material)
            if operation == "rollback":
                return self._rollback(
                    backup_fd,
                    material,
                    rollback_authorization_digest,
                )
            raise _fail()
        finally:
            os.close(backup_fd)


def _load_gate_b(
    path: Path,
    expected_digest: str,
    authority: q1_bundle.BundleAuthority,
    *,
    now_unix: int,
) -> dict[str, object]:
    expected_digest = _digest(expected_digest)
    path = _canonical_path(str(path))
    parent_fd = _open_directory_chain(path.parent, expected_uid=os.getuid())
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or not 1 <= info.st_size <= MAX_TOOL_OUTPUT
            ):
                raise _fail()
            raw = os.read(descriptor, info.st_size)
            if len(raw) != info.st_size or os.read(descriptor, 1):
                raise _fail()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
    if _digest_bytes(raw) != expected_digest:
        raise _fail()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or q1_bundle.canonical_json_bytes(value) != raw:
        raise _fail()
    try:
        return q1_bundle.validate_gate_b_attestation(
            value,
            authority,
            now_unix=now_unix,
        )
    except q1_bundle.Q1BundleError as exc:
        raise _fail() from exc


def _validate_gate_b_request(
    request: HelperRequest,
    approval: dict[str, object],
) -> None:
    if (
        request.transaction_id != approval["transactionId"]
        or request.target_id != approval["targetId"]
        or request.authorities.plan_digest != approval["planDigest"]
        or (
            request.operation == "rollback"
            and request.rollback_authorization_digest != approval["rollbackReceiptSha256"]
        )
        or (request.operation != "rollback" and request.rollback_authorization_digest is not None)
    ):
        raise _fail()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-fd", type=int, required=True)
    parser.add_argument("--response-fd", type=int, required=True)
    parser.add_argument("--gate-b-attestation", type=Path, required=True)
    parser.add_argument("--expect-gate-b-sha256", required=True)
    parser.add_argument("--input-bundle", type=Path, required=True)
    parser.add_argument("--expect-input-bundle-sha256", required=True)
    parser.add_argument("--expect-bundle-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--acceptance-receipt", type=Path, required=True)
    parser.add_argument("--setfacl", type=Path, required=True)
    parser.add_argument("--getfacl", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    ns = parser.parse_args(argv)
    try:
        validate_clean_environment()
        previous_umask = os.umask(0o077)
        os.umask(previous_umask)
        request_info = os.fstat(ns.request_fd)
        response_info = os.fstat(ns.response_fd)
        q1_bundle.validate_endpoint_context(
            euid=os.geteuid(),
            egid=os.getegid(),
            cwd=os.getcwd(),
            umask=previous_umask,
            request_fd_mode=request_info.st_mode,
            response_fd_mode=response_info.st_mode,
            request_fd_device=request_info.st_dev,
            request_fd_inode=request_info.st_ino,
            response_fd_device=response_info.st_dev,
            response_fd_inode=response_info.st_ino,
            request_fd=ns.request_fd,
            response_fd=ns.response_fd,
            environment=dict(os.environ),
        )
        authority = q1_bundle.validate_bundle_file(
            ns.input_bundle,
            expected_sha256=ns.expect_input_bundle_sha256,
            expected_run_id=ns.run_id,
            expected_plan_digest=ns.plan_digest,
            expected_bundle_id=ns.expect_bundle_id,
        )
        now_unix = int(time.time())
        approval = _load_gate_b(
            ns.gate_b_attestation,
            ns.expect_gate_b_sha256,
            authority,
            now_unix=now_unix,
        )
        endpoint = cast(dict[str, object], authority.manifest["gateB"])

        def endpoint_tool(name: str) -> tuple[Path, str]:
            value = cast(dict[str, object], endpoint[name])
            path = _canonical_path(value["path"])
            return path, _digest(value["sha256"])

        validator_path = getattr(q1_bundle, "__file__", None)
        if type(validator_path) is not str:
            raise _fail()
        expected_validator_path, expected_validator_digest = endpoint_tool("bundleValidator")
        if Path(validator_path) != expected_validator_path:
            raise _fail()
        validator = executable_identity(
            Path(validator_path),
            expected_validator_digest,
            require_executable=False,
        )
        expected_helper_path, expected_helper_digest = endpoint_tool("helper")
        if Path(__file__) != expected_helper_path:
            raise _fail()
        helper = executable_identity(
            Path(__file__),
            expected_helper_digest,
            require_executable=False,
        )
        expected_python_path, expected_python_digest = endpoint_tool("python")
        expected_setfacl_path, expected_setfacl_digest = endpoint_tool("setfacl")
        expected_getfacl_path, expected_getfacl_digest = endpoint_tool("getfacl")
        if (
            Path(sys.executable) != expected_python_path
            or ns.setfacl != expected_setfacl_path
            or ns.getfacl != expected_getfacl_path
        ):
            raise _fail()
        python = executable_identity(expected_python_path, expected_python_digest)
        setfacl = executable_identity(expected_setfacl_path, expected_setfacl_digest)
        getfacl = executable_identity(expected_getfacl_path, expected_getfacl_digest)
        del helper, python, validator
        acceptance_receipt = q1_bundle.validate_acceptance_receipt_output_path(
            ns.acceptance_receipt,
            authority,
        )
        q1_bundle.create_acceptance_receipt(
            acceptance_receipt,
            authority,
            transaction_id=cast(str, approval["transactionId"]),
        )
        boundary = QualificationHostActionBoundary(
            access_uid=cast(int, approval["accessUid"]),
            access_gid=cast(int, approval["accessGid"]),
            setfacl=setfacl,
            getfacl=getfacl,
        )
        engine = StrictCRIHostHelperEngine(boundary)
        os.set_blocking(ns.request_fd, False)
        os.set_blocking(ns.response_fd, False)
        deadline = time.monotonic() + q1_bundle.GATE_B_TIMEOUT_SECONDS
        for _index in range(cast(int, approval["requestCount"])):
            request = HelperRequest.from_payload(
                _read_protocol_bounded(ns.request_fd, deadline=deadline)
            )
            _validate_gate_b_request(request, approval)
            response = engine.handle(request)
            _write_protocol_bounded(
                ns.response_fd,
                response.to_payload(),
                deadline=deadline,
            )
        return 0
    except (
        QualificationHelperError,
        ProfileHostHelperError,
        q1_bundle.Q1BundleError,
        OSError,
        TypeError,
        ValueError,
    ):
        print("K1s Q1 guest helper failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
