"""Digest-bound strict-CRI host preparation and exact rollback journals."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from ae.kctl.profile_host_helper import (
    HelperAuthorityFiles,
    HelperOperation,
    HelperRequest,
    HelperResponse,
    ProfileHostHelperError,
    helper_implementation_digest,
    read_protocol_message,
    write_protocol_message,
)
from ae.kctl.profile_host_plan import (
    ProfileHostPlanError,
    StrictCRIActionMaterial,
    StrictCRIHostAuthorities,
    canonical_json_bytes,
    load_strict_cri_host_authorities,
    strict_cri_action_materials,
)

API_VERSION = "k1s.dev/v1alpha1"
RECEIPT_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1
MAX_PRIVATE_STATE_BYTES = 2 * 1024 * 1024
QUALIFICATION_POSTURE = "source-validated/unqualified"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TRANSACTION_RE = re.compile(r"ht1-[0-9a-f]{32}")
_IDENTIFIER_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

TransactionStatus = Literal["prepared", "already-prepared", "rolled-back", "already-rolled-back"]


class ProfileHostTransactionError(RuntimeError):
    """The private transaction authority is absent, divergent, or unsafe."""


def _fail() -> ProfileHostTransactionError:
    return ProfileHostTransactionError("strict CRI host transaction failed closed")


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest_value(value: object) -> str:
    return _digest_bytes(canonical_json_bytes(value))


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _require_transaction_id(value: object) -> str:
    if type(value) is not str or _TRANSACTION_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _require_identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _is_prefix(observed: list[str | None], expected: list[str]) -> bool:
    seen_empty = False
    for actual, wanted in zip(observed, expected, strict=True):
        if actual is None:
            seen_empty = True
        elif seen_empty or actual != wanted:
            return False
    return True


def _is_suffix(observed: list[str | None], expected: list[str]) -> bool:
    return _is_prefix(list(reversed(observed)), list(reversed(expected)))


def _canonical_absolute(value: object) -> Path:
    if type(value) is not str or not value.startswith("/") or value == "/":
        raise _fail()
    if value.startswith("//") or value.endswith("/") or "\\" in value or "\x00" in value:
        raise _fail()
    if any(part in {"", ".", ".."} for part in value.split("/")[1:]):
        raise _fail()
    if str(PurePosixPath(value)) != value:
        raise _fail()
    return Path(value)


class HostHelperClient(Protocol):
    def execute(self, request: HelperRequest) -> HelperResponse: ...


@dataclass(slots=True)
class PipeHostHelperClient:
    """A bounded protocol client; it never launches or authenticates its peer."""

    request_fd: int = field(repr=False)
    response_fd: int = field(repr=False)

    def execute(self, request: HelperRequest) -> HelperResponse:
        write_protocol_message(self.request_fd, request.to_payload())
        response = HelperResponse.from_payload(read_protocol_message(self.response_fd))
        if (
            response.operation != request.operation
            or response.transaction_id != request.transaction_id
            or response.target_id != request.target_id
            or response.action_index != request.action_index
            or response.helper_identity_digest != request.helper_identity_digest
        ):
            raise _fail()
        return response


@dataclass(frozen=True, slots=True)
class StrictCRIHostReceipt:
    status: Literal["applied", "rolled-back"]
    transaction_id: str
    target_id: str
    plan_digest: str
    plan_file_digest: str
    intent_digest: str
    facts_digest: str
    helper_identity_digest: str
    action_observation_digests: tuple[str, ...]
    original_applied_receipt_digest: str | None
    receipt_digest: str

    def __post_init__(self) -> None:
        if self.status not in {"applied", "rolled-back"}:
            raise _fail()
        _require_transaction_id(self.transaction_id)
        _require_identifier(self.target_id)
        for value in (
            self.plan_digest,
            self.plan_file_digest,
            self.intent_digest,
            self.facts_digest,
            self.helper_identity_digest,
            self.receipt_digest,
            *self.action_observation_digests,
        ):
            _require_digest(value)
        if self.status == "applied" and self.original_applied_receipt_digest is not None:
            raise _fail()
        if self.status == "rolled-back":
            _require_digest(self.original_applied_receipt_digest)
        if self.receipt_digest != _digest_value(self._payload(include_digest=False)):
            raise _fail()

    def _payload(self, *, include_digest: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "actionObservationDigests": list(self.action_observation_digests),
            "apiVersion": API_VERSION,
            "factsDigest": self.facts_digest,
            "helperIdentitySha256": self.helper_identity_digest,
            "intentDigest": self.intent_digest,
            "kind": "StrictCRIHostPreparationReceipt",
            "originalAppliedReceiptDigest": self.original_applied_receipt_digest,
            "planDigest": self.plan_digest,
            "planFileDigest": self.plan_file_digest,
            "schemaVersion": RECEIPT_SCHEMA_VERSION,
            "status": self.status,
            "targetId": self.target_id,
            "transactionId": self.transaction_id,
        }
        if include_digest:
            payload["receiptDigest"] = self.receipt_digest
        return payload

    def to_payload(self) -> dict[str, object]:
        return self._payload(include_digest=True)

    @classmethod
    def create_applied(
        cls,
        *,
        transaction_id: str,
        target_id: str,
        authorities: StrictCRIHostAuthorities,
        helper_identity_digest: str,
        observations: tuple[str, ...],
    ) -> StrictCRIHostReceipt:
        base = {
            "actionObservationDigests": list(observations),
            "apiVersion": API_VERSION,
            "factsDigest": authorities.facts_digest,
            "helperIdentitySha256": helper_identity_digest,
            "intentDigest": authorities.intent_digest,
            "kind": "StrictCRIHostPreparationReceipt",
            "originalAppliedReceiptDigest": None,
            "planDigest": authorities.plan_digest,
            "planFileDigest": authorities.plan_file_digest,
            "schemaVersion": RECEIPT_SCHEMA_VERSION,
            "status": "applied",
            "targetId": target_id,
            "transactionId": transaction_id,
        }
        return cls(
            status="applied",
            transaction_id=transaction_id,
            target_id=target_id,
            plan_digest=authorities.plan_digest,
            plan_file_digest=authorities.plan_file_digest,
            intent_digest=authorities.intent_digest,
            facts_digest=authorities.facts_digest,
            helper_identity_digest=helper_identity_digest,
            action_observation_digests=observations,
            original_applied_receipt_digest=None,
            receipt_digest=_digest_value(base),
        )

    @classmethod
    def create_rolled_back(
        cls,
        *,
        applied: StrictCRIHostReceipt,
        observations: tuple[str, ...],
    ) -> StrictCRIHostReceipt:
        base = {
            "actionObservationDigests": list(observations),
            "apiVersion": API_VERSION,
            "factsDigest": applied.facts_digest,
            "helperIdentitySha256": applied.helper_identity_digest,
            "intentDigest": applied.intent_digest,
            "kind": "StrictCRIHostPreparationReceipt",
            "originalAppliedReceiptDigest": applied.receipt_digest,
            "planDigest": applied.plan_digest,
            "planFileDigest": applied.plan_file_digest,
            "schemaVersion": RECEIPT_SCHEMA_VERSION,
            "status": "rolled-back",
            "targetId": applied.target_id,
            "transactionId": applied.transaction_id,
        }
        return cls(
            status="rolled-back",
            transaction_id=applied.transaction_id,
            target_id=applied.target_id,
            plan_digest=applied.plan_digest,
            plan_file_digest=applied.plan_file_digest,
            intent_digest=applied.intent_digest,
            facts_digest=applied.facts_digest,
            helper_identity_digest=applied.helper_identity_digest,
            action_observation_digests=observations,
            original_applied_receipt_digest=applied.receipt_digest,
            receipt_digest=_digest_value(base),
        )

    @classmethod
    def from_payload(cls, value: object) -> StrictCRIHostReceipt:
        if type(value) is not dict or set(value) != {
            "actionObservationDigests",
            "apiVersion",
            "factsDigest",
            "helperIdentitySha256",
            "intentDigest",
            "kind",
            "originalAppliedReceiptDigest",
            "planDigest",
            "planFileDigest",
            "receiptDigest",
            "schemaVersion",
            "status",
            "targetId",
            "transactionId",
        }:
            raise _fail()
        if (
            value["apiVersion"] != API_VERSION
            or value["kind"] != "StrictCRIHostPreparationReceipt"
            or value["schemaVersion"] != RECEIPT_SCHEMA_VERSION
        ):
            raise _fail()
        observations = value["actionObservationDigests"]
        if type(observations) is not list or not all(type(item) is str for item in observations):
            raise _fail()
        status = value["status"]
        if status not in {"applied", "rolled-back"}:
            raise _fail()
        return cls(
            status=status,
            transaction_id=_require_transaction_id(value["transactionId"]),
            target_id=_require_identifier(value["targetId"]),
            plan_digest=_require_digest(value["planDigest"]),
            plan_file_digest=_require_digest(value["planFileDigest"]),
            intent_digest=_require_digest(value["intentDigest"]),
            facts_digest=_require_digest(value["factsDigest"]),
            helper_identity_digest=_require_digest(value["helperIdentitySha256"]),
            action_observation_digests=tuple(_require_digest(item) for item in observations),
            original_applied_receipt_digest=(
                None
                if value["originalAppliedReceiptDigest"] is None
                else _require_digest(value["originalAppliedReceiptDigest"])
            ),
            receipt_digest=_require_digest(value["receiptDigest"]),
        )


@dataclass(frozen=True, slots=True)
class HostTransactionResult:
    action: Literal["prepare", "rollback"]
    status: TransactionStatus
    transaction_id: str
    target_id: str
    plan_digest: str
    receipt_digest: str
    applied_receipt_digest: str

    def public_payload(self) -> dict[str, object]:
        return {
            "action": self.action,
            "apiVersion": API_VERSION,
            "appliedReceiptDigest": self.applied_receipt_digest,
            "kind": "StrictCRIHostTransactionResult",
            "mutating": True,
            "planDigest": self.plan_digest,
            "qualificationPosture": QUALIFICATION_POSTURE,
            "receiptDigest": self.receipt_digest,
            "schemaVersion": 1,
            "status": self.status,
            "targetId": self.target_id,
            "transactionId": self.transaction_id,
            "warnings": ["Live CRI/containerd enforcement remains unqualified until Q1."],
        }


class _TransactionLock(AbstractContextManager["_TransactionLock"]):
    def __init__(self, root: Path, *, owner_uid: int) -> None:
        self.root = root
        self.owner_uid = owner_uid
        self.fd = -1
        self.root_fd = -1

    def __enter__(self) -> _TransactionLock:
        try:
            self.root_fd = _open_private_directory(self.root, owner_uid=self.owner_uid)
            root_info = os.fstat(self.root_fd)
            flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                self.fd = os.open(
                    "host.lock",
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=self.root_fd,
                )
                os.fsync(self.root_fd)
            except FileExistsError:
                self.fd = os.open("host.lock", flags, dir_fd=self.root_fd)
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_dev != root_info.st_dev
            ):
                raise _fail()
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.__exit__(None, None, None)
            raise _fail() from exc
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1


class StrictCRIHostTransactionManager:
    def __init__(
        self,
        helper: HostHelperClient,
        *,
        owner_uid: int | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        crash_hook: Callable[[str, int | None], None] | None = None,
    ) -> None:
        self.helper = helper
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self.random_bytes = random_bytes
        self.crash_hook = crash_hook or (lambda _phase, _index: None)

    def prepare(
        self,
        *,
        intent_path: Path,
        expected_intent_digest: str,
        facts_path: Path,
        expected_facts_digest: str,
        plan_path: Path,
        expected_plan_file_digest: str,
        expected_plan_digest: str,
        transaction_root: Path,
        transaction_id: str,
        confirmation: str,
        expected_helper_identity_digest: str,
    ) -> HostTransactionResult:
        authorities = self._authorities(
            intent_path=intent_path,
            expected_intent_digest=expected_intent_digest,
            facts_path=facts_path,
            expected_facts_digest=expected_facts_digest,
            plan_path=plan_path,
            expected_plan_file_digest=expected_plan_file_digest,
            expected_plan_digest=expected_plan_digest,
        )
        materials = strict_cri_action_materials(authorities)
        target_id = _require_identifier(authorities.plan["target"]["targetId"])
        if confirmation != f"{target_id}:{authorities.plan_digest}":
            raise _fail()
        helper_identity = _require_digest(expected_helper_identity_digest)
        if helper_identity != helper_implementation_digest():
            raise _fail()
        root = _canonical_absolute(str(transaction_root))
        txid = _require_transaction_id(transaction_id)
        with _TransactionLock(root, owner_uid=self.owner_uid):
            layout = self._ensure_layout(root, txid)
            receipts = root / "receipts"
            applied_path = receipts / f"{txid}.applied.json"
            rollback_path = receipts / f"{txid}.rollback.json"
            if _read_optional_json(rollback_path, owner_uid=self.owner_uid) is not None:
                raise _fail()
            journal_path = layout / "journal.json"
            authority_dir = layout / "authorities"
            authority_paths = self._persist_authorities(authority_dir, authorities)
            journal = _read_optional_json(journal_path, owner_uid=self.owner_uid)
            if journal is None:
                journal = self._new_journal(authorities, target_id, txid, helper_identity)
                _atomic_json_write(
                    journal_path, journal, owner_uid=self.owner_uid, random_bytes=self.random_bytes
                )
                self.crash_hook("planned", None)
            self._match_journal(journal, authorities, target_id, txid, helper_identity)
            authority_files = self._helper_authorities(*authority_paths, authorities)
            snapshots = self._digest_list(journal, "snapshotDigests", len(materials))
            applied = self._digest_list(journal, "appliedDigests", len(materials))
            existing = _read_optional_json(applied_path, owner_uid=self.owner_uid)
            self._validate_prepare_progress(
                journal,
                materials,
                snapshots=snapshots,
                applied=applied,
                receipt_present=existing is not None,
            )
            if existing is not None:
                receipt = StrictCRIHostReceipt.from_payload(existing)
                self._match_receipt(receipt, authorities, target_id, helper_identity)
                expected = tuple(material.desired_after_digest for material in materials)
                if tuple(applied) != expected or receipt.action_observation_digests != expected:
                    raise _fail()
                if journal.get("phase") not in {"applying", "applied"}:
                    raise _fail()
                if (
                    journal.get("phase") == "applied"
                    and journal.get("appliedReceiptDigest") != receipt.receipt_digest
                ):
                    raise _fail()
                journal["phase"] = "applied"
                journal["appliedReceiptDigest"] = receipt.receipt_digest
                _atomic_json_write(
                    journal_path,
                    journal,
                    owner_uid=self.owner_uid,
                    random_bytes=self.random_bytes,
                )
                return self._result("prepare", "already-prepared", receipt, receipt)
            if journal.get("phase") in {"planned", "snapshotting"}:
                if journal.get("phase") == "planned":
                    journal["phase"] = "snapshotting"
                    _atomic_json_write(
                        journal_path,
                        journal,
                        owner_uid=self.owner_uid,
                        random_bytes=self.random_bytes,
                    )
                    self.crash_hook("snapshotting", None)
                for index, material in enumerate(materials):
                    if snapshots[index] is None:
                        response = self._helper_request(
                            operation="snapshot",
                            txid=txid,
                            target_id=target_id,
                            index=index,
                            authorities=authority_files,
                            root=root,
                            material=material,
                        )
                        snapshots[index] = response.observation_digest
                        journal["snapshotDigests"] = snapshots
                        _atomic_json_write(
                            journal_path,
                            journal,
                            owner_uid=self.owner_uid,
                            random_bytes=self.random_bytes,
                        )
                        self.crash_hook("snapshot", index)
                journal["phase"] = "snapshotted"
                _atomic_json_write(
                    journal_path,
                    journal,
                    owner_uid=self.owner_uid,
                    random_bytes=self.random_bytes,
                )
                self.crash_hook("snapshotted", None)
            if journal.get("phase") == "snapshotted":
                journal["phase"] = "applying"
                _atomic_json_write(
                    journal_path,
                    journal,
                    owner_uid=self.owner_uid,
                    random_bytes=self.random_bytes,
                )
                self.crash_hook("applying", None)
            for index, material in enumerate(materials):
                if applied[index] is None:
                    response = self._helper_request(
                        operation="apply",
                        txid=txid,
                        target_id=target_id,
                        index=index,
                        authorities=authority_files,
                        root=root,
                        material=material,
                    )
                    if response.observation_digest != material.desired_after_digest:
                        raise _fail()
                    applied[index] = response.observation_digest
                    journal["phase"] = "applying"
                    journal["appliedDigests"] = applied
                    _atomic_json_write(
                        journal_path,
                        journal,
                        owner_uid=self.owner_uid,
                        random_bytes=self.random_bytes,
                    )
                    self.crash_hook("apply", index)
            receipt = StrictCRIHostReceipt.create_applied(
                transaction_id=txid,
                target_id=target_id,
                authorities=authorities,
                helper_identity_digest=helper_identity,
                observations=tuple(_require_digest(item) for item in applied),
            )
            _atomic_json_write(
                applied_path,
                receipt.to_payload(),
                owner_uid=self.owner_uid,
                random_bytes=self.random_bytes,
            )
            self.crash_hook("applied-receipt", None)
            journal["phase"] = "applied"
            journal["appliedReceiptDigest"] = receipt.receipt_digest
            _atomic_json_write(
                journal_path, journal, owner_uid=self.owner_uid, random_bytes=self.random_bytes
            )
            return self._result("prepare", "prepared", receipt, receipt)

    def rollback(
        self,
        *,
        transaction_root: Path,
        transaction_id: str,
        expected_applied_receipt_digest: str,
        confirmation: str,
        expected_helper_identity_digest: str,
    ) -> HostTransactionResult:
        txid = _require_transaction_id(transaction_id)
        applied_digest = _require_digest(expected_applied_receipt_digest)
        helper_identity = _require_digest(expected_helper_identity_digest)
        if helper_identity != helper_implementation_digest():
            raise _fail()
        root = _canonical_absolute(str(transaction_root))
        with _TransactionLock(root, owner_uid=self.owner_uid):
            layout = self._layout(root, txid)
            journal_path = layout / "journal.json"
            journal = _read_json(journal_path, owner_uid=self.owner_uid)
            authority_dir = layout / "authorities"
            _validate_private_directory(authority_dir, owner_uid=self.owner_uid)
            authorities = self._authorities(
                intent_path=authority_dir / "intent.json",
                expected_intent_digest=_require_digest(journal.get("intentDigest")),
                facts_path=authority_dir / "facts.json",
                expected_facts_digest=_require_digest(journal.get("factsDigest")),
                plan_path=authority_dir / "plan.json",
                expected_plan_file_digest=_require_digest(journal.get("planFileDigest")),
                expected_plan_digest=_require_digest(journal.get("planDigest")),
            )
            target_id = _require_identifier(authorities.plan["target"]["targetId"])
            if confirmation != f"{target_id}:{txid}:{applied_digest}":
                raise _fail()
            materials = strict_cri_action_materials(authorities)
            receipts = root / "receipts"
            _validate_private_directory(receipts, owner_uid=self.owner_uid)
            applied = load_strict_cri_host_receipt(
                receipts / f"{txid}.applied.json",
                expected_digest=applied_digest,
                owner_uid=self.owner_uid,
            )
            self._match_receipt(applied, authorities, target_id, helper_identity)
            rolled_path = receipts / f"{txid}.rollback.json"
            existing = _read_optional_json(rolled_path, owner_uid=self.owner_uid)
            if existing is not None:
                rolled = StrictCRIHostReceipt.from_payload(existing)
                self._match_rolled_receipt(rolled, applied, authorities, target_id)
                expected_rollback = tuple(
                    material.desired_after_digest
                    if material.rollback == "none"
                    else material.expected_before_digest
                    for material in materials
                )
                rollback_digests = self._digest_list(journal, "rollbackDigests", len(materials))
                if (
                    tuple(rollback_digests) != expected_rollback
                    or rolled.action_observation_digests != expected_rollback
                    or journal.get("phase") not in {"rolling-back", "rolled-back"}
                ):
                    raise _fail()
                if (
                    journal.get("phase") == "rolled-back"
                    and journal.get("rollbackReceiptDigest") != rolled.receipt_digest
                ):
                    raise _fail()
                journal["phase"] = "rolled-back"
                journal["rollbackReceiptDigest"] = rolled.receipt_digest
                _atomic_json_write(
                    journal_path,
                    journal,
                    owner_uid=self.owner_uid,
                    random_bytes=self.random_bytes,
                )
                return self._result("rollback", "already-rolled-back", rolled, applied)
            self._match_journal(journal, authorities, target_id, txid, helper_identity)
            if journal.get("phase") not in {"applied", "rolling-back"}:
                raise _fail()
            authority_files = self._helper_authorities(
                authority_dir / "intent.json",
                authority_dir / "facts.json",
                authority_dir / "plan.json",
                authorities,
            )
            rollback_digests = self._digest_list(journal, "rollbackDigests", len(materials))
            snapshots = self._digest_list(journal, "snapshotDigests", len(materials))
            applied_digests = self._digest_list(journal, "appliedDigests", len(materials))
            self._validate_rollback_progress(
                journal,
                materials,
                snapshots=snapshots,
                applied=applied_digests,
                rollback_digests=rollback_digests,
                receipt_present=existing is not None,
            )
            if journal.get("phase") == "applied":
                journal["phase"] = "rolling-back"
                _atomic_json_write(
                    journal_path,
                    journal,
                    owner_uid=self.owner_uid,
                    random_bytes=self.random_bytes,
                )
                self.crash_hook("rolling-back", None)
            for index in reversed(range(len(materials))):
                material = materials[index]
                if material.rollback == "none":
                    if rollback_digests[index] is None:
                        rollback_digests[index] = material.desired_after_digest
                    continue
                if rollback_digests[index] is None:
                    response = self._helper_request(
                        operation="rollback",
                        txid=txid,
                        target_id=target_id,
                        index=index,
                        authorities=authority_files,
                        root=root,
                        material=material,
                        rollback_authorization=applied.receipt_digest,
                    )
                    if response.observation_digest != material.expected_before_digest:
                        raise _fail()
                    rollback_digests[index] = response.observation_digest
                    journal["phase"] = "rolling-back"
                    journal["rollbackDigests"] = rollback_digests
                    _atomic_json_write(
                        journal_path,
                        journal,
                        owner_uid=self.owner_uid,
                        random_bytes=self.random_bytes,
                    )
                    self.crash_hook("rollback", index)
            journal["rollbackDigests"] = rollback_digests
            _atomic_json_write(
                journal_path,
                journal,
                owner_uid=self.owner_uid,
                random_bytes=self.random_bytes,
            )
            self.crash_hook("rollback-complete", None)
            rolled = StrictCRIHostReceipt.create_rolled_back(
                applied=applied,
                observations=tuple(_require_digest(item) for item in rollback_digests),
            )
            _atomic_json_write(
                rolled_path,
                rolled.to_payload(),
                owner_uid=self.owner_uid,
                random_bytes=self.random_bytes,
            )
            self.crash_hook("rollback-receipt", None)
            journal["phase"] = "rolled-back"
            journal["rollbackReceiptDigest"] = rolled.receipt_digest
            _atomic_json_write(
                journal_path, journal, owner_uid=self.owner_uid, random_bytes=self.random_bytes
            )
            return self._result("rollback", "rolled-back", rolled, applied)

    @staticmethod
    def _authorities(**kwargs: object) -> StrictCRIHostAuthorities:
        try:
            for field_name in ("intent_path", "facts_path", "plan_path"):
                value = kwargs.get(field_name)
                if isinstance(value, Path):
                    kwargs[field_name] = str(value)
            return load_strict_cri_host_authorities(**kwargs)
        except (OSError, ProfileHostPlanError, TypeError, ValueError) as exc:
            raise _fail() from exc

    def _ensure_layout(self, root: Path, txid: str) -> Path:
        transactions = root / "transactions"
        _ensure_private_directory(transactions, owner_uid=self.owner_uid)
        _ensure_private_directory(root / "receipts", owner_uid=self.owner_uid)
        layout = transactions / txid
        _ensure_private_directory(layout, owner_uid=self.owner_uid)
        _ensure_private_directory(layout / "backups", owner_uid=self.owner_uid)
        return layout

    def _layout(self, root: Path, txid: str) -> Path:
        _validate_private_directory(root, owner_uid=self.owner_uid)
        transactions = root / "transactions"
        _validate_private_directory(transactions, owner_uid=self.owner_uid)
        _validate_private_directory(root / "receipts", owner_uid=self.owner_uid)
        layout = transactions / txid
        _validate_private_directory(layout, owner_uid=self.owner_uid)
        _validate_private_directory(layout / "backups", owner_uid=self.owner_uid)
        return layout

    def _persist_authorities(
        self,
        authority_dir: Path,
        authorities: StrictCRIHostAuthorities,
    ) -> tuple[Path, Path, Path]:
        _ensure_private_directory(authority_dir, owner_uid=self.owner_uid)
        values = (
            (authority_dir / "intent.json", authorities.intent),
            (authority_dir / "facts.json", authorities.facts),
            (authority_dir / "plan.json", authorities.plan),
        )
        for path, payload in values:
            existing = _read_optional_json(path, owner_uid=self.owner_uid)
            if existing is None:
                _atomic_json_write(
                    path,
                    payload,
                    owner_uid=self.owner_uid,
                    random_bytes=self.random_bytes,
                )
            elif existing != payload:
                raise _fail()
        return values[0][0], values[1][0], values[2][0]

    @staticmethod
    def _new_journal(
        authorities: StrictCRIHostAuthorities,
        target_id: str,
        txid: str,
        helper_identity: str,
    ) -> dict[str, object]:
        count = len(strict_cri_action_materials(authorities))
        return {
            "apiVersion": API_VERSION,
            "appliedDigests": [None] * count,
            "appliedReceiptDigest": None,
            "factsDigest": authorities.facts_digest,
            "helperIdentitySha256": helper_identity,
            "intentDigest": authorities.intent_digest,
            "kind": "StrictCRIHostTransactionJournal",
            "phase": "planned",
            "planDigest": authorities.plan_digest,
            "planFileDigest": authorities.plan_file_digest,
            "rollbackDigests": [None] * count,
            "rollbackReceiptDigest": None,
            "schemaVersion": JOURNAL_SCHEMA_VERSION,
            "snapshotDigests": [None] * count,
            "targetId": target_id,
            "transactionId": txid,
        }

    @staticmethod
    def _match_journal(
        journal: dict[str, object],
        authorities: StrictCRIHostAuthorities,
        target_id: str,
        txid: str,
        helper_identity: str,
    ) -> None:
        expected_keys = {
            "apiVersion",
            "appliedDigests",
            "appliedReceiptDigest",
            "factsDigest",
            "helperIdentitySha256",
            "intentDigest",
            "kind",
            "phase",
            "planDigest",
            "planFileDigest",
            "rollbackDigests",
            "rollbackReceiptDigest",
            "schemaVersion",
            "snapshotDigests",
            "targetId",
            "transactionId",
        }
        if (
            set(journal) != expected_keys
            or journal.get("apiVersion") != API_VERSION
            or journal.get("kind") != "StrictCRIHostTransactionJournal"
            or journal.get("schemaVersion") != JOURNAL_SCHEMA_VERSION
            or journal.get("transactionId") != txid
            or journal.get("targetId") != target_id
            or journal.get("planDigest") != authorities.plan_digest
            or journal.get("planFileDigest") != authorities.plan_file_digest
            or journal.get("intentDigest") != authorities.intent_digest
            or journal.get("factsDigest") != authorities.facts_digest
            or journal.get("helperIdentitySha256") != helper_identity
            or journal.get("phase")
            not in {
                "planned",
                "snapshotting",
                "snapshotted",
                "applying",
                "applied",
                "rolling-back",
                "rolled-back",
            }
        ):
            raise _fail()

    @staticmethod
    def _match_receipt(
        receipt: StrictCRIHostReceipt,
        authorities: StrictCRIHostAuthorities,
        target_id: str,
        helper_identity: str,
    ) -> None:
        if (
            receipt.status != "applied"
            or receipt.target_id != target_id
            or receipt.plan_digest != authorities.plan_digest
            or receipt.plan_file_digest != authorities.plan_file_digest
            or receipt.intent_digest != authorities.intent_digest
            or receipt.facts_digest != authorities.facts_digest
            or receipt.helper_identity_digest != helper_identity
        ):
            raise _fail()

    @staticmethod
    def _match_rolled_receipt(
        rolled: StrictCRIHostReceipt,
        applied: StrictCRIHostReceipt,
        authorities: StrictCRIHostAuthorities,
        target_id: str,
    ) -> None:
        if (
            rolled.status != "rolled-back"
            or rolled.transaction_id != applied.transaction_id
            or rolled.target_id != target_id
            or rolled.plan_digest != authorities.plan_digest
            or rolled.plan_file_digest != authorities.plan_file_digest
            or rolled.intent_digest != authorities.intent_digest
            or rolled.facts_digest != authorities.facts_digest
            or rolled.helper_identity_digest != applied.helper_identity_digest
            or rolled.original_applied_receipt_digest != applied.receipt_digest
        ):
            raise _fail()

    @staticmethod
    def _validate_prepare_progress(
        journal: dict[str, object],
        materials: tuple[StrictCRIActionMaterial, ...],
        *,
        snapshots: list[str | None],
        applied: list[str | None],
        receipt_present: bool,
    ) -> None:
        expected_snapshots = [material.expected_before_digest for material in materials]
        expected_applied = [material.desired_after_digest for material in materials]
        if not _is_prefix(snapshots, expected_snapshots) or not _is_prefix(
            applied, expected_applied
        ):
            raise _fail()
        snapshot_count = sum(item is not None for item in snapshots)
        applied_count = sum(item is not None for item in applied)
        phase = journal.get("phase")
        valid = (
            (phase == "planned" and snapshot_count == 0 and applied_count == 0)
            or (
                phase == "snapshotting"
                and 0 <= snapshot_count <= len(materials)
                and applied_count == 0
            )
            or (phase == "snapshotted" and snapshot_count == len(materials) and applied_count == 0)
            or (
                phase == "applying"
                and snapshot_count == len(materials)
                and 0 <= applied_count <= len(materials)
            )
            or (
                phase == "applied"
                and snapshot_count == len(materials)
                and applied_count == len(materials)
                and receipt_present
            )
        )
        if not valid:
            raise _fail()
        if phase == "applied":
            if not receipt_present or journal.get("appliedReceiptDigest") is None:
                raise _fail()
        elif journal.get("appliedReceiptDigest") is not None:
            raise _fail()
        rollback_value = journal.get("rollbackDigests")
        if type(rollback_value) is not list:
            raise _fail()
        if (
            any(item is not None for item in rollback_value)
            or journal.get("rollbackReceiptDigest") is not None
        ):
            raise _fail()

    @staticmethod
    def _validate_rollback_progress(
        journal: dict[str, object],
        materials: tuple[StrictCRIActionMaterial, ...],
        *,
        snapshots: list[str | None],
        applied: list[str | None],
        rollback_digests: list[str | None],
        receipt_present: bool,
    ) -> None:
        expected_snapshots = [material.expected_before_digest for material in materials]
        expected_applied = [material.desired_after_digest for material in materials]
        if snapshots != expected_snapshots or applied != expected_applied:
            raise _fail()
        expected = [
            material.desired_after_digest
            if material.rollback == "none"
            else material.expected_before_digest
            for material in materials
        ]
        if not _is_suffix(rollback_digests, expected):
            raise _fail()
        completed = sum(item is not None for item in rollback_digests)
        phase = journal.get("phase")
        valid = (
            (phase == "applied" and completed == 0)
            or (phase == "rolling-back" and 0 <= completed <= len(materials))
            or (phase == "rolled-back" and completed == len(materials) and receipt_present)
        )
        if not valid or journal.get("appliedReceiptDigest") is None:
            raise _fail()
        if phase == "rolled-back":
            if not receipt_present or journal.get("rollbackReceiptDigest") is None:
                raise _fail()
        elif journal.get("rollbackReceiptDigest") is not None:
            raise _fail()

    @staticmethod
    def _digest_list(journal: dict[str, object], field_name: str, count: int) -> list[str | None]:
        value = journal.get(field_name)
        if (
            type(value) is not list
            or len(value) != count
            or any(
                item is not None and (type(item) is not str or _DIGEST_RE.fullmatch(item) is None)
                for item in value
            )
        ):
            raise _fail()
        return list(value)

    @staticmethod
    def _helper_authorities(
        intent_path: Path,
        facts_path: Path,
        plan_path: Path,
        authorities: StrictCRIHostAuthorities,
    ) -> HelperAuthorityFiles:
        return HelperAuthorityFiles(
            intent_path=intent_path,
            facts_path=facts_path,
            plan_path=plan_path,
            intent_digest=authorities.intent_digest,
            facts_digest=authorities.facts_digest,
            plan_file_digest=authorities.plan_file_digest,
            plan_digest=authorities.plan_digest,
        )

    def _helper_request(
        self,
        *,
        operation: HelperOperation,
        txid: str,
        target_id: str,
        index: int,
        authorities: HelperAuthorityFiles,
        root: Path,
        material: StrictCRIActionMaterial,
        rollback_authorization: str | None = None,
    ) -> HelperResponse:
        request = HelperRequest(
            operation=operation,
            transaction_id=txid,
            target_id=target_id,
            action_index=index,
            action_id=material.action_id,
            expected_before_digest=material.expected_before_digest,
            desired_after_digest=material.desired_after_digest,
            authorities=authorities,
            transaction_root=root,
            helper_identity_digest=helper_implementation_digest(),
            rollback_authorization_digest=rollback_authorization,
        )
        try:
            response = self.helper.execute(request)
        except (OSError, ProfileHostHelperError) as exc:
            raise _fail() from exc
        if (
            response.action_id != material.action_id
            or response.expected_before_digest != material.expected_before_digest
            or response.desired_after_digest != material.desired_after_digest
        ):
            raise _fail()
        return response

    @staticmethod
    def _result(
        action: Literal["prepare", "rollback"],
        status: TransactionStatus,
        receipt: StrictCRIHostReceipt,
        applied: StrictCRIHostReceipt,
    ) -> HostTransactionResult:
        return HostTransactionResult(
            action=action,
            status=status,
            transaction_id=receipt.transaction_id,
            target_id=receipt.target_id,
            plan_digest=receipt.plan_digest,
            receipt_digest=receipt.receipt_digest,
            applied_receipt_digest=applied.receipt_digest,
        )


def load_strict_cri_host_receipt(
    path: Path, *, expected_digest: str, owner_uid: int | None = None
) -> StrictCRIHostReceipt:
    owner = os.getuid() if owner_uid is None else owner_uid
    payload = _read_json(_canonical_absolute(str(path)), owner_uid=owner)
    receipt = StrictCRIHostReceipt.from_payload(payload)
    if receipt.receipt_digest != _require_digest(expected_digest):
        raise _fail()
    return receipt


def _validate_private_directory(path: Path, *, owner_uid: int) -> os.stat_result:
    fd = _open_private_directory(path, owner_uid=owner_uid)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def _open_directory_chain(path: Path) -> int:
    canonical = _canonical_absolute(str(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for part in canonical.parts[1:]:
            child = os.open(part, flags, dir_fd=current)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise _fail()
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def _open_private_directory(
    path: Path,
    *,
    owner_uid: int,
    expected_device: int | None = None,
) -> int:
    fd = -1
    try:
        fd = _open_directory_chain(path)
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or (expected_device is not None and info.st_dev != expected_device)
        ):
            raise _fail()
        return fd
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise


def _ensure_private_directory(path: Path, *, owner_uid: int) -> None:
    canonical = _canonical_absolute(str(path))
    parent_fd = _open_private_directory(canonical.parent, owner_uid=owner_uid)
    try:
        parent_info = os.fstat(parent_fd)
        try:
            os.mkdir(canonical.name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        child_fd = os.open(
            canonical.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            child = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(child.st_mode)
                or child.st_uid != owner_uid
                or stat.S_IMODE(child.st_mode) != 0o700
                or child.st_dev != parent_info.st_dev
            ):
                raise _fail()
        finally:
            os.close(child_fd)
    except (OSError, ValueError) as exc:
        raise _fail() from exc
    finally:
        os.close(parent_fd)


def _read_optional_json(path: Path, *, owner_uid: int) -> dict[str, object] | None:
    try:
        return _read_json(path, owner_uid=owner_uid)
    except FileNotFoundError:
        return None


def _read_json(path: Path, *, owner_uid: int) -> dict[str, object]:
    canonical = _canonical_absolute(str(path))
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    parent_fd = -1
    fd = -1
    raw = b""
    try:
        parent_fd = _open_private_directory(canonical.parent, owner_uid=owner_uid)
        parent_info = os.fstat(parent_fd)
        before = os.stat(canonical.name, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(canonical.name, flags, dir_fd=parent_fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
            or info.st_dev != parent_info.st_dev
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 1 <= info.st_size <= MAX_PRIVATE_STATE_BYTES
        ):
            raise _fail()
        raw = b""
        while len(raw) < info.st_size:
            chunk = os.read(fd, info.st_size - len(raw))
            if not chunk:
                raise _fail()
            raw += chunk
        if os.read(fd, 1):
            raise _fail()
        after = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise _fail()
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise _fail() from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail() from exc
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise _fail()
    return value


def _atomic_json_write(
    path: Path,
    payload: dict[str, object],
    *,
    owner_uid: int,
    random_bytes: Callable[[int], bytes],
) -> None:
    canonical = _canonical_absolute(str(path))
    parent_fd = _open_private_directory(canonical.parent, owner_uid=owner_uid)
    parent_info = os.fstat(parent_fd)
    existing = _optional_private_file_identity(
        parent_fd,
        canonical.name,
        owner_uid=owner_uid,
        expected_device=parent_info.st_dev,
    )
    token = random_bytes(16)
    if len(token) != 16:
        os.close(parent_fd)
        raise _fail()
    temporary = f".{canonical.name}.tmp-{token.hex()}"
    raw = canonical_json_bytes(payload)
    if len(raw) > MAX_PRIVATE_STATE_BYTES:
        os.close(parent_fd)
        raise _fail()
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise _fail()
            view = view[written:]
        os.fsync(fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_dev != parent_info.st_dev
        ):
            raise _fail()
        os.close(fd)
        fd = -1
        observed = _optional_private_file_identity(
            parent_fd,
            canonical.name,
            owner_uid=owner_uid,
            expected_device=parent_info.st_dev,
        )
        if observed != existing:
            raise _fail()
        os.replace(
            temporary,
            canonical.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _optional_private_file_identity(
    parent_fd: int,
    name: str,
    *,
    owner_uid: int,
    expected_device: int,
) -> tuple[int, int, int, int, int, int] | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != owner_uid
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_dev != expected_device
    ):
        raise _fail()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_size,
        info.st_ctime_ns,
    )


def _fsync_directory(path: Path) -> None:
    fd = _open_directory_chain(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


__all__ = [
    "HostHelperClient",
    "HostTransactionResult",
    "PipeHostHelperClient",
    "ProfileHostTransactionError",
    "QUALIFICATION_POSTURE",
    "StrictCRIHostReceipt",
    "StrictCRIHostTransactionManager",
    "load_strict_cri_host_receipt",
]
