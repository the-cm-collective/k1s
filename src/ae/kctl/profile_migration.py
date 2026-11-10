"""Explicit fail-closed migration of stopped legacy profile records.

This module never discovers a source, adopts a process, or starts a runtime.  It
reads one exact legacy record, proves that its recorded PID is absent, and can
import only a clean stopped authority beneath an explicitly supplied K-P1-3
state root.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast

from ae.kctl.profile_paths import (
    _ensure_private_directory,
    validate_identifier,
    validate_run_id,
)
from ae.kctl.profile_state import (
    API_VERSION,
    ComponentState,
    CurrentState,
    ProcessIdentity,
    ProfileStateError,
    ProfileStateStore,
    RunState,
    StateConflictError,
    _atomic_json_write,
    _payload_digest,
    _read_canonical_json,
    command_digest,
    run_state_from_payload,
    utc_now,
)
from ae.kctl.profile_supervisor import LinuxProcInspector
from ae.kctl.profiles import LegacyMigrationTarget, legacy_migration_target

MAX_LEGACY_RECORD_BYTES = 64 * 1024
MIGRATION_SCHEMA_VERSION = 1
MIGRATION_PROFILES = ("edge", "edge-core", "core-edge")

MigrationStatus = Literal["available", "already-applied"]
ReceiptStatus = Literal["applied", "rolled-back"]
TransactionOperation = Literal["apply", "rollback"]


class ProfileMigrationError(ProfileStateError):
    """The explicitly selected migration cannot proceed safely."""


class ExactProcessInspector(Protocol):
    """Read only the exact PID named by the caller-selected legacy record."""

    def inspect(self, pid: int) -> ProcessIdentity | None: ...


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _digest_payload(payload: Mapping[str, object]) -> str:
    return _digest_bytes(_canonical_bytes(payload))


def _validate_digest(value: object, *, field: str) -> str:
    if type(value) is not str or len(value) != 71 or not value.startswith("sha256:"):
        raise ProfileMigrationError(f"{field} must be one SHA-256 identity")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ProfileMigrationError(f"{field} must be one SHA-256 identity") from exc
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
        raise ProfileMigrationError(f"{field} must be canonical text")
    return value


def _strict_int(value: object, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProfileMigrationError(f"{field} must be an integer >= {minimum}")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], *, field: str) -> None:
    if set(value) != expected:
        raise ProfileMigrationError(f"{field} has missing or unknown keys")


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ProfileMigrationError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileMigrationError("legacy record contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ProfileMigrationError(f"unsupported JSON number: {value}")


def _canonical_absolute(value: str | Path, *, field: str, must_exist: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ProfileMigrationError(f"{field} must be absolute")
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise ProfileMigrationError(f"{field} is unavailable") from exc
    if resolved != path:
        raise ProfileMigrationError(f"{field} must be canonical and symlink-free")
    return path


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


@contextmanager
def _exclusive_state_root(state_root: Path):  # type: ignore[no-untyped-def]
    """Serialize first-profile scaffolding without creating an ambient lock file."""

    fd = os.open(
        state_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _legacy_timestamp(value: object) -> tuple[str, str]:
    text = _strict_text(value, field="legacy started_at", maximum=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProfileMigrationError("legacy started_at is not canonical UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.resolution * 0:
        raise ProfileMigrationError("legacy started_at is not canonical UTC")
    if parsed.isoformat() != text:
        raise ProfileMigrationError("legacy started_at is not canonical UTC")
    return text, parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_runner_path(value: object) -> str:
    text = _strict_text(value, field="legacy runner path", maximum=4096)
    path = PurePosixPath(text)
    if (
        not path.is_absolute()
        or "//" in text
        or any(part in {"", ".", ".."} for part in path.parts)
        or tuple(path.parts[-3:]) != ("scripts", "dev", "run_profile.sh")
        or str(path) != text
    ):
        raise ProfileMigrationError("legacy runner path is not canonical")
    return text


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mode: int
    owner_uid: int

    def to_payload(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "ownerUid": self.owner_uid,
            "size": self.size,
        }

    @classmethod
    def from_payload(cls, value: object) -> FileIdentity:
        raw = _mapping(value, field="file identity")
        _exact_keys(raw, {"device", "inode", "mode", "ownerUid", "size"}, field="file identity")
        return cls(
            device=_strict_int(raw["device"], field="file device"),
            inode=_strict_int(raw["inode"], field="file inode", minimum=1),
            size=_strict_int(raw["size"], field="file size"),
            mode=_strict_int(raw["mode"], field="file mode"),
            owner_uid=_strict_int(raw["ownerUid"], field="file owner UID"),
        )


@dataclass(frozen=True, slots=True)
class LegacyRecord:
    profile: str
    pid: int
    command: tuple[str, str]
    started_at: str
    normalized_started_at: str


@dataclass(frozen=True, slots=True)
class LegacySnapshot:
    record: LegacyRecord
    record_digest: str
    file_identity: FileIdentity
    root_identity: FileIdentity


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    legacy_profile: str
    destination_profile: str
    compatibility_variant: str | None
    components: tuple[str, ...]
    source_record_digest: str
    source_file_identity: FileIdentity
    source_root_identity: FileIdentity
    destination_root_identity: FileIdentity
    plan_digest: str
    migration_id: str
    run_id: str
    destination_status: MigrationStatus

    def public_payload(self) -> dict[str, object]:
        return {
            "apiVersion": API_VERSION,
            "compatibilityVariant": self.compatibility_variant,
            "components": list(self.components),
            "destinationProfile": self.destination_profile,
            "destinationStatus": self.destination_status,
            "kind": "ProfileMigrationPlan",
            "legacyProfile": self.legacy_profile,
            "migrationId": self.migration_id,
            "mutating": False,
            "planDigest": self.plan_digest,
            "runId": self.run_id,
            "schemaVersion": MIGRATION_SCHEMA_VERSION,
            "sourceRecordDigest": self.source_record_digest,
        }


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    """Stable identity for a migration-created owner-private directory."""

    device: int
    inode: int
    owner_uid: int
    mode: int

    def __post_init__(self) -> None:
        _strict_int(self.device, field="directory device")
        _strict_int(self.inode, field="directory inode", minimum=1)
        _strict_int(self.owner_uid, field="directory owner UID")
        _strict_int(self.mode, field="directory mode")

    def to_payload(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "ownerUid": self.owner_uid,
        }

    @classmethod
    def from_payload(cls, value: object) -> DirectoryIdentity:
        raw = _mapping(value, field="directory identity")
        _exact_keys(raw, {"device", "inode", "mode", "ownerUid"}, field="directory identity")
        return cls(
            device=_strict_int(raw["device"], field="directory device"),
            inode=_strict_int(raw["inode"], field="directory inode", minimum=1),
            owner_uid=_strict_int(raw["ownerUid"], field="directory owner UID"),
            mode=_strict_int(raw["mode"], field="directory mode"),
        )

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.device, self.inode, self.owner_uid, self.mode)


@dataclass(frozen=True, slots=True)
class ScaffoldProvenance:
    """Exact state-only scaffolding observed absent before this apply."""

    profiles_directory_created: bool
    profile_directory_created: bool
    runs_directory_created: bool
    runs_directory_identity: DirectoryIdentity | None
    migrations_directory_created: bool
    lock_created: bool

    def __post_init__(self) -> None:
        if not all(
            type(value) is bool
            for value in (
                self.profiles_directory_created,
                self.profile_directory_created,
                self.runs_directory_created,
                self.migrations_directory_created,
                self.lock_created,
            )
        ):
            raise ProfileMigrationError("scaffold provenance fields must be bools")
        if self.runs_directory_created != (self.runs_directory_identity is not None):
            raise ProfileMigrationError(
                "runs directory provenance must bind exactly one created identity"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "lockCreated": self.lock_created,
            "migrationsDirectoryCreated": self.migrations_directory_created,
            "profileDirectoryCreated": self.profile_directory_created,
            "profilesDirectoryCreated": self.profiles_directory_created,
            "runsDirectoryCreated": self.runs_directory_created,
            "runsDirectoryIdentity": (
                None
                if self.runs_directory_identity is None
                else self.runs_directory_identity.to_payload()
            ),
        }

    @classmethod
    def from_payload(cls, value: object) -> ScaffoldProvenance:
        raw = _mapping(value, field="scaffold provenance")
        _exact_keys(
            raw,
            {
                "lockCreated",
                "migrationsDirectoryCreated",
                "profileDirectoryCreated",
                "profilesDirectoryCreated",
                "runsDirectoryCreated",
                "runsDirectoryIdentity",
            },
            field="scaffold provenance",
        )
        boolean_fields = (
            raw["lockCreated"],
            raw["migrationsDirectoryCreated"],
            raw["profileDirectoryCreated"],
            raw["profilesDirectoryCreated"],
            raw["runsDirectoryCreated"],
        )
        if not all(type(item) is bool for item in boolean_fields):
            raise ProfileMigrationError("scaffold provenance fields must be bools")
        return cls(
            profiles_directory_created=cast(bool, raw["profilesDirectoryCreated"]),
            profile_directory_created=cast(bool, raw["profileDirectoryCreated"]),
            runs_directory_created=cast(bool, raw["runsDirectoryCreated"]),
            runs_directory_identity=(
                None
                if raw["runsDirectoryIdentity"] is None
                else DirectoryIdentity.from_payload(raw["runsDirectoryIdentity"])
            ),
            migrations_directory_created=cast(bool, raw["migrationsDirectoryCreated"]),
            lock_created=cast(bool, raw["lockCreated"]),
        )


@dataclass(frozen=True, slots=True)
class MigrationReceipt:
    migration_id: str
    legacy_profile: str
    destination_profile: str
    compatibility_variant: str | None
    components: tuple[str, ...]
    source_record_digest: str
    source_file_identity: FileIdentity
    source_root_identity: FileIdentity
    plan_digest: str
    run_id: str
    generation: int
    revision: int
    current_digest: str
    run_digest: str
    scaffold_provenance: ScaffoldProvenance
    status: ReceiptStatus
    applied_at: str
    rolled_back_at: str | None = None
    applied_receipt_digest: str | None = None

    def __post_init__(self) -> None:
        _validate_migration_id(self.migration_id)
        validate_identifier(self.legacy_profile, field="legacy profile")
        validate_identifier(self.destination_profile, field="destination profile")
        if self.compatibility_variant is not None:
            validate_identifier(self.compatibility_variant, field="compatibility variant")
        if not self.components or len(self.components) != len(set(self.components)):
            raise ProfileMigrationError("migration components must be unique")
        for component in self.components:
            validate_identifier(component, field="migration component")
        _validate_digest(self.source_record_digest, field="source record digest")
        _validate_digest(self.plan_digest, field="plan digest")
        validate_run_id(self.run_id)
        _strict_int(self.generation, field="receipt generation", minimum=1)
        _strict_int(self.revision, field="receipt revision", minimum=1)
        _validate_digest(self.current_digest, field="receipt current digest")
        _validate_digest(self.run_digest, field="receipt run digest")
        if self.status not in {"applied", "rolled-back"}:
            raise ProfileMigrationError("migration receipt status is unsupported")
        _canonical_audit_timestamp(self.applied_at)
        if self.status == "applied" and self.rolled_back_at is not None:
            raise ProfileMigrationError("applied receipt cannot have rollback time")
        if self.status == "applied" and self.applied_receipt_digest is not None:
            raise ProfileMigrationError("applied receipt cannot bind itself")
        if self.status == "rolled-back":
            if self.rolled_back_at is None:
                raise ProfileMigrationError("rolled-back receipt requires rollback time")
            _canonical_audit_timestamp(self.rolled_back_at)
            if self.applied_receipt_digest is None:
                raise ProfileMigrationError(
                    "rolled-back receipt requires its applied authorization digest"
                )
            _validate_digest(
                self.applied_receipt_digest,
                field="applied receipt authorization digest",
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "apiVersion": API_VERSION,
            "appliedReceiptDigest": self.applied_receipt_digest,
            "appliedAt": self.applied_at,
            "compatibilityVariant": self.compatibility_variant,
            "components": list(self.components),
            "currentDigest": self.current_digest,
            "destinationProfile": self.destination_profile,
            "generation": self.generation,
            "kind": "ProfileMigrationReceipt",
            "legacyProfile": self.legacy_profile,
            "migrationId": self.migration_id,
            "planDigest": self.plan_digest,
            "revision": self.revision,
            "rolledBackAt": self.rolled_back_at,
            "runDigest": self.run_digest,
            "runId": self.run_id,
            "scaffoldProvenance": self.scaffold_provenance.to_payload(),
            "schemaVersion": MIGRATION_SCHEMA_VERSION,
            "sourceFileIdentity": self.source_file_identity.to_payload(),
            "sourceRecordDigest": self.source_record_digest,
            "sourceRootIdentity": self.source_root_identity.to_payload(),
            "status": self.status,
        }

    @classmethod
    def from_payload(cls, value: object) -> MigrationReceipt:
        raw = _mapping(value, field="migration receipt")
        _exact_keys(
            raw,
            {
                "apiVersion",
                "appliedReceiptDigest",
                "appliedAt",
                "compatibilityVariant",
                "components",
                "currentDigest",
                "destinationProfile",
                "generation",
                "kind",
                "legacyProfile",
                "migrationId",
                "planDigest",
                "revision",
                "rolledBackAt",
                "runDigest",
                "runId",
                "scaffoldProvenance",
                "schemaVersion",
                "sourceFileIdentity",
                "sourceRecordDigest",
                "sourceRootIdentity",
                "status",
            },
            field="migration receipt",
        )
        if (
            raw["apiVersion"] != API_VERSION
            or raw["kind"] != "ProfileMigrationReceipt"
            or raw["schemaVersion"] != MIGRATION_SCHEMA_VERSION
        ):
            raise ProfileMigrationError("migration receipt envelope differs")
        components = raw["components"]
        if type(components) is not list or not all(type(item) is str for item in components):
            raise ProfileMigrationError("migration receipt components must be strings")
        rolled_back_at = raw["rolledBackAt"]
        applied_receipt_digest = raw["appliedReceiptDigest"]
        variant = raw["compatibilityVariant"]
        return cls(
            migration_id=_strict_text(raw["migrationId"], field="migration ID"),
            legacy_profile=_strict_text(raw["legacyProfile"], field="legacy profile"),
            destination_profile=_strict_text(
                raw["destinationProfile"], field="destination profile"
            ),
            compatibility_variant=(
                None if variant is None else _strict_text(variant, field="compatibility variant")
            ),
            components=tuple(cast(list[str], components)),
            source_record_digest=_validate_digest(
                raw["sourceRecordDigest"], field="source record digest"
            ),
            source_file_identity=FileIdentity.from_payload(raw["sourceFileIdentity"]),
            source_root_identity=FileIdentity.from_payload(raw["sourceRootIdentity"]),
            plan_digest=_validate_digest(raw["planDigest"], field="plan digest"),
            run_id=_strict_text(raw["runId"], field="run ID"),
            generation=_strict_int(raw["generation"], field="receipt generation", minimum=1),
            revision=_strict_int(raw["revision"], field="receipt revision", minimum=1),
            current_digest=_validate_digest(raw["currentDigest"], field="current digest"),
            run_digest=_validate_digest(raw["runDigest"], field="run digest"),
            scaffold_provenance=ScaffoldProvenance.from_payload(raw["scaffoldProvenance"]),
            status=cast(ReceiptStatus, _strict_text(raw["status"], field="receipt status")),
            applied_at=_strict_text(raw["appliedAt"], field="appliedAt"),
            rolled_back_at=(
                None
                if rolled_back_at is None
                else _strict_text(rolled_back_at, field="rolledBackAt")
            ),
            applied_receipt_digest=(
                None
                if applied_receipt_digest is None
                else _validate_digest(
                    applied_receipt_digest,
                    field="applied receipt authorization digest",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class MigrationTransaction:
    operation: TransactionOperation
    migration_id: str
    plan_digest: str
    destination_profile: str
    run_id: str
    owner_uid: int
    before_current: dict[str, object] | None
    before_run: dict[str, object] | None
    before_receipt: dict[str, object] | None
    after_current: dict[str, object] | None
    after_run: dict[str, object] | None
    after_receipt: dict[str, object]
    before_current_digest: str
    before_run_digest: str
    before_receipt_digest: str
    after_current_digest: str
    after_run_digest: str
    after_receipt_digest: str

    @classmethod
    def create(
        cls,
        *,
        operation: TransactionOperation,
        migration_id: str,
        plan_digest: str,
        destination_profile: str,
        run_id: str,
        owner_uid: int,
        before_current: Mapping[str, object] | None,
        before_run: Mapping[str, object] | None,
        before_receipt: Mapping[str, object] | None,
        after_current: Mapping[str, object] | None,
        after_run: Mapping[str, object] | None,
        after_receipt: Mapping[str, object],
    ) -> MigrationTransaction:
        before_current_copy = None if before_current is None else dict(before_current)
        before_run_copy = None if before_run is None else dict(before_run)
        before_receipt_copy = None if before_receipt is None else dict(before_receipt)
        after_current_copy = None if after_current is None else dict(after_current)
        after_run_copy = None if after_run is None else dict(after_run)
        after_receipt_copy = dict(after_receipt)
        return cls(
            operation=operation,
            migration_id=migration_id,
            plan_digest=plan_digest,
            destination_profile=destination_profile,
            run_id=run_id,
            owner_uid=owner_uid,
            before_current=before_current_copy,
            before_run=before_run_copy,
            before_receipt=before_receipt_copy,
            after_current=after_current_copy,
            after_run=after_run_copy,
            after_receipt=after_receipt_copy,
            before_current_digest=_payload_digest(before_current_copy),
            before_run_digest=_payload_digest(before_run_copy),
            before_receipt_digest=_payload_digest(before_receipt_copy),
            after_current_digest=_payload_digest(after_current_copy),
            after_run_digest=_payload_digest(after_run_copy),
            after_receipt_digest=_payload_digest(after_receipt_copy),
        )

    def __post_init__(self) -> None:
        if self.operation not in {"apply", "rollback"}:
            raise ProfileMigrationError("migration transaction operation is unsupported")
        _validate_migration_id(self.migration_id)
        _validate_digest(self.plan_digest, field="transaction plan digest")
        validate_identifier(self.destination_profile, field="transaction profile")
        validate_run_id(self.run_id)
        _strict_int(self.owner_uid, field="transaction owner UID")
        for digest, payload, field in (
            (self.before_current_digest, self.before_current, "before current digest"),
            (self.before_run_digest, self.before_run, "before run digest"),
            (self.before_receipt_digest, self.before_receipt, "before receipt digest"),
            (self.after_current_digest, self.after_current, "after current digest"),
            (self.after_run_digest, self.after_run, "after run digest"),
            (self.after_receipt_digest, self.after_receipt, "after receipt digest"),
        ):
            _validate_digest(digest, field=field)
            if digest != _payload_digest(payload):
                raise ProfileMigrationError(f"{field} differs from its payload")

    def to_payload(self) -> dict[str, object]:
        return {
            "afterCurrent": self.after_current,
            "afterCurrentDigest": self.after_current_digest,
            "afterReceipt": self.after_receipt,
            "afterReceiptDigest": self.after_receipt_digest,
            "afterRun": self.after_run,
            "afterRunDigest": self.after_run_digest,
            "apiVersion": API_VERSION,
            "beforeCurrent": self.before_current,
            "beforeCurrentDigest": self.before_current_digest,
            "beforeReceipt": self.before_receipt,
            "beforeReceiptDigest": self.before_receipt_digest,
            "beforeRun": self.before_run,
            "beforeRunDigest": self.before_run_digest,
            "destinationProfile": self.destination_profile,
            "kind": "ProfileMigrationTransaction",
            "migrationId": self.migration_id,
            "operation": self.operation,
            "ownerUid": self.owner_uid,
            "planDigest": self.plan_digest,
            "runId": self.run_id,
            "schemaVersion": MIGRATION_SCHEMA_VERSION,
        }

    @classmethod
    def from_payload(cls, value: object) -> MigrationTransaction:
        raw = _mapping(value, field="migration transaction")
        expected = {
            "afterCurrent",
            "afterCurrentDigest",
            "afterReceipt",
            "afterReceiptDigest",
            "afterRun",
            "afterRunDigest",
            "apiVersion",
            "beforeCurrent",
            "beforeCurrentDigest",
            "beforeReceipt",
            "beforeReceiptDigest",
            "beforeRun",
            "beforeRunDigest",
            "destinationProfile",
            "kind",
            "migrationId",
            "operation",
            "ownerUid",
            "planDigest",
            "runId",
            "schemaVersion",
        }
        _exact_keys(raw, expected, field="migration transaction")
        if (
            raw["apiVersion"] != API_VERSION
            or raw["kind"] != "ProfileMigrationTransaction"
            or raw["schemaVersion"] != MIGRATION_SCHEMA_VERSION
        ):
            raise ProfileMigrationError("migration transaction envelope differs")

        def optional_mapping(value: object, *, field: str) -> dict[str, object] | None:
            return None if value is None else _mapping(value, field=field)

        return cls(
            operation=cast(
                TransactionOperation,
                _strict_text(raw["operation"], field="transaction operation"),
            ),
            migration_id=_strict_text(raw["migrationId"], field="migration ID"),
            plan_digest=_validate_digest(raw["planDigest"], field="plan digest"),
            destination_profile=_strict_text(
                raw["destinationProfile"], field="destination profile"
            ),
            run_id=_strict_text(raw["runId"], field="run ID"),
            owner_uid=_strict_int(raw["ownerUid"], field="owner UID"),
            before_current=optional_mapping(raw["beforeCurrent"], field="beforeCurrent"),
            before_run=optional_mapping(raw["beforeRun"], field="beforeRun"),
            before_receipt=optional_mapping(raw["beforeReceipt"], field="beforeReceipt"),
            after_current=optional_mapping(raw["afterCurrent"], field="afterCurrent"),
            after_run=optional_mapping(raw["afterRun"], field="afterRun"),
            after_receipt=_mapping(raw["afterReceipt"], field="afterReceipt"),
            before_current_digest=_validate_digest(
                raw["beforeCurrentDigest"], field="before current digest"
            ),
            before_run_digest=_validate_digest(raw["beforeRunDigest"], field="before run digest"),
            before_receipt_digest=_validate_digest(
                raw["beforeReceiptDigest"], field="before receipt digest"
            ),
            after_current_digest=_validate_digest(
                raw["afterCurrentDigest"], field="after current digest"
            ),
            after_run_digest=_validate_digest(raw["afterRunDigest"], field="after run digest"),
            after_receipt_digest=_validate_digest(
                raw["afterReceiptDigest"], field="after receipt digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class MigrationResult:
    action: Literal["apply", "rollback"]
    status: Literal["applied", "already-applied", "rolled-back", "already-rolled-back"]
    legacy_profile: str
    destination_profile: str
    migration_id: str
    run_id: str
    plan_digest: str
    receipt_digest: str

    def public_payload(self) -> dict[str, object]:
        return {
            "action": self.action,
            "apiVersion": API_VERSION,
            "destinationProfile": self.destination_profile,
            "kind": "ProfileMigrationResult",
            "legacyProfile": self.legacy_profile,
            "migrationId": self.migration_id,
            "planDigest": self.plan_digest,
            "receiptDigest": self.receipt_digest,
            "runId": self.run_id,
            "schemaVersion": MIGRATION_SCHEMA_VERSION,
            "status": self.status,
        }


def _validate_migration_id(value: str) -> str:
    if type(value) is not str or len(value) != 35 or not value.startswith("m1-"):
        raise ProfileMigrationError("migration ID must use m1-<32 lowercase hex>")
    try:
        int(value[3:], 16)
    except ValueError as exc:
        raise ProfileMigrationError("migration ID must use m1-<32 lowercase hex>") from exc
    if value[3:] != value[3:].lower():
        raise ProfileMigrationError("migration ID must use m1-<32 lowercase hex>")
    return value


def _canonical_audit_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ProfileMigrationError("audit timestamp is not canonical UTC") from exc
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise ProfileMigrationError("audit timestamp is not canonical UTC")
    return value


def _file_identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        mode=stat.S_IMODE(info.st_mode),
        owner_uid=info.st_uid,
    )


def _read_all(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            raise ProfileMigrationError("legacy record changed while being read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise ProfileMigrationError("legacy record changed while being read")
    return b"".join(chunks)


def _read_legacy_snapshot(
    legacy_root: Path,
    legacy_profile: str,
    *,
    owner_uid: int,
) -> LegacySnapshot:
    if legacy_profile not in MIGRATION_PROFILES:
        legacy_migration_target(legacy_profile)
    root = _canonical_absolute(legacy_root, field="legacy root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise ProfileMigrationError("legacy root cannot be opened safely") from exc
    try:
        root_info = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != owner_uid
            or stat.S_IMODE(root_info.st_mode) & 0o022
        ):
            raise ProfileMigrationError("legacy root ownership or mode is unsafe")
        filename = f"{legacy_profile}.json"
        try:
            fd = os.open(
                filename,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except OSError as exc:
            raise ProfileMigrationError("legacy record cannot be opened safely") from exc
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != owner_uid
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) & 0o022
                or before.st_size <= 0
                or before.st_size > MAX_LEGACY_RECORD_BYTES
                or before.st_dev != root_info.st_dev
            ):
                raise ProfileMigrationError("legacy record ownership or identity is unsafe")
            payload = _read_all(fd, before.st_size)
            after = os.fstat(fd)
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
                raise ProfileMigrationError("legacy record changed while being read")
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)

    if payload.startswith(b"\xef\xbb\xbf") or b"\x00" in payload or b"\r" in payload:
        raise ProfileMigrationError("legacy record encoding is not canonical")
    try:
        text = payload.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileMigrationError("legacy record is not canonical JSON") from exc
    raw = _mapping(value, field="legacy record")
    _exact_keys(
        raw,
        {"command", "pid", "profile", "schema_version", "started_at"},
        field="legacy record",
    )
    if _canonical_bytes(raw) != payload:
        raise ProfileMigrationError("legacy record bytes are not canonical JSON")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ProfileMigrationError("legacy record schema is unsupported")
    if raw["profile"] != legacy_profile:
        raise ProfileMigrationError("legacy record profile differs")
    pid = _strict_int(raw["pid"], field="legacy PID", minimum=1)
    if pid > 2**31 - 1:
        raise ProfileMigrationError("legacy PID is outside the accepted range")
    command = raw["command"]
    if type(command) is not list or len(command) != 2:
        raise ProfileMigrationError("legacy command differs from the exact contract")
    runner = _validate_runner_path(command[0])
    runtime_profile = _strict_text(command[1], field="legacy runtime profile", maximum=64)
    expected_runtime = "k1s-core" if legacy_profile == "core-edge" else "k1s-edge"
    if runtime_profile != expected_runtime:
        raise ProfileMigrationError("legacy runtime profile differs from the exact mapping")
    started_at, normalized = _legacy_timestamp(raw["started_at"])
    return LegacySnapshot(
        record=LegacyRecord(
            profile=legacy_profile,
            pid=pid,
            command=(runner, runtime_profile),
            started_at=started_at,
            normalized_started_at=normalized,
        ),
        record_digest=_digest_bytes(payload),
        file_identity=_file_identity(before),
        root_identity=_file_identity(root_info),
    )


def _destination_root_identity(path: Path, *, owner_uid: int) -> tuple[Path, FileIdentity]:
    root = _canonical_absolute(path, field="destination root")
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProfileMigrationError("destination root cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ProfileMigrationError("destination root must be owner-private")
        return root, _file_identity(info)
    finally:
        os.close(fd)


def _receipt_path(store: ProfileStateStore, migration_id: str) -> Path:
    filename = f"{_validate_migration_id(migration_id)}.json"
    return store.layout.profile_state / "migrations" / filename


def _read_receipt(store: ProfileStateStore, migration_id: str) -> MigrationReceipt | None:
    raw = _read_canonical_json(
        _receipt_path(store, migration_id),
        owner_uid=store.layout.roots.owner_uid,
        optional=True,
    )
    return None if raw is None else MigrationReceipt.from_payload(raw)


def _read_migration_transaction(store: ProfileStateStore) -> MigrationTransaction | None:
    raw = _read_canonical_json(
        store.layout.transaction,
        owner_uid=store.layout.roots.owner_uid,
        optional=True,
    )
    return None if raw is None else MigrationTransaction.from_payload(raw)


def _write_receipt(store: ProfileStateStore, receipt: MigrationReceipt) -> None:
    _atomic_json_write(
        _receipt_path(store, receipt.migration_id),
        receipt.to_payload(),
        owner_uid=store.layout.roots.owner_uid,
        random_bytes=store.random_bytes,
    )


def _write_migration_transaction(
    store: ProfileStateStore, transaction: MigrationTransaction
) -> None:
    _atomic_json_write(
        store.layout.transaction,
        transaction.to_payload(),
        owner_uid=store.layout.roots.owner_uid,
        random_bytes=store.random_bytes,
    )


def _observed_payloads(
    store: ProfileStateStore, migration_id: str, run_id: str
) -> tuple[dict[str, object] | None, dict[str, object] | None, dict[str, object] | None]:
    current = store._read_current_payload(optional=True)
    run = store._read_run_payload(run_id, optional=True)
    receipt = _read_canonical_json(
        _receipt_path(store, migration_id),
        owner_uid=store.layout.roots.owner_uid,
        optional=True,
    )
    return current, run, receipt


def _digests(
    payloads: tuple[
        Mapping[str, object] | None,
        Mapping[str, object] | None,
        Mapping[str, object] | None,
    ],
) -> tuple[str, str, str]:
    return tuple(_payload_digest(item) for item in payloads)  # type: ignore[return-value]


class ProfileMigrationManager:
    """Plan and transact one exact stopped legacy compatibility migration."""

    def __init__(
        self,
        *,
        inspector: ExactProcessInspector | None = None,
        owner_uid: int | None = None,
        clock: Callable[[], str] = utc_now,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self.inspector = LinuxProcInspector() if inspector is None else inspector
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self.clock = clock
        self.random_bytes = random_bytes

    def plan(
        self,
        legacy_profile: str,
        *,
        legacy_root: Path,
        destination_profile: str,
        destination_root: Path,
    ) -> MigrationPlan:
        plan, snapshot, target, destination = self._build_authority(
            legacy_profile,
            legacy_root=legacy_root,
            destination_profile=destination_profile,
            destination_root=destination_root,
        )
        # Public planning is deliberately fail-closed on every pending journal.
        status = self._destination_status(
            destination,
            target,
            plan_digest=plan.plan_digest,
            migration_id=plan.migration_id,
            run_id=plan.run_id,
            snapshot=snapshot,
        )
        return replace(plan, destination_status=status)

    def _build_authority(
        self,
        legacy_profile: str,
        *,
        legacy_root: Path,
        destination_profile: str,
        destination_root: Path,
    ) -> tuple[MigrationPlan, LegacySnapshot, LegacyMigrationTarget, Path]:
        """Build immutable authority without inspecting mutable destination state."""

        target = legacy_migration_target(legacy_profile)
        self._require_destination_mapping(target, destination_profile)
        snapshot = _read_legacy_snapshot(
            legacy_root,
            legacy_profile,
            owner_uid=self.owner_uid,
        )
        self._require_stopped(snapshot)
        destination, destination_identity = _destination_root_identity(
            destination_root,
            owner_uid=self.owner_uid,
        )
        digest_payload = {
            "apiVersion": API_VERSION,
            "compatibilityVariant": target.compatibility_variant,
            "components": list(target.components),
            "destinationProfile": target.canonical_name,
            "destinationRoot": str(destination),
            "destinationRootIdentity": {
                "device": destination_identity.device,
                "inode": destination_identity.inode,
                "mode": destination_identity.mode,
                "ownerUid": destination_identity.owner_uid,
            },
            "kind": "ProfileMigrationPlanAuthority",
            "legacyProfile": target.legacy_name,
            "schemaVersion": MIGRATION_SCHEMA_VERSION,
            "sourceFileIdentity": snapshot.file_identity.to_payload(),
            "sourceRecordDigest": snapshot.record_digest,
            "sourceRootIdentity": snapshot.root_identity.to_payload(),
        }
        plan_digest = _digest_payload(digest_payload)
        digest_hex = plan_digest.removeprefix("sha256:")
        migration_id = f"m1-{digest_hex[:32]}"
        run_id = f"r1-{digest_hex[32:]}"
        plan = MigrationPlan(
            legacy_profile=target.legacy_name,
            destination_profile=target.canonical_name,
            compatibility_variant=target.compatibility_variant,
            components=target.components,
            source_record_digest=snapshot.record_digest,
            source_file_identity=snapshot.file_identity,
            source_root_identity=snapshot.root_identity,
            destination_root_identity=destination_identity,
            plan_digest=plan_digest,
            migration_id=migration_id,
            run_id=run_id,
            destination_status="available",
        )
        return plan, snapshot, target, destination

    def apply(
        self,
        legacy_profile: str,
        *,
        legacy_root: Path,
        destination_profile: str,
        destination_root: Path,
        expected_plan_digest: str,
    ) -> MigrationResult:
        expected = _validate_digest(expected_plan_digest, field="expected plan digest")
        plan, snapshot, _target, destination = self._build_authority(
            legacy_profile,
            legacy_root=legacy_root,
            destination_profile=destination_profile,
            destination_root=destination_root,
        )
        if plan.plan_digest != expected:
            raise StateConflictError("migration plan digest changed")
        # This is the last operation before destination scaffolding can be created.
        self._require_stopped(snapshot)
        store = self._state_store(destination, destination_profile)
        with _exclusive_state_root(destination):
            snapshot = _read_legacy_snapshot(
                legacy_root,
                legacy_profile,
                owner_uid=self.owner_uid,
            )
            self._require_snapshot(plan, snapshot)
            self._require_stopped(snapshot)
            provenance = self._ensure_destination_scaffold(store)
            with store.locked(blocking=True):
                snapshot = _read_legacy_snapshot(
                    legacy_root,
                    legacy_profile,
                    owner_uid=self.owner_uid,
                )
                self._require_snapshot(plan, snapshot)
                # Repeated under the profile lock immediately before journaling.
                self._require_stopped(snapshot)
                receipt, status = self._apply_locked(store, plan, snapshot, provenance)
        return MigrationResult(
            action="apply",
            status=status,
            legacy_profile=legacy_profile,
            destination_profile=destination_profile,
            migration_id=plan.migration_id,
            run_id=plan.run_id,
            plan_digest=plan.plan_digest,
            receipt_digest=_payload_digest(receipt.to_payload()),
        )

    def rollback(
        self,
        legacy_profile: str,
        *,
        legacy_root: Path,
        destination_profile: str,
        destination_root: Path,
        migration_id: str,
        expected_receipt_digest: str,
        confirmation: str,
    ) -> MigrationResult:
        _validate_migration_id(migration_id)
        expected_receipt = _validate_digest(
            expected_receipt_digest,
            field="expected receipt digest",
        )
        plan, snapshot, _target, destination = self._build_authority(
            legacy_profile,
            legacy_root=legacy_root,
            destination_profile=destination_profile,
            destination_root=destination_root,
        )
        if migration_id != plan.migration_id:
            raise StateConflictError("migration ID differs from the exact plan")
        expected_confirmation = f"{destination_profile}/{plan.run_id}"
        if confirmation != expected_confirmation:
            raise StateConflictError("rollback confirmation differs from the imported run")
        store = self._state_store(destination, destination_profile)
        if not store.layout.profile_state.exists():
            raise StateConflictError("migration destination authority is absent")
        with store.locked(blocking=True, create=False):
            snapshot = _read_legacy_snapshot(
                legacy_root,
                legacy_profile,
                owner_uid=self.owner_uid,
            )
            self._require_snapshot(plan, snapshot)
            self._require_stopped(snapshot)
            receipt, status = self._rollback_locked(
                store,
                plan,
                snapshot,
                expected_receipt,
            )
        return MigrationResult(
            action="rollback",
            status=status,
            legacy_profile=legacy_profile,
            destination_profile=destination_profile,
            migration_id=plan.migration_id,
            run_id=plan.run_id,
            plan_digest=plan.plan_digest,
            receipt_digest=_payload_digest(receipt.to_payload()),
        )

    @staticmethod
    def _require_destination_mapping(
        target: LegacyMigrationTarget, destination_profile: str
    ) -> None:
        if destination_profile != target.canonical_name:
            raise ProfileMigrationError("destination profile differs from the frozen mapping")

    def _require_stopped(self, snapshot: LegacySnapshot) -> None:
        try:
            observed = self.inspector.inspect(snapshot.record.pid)
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ProfileMigrationError("legacy process identity is ambiguous") from exc
        if observed is not None:
            raise StateConflictError("legacy PID is active and cannot be migrated")

    @staticmethod
    def _require_snapshot(plan: MigrationPlan, snapshot: LegacySnapshot) -> None:
        if (
            snapshot.record.profile != plan.legacy_profile
            or snapshot.record_digest != plan.source_record_digest
            or snapshot.file_identity != plan.source_file_identity
            or snapshot.root_identity != plan.source_root_identity
        ):
            raise StateConflictError("legacy source identity changed after planning")

    def _state_store(self, destination_root: Path, destination_profile: str) -> ProfileStateStore:
        return ProfileStateStore.for_explicit_state_root(
            destination_root,
            destination_profile,
            owner_uid=self.owner_uid,
            clock=self.clock,
            random_bytes=self.random_bytes,
        )

    def _destination_status(
        self,
        destination_root: Path,
        target: LegacyMigrationTarget,
        *,
        plan_digest: str,
        migration_id: str,
        run_id: str,
        snapshot: LegacySnapshot,
    ) -> MigrationStatus:
        store = self._state_store(destination_root, target.canonical_name)
        profile_path = store.layout.profile_state
        try:
            info = profile_path.lstat()
        except FileNotFoundError:
            return "available"
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != self.owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ProfileMigrationError("destination profile state is unsafe")
        with store.locked(shared=True, create=False):
            transaction = _read_canonical_json(
                store.layout.transaction,
                owner_uid=self.owner_uid,
                optional=True,
            )
            if transaction is not None:
                raise StateConflictError("destination migration transaction requires recovery")
            receipt = _read_receipt(store, migration_id)
            current = store._read_current_payload(optional=True)
            run = store._read_run_payload(run_id, optional=True)
            if receipt is None and current is None and run is None:
                self._require_empty_scaffold(store)
                return "available"
            if receipt is None or receipt.status != "applied":
                raise StateConflictError("destination contains non-idempotent migration state")
            self._require_receipt_plan(receipt, target, plan_digest, snapshot, run_id)
            if (
                _payload_digest(current) != receipt.current_digest
                or _payload_digest(run) != receipt.run_digest
            ):
                raise StateConflictError("destination state differs from its migration receipt")
            assert current is not None and run is not None
            CurrentState.from_payload(current)
            store._read_run(run_id)
            return "already-applied"

    def _ensure_destination_scaffold(self, store: ProfileStateStore) -> ScaffoldProvenance:
        profiles = store.layout.roots.state / "profiles"
        profile = store.layout.profile_state
        runs = profile / "runs"
        migrations = profile / "migrations"
        profiles_created = not _lexists(profiles)
        profile_created = not _lexists(profile)
        runs_created = not _lexists(runs)
        migrations_created = not _lexists(migrations)
        lock_created = not _lexists(store.layout.lock)
        store._ensure_state_profile_only()
        _ensure_private_directory(
            migrations,
            owner_uid=self.owner_uid,
        )
        runs_info = runs.lstat()
        runs_identity = (
            DirectoryIdentity(
                device=runs_info.st_dev,
                inode=runs_info.st_ino,
                owner_uid=runs_info.st_uid,
                mode=stat.S_IMODE(runs_info.st_mode),
            )
            if runs_created
            else None
        )
        return ScaffoldProvenance(
            profiles_directory_created=profiles_created,
            profile_directory_created=profile_created,
            runs_directory_created=runs_created,
            runs_directory_identity=runs_identity,
            migrations_directory_created=migrations_created,
            lock_created=lock_created,
        )

    def _require_empty_scaffold(self, store: ProfileStateStore) -> None:
        allowed = {"profile.lock", "runs", "migrations"}
        names = {item.name for item in store.layout.profile_state.iterdir()}
        if not names <= allowed:
            raise StateConflictError("destination profile contains unowned state")
        for name in ("runs", "migrations"):
            path = store.layout.profile_state / name
            if path.exists() and any(path.iterdir()):
                raise StateConflictError("destination profile contains unowned state")

    def _build_imported_state(
        self,
        plan: MigrationPlan,
        snapshot: LegacySnapshot,
    ) -> tuple[RunState, CurrentState]:
        component_states = tuple(
            ComponentState(
                component_id=component,
                command_digest=command_digest(
                    (
                        "legacy-stopped-evidence-v1",
                        plan.legacy_profile,
                        plan.destination_profile,
                        component,
                        snapshot.record_digest,
                    )
                ),
                log_path=f"logs/{component}.log",
                status="stopped",
            )
            for component in plan.components
        )
        run = RunState(
            profile=plan.destination_profile,
            requested_name=plan.legacy_profile,
            run_id=plan.run_id,
            generation=1,
            revision=1,
            owner_uid=self.owner_uid,
            phase="stopped",
            created_at=snapshot.record.normalized_started_at,
            updated_at=snapshot.record.normalized_started_at,
            components=component_states,
            cleanup_complete=True,
        )
        current = CurrentState(
            profile=run.profile,
            run_id=run.run_id,
            generation=run.generation,
            revision=run.revision,
            owner_uid=run.owner_uid,
            updated_at=run.updated_at,
        )
        return run, current

    def _build_receipt(
        self,
        plan: MigrationPlan,
        snapshot: LegacySnapshot,
        run: RunState,
        current: CurrentState,
        provenance: ScaffoldProvenance,
    ) -> MigrationReceipt:
        return MigrationReceipt(
            migration_id=plan.migration_id,
            legacy_profile=plan.legacy_profile,
            destination_profile=plan.destination_profile,
            compatibility_variant=plan.compatibility_variant,
            components=plan.components,
            source_record_digest=plan.source_record_digest,
            source_file_identity=snapshot.file_identity,
            source_root_identity=snapshot.root_identity,
            plan_digest=plan.plan_digest,
            run_id=run.run_id,
            generation=run.generation,
            revision=run.revision,
            current_digest=_payload_digest(current.to_payload()),
            run_digest=_payload_digest(run.to_payload()),
            scaffold_provenance=provenance,
            status="applied",
            applied_at=_canonical_audit_timestamp(self.clock()),
        )

    def _apply_locked(
        self,
        store: ProfileStateStore,
        plan: MigrationPlan,
        snapshot: LegacySnapshot,
        provenance: ScaffoldProvenance,
    ) -> tuple[MigrationReceipt, Literal["applied", "already-applied"]]:
        transaction = _read_migration_transaction(store)
        if transaction is not None:
            self._require_transaction(transaction, plan, "apply")
            self._advance_apply(store, transaction)
            receipt = _read_receipt(store, plan.migration_id)
            assert receipt is not None
            return receipt, "applied"

        observed = _observed_payloads(store, plan.migration_id, plan.run_id)
        receipt = None if observed[2] is None else MigrationReceipt.from_payload(observed[2])
        if receipt is not None:
            if receipt.status != "applied":
                raise StateConflictError("rolled-back migration cannot be reapplied")
            target = legacy_migration_target(plan.legacy_profile)
            self._require_receipt_plan(
                receipt,
                target,
                plan.plan_digest,
                snapshot,
                plan.run_id,
            )
            if _digests(observed) != (
                receipt.current_digest,
                receipt.run_digest,
                _payload_digest(receipt.to_payload()),
            ):
                raise StateConflictError("applied migration state differs")
            return receipt, "already-applied"
        if observed != (None, None, None):
            raise StateConflictError("destination contains unowned state")
        self._require_empty_scaffold(store)
        run, current = self._build_imported_state(plan, snapshot)
        receipt = self._build_receipt(plan, snapshot, run, current, provenance)
        transaction = MigrationTransaction.create(
            operation="apply",
            migration_id=plan.migration_id,
            plan_digest=plan.plan_digest,
            destination_profile=plan.destination_profile,
            run_id=plan.run_id,
            owner_uid=self.owner_uid,
            before_current=None,
            before_run=None,
            before_receipt=None,
            after_current=current.to_payload(),
            after_run=run.to_payload(),
            after_receipt=receipt.to_payload(),
        )
        _write_migration_transaction(store, transaction)
        self._advance_apply(store, transaction)
        return receipt, "applied"

    def _advance_apply(
        self,
        store: ProfileStateStore,
        transaction: MigrationTransaction,
    ) -> None:
        observed = _observed_payloads(store, transaction.migration_id, transaction.run_id)
        before = (
            transaction.before_current_digest,
            transaction.before_run_digest,
            transaction.before_receipt_digest,
        )
        receipt_only = (
            transaction.before_current_digest,
            transaction.before_run_digest,
            transaction.after_receipt_digest,
        )
        receipt_and_run = (
            transaction.before_current_digest,
            transaction.after_run_digest,
            transaction.after_receipt_digest,
        )
        after = (
            transaction.after_current_digest,
            transaction.after_run_digest,
            transaction.after_receipt_digest,
        )
        seen = _digests(observed)
        if seen == before:
            receipt = MigrationReceipt.from_payload(transaction.after_receipt)
            _write_receipt(store, receipt)
            assert transaction.after_run is not None
            store._write_run(run_state_from_payload(transaction.after_run))
            assert transaction.after_current is not None
            store._write_current_payload(transaction.after_current)
        elif seen == receipt_only:
            assert transaction.after_run is not None
            store._write_run(run_state_from_payload(transaction.after_run))
            assert transaction.after_current is not None
            store._write_current_payload(transaction.after_current)
        elif seen == receipt_and_run:
            assert transaction.after_current is not None
            store._write_current_payload(transaction.after_current)
        elif seen != after:
            raise StateConflictError("migration apply transaction state is unrecoverable")
        store._remove_transaction()

    def _rollback_locked(
        self,
        store: ProfileStateStore,
        plan: MigrationPlan,
        snapshot: LegacySnapshot,
        expected_receipt_digest: str,
    ) -> tuple[MigrationReceipt, Literal["rolled-back", "already-rolled-back"]]:
        transaction = _read_migration_transaction(store)
        if transaction is not None:
            self._require_transaction(transaction, plan, "rollback")
            if transaction.before_receipt_digest != expected_receipt_digest:
                raise StateConflictError("rollback receipt digest differs")
            self._advance_rollback(store, transaction)
            receipt = _read_receipt(store, plan.migration_id)
            assert receipt is not None
            return receipt, "rolled-back"

        observed = _observed_payloads(store, plan.migration_id, plan.run_id)
        if observed[2] is None:
            raise StateConflictError("migration receipt is absent")
        receipt = MigrationReceipt.from_payload(observed[2])
        actual_receipt_digest = _payload_digest(observed[2])
        if receipt.status == "rolled-back":
            if receipt.applied_receipt_digest != expected_receipt_digest:
                raise StateConflictError("rollback authorization digest differs")
            if observed[0] is not None or observed[1] is not None:
                raise StateConflictError("rolled-back migration retained state authority")
            return receipt, "already-rolled-back"
        if actual_receipt_digest != expected_receipt_digest:
            raise StateConflictError("migration receipt digest differs")
        target = legacy_migration_target(plan.legacy_profile)
        self._require_receipt_plan(
            receipt,
            target,
            plan.plan_digest,
            snapshot,
            plan.run_id,
        )
        if (
            _payload_digest(observed[0]) != receipt.current_digest
            or _payload_digest(observed[1]) != receipt.run_digest
        ):
            raise StateConflictError("migration destination drifted after apply")
        runs_identity = receipt.scaffold_provenance.runs_directory_identity
        if runs_identity is not None:
            store.require_exact_runs_directory_identity(runs_identity.as_tuple())
        assert observed[0] is not None and observed[1] is not None
        CurrentState.from_payload(observed[0])
        store._read_run(plan.run_id)
        rolled_back = replace(
            receipt,
            status="rolled-back",
            rolled_back_at=_canonical_audit_timestamp(self.clock()),
            applied_receipt_digest=actual_receipt_digest,
        )
        transaction = MigrationTransaction.create(
            operation="rollback",
            migration_id=plan.migration_id,
            plan_digest=plan.plan_digest,
            destination_profile=plan.destination_profile,
            run_id=plan.run_id,
            owner_uid=self.owner_uid,
            before_current=observed[0],
            before_run=observed[1],
            before_receipt=observed[2],
            after_current=None,
            after_run=None,
            after_receipt=rolled_back.to_payload(),
        )
        _write_migration_transaction(store, transaction)
        self._advance_rollback(store, transaction)
        return rolled_back, "rolled-back"

    def _advance_rollback(
        self,
        store: ProfileStateStore,
        transaction: MigrationTransaction,
    ) -> None:
        observed = _observed_payloads(store, transaction.migration_id, transaction.run_id)
        before = (
            transaction.before_current_digest,
            transaction.before_run_digest,
            transaction.before_receipt_digest,
        )
        without_current = (
            transaction.after_current_digest,
            transaction.before_run_digest,
            transaction.before_receipt_digest,
        )
        without_state = (
            transaction.after_current_digest,
            transaction.after_run_digest,
            transaction.before_receipt_digest,
        )
        after = (
            transaction.after_current_digest,
            transaction.after_run_digest,
            transaction.after_receipt_digest,
        )
        seen = _digests(observed)
        after_receipt = MigrationReceipt.from_payload(transaction.after_receipt)
        runs_identity = after_receipt.scaffold_provenance.runs_directory_identity
        expected_runs_identity = None if runs_identity is None else runs_identity.as_tuple()
        if seen == before:
            store.remove_exact_current_payload(transaction.before_current_digest)
            store.remove_exact_run_payload(
                transaction.run_id,
                transaction.before_run_digest,
                expected_runs_directory_identity=expected_runs_identity,
            )
            _write_receipt(store, after_receipt)
        elif seen == without_current:
            store.remove_exact_run_payload(
                transaction.run_id,
                transaction.before_run_digest,
                expected_runs_directory_identity=expected_runs_identity,
            )
            _write_receipt(store, after_receipt)
        elif seen == without_state:
            _write_receipt(store, after_receipt)
        elif seen != after:
            raise StateConflictError("migration rollback transaction state is unrecoverable")
        store._remove_transaction()

    @staticmethod
    def _require_transaction(
        transaction: MigrationTransaction,
        plan: MigrationPlan,
        operation: TransactionOperation,
    ) -> None:
        if (
            transaction.operation != operation
            or transaction.migration_id != plan.migration_id
            or transaction.plan_digest != plan.plan_digest
            or transaction.destination_profile != plan.destination_profile
            or transaction.run_id != plan.run_id
        ):
            raise StateConflictError("pending migration transaction differs")

    @staticmethod
    def _require_receipt_plan(
        receipt: MigrationReceipt,
        target: LegacyMigrationTarget,
        plan_digest: str,
        snapshot: LegacySnapshot,
        run_id: str,
    ) -> None:
        if (
            receipt.legacy_profile != target.legacy_name
            or receipt.destination_profile != target.canonical_name
            or receipt.compatibility_variant != target.compatibility_variant
            or receipt.components != target.components
            or receipt.source_record_digest != snapshot.record_digest
            or receipt.source_file_identity != snapshot.file_identity
            or receipt.source_root_identity != snapshot.root_identity
            or receipt.plan_digest != plan_digest
            or receipt.run_id != run_id
        ):
            raise StateConflictError("migration receipt differs from the exact plan")


def render_migration_text(payload: Mapping[str, object]) -> str:
    """Render public migration facts without local paths or command material."""

    if payload.get("kind") == "ProfileMigrationPlan":
        return " ".join(
            (
                f"legacy={payload['legacyProfile']}",
                f"destination={payload['destinationProfile']}",
                f"status={payload['destinationStatus']}",
                f"migration={payload['migrationId']}",
                f"run={payload['runId']}",
                f"plan={payload['planDigest']}",
            )
        )
    return " ".join(
        (
            f"action={payload['action']}",
            f"legacy={payload['legacyProfile']}",
            f"destination={payload['destinationProfile']}",
            f"status={payload['status']}",
            f"migration={payload['migrationId']}",
            f"run={payload['runId']}",
            f"receipt={payload['receiptDigest']}",
        )
    )


__all__ = [
    "MIGRATION_PROFILES",
    "MigrationPlan",
    "MigrationReceipt",
    "MigrationResult",
    "ProfileMigrationError",
    "ProfileMigrationManager",
    "render_migration_text",
]
