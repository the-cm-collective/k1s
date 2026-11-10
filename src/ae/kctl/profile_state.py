"""Durable, owner-scoped state for installed k1s profile runs."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

from ae.kctl.profile_paths import (
    ProfileLayout,
    ProfileRoots,
    _ensure_private_directory,
    validate_bootstrap_id,
    validate_identifier,
    validate_relative_path,
    validate_run_id,
)

API_VERSION = "k1s.dev/v1alpha1"
SCHEMA_VERSION = 4
PREVIOUS_SCHEMA_VERSION = 3
BACKEND_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
TRANSACTION_SCHEMA_VERSION = 1
DELETION_SCHEMA_VERSION = 1
MAX_STATE_BYTES = 1024 * 1024
MAX_LOG_SNAPSHOT_BYTES = 1024 * 1024

RunPhase = Literal[
    "preparing",
    "starting",
    "running",
    "stopping",
    "stopped",
    "failed",
    "orphaned",
]
ComponentStatus = Literal["prepared", "starting", "running", "stopping", "stopped", "failed"]

RUN_PHASES: tuple[RunPhase, ...] = (
    "preparing",
    "starting",
    "running",
    "stopping",
    "stopped",
    "failed",
    "orphaned",
)
_TRANSITIONS: Mapping[RunPhase, frozenset[RunPhase]] = {
    "preparing": frozenset({"starting", "stopping", "failed"}),
    "starting": frozenset({"running", "stopping", "failed", "orphaned"}),
    "running": frozenset({"stopping", "failed", "orphaned"}),
    "stopping": frozenset({"stopped", "failed", "orphaned"}),
    "failed": frozenset({"stopping"}),
    "orphaned": frozenset({"stopping"}),
    "stopped": frozenset(),
}
_UNSET = object()


class ProfileStateError(RuntimeError):
    """Profile state is corrupt, contradictory, or unsafe to mutate."""


class ProfileBusyError(ProfileStateError):
    """Another process owns the requested profile lock."""


class StateConflictError(ProfileStateError):
    """A generation, revision, run, or transition precondition failed."""


class ResourceCleaner(Protocol):
    """Allowlisted cleanup boundary for one exact owned resource receipt."""

    def cleanup_resource(self, receipt: ResourceReceipt) -> None: ...


def _canonical_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or value != value.strip():
        raise ProfileStateError("timestamp must be canonical RFC3339 UTC text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProfileStateError("timestamp must be canonical RFC3339 UTC text") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.resolution * 0:
        raise ProfileStateError("timestamp must use UTC")
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ProfileStateError("timestamp must be canonical RFC3339 UTC text")
    return value


def utc_now() -> str:
    """Return a canonical audit timestamp; ordering never depends on it."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def command_digest(argv: Sequence[str]) -> str:
    """Hash an argv without ambiguous joining or persisting its values."""

    if not argv:
        raise ProfileStateError("a component command must not be empty")
    digest = hashlib.sha256()
    for argument in argv:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ProfileStateError("component command arguments must be NUL-free text")
        encoded = argument.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def _validate_digest(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise ProfileStateError(f"{field} must be one SHA-256 identity")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ProfileStateError(f"{field} must be one SHA-256 identity") from exc
    return value


def _payload_digest(value: Mapping[str, object] | None) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _strict_int(value: object, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProfileStateError(f"{field} must be an integer >= {minimum}")
    return value


def _strict_text(value: object, *, field: str, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value) > maximum
    ):
        raise ProfileStateError(f"{field} must be canonical single-line text")
    return value


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Exact Linux process identity; a PID alone is never authoritative."""

    pid: int
    process_group_id: int
    session_id: int
    boot_id: str
    start_time_ticks: int
    executable_device: int
    executable_inode: int

    def __post_init__(self) -> None:
        _strict_int(self.pid, field="pid", minimum=1)
        _strict_int(self.process_group_id, field="processGroupId", minimum=1)
        _strict_int(self.session_id, field="sessionId", minimum=1)
        _strict_text(self.boot_id, field="bootId", maximum=128)
        _strict_int(self.start_time_ticks, field="startTimeTicks")
        _strict_int(self.executable_device, field="executableDevice")
        _strict_int(self.executable_inode, field="executableInode", minimum=1)

    def to_payload(self) -> dict[str, object]:
        return {
            "bootId": self.boot_id,
            "executableDevice": self.executable_device,
            "executableInode": self.executable_inode,
            "pid": self.pid,
            "processGroupId": self.process_group_id,
            "sessionId": self.session_id,
            "startTimeTicks": self.start_time_ticks,
        }

    @classmethod
    def from_payload(cls, value: object) -> ProcessIdentity:
        raw = _mapping(value, field="processIdentity")
        _exact_keys(
            raw,
            {
                "bootId",
                "executableDevice",
                "executableInode",
                "pid",
                "processGroupId",
                "sessionId",
                "startTimeTicks",
            },
            field="processIdentity",
        )
        return cls(
            pid=_strict_int(raw["pid"], field="pid", minimum=1),
            process_group_id=_strict_int(raw["processGroupId"], field="processGroupId", minimum=1),
            session_id=_strict_int(raw["sessionId"], field="sessionId", minimum=1),
            boot_id=_strict_text(raw["bootId"], field="bootId", maximum=128),
            start_time_ticks=_strict_int(raw["startTimeTicks"], field="startTimeTicks"),
            executable_device=_strict_int(raw["executableDevice"], field="executableDevice"),
            executable_inode=_strict_int(
                raw["executableInode"], field="executableInode", minimum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentState:
    component_id: str
    command_digest: str
    log_path: str
    status: ComponentStatus
    process_identity: ProcessIdentity | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.component_id, field="component ID")
        _validate_digest(self.command_digest, field="command digest")
        validate_relative_path(self.log_path, field="component log path")
        if self.status not in {
            "prepared",
            "starting",
            "running",
            "stopping",
            "stopped",
            "failed",
        }:
            raise ProfileStateError("component status is unsupported")
        active = self.status in {"starting", "running", "stopping"}
        if active and self.process_identity is None:
            raise ProfileStateError("an active component requires exact process identity")
        if not active and self.process_identity is not None:
            raise ProfileStateError("a non-active component must not retain process authority")

    def to_payload(self) -> dict[str, object]:
        return {
            "commandDigest": self.command_digest,
            "componentId": self.component_id,
            "logPath": self.log_path,
            "processIdentity": (
                self.process_identity.to_payload() if self.process_identity is not None else None
            ),
            "status": self.status,
        }

    @classmethod
    def from_payload(cls, value: object) -> ComponentState:
        raw = _mapping(value, field="component")
        _exact_keys(
            raw,
            {"commandDigest", "componentId", "logPath", "processIdentity", "status"},
            field="component",
        )
        identity = raw["processIdentity"]
        status = _strict_text(raw["status"], field="component.status", maximum=32)
        return cls(
            component_id=validate_identifier(
                _strict_text(raw["componentId"], field="component.componentId"),
                field="component ID",
            ),
            command_digest=_strict_text(
                raw["commandDigest"], field="component.commandDigest", maximum=71
            ),
            log_path=validate_relative_path(
                _strict_text(raw["logPath"], field="component.logPath"),
                field="component log path",
            ),
            status=cast(ComponentStatus, status),
            process_identity=(None if identity is None else ProcessIdentity.from_payload(identity)),
        )


@dataclass(frozen=True, slots=True)
class SecretReceipt:
    secret_id: str
    relative_path: str
    owner_profile: str
    owner_run_id: str
    device: int
    inode: int
    mode: int
    created_at: str

    def __post_init__(self) -> None:
        validate_identifier(self.secret_id, field="secret ID")
        validate_relative_path(self.relative_path, field="secret relative path")
        validate_identifier(self.owner_profile, field="secret owner profile")
        validate_run_id(self.owner_run_id)
        _strict_int(self.device, field="secret device")
        _strict_int(self.inode, field="secret inode", minimum=1)
        if self.mode != 0o600:
            raise ProfileStateError("secret receipt mode must be 0600")
        _canonical_timestamp(self.created_at)

    def to_payload(self) -> dict[str, object]:
        return {
            "createdAt": self.created_at,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "ownerProfile": self.owner_profile,
            "ownerRunId": self.owner_run_id,
            "relativePath": self.relative_path,
            "secretId": self.secret_id,
        }

    @classmethod
    def from_payload(cls, value: object) -> SecretReceipt:
        raw = _mapping(value, field="secret receipt")
        _exact_keys(
            raw,
            {
                "createdAt",
                "device",
                "inode",
                "mode",
                "ownerProfile",
                "ownerRunId",
                "relativePath",
                "secretId",
            },
            field="secret receipt",
        )
        return cls(
            secret_id=validate_identifier(
                _strict_text(raw["secretId"], field="secretId"), field="secret ID"
            ),
            relative_path=validate_relative_path(
                _strict_text(raw["relativePath"], field="secret relativePath"),
                field="secret relative path",
            ),
            owner_profile=validate_identifier(
                _strict_text(raw["ownerProfile"], field="secret ownerProfile"),
                field="secret owner profile",
            ),
            owner_run_id=validate_run_id(
                _strict_text(raw["ownerRunId"], field="secret ownerRunId")
            ),
            device=_strict_int(raw["device"], field="secret device"),
            inode=_strict_int(raw["inode"], field="secret inode", minimum=1),
            mode=_strict_int(raw["mode"], field="secret mode"),
            created_at=_canonical_timestamp(
                _strict_text(raw["createdAt"], field="secret createdAt")
            ),
        )


@dataclass(frozen=True, slots=True)
class ResourceReceipt:
    resource_type: str
    resource_id: str
    cleanup_handler_id: str
    ordinal: int
    owner_profile: str
    owner_run_id: str
    created_at: str

    def __post_init__(self) -> None:
        validate_identifier(self.resource_type, field="resource type")
        validate_identifier(self.resource_id, field="resource ID")
        validate_identifier(self.cleanup_handler_id, field="cleanup handler ID")
        _strict_int(self.ordinal, field="resource ordinal")
        validate_identifier(self.owner_profile, field="resource owner profile")
        validate_run_id(self.owner_run_id)
        _canonical_timestamp(self.created_at)

    def to_payload(self) -> dict[str, object]:
        return {
            "cleanupHandlerId": self.cleanup_handler_id,
            "createdAt": self.created_at,
            "ordinal": self.ordinal,
            "ownerProfile": self.owner_profile,
            "ownerRunId": self.owner_run_id,
            "resourceId": self.resource_id,
            "resourceType": self.resource_type,
        }

    @classmethod
    def from_payload(cls, value: object) -> ResourceReceipt:
        raw = _mapping(value, field="resource receipt")
        _exact_keys(
            raw,
            {
                "cleanupHandlerId",
                "createdAt",
                "ordinal",
                "ownerProfile",
                "ownerRunId",
                "resourceId",
                "resourceType",
            },
            field="resource receipt",
        )
        return cls(
            resource_type=validate_identifier(
                _strict_text(raw["resourceType"], field="resourceType"),
                field="resource type",
            ),
            resource_id=validate_identifier(
                _strict_text(raw["resourceId"], field="resourceId"), field="resource ID"
            ),
            cleanup_handler_id=validate_identifier(
                _strict_text(raw["cleanupHandlerId"], field="cleanupHandlerId"),
                field="cleanup handler ID",
            ),
            ordinal=_strict_int(raw["ordinal"], field="resource ordinal"),
            owner_profile=validate_identifier(
                _strict_text(raw["ownerProfile"], field="resource ownerProfile"),
                field="resource owner profile",
            ),
            owner_run_id=validate_run_id(
                _strict_text(raw["ownerRunId"], field="resource ownerRunId")
            ),
            created_at=_canonical_timestamp(
                _strict_text(raw["createdAt"], field="resource createdAt")
            ),
        )


@dataclass(frozen=True, slots=True)
class FailureRecord:
    code: str
    summary: str

    def __post_init__(self) -> None:
        validate_identifier(self.code, field="failure code")
        _strict_text(self.summary, field="failure summary", maximum=256)

    def to_payload(self) -> dict[str, str]:
        return {"code": self.code, "summary": self.summary}

    @classmethod
    def from_payload(cls, value: object) -> FailureRecord:
        raw = _mapping(value, field="failure")
        _exact_keys(raw, {"code", "summary"}, field="failure")
        return cls(
            code=validate_identifier(
                _strict_text(raw["code"], field="failure.code"), field="failure code"
            ),
            summary=_strict_text(raw["summary"], field="failure.summary", maximum=256),
        )


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    """Crash-recoverable intent for deleting one exact stopped run payload."""

    profile: str
    run_id: str
    generation: int
    owner_uid: int
    confirmation: str
    state_digest: str
    status: Literal["pending", "complete"]
    targets: tuple[str, ...]
    created_at: str
    completed_at: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.profile, field="deletion profile")
        validate_run_id(self.run_id)
        _strict_int(self.generation, field="deletion generation", minimum=1)
        _strict_int(self.owner_uid, field="deletion ownerUid")
        if self.confirmation != f"{self.profile}/{self.run_id}":
            raise ProfileStateError("deletion confirmation differs from the exact run")
        _validate_digest(self.state_digest, field="deletion state digest")
        if self.status not in {"pending", "complete"}:
            raise ProfileStateError("deletion status is unsupported")
        if self.targets != ("cache", "data", "logs"):
            raise ProfileStateError("deletion targets differ from the canonical set")
        _canonical_timestamp(self.created_at)
        if self.status == "pending" and self.completed_at is not None:
            raise ProfileStateError("pending deletion cannot have a completion time")
        if self.status == "complete":
            if self.completed_at is None:
                raise ProfileStateError("complete deletion requires a completion time")
            _canonical_timestamp(self.completed_at)

    def to_payload(self) -> dict[str, object]:
        return {
            "apiVersion": API_VERSION,
            "completedAt": self.completed_at,
            "confirmation": self.confirmation,
            "createdAt": self.created_at,
            "generation": self.generation,
            "kind": "ProfileRunDeletion",
            "ownerUid": self.owner_uid,
            "profile": self.profile,
            "runId": self.run_id,
            "schemaVersion": DELETION_SCHEMA_VERSION,
            "stateDigest": self.state_digest,
            "status": self.status,
            "targets": list(self.targets),
        }

    @classmethod
    def from_payload(cls, value: object) -> DeletionRecord:
        raw = _mapping(value, field="deletion record")
        _exact_keys(
            raw,
            {
                "apiVersion",
                "completedAt",
                "confirmation",
                "createdAt",
                "generation",
                "kind",
                "ownerUid",
                "profile",
                "runId",
                "schemaVersion",
                "stateDigest",
                "status",
                "targets",
            },
            field="deletion record",
        )
        if (
            raw["apiVersion"] != API_VERSION
            or raw["kind"] != "ProfileRunDeletion"
            or raw["schemaVersion"] != DELETION_SCHEMA_VERSION
        ):
            raise ProfileStateError("deletion record envelope differs")
        completed_at = raw["completedAt"]
        targets = _list(raw["targets"], field="deletion targets")
        if not all(type(target) is str for target in targets):
            raise ProfileStateError("deletion targets must be strings")
        return cls(
            profile=validate_identifier(
                _strict_text(raw["profile"], field="deletion.profile"),
                field="deletion profile",
            ),
            run_id=validate_run_id(_strict_text(raw["runId"], field="deletion.runId")),
            generation=_strict_int(raw["generation"], field="deletion.generation", minimum=1),
            owner_uid=_strict_int(raw["ownerUid"], field="deletion.ownerUid"),
            confirmation=_strict_text(raw["confirmation"], field="deletion.confirmation"),
            state_digest=_strict_text(raw["stateDigest"], field="deletion.stateDigest", maximum=71),
            status=cast(
                Literal["pending", "complete"],
                _strict_text(raw["status"], field="deletion.status", maximum=16),
            ),
            targets=cast(tuple[str, ...], tuple(targets)),
            created_at=_canonical_timestamp(
                _strict_text(raw["createdAt"], field="deletion.createdAt")
            ),
            completed_at=(
                None
                if completed_at is None
                else _canonical_timestamp(_strict_text(completed_at, field="deletion.completedAt"))
            ),
        )


@dataclass(frozen=True, slots=True)
class ProfileLogSnapshot:
    profile: str
    run_id: str
    component_id: str
    lines: tuple[str, ...]
    truncated: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "apiVersion": API_VERSION,
            "component": self.component_id,
            "kind": "ProfileLogSnapshot",
            "lines": list(self.lines),
            "profile": self.profile,
            "runId": self.run_id,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class CurrentState:
    profile: str
    run_id: str
    generation: int
    revision: int
    owner_uid: int
    updated_at: str

    def to_payload(self) -> dict[str, object]:
        return {
            "apiVersion": API_VERSION,
            "generation": self.generation,
            "kind": "ProfileCurrent",
            "ownerUid": self.owner_uid,
            "profile": self.profile,
            "revision": self.revision,
            "runId": self.run_id,
            "schemaVersion": SCHEMA_VERSION,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_payload(cls, value: object) -> CurrentState:
        raw = _mapping(value, field="current state")
        _exact_keys(
            raw,
            {
                "apiVersion",
                "generation",
                "kind",
                "ownerUid",
                "profile",
                "revision",
                "runId",
                "schemaVersion",
                "updatedAt",
            },
            field="current state",
        )
        _require_envelope(raw, kind="ProfileCurrent")
        return cls(
            profile=validate_identifier(
                _strict_text(raw["profile"], field="current.profile"), field="profile"
            ),
            run_id=validate_run_id(_strict_text(raw["runId"], field="current.runId")),
            generation=_strict_int(raw["generation"], field="current.generation", minimum=1),
            revision=_strict_int(raw["revision"], field="current.revision", minimum=1),
            owner_uid=_strict_int(raw["ownerUid"], field="current.ownerUid"),
            updated_at=_canonical_timestamp(
                _strict_text(raw["updatedAt"], field="current.updatedAt")
            ),
        )


@dataclass(frozen=True, slots=True)
class StateTransaction:
    """One exact current/run two-file mutation and its before/after identities."""

    profile: str
    run_id: str
    generation: int
    owner_uid: int
    before_current: dict[str, object] | None
    before_run: dict[str, object] | None
    after_current: dict[str, object]
    after_run: dict[str, object]
    before_current_digest: str
    before_run_digest: str
    after_current_digest: str
    after_run_digest: str

    def __post_init__(self) -> None:
        validate_identifier(self.profile, field="transaction profile")
        validate_run_id(self.run_id)
        _strict_int(self.generation, field="transaction generation", minimum=1)
        _strict_int(self.owner_uid, field="transaction ownerUid")
        expected = (
            (self.before_current_digest, self.before_current, "before current digest"),
            (self.before_run_digest, self.before_run, "before run digest"),
            (self.after_current_digest, self.after_current, "after current digest"),
            (self.after_run_digest, self.after_run, "after run digest"),
        )
        for digest, payload, field in expected:
            _validate_digest(digest, field=field)
            if digest != _payload_digest(payload):
                raise ProfileStateError(f"{field} differs from its payload")

    @classmethod
    def create(
        cls,
        *,
        profile: str,
        run_id: str,
        generation: int,
        owner_uid: int,
        before_current: Mapping[str, object] | None,
        before_run: Mapping[str, object] | None,
        after_current: Mapping[str, object],
        after_run: Mapping[str, object],
    ) -> StateTransaction:
        before_current_copy = None if before_current is None else dict(before_current)
        before_run_copy = None if before_run is None else dict(before_run)
        after_current_copy = dict(after_current)
        after_run_copy = dict(after_run)
        return cls(
            profile=profile,
            run_id=run_id,
            generation=generation,
            owner_uid=owner_uid,
            before_current=before_current_copy,
            before_run=before_run_copy,
            after_current=after_current_copy,
            after_run=after_run_copy,
            before_current_digest=_payload_digest(before_current_copy),
            before_run_digest=_payload_digest(before_run_copy),
            after_current_digest=_payload_digest(after_current_copy),
            after_run_digest=_payload_digest(after_run_copy),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "afterCurrent": self.after_current,
            "afterCurrentDigest": self.after_current_digest,
            "afterRun": self.after_run,
            "afterRunDigest": self.after_run_digest,
            "apiVersion": API_VERSION,
            "beforeCurrent": self.before_current,
            "beforeCurrentDigest": self.before_current_digest,
            "beforeRun": self.before_run,
            "beforeRunDigest": self.before_run_digest,
            "generation": self.generation,
            "kind": "ProfileStateTransaction",
            "ownerUid": self.owner_uid,
            "profile": self.profile,
            "runId": self.run_id,
            "schemaVersion": TRANSACTION_SCHEMA_VERSION,
        }

    @classmethod
    def from_payload(cls, value: object) -> StateTransaction:
        raw = _mapping(value, field="state transaction")
        _exact_keys(
            raw,
            {
                "afterCurrent",
                "afterCurrentDigest",
                "afterRun",
                "afterRunDigest",
                "apiVersion",
                "beforeCurrent",
                "beforeCurrentDigest",
                "beforeRun",
                "beforeRunDigest",
                "generation",
                "kind",
                "ownerUid",
                "profile",
                "runId",
                "schemaVersion",
            },
            field="state transaction",
        )
        if (
            raw["apiVersion"] != API_VERSION
            or raw["kind"] != "ProfileStateTransaction"
            or raw["schemaVersion"] != TRANSACTION_SCHEMA_VERSION
        ):
            raise ProfileStateError("state transaction envelope differs")

        def optional_mapping(payload: object, *, field: str) -> dict[str, object] | None:
            return None if payload is None else _mapping(payload, field=field)

        return cls(
            profile=validate_identifier(
                _strict_text(raw["profile"], field="transaction.profile"),
                field="transaction profile",
            ),
            run_id=validate_run_id(_strict_text(raw["runId"], field="transaction.runId")),
            generation=_strict_int(raw["generation"], field="transaction.generation", minimum=1),
            owner_uid=_strict_int(raw["ownerUid"], field="transaction.ownerUid"),
            before_current=optional_mapping(
                raw["beforeCurrent"], field="transaction.beforeCurrent"
            ),
            before_run=optional_mapping(raw["beforeRun"], field="transaction.beforeRun"),
            after_current=_mapping(raw["afterCurrent"], field="transaction.afterCurrent"),
            after_run=_mapping(raw["afterRun"], field="transaction.afterRun"),
            before_current_digest=_strict_text(
                raw["beforeCurrentDigest"], field="transaction.beforeCurrentDigest", maximum=71
            ),
            before_run_digest=_strict_text(
                raw["beforeRunDigest"], field="transaction.beforeRunDigest", maximum=71
            ),
            after_current_digest=_strict_text(
                raw["afterCurrentDigest"], field="transaction.afterCurrentDigest", maximum=71
            ),
            after_run_digest=_strict_text(
                raw["afterRunDigest"], field="transaction.afterRunDigest", maximum=71
            ),
        )


@dataclass(frozen=True, slots=True)
class RunState:
    profile: str
    requested_name: str
    run_id: str
    generation: int
    revision: int
    owner_uid: int
    phase: RunPhase
    created_at: str
    updated_at: str
    backend_id: str | None = None
    plan_digest: str | None = None
    bootstrap_id: str | None = None
    private_bootstrap_digest: str | None = None
    host_plan_digest: str | None = None
    host_receipt_digest: str | None = None
    components: tuple[ComponentState, ...] = ()
    resource_receipts: tuple[ResourceReceipt, ...] = ()
    secret_receipts: tuple[SecretReceipt, ...] = ()
    cleanup_complete: bool = False
    failure: FailureRecord | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.profile, field="profile")
        validate_identifier(self.requested_name, field="requested name")
        validate_run_id(self.run_id)
        _strict_int(self.generation, field="generation", minimum=1)
        _strict_int(self.revision, field="revision", minimum=1)
        _strict_int(self.owner_uid, field="ownerUid")
        if self.phase not in RUN_PHASES:
            raise ProfileStateError("unsupported run phase")
        _canonical_timestamp(self.created_at)
        _canonical_timestamp(self.updated_at)
        if (self.backend_id is None) != (self.plan_digest is None):
            raise ProfileStateError("backend identity and execution plan digest must be paired")
        if self.backend_id is not None:
            if self.backend_id not in {"podman", "docker", "gateway", "strict-cri"}:
                raise ProfileStateError("run backend identity is unsupported")
            assert self.plan_digest is not None
            _validate_digest(self.plan_digest, field="execution plan digest")
        if (self.bootstrap_id is None) != (self.private_bootstrap_digest is None):
            raise ProfileStateError("bootstrap ID and private digest must be paired")
        if self.bootstrap_id is not None:
            validate_bootstrap_id(self.bootstrap_id)
            assert self.private_bootstrap_digest is not None
            _validate_digest(self.private_bootstrap_digest, field="private bootstrap digest")
            if self.profile not in {"edge-gateway", "node"}:
                raise ProfileStateError("bootstrap identity is not valid for this profile")
        elif self.profile in {"edge-gateway", "node"}:
            raise ProfileStateError("canonical edge and node runs require bootstrap identity")
        if (self.host_plan_digest is None) != (self.host_receipt_digest is None):
            raise ProfileStateError("host plan and receipt digests must be paired")
        if self.host_plan_digest is not None:
            _validate_digest(self.host_plan_digest, field="host plan digest")
            assert self.host_receipt_digest is not None
            _validate_digest(self.host_receipt_digest, field="host receipt digest")
            if self.profile != "node" or self.backend_id != "strict-cri":
                raise ProfileStateError("host authority is valid only for a strict-CRI node")
        elif self.backend_id == "strict-cri":
            raise ProfileStateError("strict-CRI node state requires host authority")
        if type(self.cleanup_complete) is not bool:
            raise ProfileStateError("cleanupComplete must be a bool")
        if self.phase == "stopped" and not self.cleanup_complete:
            raise ProfileStateError("stopped state requires complete cleanup")
        if self.cleanup_complete and (self.resource_receipts or self.secret_receipts):
            raise ProfileStateError("complete cleanup cannot retain owned receipts")
        if self.phase == "stopped" and any(
            component.status != "stopped" or component.process_identity is not None
            for component in self.components
        ):
            raise ProfileStateError("stopped state cannot retain live component authority")
        _require_unique(
            (component.component_id for component in self.components), field="component ID"
        )
        _require_unique((receipt.secret_id for receipt in self.secret_receipts), field="secret ID")
        _require_unique(
            (
                f"{receipt.resource_type}:{receipt.resource_id}"
                for receipt in self.resource_receipts
            ),
            field="resource receipt",
        )
        ordinals = [receipt.ordinal for receipt in self.resource_receipts]
        if ordinals != sorted(ordinals) or len(ordinals) != len(set(ordinals)):
            raise ProfileStateError("resource receipt ordinals must be unique and ordered")
        for secret_receipt in self.secret_receipts:
            if (
                secret_receipt.owner_profile != self.profile
                or secret_receipt.owner_run_id != self.run_id
            ):
                raise ProfileStateError("receipt ownership differs from its run")
        for resource_receipt in self.resource_receipts:
            if (
                resource_receipt.owner_profile != self.profile
                or resource_receipt.owner_run_id != self.run_id
            ):
                raise ProfileStateError("receipt ownership differs from its run")

    def to_payload(self) -> dict[str, object]:
        return {
            "apiVersion": API_VERSION,
            "backendId": self.backend_id,
            "bootstrapId": self.bootstrap_id,
            "cleanupComplete": self.cleanup_complete,
            "components": [component.to_payload() for component in self.components],
            "createdAt": self.created_at,
            "failure": self.failure.to_payload() if self.failure is not None else None,
            "generation": self.generation,
            "hostPlanDigest": self.host_plan_digest,
            "hostReceiptDigest": self.host_receipt_digest,
            "kind": "ProfileRunState",
            "ownerUid": self.owner_uid,
            "phase": self.phase,
            "planDigest": self.plan_digest,
            "privateBootstrapDigest": self.private_bootstrap_digest,
            "profile": self.profile,
            "requestedName": self.requested_name,
            "resourceReceipts": [receipt.to_payload() for receipt in self.resource_receipts],
            "revision": self.revision,
            "runId": self.run_id,
            "schemaVersion": SCHEMA_VERSION,
            "secretReceipts": [receipt.to_payload() for receipt in self.secret_receipts],
            "updatedAt": self.updated_at,
        }


class ProfileLock(AbstractContextManager["ProfileLock"]):
    """Stable owner-private advisory lock for one canonical profile."""

    def __init__(
        self,
        path: Path,
        *,
        owner_uid: int,
        shared: bool = False,
        blocking: bool = False,
        create: bool = True,
    ) -> None:
        self.path = path
        self.owner_uid = owner_uid
        self.shared = shared
        self.blocking = blocking
        self.create = create
        self._fd: int | None = None

    def __enter__(self) -> ProfileLock:
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        if self.create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(self.path, flags)
        else:
            try:
                fd = os.open(self.path, flags)
            except FileNotFoundError as exc:
                raise ProfileStateError("authoritative profile lock is missing") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid:
                raise ProfileStateError("profile lock is not an owner-controlled regular file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ProfileStateError("profile lock must have mode 0600")
            operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
            if not self.blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, operation)
            except BlockingIOError as exc:
                raise ProfileBusyError("profile_busy") from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class ProfileStateStore:
    """Canonical state, receipts, and lock ownership for one profile."""

    def __init__(
        self,
        roots: ProfileRoots,
        requested_profile: str,
        *,
        clock: Callable[[], str] = utc_now,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.layout = ProfileLayout.resolve(roots, requested_profile)
        self.clock = clock
        self.random_bytes = random_bytes
        self._state_root_only = False

    @classmethod
    def for_explicit_state_root(
        cls,
        state_root: Path,
        requested_profile: str,
        *,
        owner_uid: int | None = None,
        clock: Callable[[], str] = utc_now,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> ProfileStateStore:
        """Bind only an explicit existing K-P1-3 state root for migration.

        This constructor deliberately does not derive HOME/XDG defaults.  Config,
        data, and cache are never created or touched by state-only operations.
        """

        root = Path(state_root)
        if not root.is_absolute() or root.resolve(strict=True) != root:
            raise ProfileStateError("destination state root must be absolute and canonical")
        info = root.lstat()
        expected_uid = os.geteuid() if owner_uid is None else owner_uid
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ProfileStateError("destination state root must be owner-private")
        roots = ProfileRoots(
            config=root,
            state=root,
            data=root,
            cache=root,
            owner_uid=expected_uid,
        )
        store = cls(
            roots,
            requested_profile,
            clock=clock,
            random_bytes=random_bytes,
        )
        store._state_root_only = True
        return store

    def _ensure_state_profile_only(self) -> None:
        """Create only state authority directories for an explicit migration."""

        root = self.layout.roots.state
        _ensure_private_directory(root, owner_uid=self.layout.roots.owner_uid)
        _ensure_private_directory(root / "profiles", owner_uid=self.layout.roots.owner_uid)
        _ensure_private_directory(self.layout.profile_state, owner_uid=self.layout.roots.owner_uid)
        _ensure_private_directory(
            self.layout.profile_state / "runs",
            owner_uid=self.layout.roots.owner_uid,
        )

    def locked(
        self,
        *,
        shared: bool = False,
        blocking: bool = False,
        create: bool = True,
    ) -> ProfileLock:
        if create:
            if self._state_root_only:
                self._ensure_state_profile_only()
            else:
                self.layout.ensure_profile()
        return ProfileLock(
            self.layout.lock,
            owner_uid=self.layout.roots.owner_uid,
            shared=shared,
            blocking=blocking,
            create=create,
        )

    def begin_run(
        self,
        *,
        backend_id: str | None = None,
        plan_digest: str | None = None,
        bootstrap_id: str | None = None,
        private_bootstrap_digest: str | None = None,
        host_plan_digest: str | None = None,
        host_receipt_digest: str | None = None,
    ) -> RunState:
        with self.locked():
            self._require_no_transaction()
            current_payload = self._read_current_payload(optional=True)
            current = (
                None if current_payload is None else CurrentState.from_payload(current_payload)
            )
            generation = 1
            if current is not None:
                previous = self._read_run(current.run_id)
                self._require_consistent(current, previous)
                self._require_no_pending_deletion(previous.run_id)
                if previous.phase != "stopped" or not previous.cleanup_complete:
                    raise StateConflictError("the current profile run is not cleanly terminal")
                generation = previous.generation + 1

            raw = self.random_bytes(16)
            if len(raw) != 16:
                raise ProfileStateError("run ID randomness source returned the wrong size")
            run_id = validate_run_id(f"r1-{raw.hex()}")
            timestamp = _canonical_timestamp(self.clock())
            state = RunState(
                profile=self.layout.canonical_profile,
                requested_name=self.layout.requested_profile,
                run_id=run_id,
                generation=generation,
                revision=1,
                owner_uid=self.layout.roots.owner_uid,
                phase="preparing",
                created_at=timestamp,
                updated_at=timestamp,
                backend_id=backend_id,
                plan_digest=plan_digest,
                bootstrap_id=bootstrap_id,
                private_bootstrap_digest=private_bootstrap_digest,
                host_plan_digest=host_plan_digest,
                host_receipt_digest=host_receipt_digest,
            )
            run_layout = self.layout.for_run(run_id)
            if run_layout.run_state.parent.exists():
                raise StateConflictError("generated run ID already exists")
            run_layout.ensure_run()
            self._persist_pair(
                before_current=current_payload,
                before_run=None,
                state=state,
            )
            return state

    def inspect(self) -> RunState | None:
        try:
            profile_info = self.layout.profile_state.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISDIR(profile_info.st_mode)
            or stat.S_ISLNK(profile_info.st_mode)
            or profile_info.st_uid != self.layout.roots.owner_uid
            or stat.S_IMODE(profile_info.st_mode) != 0o700
        ):
            raise ProfileStateError("profile state directory is not owner-private")
        with self.locked(shared=True, create=False):
            if self._read_transaction(optional=True) is not None:
                raise StateConflictError("profile state transaction requires explicit recovery")
            current = self._read_current(optional=True)
            if current is None:
                return None
            state = self._read_run(current.run_id)
            self._require_no_pending_deletion(state.run_id)
            self._require_consistent(current, state)
            return state

    def inspect_run(self, run_id: str) -> RunState:
        """Read one exact historical run without selecting it by discovery."""

        validate_run_id(run_id)
        with self.locked(shared=True, create=False):
            self._require_no_transaction()
            self._require_no_pending_deletion(run_id)
            state = self._read_run(run_id)
            self._require_run_owner(state)
            return state

    def deletion_record(self, run_id: str) -> DeletionRecord | None:
        validate_run_id(run_id)
        with self.locked(shared=True, create=False):
            record = self._read_deletion(run_id, optional=True)
            if record is not None:
                self._require_deletion_owner(record)
            return record

    def read_log_snapshot(
        self, run_id: str, component_id: str, *, tail: int = 200
    ) -> ProfileLogSnapshot:
        """Read a bounded exact component log under the profile's shared lock."""

        validate_run_id(run_id)
        component_name = validate_identifier(component_id, field="component ID")
        if type(tail) is not int or not 0 <= tail <= 10_000:
            raise ProfileStateError("log tail must be an integer from 0 through 10000")
        with self.locked(shared=True, create=False):
            self._require_no_transaction()
            self._require_no_pending_deletion(run_id)
            state = self._read_run(run_id)
            self._require_run_owner(state)
            component = next(
                (item for item in state.components if item.component_id == component_name), None
            )
            if component is None:
                raise StateConflictError("requested component is not recorded by the run")
            if component.log_path != f"logs/{component_name}.log":
                raise ProfileStateError("component log descriptor is not canonical")
            run_layout = self.layout.for_run(run_id)
            run_parent_fd = _open_owned_directory(
                run_layout.run_state.parent,
                owner_uid=self.layout.roots.owner_uid,
            )
            try:
                run_parent_info = os.fstat(run_parent_fd)
                directory_fd = _open_owned_child_directory(
                    run_parent_fd,
                    "logs",
                    owner_uid=self.layout.roots.owner_uid,
                    expected_device=run_parent_info.st_dev,
                )
            finally:
                os.close(run_parent_fd)
            try:
                directory_info = os.fstat(directory_fd)
                try:
                    fd = os.open(
                        f"{component_name}.log",
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError as exc:
                    raise ProfileStateError("recorded component log is missing") from exc
                except OSError as exc:
                    raise ProfileStateError(
                        "recorded component log cannot be opened safely"
                    ) from exc
                try:
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != self.layout.roots.owner_uid
                        or info.st_dev != directory_info.st_dev
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1
                    ):
                        raise ProfileStateError("recorded component log identity is unsafe")
                    snapshot_length = min(info.st_size, MAX_LOG_SNAPSHOT_BYTES)
                    start = info.st_size - snapshot_length
                    os.lseek(fd, start, os.SEEK_SET)
                    chunks: list[bytes] = []
                    remaining = snapshot_length
                    while remaining:
                        chunk = os.read(fd, min(65536, remaining))
                        if not chunk:
                            raise ProfileStateError(
                                "component log changed during its bounded snapshot"
                            )
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    payload = b"".join(chunks)
                finally:
                    os.close(fd)
            finally:
                os.close(directory_fd)
            if b"\x00" in payload:
                raise ProfileStateError("component log contains invalid bytes")
            truncated = start > 0
            if truncated:
                newline = payload.find(b"\n")
                payload = b"" if newline < 0 else payload[newline + 1 :]
            try:
                all_lines = payload.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise ProfileStateError("component log is not valid UTF-8") from exc
            selected = all_lines[-tail:] if tail else []
            return ProfileLogSnapshot(
                profile=state.profile,
                run_id=state.run_id,
                component_id=component_name,
                lines=tuple(selected),
                truncated=truncated or len(selected) < len(all_lines),
            )

    def delete_run(self, run_id: str, *, confirmation: str) -> DeletionRecord:
        """Delete only exact owned payload for one clean stopped run."""

        validate_run_id(run_id)
        with self.locked():
            authority = self._read_deletion_transaction(optional=True)
            if authority is None:
                self._require_no_transaction()
            state = self._read_run(run_id)
            self._require_run_owner(state)
            expected_confirmation = f"{state.profile}/{state.run_id}"
            if confirmation != expected_confirmation:
                raise StateConflictError("delete confirmation differs from the exact profile/run")
            if (
                state.phase != "stopped"
                or not state.cleanup_complete
                or state.resource_receipts
                or state.secret_receipts
                or any(
                    item.status != "stopped" or item.process_identity is not None
                    for item in state.components
                )
            ):
                raise StateConflictError("only a completely cleaned stopped run may be deleted")
            state_digest = _payload_digest(state.to_payload())
            record = self._read_deletion(run_id, optional=True)
            if authority is not None:
                self._require_deletion_matches(authority, state, state_digest)
                if record is not None:
                    self._require_same_deletion_record(record, authority)
                record = authority
            elif record is None:
                record = DeletionRecord(
                    profile=state.profile,
                    run_id=state.run_id,
                    generation=state.generation,
                    owner_uid=state.owner_uid,
                    confirmation=expected_confirmation,
                    state_digest=state_digest,
                    status="pending",
                    targets=("cache", "data", "logs"),
                    created_at=_canonical_timestamp(self.clock()),
                )
                self._write_deletion_transaction(record)
                self._write_deletion(record)
            else:
                self._require_deletion_matches(record, state, state_digest)
                if record.status == "complete":
                    for _, target in self._deletion_targets(run_id):
                        if not _owned_tree_absent(target, owner_uid=self.layout.roots.owner_uid):
                            raise StateConflictError(
                                "completed deletion target unexpectedly exists"
                            )
                    return record
                raise StateConflictError(
                    "pending deletion lacks its authoritative transaction intent"
                )
            for _, target in self._deletion_targets(run_id):
                _remove_owned_tree(target, owner_uid=self.layout.roots.owner_uid)
            complete = replace(
                record,
                status="complete",
                completed_at=_canonical_timestamp(self.clock()),
            )
            self._write_deletion(complete)
            self._remove_transaction()
            return complete

    def transition(
        self,
        run_id: str,
        phase: RunPhase,
        *,
        expected_revision: int | None = None,
        failure: FailureRecord | None | object = _UNSET,
        cleanup_complete: bool | None = None,
    ) -> RunState:
        if phase not in RUN_PHASES:
            raise ProfileStateError("unsupported run phase")

        def mutate(state: RunState) -> RunState:
            if phase not in _TRANSITIONS[state.phase]:
                raise StateConflictError(f"illegal run transition: {state.phase} -> {phase}")
            next_cleanup = state.cleanup_complete if cleanup_complete is None else cleanup_complete
            next_failure = (
                state.failure if failure is _UNSET else cast(FailureRecord | None, failure)
            )
            return replace(
                state,
                phase=phase,
                cleanup_complete=next_cleanup,
                failure=next_failure,
            )

        return self._mutate(run_id, mutate, expected_revision=expected_revision)

    def put_component(
        self,
        run_id: str,
        component: ComponentState,
        *,
        expected_revision: int | None = None,
    ) -> RunState:
        def mutate(state: RunState) -> RunState:
            by_id = {item.component_id: item for item in state.components}
            by_id[component.component_id] = component
            return replace(state, components=tuple(by_id[key] for key in sorted(by_id)))

        return self._mutate(run_id, mutate, expected_revision=expected_revision)

    def create_secret(
        self,
        run_id: str,
        secret_id: str,
        payload: bytes,
        *,
        expected_revision: int | None = None,
    ) -> tuple[RunState, SecretReceipt]:
        validate_run_id(run_id)
        secret = validate_identifier(secret_id, field="secret ID")
        if not isinstance(payload, bytes) or not payload:
            raise ProfileStateError("generated secret payload must be nonempty bytes")
        with self.locked():
            state, current = self._load_consistent_run(run_id, expected_revision)
            if any(receipt.secret_id == secret for receipt in state.secret_receipts):
                raise StateConflictError("secret receipt already exists")
            layout = self.layout.for_run(run_id)
            layout.ensure_run()
            path = layout.secret_path(secret)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError as exc:
                raise StateConflictError("generated secret already exists") from exc
            try:
                _write_all(fd, payload)
                os.fsync(fd)
                info = os.fstat(fd)
            finally:
                os.close(fd)
            _fsync_directory(path.parent)
            receipt = SecretReceipt(
                secret_id=secret,
                relative_path=path.relative_to(self.layout.roots.data).as_posix(),
                owner_profile=state.profile,
                owner_run_id=state.run_id,
                device=info.st_dev,
                inode=info.st_ino,
                mode=stat.S_IMODE(info.st_mode),
                created_at=_canonical_timestamp(self.clock()),
            )
            updated = self._next_revision(
                replace(state, secret_receipts=(*state.secret_receipts, receipt))
            )
            self._persist_mutation(current, updated)
            return updated, receipt

    def verify_secret(self, run_id: str, secret_id: str, payload: bytes) -> Path:
        """Privately revalidate one exact receipt and its expected secret bytes."""

        validate_run_id(run_id)
        secret = validate_identifier(secret_id, field="secret ID")
        if not isinstance(payload, bytes) or not payload:
            raise ProfileStateError("expected secret payload must be nonempty bytes")
        with self.locked(shared=True, create=False):
            state, _current = self._load_consistent_run(run_id, None)
            matches = [receipt for receipt in state.secret_receipts if receipt.secret_id == secret]
            if len(matches) != 1:
                raise StateConflictError("exact generated secret receipt is unavailable")
            receipt = matches[0]
            path = self.layout.for_run(run_id).secret_path(secret)
            expected_relative = path.relative_to(self.layout.roots.data).as_posix()
            if (
                receipt.relative_path != expected_relative
                or receipt.owner_profile != state.profile
                or receipt.owner_run_id != state.run_id
            ):
                raise StateConflictError("generated secret receipt ownership differs")
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                raise StateConflictError("generated secret is unavailable or unsafe") from exc
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != self.layout.roots.owner_uid
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (receipt.device, receipt.inode)
                    or before.st_size != len(payload)
                ):
                    raise StateConflictError("generated secret identity differs")
                chunks: list[bytes] = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(fd, min(remaining, 64 * 1024))
                    if not chunk:
                        raise StateConflictError("generated secret changed while being read")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(fd, 1):
                    raise StateConflictError("generated secret changed while being read")
                after = os.fstat(fd)
                if (before.st_dev, before.st_ino, before.st_size) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                ) or after.st_nlink != 1:
                    raise StateConflictError("generated secret changed while being read")
            finally:
                os.close(fd)
            if not hmac.compare_digest(b"".join(chunks), payload):
                raise StateConflictError("generated secret content differs")
            return path

    def add_resource(
        self,
        run_id: str,
        *,
        resource_type: str,
        resource_id: str,
        cleanup_handler_id: str,
        expected_revision: int | None = None,
    ) -> tuple[RunState, ResourceReceipt]:
        def mutate(state: RunState) -> RunState:
            key = (resource_type, resource_id)
            if any(
                (receipt.resource_type, receipt.resource_id) == key
                for receipt in state.resource_receipts
            ):
                raise StateConflictError("resource receipt already exists")
            receipt = ResourceReceipt(
                resource_type=resource_type,
                resource_id=resource_id,
                cleanup_handler_id=cleanup_handler_id,
                ordinal=len(state.resource_receipts),
                owner_profile=state.profile,
                owner_run_id=state.run_id,
                created_at=_canonical_timestamp(self.clock()),
            )
            return replace(state, resource_receipts=(*state.resource_receipts, receipt))

        updated = self._mutate(run_id, mutate, expected_revision=expected_revision)
        return updated, updated.resource_receipts[-1]

    def cleanup_owned(self, run_id: str, cleaner: ResourceCleaner) -> RunState:
        """Clean only exact recorded resources and secrets, retaining retry state."""

        with self.locked():
            state, current = self._load_consistent_run(run_id, None)
            if state.phase not in {"stopping", "failed", "orphaned"}:
                raise StateConflictError(
                    f"cleanup is not authorized while the run is {state.phase}"
                )
            resources = list(state.resource_receipts)
            for index in range(len(resources) - 1, -1, -1):
                receipt = resources[index]
                self._require_receipt_owner(state, receipt.owner_profile, receipt.owner_run_id)
                try:
                    cleaner.cleanup_resource(receipt)
                except Exception as exc:
                    failure = FailureRecord(
                        code="resource-cleanup-failed",
                        summary=f"cleanup handler failed for resource ordinal {receipt.ordinal}",
                    )
                    failed = self._next_revision(replace(state, failure=failure))
                    self._persist_mutation(current, failed)
                    raise ProfileStateError("owned resource cleanup failed") from exc
                resources.pop(index)
                state = self._next_revision(replace(state, resource_receipts=tuple(resources)))
                current = self._persist_mutation(current, state)

            remaining_secrets: list[SecretReceipt] = []
            for secret_receipt in state.secret_receipts:
                self._require_receipt_owner(
                    state,
                    secret_receipt.owner_profile,
                    secret_receipt.owner_run_id,
                )
                self._remove_exact_secret(secret_receipt)
            state = self._next_revision(
                replace(
                    state,
                    secret_receipts=tuple(remaining_secrets),
                    cleanup_complete=True,
                )
            )
            self._persist_mutation(current, state)
            return state

    def settle_components(self, run_id: str, *, expected_revision: int) -> RunState:
        """Remove process authority after every recorded component is known stopped."""

        def mutate(state: RunState) -> RunState:
            if state.phase != "stopping":
                raise StateConflictError("components can settle only while the run is stopping")
            return replace(
                state,
                components=tuple(
                    replace(component, status="stopped", process_identity=None)
                    for component in state.components
                ),
            )

        return self._mutate(run_id, mutate, expected_revision=expected_revision)

    def _remove_exact_secret(self, receipt: SecretReceipt) -> None:
        path = self.layout.roots.data / receipt.relative_path
        canonical = path.resolve(strict=False)
        if canonical != path or self.layout.roots.data not in path.parents:
            raise ProfileStateError("secret receipt escapes the owned data root")
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != self.layout.roots.owner_uid
            or info.st_dev != receipt.device
            or info.st_ino != receipt.inode
            or stat.S_IMODE(info.st_mode) != receipt.mode
        ):
            raise ProfileStateError("secret identity differs from its receipt")
        path.unlink()
        _fsync_directory(path.parent)

    def _mutate(
        self,
        run_id: str,
        mutation: Callable[[RunState], RunState],
        *,
        expected_revision: int | None,
    ) -> RunState:
        with self.locked():
            state, current = self._load_consistent_run(run_id, expected_revision)
            updated = self._next_revision(mutation(state))
            self._persist_mutation(current, updated)
            return updated

    def _next_revision(self, state: RunState) -> RunState:
        return replace(
            state,
            revision=state.revision + 1,
            updated_at=_canonical_timestamp(self.clock()),
        )

    def _load_consistent_run(
        self, run_id: str, expected_revision: int | None
    ) -> tuple[RunState, CurrentState]:
        validate_run_id(run_id)
        self._require_no_transaction()
        self._require_no_pending_deletion(run_id)
        current = self._read_current(optional=False)
        assert current is not None
        if current.run_id != run_id:
            raise StateConflictError("requested run is not current")
        state = self._read_run(run_id)
        self._require_consistent(current, state)
        if state.phase == "stopped":
            raise StateConflictError("a stopped run is immutable")
        if expected_revision is not None and state.revision != expected_revision:
            raise StateConflictError("run revision changed")
        return state, current

    def _require_run_owner(self, state: RunState) -> None:
        if (
            state.profile != self.layout.canonical_profile
            or state.owner_uid != self.layout.roots.owner_uid
        ):
            raise StateConflictError("run state ownership differs from the selected profile")

    def _require_consistent(self, current: CurrentState, state: RunState) -> None:
        if (
            current.profile != self.layout.canonical_profile
            or state.profile != self.layout.canonical_profile
            or current.run_id != state.run_id
            or current.generation != state.generation
            or current.revision != state.revision
            or current.owner_uid != state.owner_uid
            or state.owner_uid != self.layout.roots.owner_uid
        ):
            raise StateConflictError("current and run state identities differ")

    @staticmethod
    def _require_receipt_owner(state: RunState, profile: str, run_id: str) -> None:
        if profile != state.profile or run_id != state.run_id:
            raise ProfileStateError("receipt is not owned by the current run")

    def _current_for(self, state: RunState) -> CurrentState:
        return CurrentState(
            profile=state.profile,
            run_id=state.run_id,
            generation=state.generation,
            revision=state.revision,
            owner_uid=state.owner_uid,
            updated_at=state.updated_at,
        )

    def _persist_mutation(self, current: CurrentState, state: RunState) -> CurrentState:
        if current.run_id != state.run_id or current.generation != state.generation:
            raise StateConflictError("mutation changed immutable run identity")
        before_current = self._read_current_payload(optional=False)
        assert before_current is not None
        if CurrentState.from_payload(before_current) != current:
            raise StateConflictError("current state changed before mutation journaling")
        before_run = self._read_run_payload(state.run_id, optional=False)
        assert before_run is not None
        before_state = self._read_run(state.run_id)
        if before_state.run_id != state.run_id:
            raise StateConflictError("run state changed before mutation journaling")
        return self._persist_pair(
            before_current=before_current,
            before_run=before_run,
            state=state,
        )

    def _persist_pair(
        self,
        *,
        before_current: Mapping[str, object] | None,
        before_run: Mapping[str, object] | None,
        state: RunState,
    ) -> CurrentState:
        self._require_no_transaction()
        next_current = self._current_for(state)
        transaction = StateTransaction.create(
            profile=state.profile,
            run_id=state.run_id,
            generation=state.generation,
            owner_uid=state.owner_uid,
            before_current=before_current,
            before_run=before_run,
            after_current=next_current.to_payload(),
            after_run=state.to_payload(),
        )
        self._write_transaction(transaction)
        self._write_run(state)
        self._write_current(next_current)
        self._remove_transaction()
        return next_current

    def reconcile_transaction(self) -> str:
        """Repair only a recognized journal state under the exclusive lock."""

        with self.locked():
            transaction = self._read_transaction(optional=True)
            if transaction is None:
                return "clean"
            if (
                transaction.profile != self.layout.canonical_profile
                or transaction.owner_uid != self.layout.roots.owner_uid
            ):
                raise StateConflictError("state transaction ownership differs")
            current = self._read_current_payload(optional=True)
            run = self._read_run_payload(transaction.run_id, optional=True)
            current_digest = _payload_digest(current)
            run_digest = _payload_digest(run)
            before = (
                transaction.before_current_digest,
                transaction.before_run_digest,
            )
            after = (
                transaction.after_current_digest,
                transaction.after_run_digest,
            )
            observed = (current_digest, run_digest)
            if observed == before:
                self._remove_transaction()
                return "aborted"
            if observed == after:
                self._remove_transaction()
                return "completed"
            if observed == (transaction.before_current_digest, transaction.after_run_digest):
                self._write_current_payload(transaction.after_current)
                self._remove_transaction()
                return "advanced-current"
            raise StateConflictError("state transaction does not match a recoverable exact state")

    def _require_no_transaction(self) -> None:
        if self._read_transaction(optional=True) is not None:
            raise StateConflictError("profile state transaction requires explicit recovery")

    def _write_current(self, state: CurrentState) -> None:
        _atomic_json_write(
            self.layout.current,
            state.to_payload(),
            owner_uid=self.layout.roots.owner_uid,
            random_bytes=self.random_bytes,
        )

    def _write_run(self, state: RunState) -> None:
        layout = self.layout.for_run(state.run_id)
        if self._state_root_only:
            _ensure_private_directory(
                layout.run_state.parent,
                owner_uid=self.layout.roots.owner_uid,
            )
        else:
            layout.ensure_run()
        _atomic_json_write(
            layout.run_state,
            state.to_payload(),
            owner_uid=self.layout.roots.owner_uid,
            random_bytes=self.random_bytes,
        )

    def remove_exact_current_payload(self, expected_digest: str) -> None:
        """Remove the exact current object while an external transaction holds the lock."""

        _validate_digest(expected_digest, field="expected current digest")
        payload = self._read_current_payload(optional=False)
        assert payload is not None
        if _payload_digest(payload) != expected_digest:
            raise StateConflictError("current state differs from migration authority")
        _validate_existing_state_target(
            self.layout.current,
            owner_uid=self.layout.roots.owner_uid,
        )
        self.layout.current.unlink()
        _fsync_directory(self.layout.current.parent)

    def remove_exact_run_payload(
        self,
        run_id: str,
        expected_digest: str,
        *,
        expected_runs_directory_identity: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Remove only one exact imported run state and safe empty state directories."""

        validate_run_id(run_id)
        _validate_digest(expected_digest, field="expected run digest")
        layout = self.layout.for_run(run_id)
        runs = layout.run_state.parent.parent
        if expected_runs_directory_identity is not None:
            self.require_exact_runs_directory_identity(expected_runs_directory_identity)
        payload = self._read_run_payload(run_id, optional=False)
        assert payload is not None
        if _payload_digest(payload) != expected_digest:
            raise StateConflictError("run state differs from migration authority")
        run_directory = layout.run_state.parent
        info = run_directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != self.layout.roots.owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ProfileStateError("imported run directory identity is unsafe")
        if {item.name for item in run_directory.iterdir()} != {"state.json"}:
            raise StateConflictError("imported run directory contains unowned state")
        _validate_existing_state_target(
            layout.run_state,
            owner_uid=self.layout.roots.owner_uid,
        )
        layout.run_state.unlink()
        _fsync_directory(run_directory)
        run_directory.rmdir()
        _fsync_directory(runs)
        if expected_runs_directory_identity is not None:
            self.require_exact_runs_directory_identity(expected_runs_directory_identity)
            if any(runs.iterdir()):
                raise StateConflictError("migration-created runs directory is not empty")
            runs.rmdir()
            _fsync_directory(runs.parent)

    def require_exact_runs_directory_identity(
        self,
        expected_identity: tuple[int, int, int, int],
    ) -> None:
        """Require one exact migration-created runs directory before mutation."""

        runs = self.layout.profile_state / "runs"
        runs_info = runs.lstat()
        observed_identity = (
            runs_info.st_dev,
            runs_info.st_ino,
            runs_info.st_uid,
            stat.S_IMODE(runs_info.st_mode),
        )
        if (
            not stat.S_ISDIR(runs_info.st_mode)
            or stat.S_ISLNK(runs_info.st_mode)
            or observed_identity != expected_identity
        ):
            raise StateConflictError("runs directory differs from migration provenance")

    def _write_transaction(self, transaction: StateTransaction) -> None:
        _atomic_json_write(
            self.layout.transaction,
            transaction.to_payload(),
            owner_uid=self.layout.roots.owner_uid,
            random_bytes=self.random_bytes,
        )

    def _write_current_payload(self, payload: Mapping[str, object]) -> None:
        # Parse before writing so reconciliation never promotes malformed bytes.
        CurrentState.from_payload(payload)
        _atomic_json_write(
            self.layout.current,
            payload,
            owner_uid=self.layout.roots.owner_uid,
            random_bytes=self.random_bytes,
        )

    def _remove_transaction(self) -> None:
        _validate_existing_state_target(
            self.layout.transaction,
            owner_uid=self.layout.roots.owner_uid,
        )
        try:
            self.layout.transaction.unlink()
        except FileNotFoundError as exc:
            raise StateConflictError("state transaction disappeared during mutation") from exc
        _fsync_directory(self.layout.transaction.parent)

    def _read_transaction(self, *, optional: bool) -> StateTransaction | None:
        raw = _read_canonical_json(
            self.layout.transaction,
            owner_uid=self.layout.roots.owner_uid,
            optional=optional,
        )
        if raw is None:
            return None
        if raw.get("kind") == "ProfileRunDeletion":
            DeletionRecord.from_payload(raw)
            raise StateConflictError("run deletion requires explicit roll-forward")
        return StateTransaction.from_payload(raw)

    def _read_deletion_transaction(self, *, optional: bool) -> DeletionRecord | None:
        raw = _read_canonical_json(
            self.layout.transaction,
            owner_uid=self.layout.roots.owner_uid,
            optional=optional,
        )
        if raw is None:
            return None
        if raw.get("kind") != "ProfileRunDeletion":
            StateTransaction.from_payload(raw)
            raise StateConflictError("profile state transaction requires explicit recovery")
        record = DeletionRecord.from_payload(raw)
        if record.status != "pending":
            raise StateConflictError("authoritative deletion intent must remain pending")
        self._require_deletion_owner(record)
        return record

    def _read_current_payload(self, *, optional: bool) -> dict[str, object] | None:
        return _read_canonical_json(
            self.layout.current,
            owner_uid=self.layout.roots.owner_uid,
            optional=optional,
        )

    def _read_run_payload(self, run_id: str, *, optional: bool) -> dict[str, object] | None:
        return _read_canonical_json(
            self.layout.for_run(run_id).run_state,
            owner_uid=self.layout.roots.owner_uid,
            optional=optional,
        )

    def _deletion_path(self, run_id: str) -> Path:
        return self.layout.for_run(run_id).run_state.parent / "deletion.json"

    def _deletion_targets(self, run_id: str) -> tuple[tuple[str, Path], ...]:
        layout = self.layout.for_run(run_id)
        return (("cache", layout.run_cache), ("data", layout.run_data), ("logs", layout.run_logs))

    def _read_deletion(self, run_id: str, *, optional: bool) -> DeletionRecord | None:
        raw = _read_canonical_json(
            self._deletion_path(run_id),
            owner_uid=self.layout.roots.owner_uid,
            optional=optional,
        )
        return None if raw is None else DeletionRecord.from_payload(raw)

    def _write_deletion(self, record: DeletionRecord) -> None:
        self._require_deletion_owner(record)
        _atomic_json_write(
            self._deletion_path(record.run_id),
            record.to_payload(),
            owner_uid=self.layout.roots.owner_uid,
            random_bytes=self.random_bytes,
        )

    def _write_deletion_transaction(self, record: DeletionRecord) -> None:
        if record.status != "pending":
            raise ProfileStateError("authoritative deletion intent must be pending")
        self._require_deletion_owner(record)
        _atomic_json_write(
            self.layout.transaction,
            record.to_payload(),
            owner_uid=self.layout.roots.owner_uid,
            random_bytes=self.random_bytes,
        )

    def _require_deletion_owner(self, record: DeletionRecord) -> None:
        if (
            record.profile != self.layout.canonical_profile
            or record.owner_uid != self.layout.roots.owner_uid
        ):
            raise StateConflictError("deletion record ownership differs")

    def _require_no_pending_deletion(self, run_id: str) -> None:
        record = self._read_deletion(run_id, optional=True)
        if record is not None:
            self._require_deletion_owner(record)
            if record.status == "pending":
                raise StateConflictError("run deletion requires explicit roll-forward")

    def _require_deletion_matches(
        self, record: DeletionRecord, state: RunState, state_digest: str
    ) -> None:
        self._require_deletion_owner(record)
        if (
            record.run_id != state.run_id
            or record.generation != state.generation
            or record.state_digest != state_digest
            or record.confirmation != f"{state.profile}/{state.run_id}"
        ):
            raise StateConflictError("deletion record differs from the exact stopped run")

    @staticmethod
    def _require_same_deletion_record(
        observed: DeletionRecord, authoritative: DeletionRecord
    ) -> None:
        normalized = replace(observed, status="pending", completed_at=None)
        if normalized != authoritative:
            raise StateConflictError("deletion tombstone differs from authoritative intent")

    def _read_current(self, *, optional: bool) -> CurrentState | None:
        raw = self._read_current_payload(optional=optional)
        if raw is None:
            return None
        _exact_keys(
            raw,
            {
                "apiVersion",
                "generation",
                "kind",
                "ownerUid",
                "profile",
                "revision",
                "runId",
                "schemaVersion",
                "updatedAt",
            },
            field="current state",
        )
        return CurrentState.from_payload(raw)

    def _read_run(self, run_id: str) -> RunState:
        raw = self._read_run_payload(run_id, optional=False)
        assert raw is not None
        return run_state_from_payload(raw)


def run_state_from_payload(value: Mapping[str, object]) -> RunState:
    """Parse one canonical run payload for state and migration transactions."""

    raw = dict(value)
    schema_version = _require_envelope(raw, kind="ProfileRunState")
    versioned_keys = (
        {"backendId", "planDigest"}
        if schema_version in {BACKEND_SCHEMA_VERSION, PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION}
        else set()
    )
    bootstrap_keys = (
        {"bootstrapId", "privateBootstrapDigest"}
        if schema_version in {PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION}
        else set()
    )
    host_keys = (
        {"hostPlanDigest", "hostReceiptDigest"} if schema_version == SCHEMA_VERSION else set()
    )
    _exact_keys(
        raw,
        {
            "apiVersion",
            "cleanupComplete",
            "components",
            "createdAt",
            "failure",
            "generation",
            "kind",
            "ownerUid",
            "phase",
            "profile",
            "requestedName",
            "resourceReceipts",
            "revision",
            "runId",
            "schemaVersion",
            "secretReceipts",
            "updatedAt",
        }
        | versioned_keys
        | bootstrap_keys
        | host_keys,
        field="run state",
    )
    phase = _strict_text(raw["phase"], field="run.phase", maximum=32)
    if phase not in RUN_PHASES:
        raise ProfileStateError("unsupported run phase")
    if type(raw["cleanupComplete"]) is not bool:
        raise ProfileStateError("cleanupComplete must be a bool")
    failure = raw["failure"]
    return RunState(
        profile=validate_identifier(
            _strict_text(raw["profile"], field="run.profile"), field="profile"
        ),
        requested_name=validate_identifier(
            _strict_text(raw["requestedName"], field="run.requestedName"),
            field="requested name",
        ),
        run_id=validate_run_id(_strict_text(raw["runId"], field="run.runId")),
        generation=_strict_int(raw["generation"], field="run.generation", minimum=1),
        revision=_strict_int(raw["revision"], field="run.revision", minimum=1),
        owner_uid=_strict_int(raw["ownerUid"], field="run.ownerUid"),
        phase=phase,
        created_at=_canonical_timestamp(_strict_text(raw["createdAt"], field="run.createdAt")),
        updated_at=_canonical_timestamp(_strict_text(raw["updatedAt"], field="run.updatedAt")),
        backend_id=(
            None
            if schema_version == LEGACY_SCHEMA_VERSION or raw["backendId"] is None
            else _strict_text(raw["backendId"], field="run.backendId", maximum=32)
        ),
        plan_digest=(
            None
            if schema_version == LEGACY_SCHEMA_VERSION or raw["planDigest"] is None
            else _strict_text(raw["planDigest"], field="run.planDigest", maximum=71)
        ),
        bootstrap_id=(
            None
            if schema_version not in {PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION}
            or raw["bootstrapId"] is None
            else validate_bootstrap_id(
                _strict_text(raw["bootstrapId"], field="run.bootstrapId", maximum=35)
            )
        ),
        private_bootstrap_digest=(
            None
            if schema_version not in {PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION}
            or raw["privateBootstrapDigest"] is None
            else _strict_text(
                raw["privateBootstrapDigest"],
                field="run.privateBootstrapDigest",
                maximum=71,
            )
        ),
        host_plan_digest=(
            None
            if schema_version != SCHEMA_VERSION or raw["hostPlanDigest"] is None
            else _strict_text(raw["hostPlanDigest"], field="run.hostPlanDigest", maximum=71)
        ),
        host_receipt_digest=(
            None
            if schema_version != SCHEMA_VERSION or raw["hostReceiptDigest"] is None
            else _strict_text(raw["hostReceiptDigest"], field="run.hostReceiptDigest", maximum=71)
        ),
        components=tuple(
            ComponentState.from_payload(item)
            for item in _list(raw["components"], field="run.components")
        ),
        resource_receipts=tuple(
            ResourceReceipt.from_payload(item)
            for item in _list(raw["resourceReceipts"], field="run.resourceReceipts")
        ),
        secret_receipts=tuple(
            SecretReceipt.from_payload(item)
            for item in _list(raw["secretReceipts"], field="run.secretReceipts")
        ),
        cleanup_complete=raw["cleanupComplete"],
        failure=None if failure is None else FailureRecord.from_payload(failure),
    )


def _require_envelope(raw: Mapping[str, object], *, kind: str) -> int:
    if raw["apiVersion"] != API_VERSION:
        raise ProfileStateError("profile state API version mismatch")
    if raw["schemaVersion"] not in {
        LEGACY_SCHEMA_VERSION,
        BACKEND_SCHEMA_VERSION,
        PREVIOUS_SCHEMA_VERSION,
        SCHEMA_VERSION,
    }:
        raise ProfileStateError("profile state schema version mismatch")
    if raw["kind"] != kind:
        raise ProfileStateError("profile state kind mismatch")
    return raw["schemaVersion"]


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ProfileStateError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, *, field: str) -> list[object]:
    if type(value) is not list:
        raise ProfileStateError(f"{field} must be an array")
    return cast(list[object], value)


def _exact_keys(value: Mapping[str, object], expected: set[str], *, field: str) -> None:
    if set(value) != expected:
        raise ProfileStateError(f"{field} has missing or unknown keys")


def _require_unique(values: Iterator[str], *, field: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ProfileStateError(f"duplicate {field}")


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileStateError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ProfileStateError(f"unsupported JSON number: {value}")


def _read_canonical_json(path: Path, *, owner_uid: int, optional: bool) -> dict[str, object] | None:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if optional:
            return None
        raise ProfileStateError(f"required profile state is missing: {path.name}") from None
    except OSError as exc:
        raise ProfileStateError(f"profile state cannot be opened safely: {path.name}") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > MAX_STATE_BYTES
        ):
            raise ProfileStateError(f"profile state has unsafe ownership or mode: {path.name}")
        chunks: list[bytes] = []
        remaining = MAX_STATE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(fd)
    if not payload or len(payload) > MAX_STATE_BYTES or b"\x00" in payload or b"\r" in payload:
        raise ProfileStateError(f"profile state bytes are invalid: {path.name}")
    try:
        text = payload.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileStateError(f"profile state is not canonical JSON: {path.name}") from exc
    raw = _mapping(value, field=path.name)
    canonical = json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if text != canonical:
        raise ProfileStateError(f"profile state is not canonical JSON: {path.name}")
    return raw


def _atomic_json_write(
    path: Path,
    payload: Mapping[str, object],
    *,
    owner_uid: int,
    random_bytes: Callable[[int], bytes],
) -> None:
    parent_info = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or parent_info.st_uid != owner_uid
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise ProfileStateError("profile state parent is not owner-private")
    _validate_existing_state_target(path, owner_uid=owner_uid)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    token = random_bytes(16)
    if len(token) != 16:
        raise ProfileStateError("temporary-file randomness source returned the wrong size")
    temporary = path.parent / f".{path.name}.tmp-{token.hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ProfileStateError("temporary state file is not owner-private")
    except Exception:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise
    finally:
        os.close(fd)
    try:
        _validate_existing_state_target(path, owner_uid=owner_uid)
        os.replace(temporary, path)
    except Exception:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise
    _fsync_directory(path.parent)


def _validate_existing_state_target(path: Path, *, owner_uid: int) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != owner_uid
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ProfileStateError(f"existing state target is unsafe: {path.name}")


def _open_owned_directory(path: Path, *, owner_uid: int) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProfileStateError("owned directory cannot be opened safely") from exc
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        os.close(fd)
        raise ProfileStateError("owned directory identity is unsafe")
    return fd


def _open_owned_child_directory(
    parent_fd: int,
    name: str,
    *,
    owner_uid: int,
    expected_device: int,
) -> int:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise ProfileStateError("owned child directory cannot be opened safely") from exc
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner_uid
        or info.st_dev != expected_device
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        os.close(fd)
        raise ProfileStateError("owned child directory identity is unsafe")
    return fd


def _remove_owned_tree(path: Path, *, owner_uid: int) -> None:
    """Remove one exact owner-private tree by descriptor, without following links."""

    parent_fd = _open_owned_directory(path.parent, owner_uid=owner_uid)
    try:
        parent_info = os.fstat(parent_fd)
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != parent_info.st_dev:
            raise ProfileStateError("owned deletion target is unsafe")
        _remove_owned_directory_entry(
            parent_fd,
            path.name,
            owner_uid=owner_uid,
            root_device=parent_info.st_dev,
        )
    finally:
        os.close(parent_fd)
    _fsync_directory(path.parent)


def _owned_tree_absent(path: Path, *, owner_uid: int) -> bool:
    """Check one exact target through its validated parent descriptor."""

    parent_fd = _open_owned_directory(path.parent, owner_uid=owner_uid)
    try:
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    finally:
        os.close(parent_fd)


def _remove_owned_directory_entry(
    parent_fd: int,
    name: str,
    *,
    owner_uid: int,
    root_device: int,
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ProfileStateError("owned deletion target cannot be opened safely") from exc
    try:
        info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_dev != root_device
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ProfileStateError("owned deletion directory identity is unsafe")
        for child_name in sorted(os.listdir(directory_fd)):
            if child_name in {"", ".", ".."} or "/" in child_name or "\\" in child_name:
                raise ProfileStateError("owned deletion target contains an unsafe name")
            child = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
            if child.st_uid != owner_uid or child.st_dev != root_device:
                raise ProfileStateError("owned deletion target contains an unsafe member")
            if stat.S_ISDIR(child.st_mode):
                _remove_owned_directory_entry(
                    directory_fd,
                    child_name,
                    owner_uid=owner_uid,
                    root_device=root_device,
                )
            elif stat.S_ISREG(child.st_mode):
                if stat.S_IMODE(child.st_mode) != 0o600 or child.st_nlink != 1:
                    raise ProfileStateError("owned file member identity is unsafe")
                os.unlink(child_name, dir_fd=directory_fd)
            else:
                raise ProfileStateError("owned deletion target contains a special member")
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ProfileStateError("short write while persisting profile state")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "API_VERSION",
    "ComponentState",
    "CurrentState",
    "DeletionRecord",
    "FailureRecord",
    "ProcessIdentity",
    "ProfileLogSnapshot",
    "ProfileBusyError",
    "ProfileLock",
    "ProfileStateError",
    "ProfileStateStore",
    "ResourceCleaner",
    "ResourceReceipt",
    "RunPhase",
    "RunState",
    "SecretReceipt",
    "MAX_LOG_SNAPSHOT_BYTES",
    "StateTransaction",
    "StateConflictError",
    "command_digest",
    "run_state_from_payload",
    "utc_now",
]
