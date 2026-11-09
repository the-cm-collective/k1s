#!/usr/bin/env python3
"""Synthetic QH4 exact shutdown, evidence sealing, and replay contract.

This module defines the source-side boundary used by a later isolated Q1 live
executor.  It accepts only caller-supplied QMP, process, clock, and filesystem
fixtures.  It never discovers or signals a process, opens a QMP socket, starts
QEMU, or mutates host runtime state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

try:
    from scripts.qualification import kp1_q1_conductor as q1_conductor
except ModuleNotFoundError:  # Direct execution from scripts/qualification.
    import kp1_q1_conductor as q1_conductor  # type: ignore[import-not-found,no-redef]

API_VERSION = "k1s.dev/q1-teardown/v1"
SCHEMA_VERSION = 1
SHUTDOWN_KIND = "K1sQ1ExactShutdownReceipt"
INVENTORY_KIND = "K1sQ1HostInventory"
INVENTORY_COMPARISON_KIND = "K1sQ1HostInventoryComparison"
ZERO_RESIDUE_KIND = "K1sQ1ZeroResidueReceipt"
MANIFEST_KIND = "K1sQ1ReplayEvidenceManifest"
SEAL_KIND = "K1sQ1ReplayEvidenceSeal"
SCHEMA_KIND = "K1sQ1TeardownSchema"

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_QMP_RESPONSE_BYTES = 64 * 1024
MAX_QMP_POLLS = 8
MAX_PROCESS_POLLS = 8
SHUTDOWN_TIMEOUT_SECONDS = 60
MAX_ARGV_ITEMS = 256
MAX_ARGV_BYTES = 64 * 1024
MAX_INVENTORY_ITEMS = 4096
MAX_EVIDENCE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024

PROTECTED_LEAF_NAMES = frozenset(
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

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
OBJECT_RE = re.compile(r"[0-9a-f]{40}")
INVENTORY_KINDS = frozenset({"file", "listener", "network-interface", "process", "runtime-object"})
QMP_COMMANDS = ("query-status", "system_powerdown", "query-status", "quit")
QMP_INITIAL_STATUSES = frozenset({"paused", "running"})
QMP_POLL_STATUSES = frozenset({"paused", "running", "shutdown"})
QMP_ACK_STATUS = "accepted"
EVIDENCE_MEMBERS = (
    "conductor-receipt.json",
    "inventory-after.json",
    "inventory-before.json",
    "inventory-comparison.json",
    "shutdown-receipt.json",
    "zero-residue.json",
)
RESERVED_EVIDENCE_FILES = (
    "Q1-EVIDENCE-MANIFEST.json",
    "Q1-EVIDENCE-SEAL.json",
    "SHA256SUMS",
)


class TeardownPolicyError(RuntimeError):
    """QH4 authority, shutdown, evidence, or replay failed closed."""


def _fail() -> TeardownPolicyError:
    return TeardownPolicyError("K1s Q1 teardown and replay failed closed")


def canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    return (encoded + "\n").encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _fail()
        result[key] = value
    return result


def _load_canonical_json(raw: bytes, *, maximum: int = MAX_JSON_BYTES) -> dict[str, object]:
    if not raw or len(raw) > maximum or not raw.endswith(b"\n"):
        raise _fail()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, TeardownPolicyError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return cast(dict[str, object], value)


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        raise _fail()
    return cast(dict[str, object], value)


def _digest(value: object) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _object_id(value: object) -> str:
    if type(value) is not str or OBJECT_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _bind(value: Mapping[str, object], *, field: str = "receiptDigest") -> dict[str, object]:
    if field in value:
        raise _fail()
    result = dict(value)
    result[field] = _digest_bytes(canonical_json_bytes(result))
    return result


def _verify_bound(
    value: object,
    keys: set[str],
    *,
    field: str = "receiptDigest",
) -> dict[str, object]:
    result = _closed(value, {*keys, field})
    unsigned = dict(result)
    observed = unsigned.pop(field)
    if _digest(observed) != _digest_bytes(canonical_json_bytes(unsigned)):
        raise _fail()
    return dict(result)


def _authority(value: object) -> dict[str, object]:
    try:
        return q1_conductor.validate_authority(value)
    except q1_conductor.ConductorPolicyError as exc:
        raise _fail() from exc


def _authority_equal(left: object, right: object) -> bool:
    return _authority(left) == _authority(right)


def _canonical_absolute(value: object) -> str:
    if type(value) is not str or not value.startswith("/") or "\x00" in value:
        raise _fail()
    parsed = PurePosixPath(value)
    if str(parsed) != value or any(part in {"", ".", ".."} for part in parsed.parts[1:]):
        raise _fail()
    return value


def _reject_protected_path(path: Path) -> None:
    if any(part in PROTECTED_LEAF_NAMES for part in path.parts):
        raise _fail()


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time_ticks: int
    executable: str
    argv: tuple[str, ...]

    def private_payload(self) -> dict[str, object]:
        if (
            type(self.pid) is not int
            or not 1 <= self.pid <= (2**31 - 1)
            or type(self.start_time_ticks) is not int
            or not 1 <= self.start_time_ticks <= (2**63 - 1)
            or type(self.argv) is not tuple
            or not self.argv
            or len(self.argv) > MAX_ARGV_ITEMS
        ):
            raise _fail()
        executable = _canonical_absolute(self.executable)
        encoded_bytes = 0
        for item in self.argv:
            if type(item) is not str or not item or "\x00" in item:
                raise _fail()
            encoded_bytes += len(item.encode("utf-8"))
        if encoded_bytes > MAX_ARGV_BYTES or self.argv[0] != executable:
            raise _fail()
        return {
            "argv": list(self.argv),
            "executable": executable,
            "pid": self.pid,
            "startTimeTicks": self.start_time_ticks,
        }

    def digest(self) -> str:
        return _digest_bytes(
            b"k1s-q1-process-identity\0" + canonical_json_bytes(self.private_payload())
        )


@dataclass(frozen=True)
class QmpRequest:
    authority: Mapping[str, object]
    command: str
    deadline_monotonic: float
    maximum_response_bytes: int
    process_identity_digest: str
    sequence: int


@dataclass(frozen=True)
class QmpResponse:
    command: str
    status: str
    payload: bytes = b""


class QmpAdapter(Protocol):
    def execute(self, request: QmpRequest) -> QmpResponse: ...


class ProcessObserver(Protocol):
    def observe(self, pid: int) -> ProcessIdentity | None: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_time(self) -> int: ...


def _observe_exact_process(
    observer: ProcessObserver,
    expected: ProcessIdentity,
) -> ProcessIdentity | None:
    try:
        observed = observer.observe(expected.pid)
    except Exception as exc:
        raise _fail() from exc
    if observed is None:
        return None
    if type(observed) is not ProcessIdentity:
        raise _fail()
    if observed.private_payload() != expected.private_payload():
        raise _fail()
    return observed


def _qmp_command(
    adapter: QmpAdapter,
    *,
    authority: Mapping[str, object],
    clock: Clock,
    command: str,
    deadline: float,
    identity_digest: str,
    sequence: int,
) -> tuple[dict[str, object], QmpResponse]:
    if command not in QMP_COMMANDS or clock.monotonic() >= deadline:
        raise _fail()
    request = QmpRequest(
        authority=dict(authority),
        command=command,
        deadline_monotonic=deadline,
        maximum_response_bytes=MAX_QMP_RESPONSE_BYTES,
        process_identity_digest=identity_digest,
        sequence=sequence,
    )
    try:
        response = adapter.execute(request)
    except Exception as exc:
        raise _fail() from exc
    if (
        type(response) is not QmpResponse
        or response.command != command
        or type(response.status) is not str
        or not response.status
        or type(response.payload) is not bytes
        or len(response.payload) > MAX_QMP_RESPONSE_BYTES
        or clock.monotonic() > deadline
    ):
        raise _fail()
    return (
        {
            "command": command,
            "payloadSha256": _digest_bytes(response.payload),
            "payloadSize": len(response.payload),
            "sequence": sequence,
            "status": response.status,
        },
        response,
    )


def exact_qmp_shutdown(
    authority: Mapping[str, object] | q1_conductor.RunAuthority,
    *,
    process_identity: ProcessIdentity,
    qmp: QmpAdapter,
    process_observer: ProcessObserver,
    clock: Clock,
) -> dict[str, object]:
    """Model one exact QMP shutdown and prove the bound process is absent."""

    authority_value = (
        authority.as_dict()
        if type(authority) is q1_conductor.RunAuthority
        else _authority(authority)
    )
    identity_digest = process_identity.digest()
    if _observe_exact_process(process_observer, process_identity) is None:
        raise _fail()
    started_at = clock.wall_time()
    started = clock.monotonic()
    deadline = started + SHUTDOWN_TIMEOUT_SECONDS
    transcript: list[dict[str, object]] = []
    sequence = 1

    first, response = _qmp_command(
        qmp,
        authority=authority_value,
        clock=clock,
        command="query-status",
        deadline=deadline,
        identity_digest=identity_digest,
        sequence=sequence,
    )
    transcript.append(first)
    if response.status not in QMP_INITIAL_STATUSES:
        raise _fail()
    sequence += 1

    if _observe_exact_process(process_observer, process_identity) is None:
        raise _fail()
    powerdown, response = _qmp_command(
        qmp,
        authority=authority_value,
        clock=clock,
        command="system_powerdown",
        deadline=deadline,
        identity_digest=identity_digest,
        sequence=sequence,
    )
    transcript.append(powerdown)
    if response.status != QMP_ACK_STATUS:
        raise _fail()
    sequence += 1

    shutdown_observed = False
    for _ in range(MAX_QMP_POLLS):
        if _observe_exact_process(process_observer, process_identity) is None:
            raise _fail()
        poll, response = _qmp_command(
            qmp,
            authority=authority_value,
            clock=clock,
            command="query-status",
            deadline=deadline,
            identity_digest=identity_digest,
            sequence=sequence,
        )
        transcript.append(poll)
        sequence += 1
        if response.status not in QMP_POLL_STATUSES:
            raise _fail()
        if response.status == "shutdown":
            shutdown_observed = True
            break
    if not shutdown_observed:
        raise _fail()

    if _observe_exact_process(process_observer, process_identity) is None:
        raise _fail()
    quit_row, response = _qmp_command(
        qmp,
        authority=authority_value,
        clock=clock,
        command="quit",
        deadline=deadline,
        identity_digest=identity_digest,
        sequence=sequence,
    )
    transcript.append(quit_row)
    if response.status != QMP_ACK_STATUS:
        raise _fail()

    absence_observations = 0
    absent = False
    for _ in range(MAX_PROCESS_POLLS):
        absence_observations += 1
        if _observe_exact_process(process_observer, process_identity) is None:
            absent = True
            break
        if clock.monotonic() >= deadline:
            raise _fail()
    if not absent:
        raise _fail()

    receipt = _bind(
        {
            "absenceProof": {
                "observations": absence_observations,
                "status": "absent",
            },
            "apiVersion": API_VERSION,
            "authority": authority_value,
            "completedAtUnix": clock.wall_time(),
            "kind": SHUTDOWN_KIND,
            "processIdentityDigest": identity_digest,
            "qmpTranscript": transcript,
            "schemaVersion": SCHEMA_VERSION,
            "startedAtUnix": started_at,
            "status": "stopped",
            "timeoutSeconds": SHUTDOWN_TIMEOUT_SECONDS,
        }
    )
    return verify_shutdown_receipt(
        receipt,
        authority_value,
        expected_process_identity_digest=identity_digest,
    )


_SHUTDOWN_KEYS = {
    "absenceProof",
    "apiVersion",
    "authority",
    "completedAtUnix",
    "kind",
    "processIdentityDigest",
    "qmpTranscript",
    "schemaVersion",
    "startedAtUnix",
    "status",
    "timeoutSeconds",
}


def verify_shutdown_receipt(
    value: object,
    expected_authority: Mapping[str, object] | q1_conductor.RunAuthority,
    *,
    expected_process_identity_digest: str | None = None,
) -> dict[str, object]:
    expected = (
        expected_authority.as_dict()
        if type(expected_authority) is q1_conductor.RunAuthority
        else _authority(expected_authority)
    )
    receipt = _verify_bound(value, _SHUTDOWN_KEYS)
    absence = _closed(receipt["absenceProof"], {"observations", "status"})
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != SHUTDOWN_KIND
        or type(receipt["schemaVersion"]) is not int
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or not _authority_equal(receipt["authority"], expected)
        or receipt["status"] != "stopped"
        or type(receipt["startedAtUnix"]) is not int
        or type(receipt["completedAtUnix"]) is not int
        or receipt["completedAtUnix"] < receipt["startedAtUnix"]
        or type(receipt["timeoutSeconds"]) is not int
        or receipt["timeoutSeconds"] != SHUTDOWN_TIMEOUT_SECONDS
        or absence["status"] != "absent"
        or type(absence["observations"]) is not int
        or not 1 <= absence["observations"] <= MAX_PROCESS_POLLS
    ):
        raise _fail()
    identity_digest = _digest(receipt["processIdentityDigest"])
    if expected_process_identity_digest is not None and identity_digest != _digest(
        expected_process_identity_digest
    ):
        raise _fail()
    raw_transcript = receipt["qmpTranscript"]
    if type(raw_transcript) is not list or not 4 <= len(raw_transcript) <= MAX_QMP_POLLS + 3:
        raise _fail()
    commands: list[str] = []
    statuses: list[str] = []
    for sequence, raw in enumerate(raw_transcript, 1):
        row = _closed(
            raw,
            {"command", "payloadSha256", "payloadSize", "sequence", "status"},
        )
        if (
            type(row["sequence"]) is not int
            or row["sequence"] != sequence
            or row["command"] not in QMP_COMMANDS
            or type(row["status"]) is not str
            or not row["status"]
            or type(row["payloadSize"]) is not int
            or not 0 <= row["payloadSize"] <= MAX_QMP_RESPONSE_BYTES
        ):
            raise _fail()
        _digest(row["payloadSha256"])
        commands.append(row["command"])
        statuses.append(row["status"])
    if (
        commands[0:2] != ["query-status", "system_powerdown"]
        or commands[-1] != "quit"
        or any(command != "query-status" for command in commands[2:-1])
        or statuses[0] not in QMP_INITIAL_STATUSES
        or statuses[1] != QMP_ACK_STATUS
        or any(status not in QMP_POLL_STATUSES for status in statuses[2:-1])
        or statuses[-2] != "shutdown"
        or statuses[-1] != QMP_ACK_STATUS
    ):
        raise _fail()
    return dict(receipt)


@dataclass(frozen=True)
class InventoryEntry:
    kind: str
    identity: str

    def as_dict(self) -> dict[str, object]:
        if type(self.kind) is not str or self.kind not in INVENTORY_KINDS:
            raise _fail()
        return {"identity": _digest(self.identity), "kind": self.kind}


def build_inventory(
    authority: Mapping[str, object] | q1_conductor.RunAuthority,
    *,
    captured_at_unix: int,
    entries: Sequence[InventoryEntry],
) -> dict[str, object]:
    authority_value = (
        authority.as_dict()
        if type(authority) is q1_conductor.RunAuthority
        else _authority(authority)
    )
    if type(captured_at_unix) is not int or type(entries) not in {list, tuple}:
        raise _fail()
    rows = [entry.as_dict() for entry in entries]
    rows.sort(key=lambda row: (cast(str, row["kind"]), cast(str, row["identity"])))
    if len(rows) > MAX_INVENTORY_ITEMS or len(
        {(cast(str, row["kind"]), cast(str, row["identity"])) for row in rows}
    ) != len(rows):
        raise _fail()
    return _bind(
        {
            "apiVersion": API_VERSION,
            "authority": authority_value,
            "capturedAtUnix": captured_at_unix,
            "entries": rows,
            "kind": INVENTORY_KIND,
            "schemaVersion": SCHEMA_VERSION,
        }
    )


_INVENTORY_KEYS = {
    "apiVersion",
    "authority",
    "capturedAtUnix",
    "entries",
    "kind",
    "schemaVersion",
}


def verify_inventory(
    value: object,
    expected_authority: Mapping[str, object] | q1_conductor.RunAuthority,
) -> dict[str, object]:
    expected = (
        expected_authority.as_dict()
        if type(expected_authority) is q1_conductor.RunAuthority
        else _authority(expected_authority)
    )
    inventory = _verify_bound(value, _INVENTORY_KEYS)
    if (
        inventory["apiVersion"] != API_VERSION
        or inventory["kind"] != INVENTORY_KIND
        or type(inventory["schemaVersion"]) is not int
        or inventory["schemaVersion"] != SCHEMA_VERSION
        or not _authority_equal(inventory["authority"], expected)
        or type(inventory["capturedAtUnix"]) is not int
        or type(inventory["entries"]) is not list
        or len(cast(list[object], inventory["entries"])) > MAX_INVENTORY_ITEMS
    ):
        raise _fail()
    normalized: list[dict[str, object]] = []
    for raw in cast(list[object], inventory["entries"]):
        item = _closed(raw, {"identity", "kind"})
        normalized.append(
            InventoryEntry(cast(str, item["kind"]), _digest(item["identity"])).as_dict()
        )
    expected_order = sorted(
        normalized,
        key=lambda row: (cast(str, row["kind"]), cast(str, row["identity"])),
    )
    if normalized != expected_order or len(
        {(cast(str, row["kind"]), cast(str, row["identity"])) for row in normalized}
    ) != len(normalized):
        raise _fail()
    return dict(inventory)


def compare_inventories(
    before: object,
    after: object,
    expected_authority: Mapping[str, object] | q1_conductor.RunAuthority,
) -> dict[str, object]:
    authority = (
        expected_authority.as_dict()
        if type(expected_authority) is q1_conductor.RunAuthority
        else _authority(expected_authority)
    )
    before_value = verify_inventory(before, authority)
    after_value = verify_inventory(after, authority)
    before_entries = cast(list[object], before_value["entries"])
    after_entries = cast(list[object], after_value["entries"])
    if before_entries != after_entries:
        raise _fail()
    return _bind(
        {
            "afterInventoryDigest": after_value["receiptDigest"],
            "apiVersion": API_VERSION,
            "authority": authority,
            "beforeInventoryDigest": before_value["receiptDigest"],
            "entryCount": len(before_entries),
            "kind": INVENTORY_COMPARISON_KIND,
            "schemaVersion": SCHEMA_VERSION,
            "status": "equal",
        }
    )


_COMPARISON_KEYS = {
    "afterInventoryDigest",
    "apiVersion",
    "authority",
    "beforeInventoryDigest",
    "entryCount",
    "kind",
    "schemaVersion",
    "status",
}


def verify_inventory_comparison(
    value: object,
    before: object,
    after: object,
    expected_authority: Mapping[str, object] | q1_conductor.RunAuthority,
) -> dict[str, object]:
    expected = compare_inventories(before, after, expected_authority)
    observed = _verify_bound(value, _COMPARISON_KEYS)
    if (
        observed["apiVersion"] != API_VERSION
        or observed["kind"] != INVENTORY_COMPARISON_KIND
        or type(observed["schemaVersion"]) is not int
        or observed["schemaVersion"] != SCHEMA_VERSION
        or type(observed["entryCount"]) is not int
        or observed["status"] != "equal"
        or not _authority_equal(observed["authority"], expected["authority"])
        or _digest(observed["beforeInventoryDigest"]) != expected["beforeInventoryDigest"]
        or _digest(observed["afterInventoryDigest"]) != expected["afterInventoryDigest"]
        or observed != expected
    ):
        raise _fail()
    return dict(observed)


def _path_chain_is_not_link(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise _fail()


def _private_directory(path: Path) -> os.stat_result:
    _reject_protected_path(path)
    if not path.is_absolute() or Path(os.path.realpath(path)) != path:
        raise _fail()
    try:
        _path_chain_is_not_link(path)
        info = os.lstat(path)
    except OSError as exc:
        raise _fail() from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise _fail()
    return info


def _open_private_directory(
    path: Path,
    *,
    expected: os.stat_result | None = None,
) -> tuple[int, os.stat_result]:
    path_info = _private_directory(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise _fail() from exc
    authority = expected or path_info
    if (
        opened.st_dev != authority.st_dev
        or opened.st_ino != authority.st_ino
        or opened.st_uid != authority.st_uid
        or stat.S_IMODE(opened.st_mode) != PRIVATE_DIRECTORY_MODE
        or path_info.st_dev != authority.st_dev
        or path_info.st_ino != authority.st_ino
    ):
        os.close(descriptor)
        raise _fail()
    return descriptor, opened


def _path_identity_digest(path: Path, info: os.stat_result) -> str:
    return _digest_bytes(
        b"k1s-q1-private-path\0"
        + canonical_json_bytes(
            {
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
                "path": str(path),
                "uid": info.st_uid,
            }
        )
    )


def build_zero_residue_receipt(
    *,
    capsule_parent: Path,
    retained_evidence_root: Path,
    authority: Mapping[str, object] | q1_conductor.RunAuthority,
    shutdown_receipt: object,
    inventory_before: object,
    inventory_after: object,
    checked_at_unix: int,
) -> dict[str, object]:
    authority_value = (
        authority.as_dict()
        if type(authority) is q1_conductor.RunAuthority
        else _authority(authority)
    )
    if type(checked_at_unix) is not int:
        raise _fail()
    parent_info = _private_directory(capsule_parent)
    evidence_info = _private_directory(retained_evidence_root)
    run_id = cast(str, authority_value["runId"])
    run_root = capsule_parent / f"k1s-{run_id}"
    parent_descriptor = -1
    try:
        parent_descriptor, opened_parent = _open_private_directory(
            capsule_parent, expected=parent_info
        )
        if opened_parent.st_dev != parent_info.st_dev:
            raise _fail()
        os.stat(run_root.name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _fail() from exc
    else:
        raise _fail()
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    shutdown = verify_shutdown_receipt(shutdown_receipt, authority_value)
    comparison = compare_inventories(inventory_before, inventory_after, authority_value)
    return _bind(
        {
            "apiVersion": API_VERSION,
            "authority": authority_value,
            "capsuleParentIdentityDigest": _path_identity_digest(capsule_parent, parent_info),
            "checkedAtUnix": checked_at_unix,
            "hostInventoryComparisonDigest": comparison["receiptDigest"],
            "kind": ZERO_RESIDUE_KIND,
            "processIdentityDigest": shutdown["processIdentityDigest"],
            "retainedEvidenceRootIdentityDigest": _path_identity_digest(
                retained_evidence_root, evidence_info
            ),
            "runRootAbsent": True,
            "runRootIdentityDigest": _digest_bytes(
                b"k1s-q1-run-root\0" + str(run_root).encode("utf-8")
            ),
            "schemaVersion": SCHEMA_VERSION,
            "shutdownReceiptDigest": shutdown["receiptDigest"],
            "status": "zero-residue",
        }
    )


_ZERO_KEYS = {
    "apiVersion",
    "authority",
    "capsuleParentIdentityDigest",
    "checkedAtUnix",
    "hostInventoryComparisonDigest",
    "kind",
    "processIdentityDigest",
    "retainedEvidenceRootIdentityDigest",
    "runRootAbsent",
    "runRootIdentityDigest",
    "schemaVersion",
    "shutdownReceiptDigest",
    "status",
}


def verify_zero_residue_receipt(
    value: object,
    authority: Mapping[str, object] | q1_conductor.RunAuthority,
    shutdown_receipt: object,
    inventory_before: object,
    inventory_after: object,
) -> dict[str, object]:
    authority_value = (
        authority.as_dict()
        if type(authority) is q1_conductor.RunAuthority
        else _authority(authority)
    )
    receipt = _verify_bound(value, _ZERO_KEYS)
    shutdown = verify_shutdown_receipt(shutdown_receipt, authority_value)
    comparison = compare_inventories(inventory_before, inventory_after, authority_value)
    if (
        receipt["apiVersion"] != API_VERSION
        or receipt["kind"] != ZERO_RESIDUE_KIND
        or type(receipt["schemaVersion"]) is not int
        or receipt["schemaVersion"] != SCHEMA_VERSION
        or not _authority_equal(receipt["authority"], authority_value)
        or receipt["status"] != "zero-residue"
        or receipt["runRootAbsent"] is not True
        or type(receipt["checkedAtUnix"]) is not int
        or receipt["processIdentityDigest"] != shutdown["processIdentityDigest"]
        or receipt["shutdownReceiptDigest"] != shutdown["receiptDigest"]
        or receipt["hostInventoryComparisonDigest"] != comparison["receiptDigest"]
    ):
        raise _fail()
    for key in (
        "capsuleParentIdentityDigest",
        "processIdentityDigest",
        "retainedEvidenceRootIdentityDigest",
        "runRootIdentityDigest",
        "shutdownReceiptDigest",
        "hostInventoryComparisonDigest",
    ):
        _digest(receipt[key])
    return dict(receipt)


def _safe_member_at(
    name: str,
    *,
    directory_descriptor: int,
    root_info: os.stat_result,
) -> tuple[bytes, os.stat_result]:
    if (
        type(name) is not str
        or not name
        or "/" in name
        or name in {".", ".."}
        or name in PROTECTED_LEAF_NAMES
    ):
        raise _fail()
    try:
        info = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise _fail() from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or info.st_dev != root_info.st_dev
        or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
        or not 0 < info.st_size <= MAX_EVIDENCE_MEMBER_BYTES
    ):
        raise _fail()
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
                or opened.st_size != info.st_size
            ):
                raise _fail()
            raw = handle.read(MAX_EVIDENCE_MEMBER_BYTES + 1)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise _fail() from exc
    if len(raw) != info.st_size:
        raise _fail()
    return raw, info


def _write_private_exclusive(
    path: Path,
    raw: bytes,
    *,
    expected_parent: os.stat_result | None = None,
) -> None:
    _reject_protected_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = -1
    directory_descriptor = -1
    try:
        directory_descriptor, _opened_parent = _open_private_directory(
            path.parent,
            expected=expected_parent,
        )
        descriptor = os.open(
            path.name,
            flags,
            PRIVATE_FILE_MODE,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory_descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise _fail() from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _validate_member_payloads(
    payloads: Mapping[str, dict[str, object]],
    authority: Mapping[str, object],
) -> None:
    try:
        q1_conductor.verify_receipt(payloads["conductor-receipt.json"], authority)
    except q1_conductor.ConductorPolicyError as exc:
        raise _fail() from exc
    before = verify_inventory(payloads["inventory-before.json"], authority)
    after = verify_inventory(payloads["inventory-after.json"], authority)
    verify_inventory_comparison(payloads["inventory-comparison.json"], before, after, authority)
    shutdown = verify_shutdown_receipt(payloads["shutdown-receipt.json"], authority)
    verify_zero_residue_receipt(payloads["zero-residue.json"], authority, shutdown, before, after)


def _enumerate_exact_files(
    root: Path, expected: set[str]
) -> tuple[os.stat_result, dict[str, tuple[bytes, os.stat_result]]]:
    root_descriptor = -1
    try:
        root_descriptor, root_info = _open_private_directory(root)
        entries = os.listdir(root_descriptor)
    except OSError as exc:
        raise _fail() from exc
    try:
        if set(entries) != expected or len(entries) != len(expected):
            raise _fail()
        result: dict[str, tuple[bytes, os.stat_result]] = {}
        for name in entries:
            if name not in expected:
                raise _fail()
            result[name] = _safe_member_at(
                name,
                directory_descriptor=root_descriptor,
                root_info=root_info,
            )
        return root_info, result
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)


def seal_evidence(
    root: Path,
    authority: Mapping[str, object] | q1_conductor.RunAuthority,
) -> dict[str, object]:
    """Seal the exact metadata-safe QH4 evidence members once."""

    authority_value = (
        authority.as_dict()
        if type(authority) is q1_conductor.RunAuthority
        else _authority(authority)
    )
    root_info, raw_members = _enumerate_exact_files(root, set(EVIDENCE_MEMBERS))
    payloads = {name: _load_canonical_json(raw_members[name][0]) for name in EVIDENCE_MEMBERS}
    _validate_member_payloads(payloads, authority_value)
    rows: list[dict[str, object]] = []
    total = 0
    for name in EVIDENCE_MEMBERS:
        raw, info = raw_members[name]
        total += len(raw)
        rows.append(
            {
                "mode": stat.S_IMODE(info.st_mode),
                "path": name,
                "sha256": _digest_bytes(raw),
                "size": len(raw),
            }
        )
    if total > MAX_EVIDENCE_TOTAL_BYTES:
        raise _fail()
    manifest = {
        "apiVersion": API_VERSION,
        "authority": authority_value,
        "kind": MANIFEST_KIND,
        "memberCount": len(rows),
        "members": rows,
        "schemaVersion": SCHEMA_VERSION,
        "totalBytes": total,
    }
    manifest_raw = canonical_json_bytes(manifest)
    manifest_name = RESERVED_EVIDENCE_FILES[0]
    _write_private_exclusive(
        root / manifest_name,
        manifest_raw,
        expected_parent=root_info,
    )
    sums_rows: list[dict[str, object]] = [
        *rows,
        {"path": manifest_name, "sha256": _digest_bytes(manifest_raw)},
    ]
    sums_raw = "".join(
        f"{cast(str, row['sha256']).removeprefix('sha256:')}  {row['path']}\n"
        for row in sorted(sums_rows, key=lambda row: cast(str, row["path"]).encode("utf-8"))
    ).encode("ascii")
    sums_name = RESERVED_EVIDENCE_FILES[2]
    _write_private_exclusive(root / sums_name, sums_raw, expected_parent=root_info)
    seal = _bind(
        {
            "apiVersion": API_VERSION,
            "authority": authority_value,
            "evidenceManifestSha256": _digest_bytes(manifest_raw),
            "kind": SEAL_KIND,
            "memberCount": len(rows),
            "schemaVersion": SCHEMA_VERSION,
            "sha256sumsSha256": _digest_bytes(sums_raw),
            "totalBytes": total,
        },
        field="sealDigest",
    )
    _write_private_exclusive(
        root / RESERVED_EVIDENCE_FILES[1],
        canonical_json_bytes(seal),
        expected_parent=root_info,
    )
    return seal


def replay_evidence(
    root: Path,
    authority: Mapping[str, object] | q1_conductor.RunAuthority,
    *,
    expected_seal_file_sha256: str | None = None,
) -> dict[str, object]:
    """Independently replay every sealed QH4 evidence relation."""

    authority_value = (
        authority.as_dict()
        if type(authority) is q1_conductor.RunAuthority
        else _authority(authority)
    )
    expected_names = {*EVIDENCE_MEMBERS, *RESERVED_EVIDENCE_FILES}
    _root_info, files = _enumerate_exact_files(root, expected_names)
    payloads = {name: _load_canonical_json(files[name][0]) for name in EVIDENCE_MEMBERS}
    _validate_member_payloads(payloads, authority_value)
    manifest_raw = files[RESERVED_EVIDENCE_FILES[0]][0]
    manifest = _closed(
        _load_canonical_json(manifest_raw),
        {
            "apiVersion",
            "authority",
            "kind",
            "memberCount",
            "members",
            "schemaVersion",
            "totalBytes",
        },
    )
    if (
        manifest["apiVersion"] != API_VERSION
        or manifest["kind"] != MANIFEST_KIND
        or type(manifest["schemaVersion"]) is not int
        or manifest["schemaVersion"] != SCHEMA_VERSION
        or not _authority_equal(manifest["authority"], authority_value)
        or type(manifest["memberCount"]) is not int
        or manifest["memberCount"] != len(EVIDENCE_MEMBERS)
        or type(manifest["totalBytes"]) is not int
        or type(manifest["members"]) is not list
    ):
        raise _fail()
    rows: list[dict[str, object]] = []
    total = 0
    for name in EVIDENCE_MEMBERS:
        raw, info = files[name]
        total += len(raw)
        rows.append(
            {
                "mode": stat.S_IMODE(info.st_mode),
                "path": name,
                "sha256": _digest_bytes(raw),
                "size": len(raw),
            }
        )
    if manifest["members"] != rows or manifest["totalBytes"] != total:
        raise _fail()
    sums_rows: list[dict[str, object]] = [
        *rows,
        {
            "path": RESERVED_EVIDENCE_FILES[0],
            "sha256": _digest_bytes(manifest_raw),
        },
    ]
    expected_sums = "".join(
        f"{cast(str, row['sha256']).removeprefix('sha256:')}  {row['path']}\n"
        for row in sorted(sums_rows, key=lambda row: cast(str, row["path"]).encode("utf-8"))
    ).encode("ascii")
    sums_raw = files[RESERVED_EVIDENCE_FILES[2]][0]
    if sums_raw != expected_sums:
        raise _fail()
    seal_raw = files[RESERVED_EVIDENCE_FILES[1]][0]
    if expected_seal_file_sha256 is not None and _digest_bytes(seal_raw) != _digest(
        expected_seal_file_sha256
    ):
        raise _fail()
    seal = _verify_bound(
        _load_canonical_json(seal_raw),
        {
            "apiVersion",
            "authority",
            "evidenceManifestSha256",
            "kind",
            "memberCount",
            "schemaVersion",
            "sha256sumsSha256",
            "totalBytes",
        },
        field="sealDigest",
    )
    if (
        seal["apiVersion"] != API_VERSION
        or seal["kind"] != SEAL_KIND
        or type(seal["schemaVersion"]) is not int
        or seal["schemaVersion"] != SCHEMA_VERSION
        or not _authority_equal(seal["authority"], authority_value)
        or type(seal["memberCount"]) is not int
        or seal["memberCount"] != len(rows)
        or type(seal["totalBytes"]) is not int
        or seal["totalBytes"] != total
        or seal["evidenceManifestSha256"] != _digest_bytes(manifest_raw)
        or seal["sha256sumsSha256"] != _digest_bytes(sums_raw)
    ):
        raise _fail()
    return dict(seal)


def schema_document() -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "evidenceMembers": list(EVIDENCE_MEMBERS),
        "kind": SCHEMA_KIND,
        "limits": {
            "maximumEvidenceMemberBytes": MAX_EVIDENCE_MEMBER_BYTES,
            "maximumEvidenceTotalBytes": MAX_EVIDENCE_TOTAL_BYTES,
            "maximumInventoryItems": MAX_INVENTORY_ITEMS,
            "maximumProcessPolls": MAX_PROCESS_POLLS,
            "maximumQmpPolls": MAX_QMP_POLLS,
            "maximumQmpResponseBytes": MAX_QMP_RESPONSE_BYTES,
            "shutdownTimeoutSeconds": SHUTDOWN_TIMEOUT_SECONDS,
        },
        "qmpCommands": list(QMP_COMMANDS),
        "schemaVersion": SCHEMA_VERSION,
    }


def _read_private_json(path: Path, expected_sha256: str) -> dict[str, object]:
    _reject_protected_path(path)
    directory_descriptor = -1
    try:
        directory_descriptor, root_info = _open_private_directory(path.parent)
        raw, _info = _safe_member_at(
            path.name,
            directory_descriptor=directory_descriptor,
            root_info=root_info,
        )
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
    if _digest_bytes(raw) != _digest(expected_sha256):
        raise _fail()
    return _load_canonical_json(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="print the non-mutating QH4 schema")
    for name in ("seal", "replay"):
        command = commands.add_parser(name, help=f"{name} exact metadata-safe QH4 evidence")
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--authority", type=Path, required=True)
        command.add_argument("--expect-authority-sha256", required=True)
        if name == "replay":
            command.add_argument("--expect-seal-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = _parser().parse_args(argv)
    try:
        if namespace.command == "schema":
            sys.stdout.buffer.write(canonical_json_bytes(schema_document()))
            return 0
        authority = _authority(
            _read_private_json(namespace.authority, namespace.expect_authority_sha256)
        )
        if namespace.command == "seal":
            sys.stdout.buffer.write(canonical_json_bytes(seal_evidence(namespace.root, authority)))
            return 0
        if namespace.command == "replay":
            sys.stdout.buffer.write(
                canonical_json_bytes(
                    replay_evidence(
                        namespace.root,
                        authority,
                        expected_seal_file_sha256=namespace.expect_seal_sha256,
                    )
                )
            )
            return 0
        raise _fail()
    except (OSError, TypeError, ValueError, TeardownPolicyError) as exc:
        if isinstance(exc, TeardownPolicyError):
            raise
        raise _fail() from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TeardownPolicyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
