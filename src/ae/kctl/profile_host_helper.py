"""Closed strict-CRI host-helper protocol.

The helper never trusts coordinator-supplied paths or action metadata.  Every
request contains only immutable authority-file references plus an ordered
action index; the helper reloads the files, reconstructs the K-P1-7A plan, and
derives the selected action before crossing the injected filesystem boundary.

The implementation digest is a compatibility binding, not peer
authentication.  A later Q1 appliance must establish the helper process and
file-descriptor topology before making a live qualification claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from ae.kctl.profile_host_plan import (
    ProfileHostPlanError,
    StrictCRIActionMaterial,
    StrictCRIHostAuthorities,
    build_strict_cri_host_plan,
    canonical_json_bytes,
    strict_cri_action_materials,
)

PROTOCOL_VERSION = 1
HELPER_IMPLEMENTATION_VERSION = "k1s-strict-cri-helper-v1"
MAX_PROTOCOL_BYTES = 256 * 1024
MAX_BACKUP_FILE_BYTES = 64 * 1024 * 1024
MAX_BACKUP_TOTAL_BYTES = 256 * 1024 * 1024
HelperOperation = Literal["snapshot", "apply", "rollback", "verify"]

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TRANSACTION_RE = re.compile(r"ht1-[0-9a-f]{32}")
_IDENTIFIER_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_ACTION_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?")


class ProfileHostHelperError(RuntimeError):
    """One helper request is malformed, divergent, or unsafe."""


def _fail() -> ProfileHostHelperError:
    return ProfileHostHelperError("strict CRI host helper failed closed")


def helper_implementation_digest() -> str:
    """Return a stable protocol/implementation compatibility identity."""

    payload = {
        "apiVersion": "k1s.dev/v1alpha1",
        "implementation": HELPER_IMPLEMENTATION_VERSION,
        "kind": "StrictCRIHostHelperIdentity",
        "protocolVersion": PROTOCOL_VERSION,
    }
    return f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _action_id(value: object) -> str:
    if type(value) is not str or _ACTION_ID_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _transaction_id(value: object) -> str:
    if type(value) is not str or _TRANSACTION_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _private_absolute_path(value: object) -> Path:
    if type(value) is not str or not value.startswith("/") or value == "/":
        raise _fail()
    if value.startswith("//") or value.endswith("/") or "\\" in value or "\x00" in value:
        raise _fail()
    if any(part in {"", ".", ".."} for part in value.split("/")[1:]):
        raise _fail()
    if str(PurePosixPath(value)) != value:
        raise _fail()
    return Path(value)


@dataclass(frozen=True, slots=True)
class HelperAuthorityFiles:
    intent_path: Path = field(repr=False)
    facts_path: Path = field(repr=False)
    plan_path: Path = field(repr=False)
    intent_digest: str
    facts_digest: str
    plan_file_digest: str
    plan_digest: str

    def __post_init__(self) -> None:
        for path in (self.intent_path, self.facts_path, self.plan_path):
            _private_absolute_path(str(path))
        _digest(self.intent_digest)
        _digest(self.facts_digest)
        _digest(self.plan_file_digest)
        _digest(self.plan_digest)


@dataclass(frozen=True, slots=True)
class HelperRequest:
    operation: HelperOperation
    transaction_id: str
    target_id: str
    action_index: int
    action_id: str = field(repr=False)
    expected_before_digest: str = field(repr=False)
    desired_after_digest: str = field(repr=False)
    authorities: HelperAuthorityFiles = field(repr=False)
    transaction_root: Path = field(repr=False)
    helper_identity_digest: str
    rollback_authorization_digest: str | None = field(default=None, repr=False)
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.operation not in {"snapshot", "apply", "rollback", "verify"}:
            raise _fail()
        _transaction_id(self.transaction_id)
        _identifier(self.target_id)
        _action_id(self.action_id)
        _digest(self.expected_before_digest)
        _digest(self.desired_after_digest)
        if type(self.action_index) is not int or self.action_index < 0:
            raise _fail()
        _private_absolute_path(str(self.transaction_root))
        _digest(self.helper_identity_digest)
        if self.helper_identity_digest != helper_implementation_digest():
            raise _fail()
        if self.protocol_version != PROTOCOL_VERSION:
            raise _fail()
        if self.operation == "rollback":
            if self.rollback_authorization_digest is None:
                raise _fail()
            _digest(self.rollback_authorization_digest)
        elif self.rollback_authorization_digest is not None:
            raise _fail()

    def to_payload(self) -> dict[str, object]:
        return {
            "actionId": self.action_id,
            "actionIndex": self.action_index,
            "apiVersion": "k1s.dev/v1alpha1",
            "authorities": {
                "expectedFactsSha256": self.authorities.facts_digest,
                "expectedIntentSha256": self.authorities.intent_digest,
                "expectedPlanDigest": self.authorities.plan_digest,
                "expectedPlanSha256": self.authorities.plan_file_digest,
                "factsPath": str(self.authorities.facts_path),
                "intentPath": str(self.authorities.intent_path),
                "planPath": str(self.authorities.plan_path),
            },
            "helperIdentitySha256": self.helper_identity_digest,
            "kind": "StrictCRIHostHelperRequest",
            "operation": self.operation,
            "expectedBeforeSha256": self.expected_before_digest,
            "expectedPostSha256": self.desired_after_digest,
            "protocolVersion": self.protocol_version,
            "rollbackAuthorizationSha256": self.rollback_authorization_digest,
            "targetId": self.target_id,
            "transactionId": self.transaction_id,
            "transactionRoot": str(self.transaction_root),
        }

    @classmethod
    def from_payload(cls, value: object) -> HelperRequest:
        if type(value) is not dict or set(value) != {
            "actionId",
            "actionIndex",
            "apiVersion",
            "authorities",
            "helperIdentitySha256",
            "kind",
            "operation",
            "expectedBeforeSha256",
            "expectedPostSha256",
            "protocolVersion",
            "rollbackAuthorizationSha256",
            "targetId",
            "transactionId",
            "transactionRoot",
        }:
            raise _fail()
        if (
            value["apiVersion"] != "k1s.dev/v1alpha1"
            or value["kind"] != "StrictCRIHostHelperRequest"
        ):
            raise _fail()
        authority = value["authorities"]
        if type(authority) is not dict or set(authority) != {
            "expectedFactsSha256",
            "expectedIntentSha256",
            "expectedPlanDigest",
            "expectedPlanSha256",
            "factsPath",
            "intentPath",
            "planPath",
        }:
            raise _fail()
        operation = value["operation"]
        if operation not in {"snapshot", "apply", "rollback", "verify"}:
            raise _fail()
        return cls(
            operation=operation,
            transaction_id=_transaction_id(value["transactionId"]),
            target_id=_identifier(value["targetId"]),
            action_index=value["actionIndex"],
            action_id=_action_id(value["actionId"]),
            expected_before_digest=_digest(value["expectedBeforeSha256"]),
            desired_after_digest=_digest(value["expectedPostSha256"]),
            authorities=HelperAuthorityFiles(
                intent_path=_private_absolute_path(authority["intentPath"]),
                facts_path=_private_absolute_path(authority["factsPath"]),
                plan_path=_private_absolute_path(authority["planPath"]),
                intent_digest=_digest(authority["expectedIntentSha256"]),
                facts_digest=_digest(authority["expectedFactsSha256"]),
                plan_file_digest=_digest(authority["expectedPlanSha256"]),
                plan_digest=_digest(authority["expectedPlanDigest"]),
            ),
            transaction_root=_private_absolute_path(value["transactionRoot"]),
            helper_identity_digest=_digest(value["helperIdentitySha256"]),
            rollback_authorization_digest=(
                None
                if value["rollbackAuthorizationSha256"] is None
                else _digest(value["rollbackAuthorizationSha256"])
            ),
            protocol_version=value["protocolVersion"],
        )


@dataclass(frozen=True, slots=True)
class HelperResponse:
    operation: HelperOperation
    transaction_id: str
    target_id: str
    action_index: int
    action_id: str
    expected_before_digest: str
    desired_after_digest: str
    observation_digest: str
    helper_identity_digest: str = field(default_factory=helper_implementation_digest)
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.operation not in {"snapshot", "apply", "rollback", "verify"}:
            raise _fail()
        _transaction_id(self.transaction_id)
        _identifier(self.target_id)
        _action_id(self.action_id)
        if type(self.action_index) is not int or self.action_index < 0:
            raise _fail()
        for value in (
            self.expected_before_digest,
            self.desired_after_digest,
            self.observation_digest,
            self.helper_identity_digest,
        ):
            _digest(value)
        if self.helper_identity_digest != helper_implementation_digest():
            raise _fail()
        if self.protocol_version != PROTOCOL_VERSION:
            raise _fail()

    def to_payload(self) -> dict[str, object]:
        return {
            "actionId": self.action_id,
            "actionIndex": self.action_index,
            "apiVersion": "k1s.dev/v1alpha1",
            "desiredAfterDigest": self.desired_after_digest,
            "expectedBeforeDigest": self.expected_before_digest,
            "helperIdentitySha256": self.helper_identity_digest,
            "kind": "StrictCRIHostHelperResponse",
            "observationDigest": self.observation_digest,
            "operation": self.operation,
            "protocolVersion": self.protocol_version,
            "targetId": self.target_id,
            "transactionId": self.transaction_id,
        }

    @classmethod
    def from_payload(cls, value: object) -> HelperResponse:
        if type(value) is not dict or set(value) != {
            "actionId",
            "actionIndex",
            "apiVersion",
            "desiredAfterDigest",
            "expectedBeforeDigest",
            "helperIdentitySha256",
            "kind",
            "observationDigest",
            "operation",
            "protocolVersion",
            "targetId",
            "transactionId",
        }:
            raise _fail()
        if (
            value["apiVersion"] != "k1s.dev/v1alpha1"
            or value["kind"] != "StrictCRIHostHelperResponse"
        ):
            raise _fail()
        operation = value["operation"]
        if operation not in {"snapshot", "apply", "rollback", "verify"}:
            raise _fail()
        return cls(
            operation=operation,
            transaction_id=_transaction_id(value["transactionId"]),
            target_id=_identifier(value["targetId"]),
            action_index=value["actionIndex"],
            action_id=_action_id(value["actionId"]),
            expected_before_digest=_digest(value["expectedBeforeDigest"]),
            desired_after_digest=_digest(value["desiredAfterDigest"]),
            observation_digest=_digest(value["observationDigest"]),
            helper_identity_digest=_digest(value["helperIdentitySha256"]),
            protocol_version=value["protocolVersion"],
        )


class HostActionBoundary(Protocol):
    """Q1-owned filesystem/privilege boundary for one derived action."""

    def execute(
        self,
        *,
        operation: HelperOperation,
        transaction_id: str,
        transaction_root_fd: int,
        material: StrictCRIActionMaterial,
        rollback_authorization_digest: str | None,
    ) -> str:
        """Return the canonical post-observation digest or fail closed."""


class StrictCRIHostHelperEngine:
    """Authorize one request from independently reloaded authorities."""

    def __init__(self, boundary: HostActionBoundary) -> None:
        self.boundary = boundary

    def handle(self, request: HelperRequest) -> HelperResponse:
        root_fd = -1
        try:
            authorities, root_fd = _load_bound_transaction_authorities(request)
            if authorities.plan["target"]["targetId"] != request.target_id:
                raise _fail()
            materials = strict_cri_action_materials(authorities)
            if request.action_index >= len(materials):
                raise _fail()
            material = materials[request.action_index]
            if (
                request.action_id != material.action_id
                or request.expected_before_digest != material.expected_before_digest
                or request.desired_after_digest != material.desired_after_digest
            ):
                raise _fail()
            observation = self.boundary.execute(
                operation=request.operation,
                transaction_id=request.transaction_id,
                transaction_root_fd=root_fd,
                material=material,
                rollback_authorization_digest=request.rollback_authorization_digest,
            )
            expected_observation = {
                "snapshot": material.expected_before_digest,
                "apply": material.desired_after_digest,
                "rollback": material.expected_before_digest,
                "verify": material.desired_after_digest,
            }[request.operation]
            if observation != expected_observation:
                raise _fail()
            return HelperResponse(
                operation=request.operation,
                transaction_id=request.transaction_id,
                target_id=request.target_id,
                action_index=request.action_index,
                action_id=material.action_id,
                expected_before_digest=material.expected_before_digest,
                desired_after_digest=material.desired_after_digest,
                observation_digest=_digest(observation),
            )
        except ProfileHostHelperError:
            raise
        except (OSError, ProfileHostPlanError, ValueError) as exc:
            raise _fail() from exc
        finally:
            if root_fd >= 0:
                os.close(root_fd)


def _load_bound_transaction_authorities(
    request: HelperRequest,
) -> tuple[StrictCRIHostAuthorities, int]:
    """Load every private authority below one held transaction-root fd.

    The descriptor chain prevents a renamed parent or symlink substitution
    from redirecting an authority load.  This remains a filesystem/protocol
    binding, not process or peer authentication.
    """

    root = request.transaction_root
    transaction = root / "transactions" / request.transaction_id
    authority_dir = transaction / "authorities"
    expected_paths = (
        authority_dir / "intent.json",
        authority_dir / "facts.json",
        authority_dir / "plan.json",
    )
    if (
        request.authorities.intent_path,
        request.authorities.facts_path,
        request.authorities.plan_path,
    ) != expected_paths:
        raise _fail()

    owner_uid = os.getuid()
    root_fd = _open_private_root(root, owner_uid=owner_uid)
    descendants: list[int] = []
    try:
        root_info = os.fstat(root_fd)
        transactions_fd = _open_private_child_directory(
            root_fd,
            "transactions",
            owner_uid=owner_uid,
            expected_device=root_info.st_dev,
        )
        descendants.append(transactions_fd)
        transaction_fd = _open_private_child_directory(
            transactions_fd,
            request.transaction_id,
            owner_uid=owner_uid,
            expected_device=root_info.st_dev,
        )
        descendants.append(transaction_fd)
        authorities_fd = _open_private_child_directory(
            transaction_fd,
            "authorities",
            owner_uid=owner_uid,
            expected_device=root_info.st_dev,
        )
        descendants.append(authorities_fd)
        backups_fd = _open_private_child_directory(
            transaction_fd,
            "backups",
            owner_uid=owner_uid,
            expected_device=root_info.st_dev,
        )
        descendants.append(backups_fd)
        receipts_fd = _open_private_child_directory(
            root_fd,
            "receipts",
            owner_uid=owner_uid,
            expected_device=root_info.st_dev,
        )
        descendants.append(receipts_fd)

        intent, intent_raw = _read_private_canonical_json_at(
            authorities_fd,
            "intent.json",
            owner_uid=owner_uid,
            expected_device=root_info.st_dev,
        )
        facts, facts_raw = _read_private_canonical_json_at(
            authorities_fd,
            "facts.json",
            owner_uid=owner_uid,
            expected_device=root_info.st_dev,
        )
        plan, plan_raw = _read_private_canonical_json_at(
            authorities_fd,
            "plan.json",
            owner_uid=owner_uid,
            expected_device=root_info.st_dev,
        )
        if (
            _raw_digest(intent_raw) != request.authorities.intent_digest
            or _raw_digest(facts_raw) != request.authorities.facts_digest
            or _raw_digest(plan_raw) != request.authorities.plan_file_digest
        ):
            raise _fail()
        rebuilt = build_strict_cri_host_plan(
            intent,
            facts,
            intent_digest=request.authorities.intent_digest,
            facts_digest=request.authorities.facts_digest,
        )
        if plan != rebuilt or canonical_json_bytes(plan) != canonical_json_bytes(rebuilt):
            raise _fail()
        if plan.get("planDigest") != request.authorities.plan_digest:
            raise _fail()
        target = plan.get("target")
        if type(target) is not dict or target.get("ownerUid") != owner_uid:
            raise _fail()
        authorities = StrictCRIHostAuthorities(
            intent=intent,
            facts=facts,
            plan=plan,
            intent_digest=request.authorities.intent_digest,
            facts_digest=request.authorities.facts_digest,
            plan_file_digest=request.authorities.plan_file_digest,
            plan_digest=request.authorities.plan_digest,
        )
        if request.operation == "rollback":
            receipt, _raw = _read_private_canonical_json_at(
                receipts_fd,
                f"{request.transaction_id}.applied.json",
                owner_uid=owner_uid,
                expected_device=root_info.st_dev,
            )
            _validate_rollback_receipt(request, receipt)
        return authorities, root_fd
    except Exception:
        os.close(root_fd)
        raise
    finally:
        for descriptor in reversed(descendants):
            os.close(descriptor)


def _open_private_root(path: Path, *, owner_uid: int) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        info = os.fstat(current)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise _fail()
        return current
    except Exception:
        os.close(current)
        raise


def _open_private_child_directory(
    parent_fd: int,
    name: str,
    *,
    owner_uid: int,
    expected_device: int,
) -> int:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_dev != expected_device
        ):
            raise _fail()
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_private_canonical_json_at(
    parent_fd: int,
    name: str,
    *,
    owner_uid: int,
    expected_device: int,
) -> tuple[dict[str, object], bytes]:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected_device
            or opened.st_uid != owner_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= MAX_PROTOCOL_BYTES
        ):
            raise _fail()
        raw = _read_exact(descriptor, opened.st_size)
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
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return value, raw


def _raw_digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _validate_rollback_receipt(request: HelperRequest, receipt: dict[str, object]) -> None:
    receipt_digest = receipt.get("receiptDigest")
    unsigned = dict(receipt)
    unsigned.pop("receiptDigest", None)
    if (
        receipt.get("apiVersion") != "k1s.dev/v1alpha1"
        or receipt.get("kind") != "StrictCRIHostPreparationReceipt"
        or receipt.get("schemaVersion") != 1
        or receipt.get("status") != "applied"
        or receipt.get("transactionId") != request.transaction_id
        or receipt.get("targetId") != request.target_id
        or receipt.get("planDigest") != request.authorities.plan_digest
        or receipt.get("planFileDigest") != request.authorities.plan_file_digest
        or receipt.get("intentDigest") != request.authorities.intent_digest
        or receipt.get("factsDigest") != request.authorities.facts_digest
        or receipt.get("helperIdentitySha256") != request.helper_identity_digest
        or receipt_digest != request.rollback_authorization_digest
        or receipt_digest != _raw_digest(canonical_json_bytes(unsigned))
    ):
        raise _fail()


def write_protocol_message(fd: int, payload: object) -> None:
    raw = canonical_json_bytes(payload)
    if len(raw) > MAX_PROTOCOL_BYTES or type(fd) is not int or fd < 3:
        raise _fail()
    frame = f"{len(raw):08x}\n".encode("ascii") + raw
    view = memoryview(frame)
    try:
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise _fail()
            view = view[written:]
    except OSError as exc:
        raise _fail() from exc


def read_protocol_message(fd: int) -> dict[str, object]:
    if type(fd) is not int or fd < 3:
        raise _fail()
    try:
        header = _read_exact(fd, 9)
        if header[8:] != b"\n":
            raise _fail()
        size = int(header[:8], 16)
        if size <= 0 or size > MAX_PROTOCOL_BYTES:
            raise _fail()
        raw = _read_exact(fd, size)
    except (OSError, ValueError) as exc:
        raise _fail() from exc
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return value


def _read_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise _fail()
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


__all__ = [
    "HELPER_IMPLEMENTATION_VERSION",
    "HelperAuthorityFiles",
    "HelperOperation",
    "HelperRequest",
    "HelperResponse",
    "HostActionBoundary",
    "MAX_BACKUP_FILE_BYTES",
    "MAX_BACKUP_TOTAL_BYTES",
    "PROTOCOL_VERSION",
    "ProfileHostHelperError",
    "StrictCRIHostHelperEngine",
    "helper_implementation_digest",
    "read_protocol_message",
    "write_protocol_message",
]
