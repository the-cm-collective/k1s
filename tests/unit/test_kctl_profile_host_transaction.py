from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from ae.kctl.profile_host_helper import (
    HelperResponse,
    helper_implementation_digest,
)
from ae.kctl.profile_host_plan import strict_cri_action_materials
from ae.kctl.profile_host_transaction import (
    ProfileHostTransactionError,
    StrictCRIHostTransactionManager,
)
from tests.unit.test_kctl_profile_host_helper import _bundle


class _Helper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def execute(self, request):
        self.calls.append((request.operation, request.action_index))
        observation = {
            "snapshot": request.expected_before_digest,
            "apply": request.desired_after_digest,
            "rollback": request.expected_before_digest,
            "verify": request.desired_after_digest,
        }[request.operation]
        return HelperResponse(
            operation=request.operation,
            transaction_id=request.transaction_id,
            target_id=request.target_id,
            action_index=request.action_index,
            action_id=request.action_id,
            expected_before_digest=request.expected_before_digest,
            desired_after_digest=request.desired_after_digest,
            observation_digest=observation,
        )


class _Crash(RuntimeError):
    pass


def _fixture(tmp_path: Path):
    authorities, _request = _bundle(tmp_path)
    root = tmp_path / "private-host-state"
    root.mkdir(mode=0o700)
    txid = "ht1-" + "2" * 32
    target = authorities.plan["target"]["targetId"]
    kwargs = {
        "intent_path": tmp_path / "intent.json",
        "expected_intent_digest": authorities.intent_digest,
        "facts_path": tmp_path / "facts.json",
        "expected_facts_digest": authorities.facts_digest,
        "plan_path": tmp_path / "plan.json",
        "expected_plan_file_digest": authorities.plan_file_digest,
        "expected_plan_digest": authorities.plan_digest,
        "transaction_root": root,
        "transaction_id": txid,
        "confirmation": f"{target}:{authorities.plan_digest}",
        "expected_helper_identity_digest": helper_implementation_digest(),
    }
    return authorities, root, txid, target, kwargs


def _manager(helper: _Helper, *, crash_hook=None):
    return StrictCRIHostTransactionManager(
        helper,
        random_bytes=lambda length: b"\x8a" * length,
        crash_hook=crash_hook,
    )


def test_prepare_and_rollback_are_exact_idempotent_and_reverse_ordered(tmp_path: Path) -> None:
    authorities, root, txid, target, kwargs = _fixture(tmp_path)
    helper = _Helper()
    manager = _manager(helper)

    prepared = manager.prepare(**kwargs)
    replayed = manager.prepare(**kwargs)

    materials = strict_cri_action_materials(authorities)
    assert prepared.status == "prepared"
    assert replayed.status == "already-prepared"
    assert replayed.receipt_digest == prepared.receipt_digest
    assert helper.calls[: len(materials)] == [
        ("snapshot", index) for index in range(len(materials))
    ]
    assert helper.calls[len(materials) :] == [("apply", index) for index in range(len(materials))]

    rollback_kwargs = {
        "transaction_root": root,
        "transaction_id": txid,
        "expected_applied_receipt_digest": prepared.receipt_digest,
        "confirmation": f"{target}:{txid}:{prepared.receipt_digest}",
        "expected_helper_identity_digest": helper_implementation_digest(),
    }
    before = len(helper.calls)
    rolled = manager.rollback(**rollback_kwargs)
    replayed_rollback = manager.rollback(**rollback_kwargs)
    rollback_calls = helper.calls[before:]

    assert rolled.status == "rolled-back"
    assert replayed_rollback.status == "already-rolled-back"
    assert replayed_rollback.receipt_digest == rolled.receipt_digest
    assert [index for operation, index in rollback_calls if operation == "rollback"] == [
        material.index for material in reversed(materials) if material.rollback != "none"
    ]


@pytest.mark.parametrize("crash_phase", ["snapshotting", "applying"])
def test_prepare_recovers_zero_progress_write_ahead_phase(tmp_path: Path, crash_phase: str) -> None:
    _authorities, _root, _txid, _target, kwargs = _fixture(tmp_path)
    helper = _Helper()

    def crash(phase: str, index: int | None) -> None:
        if phase == crash_phase and index is None:
            raise _Crash(phase)

    with pytest.raises(_Crash, match=crash_phase):
        _manager(helper, crash_hook=crash).prepare(**kwargs)
    result = _manager(helper).prepare(**kwargs)
    assert result.status == "prepared"


def test_rollback_recovers_zero_progress_write_ahead_phase(tmp_path: Path) -> None:
    _authorities, root, txid, target, kwargs = _fixture(tmp_path)
    helper = _Helper()
    prepared = _manager(helper).prepare(**kwargs)
    rollback_kwargs = {
        "transaction_root": root,
        "transaction_id": txid,
        "expected_applied_receipt_digest": prepared.receipt_digest,
        "confirmation": f"{target}:{txid}:{prepared.receipt_digest}",
        "expected_helper_identity_digest": helper_implementation_digest(),
    }

    def crash(phase: str, index: int | None) -> None:
        if phase == "rolling-back" and index is None:
            raise _Crash(phase)

    with pytest.raises(_Crash, match="rolling-back"):
        _manager(helper, crash_hook=crash).rollback(**rollback_kwargs)
    result = _manager(helper).rollback(**rollback_kwargs)
    assert result.status == "rolled-back"


@pytest.mark.parametrize(
    ("phase", "index"),
    [
        ("planned", None),
        ("snapshot", 0),
        ("snapshotted", None),
        ("apply", 0),
        ("applied-receipt", None),
    ],
)
def test_prepare_crash_prefixes_recover_exactly(
    tmp_path: Path, phase: str, index: int | None
) -> None:
    _authorities, _root, _txid, _target, kwargs = _fixture(tmp_path)
    helper = _Helper()

    def crash(observed_phase: str, observed_index: int | None) -> None:
        if (observed_phase, observed_index) == (phase, index):
            raise _Crash(observed_phase)

    with pytest.raises(_Crash):
        _manager(helper, crash_hook=crash).prepare(**kwargs)
    assert _manager(helper).prepare(**kwargs).status in {"prepared", "already-prepared"}


@pytest.mark.parametrize(("phase", "index"), [("rollback", 9), ("rollback-receipt", None)])
def test_rollback_crash_suffixes_recover_exactly(
    tmp_path: Path, phase: str, index: int | None
) -> None:
    _authorities, root, txid, target, kwargs = _fixture(tmp_path)
    helper = _Helper()
    prepared = _manager(helper).prepare(**kwargs)
    rollback_kwargs = {
        "transaction_root": root,
        "transaction_id": txid,
        "expected_applied_receipt_digest": prepared.receipt_digest,
        "confirmation": f"{target}:{txid}:{prepared.receipt_digest}",
        "expected_helper_identity_digest": helper_implementation_digest(),
    }

    def crash(observed_phase: str, observed_index: int | None) -> None:
        if (observed_phase, observed_index) == (phase, index):
            raise _Crash(observed_phase)

    with pytest.raises(_Crash):
        _manager(helper, crash_hook=crash).rollback(**rollback_kwargs)
    assert _manager(helper).rollback(**rollback_kwargs).status in {
        "rolled-back",
        "already-rolled-back",
    }


def test_transaction_lock_is_nonblocking_and_never_taken_over(tmp_path: Path) -> None:
    _authorities, root, _txid, _target, kwargs = _fixture(tmp_path)
    lock_path = root / "host.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ProfileHostTransactionError, match="failed closed"):
            _manager(_Helper()).prepare(**kwargs)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink"])
def test_journal_identity_drift_fails_closed(tmp_path: Path, unsafe: str) -> None:
    _authorities, root, txid, _target, kwargs = _fixture(tmp_path)

    def crash(phase: str, index: int | None) -> None:
        if phase == "planned" and index is None:
            raise _Crash(phase)

    with pytest.raises(_Crash):
        _manager(_Helper(), crash_hook=crash).prepare(**kwargs)
    journal = root / "transactions" / txid / "journal.json"
    original = journal.read_bytes()
    journal.unlink()
    other = tmp_path / "unsafe-journal.json"
    other.write_bytes(original)
    other.chmod(0o600)
    if unsafe == "symlink":
        journal.symlink_to(other)
    else:
        os.link(other, journal)

    with pytest.raises(ProfileHostTransactionError, match="failed closed"):
        _manager(_Helper()).prepare(**kwargs)


def test_divergent_noncontiguous_journal_progress_fails_closed(tmp_path: Path) -> None:
    _authorities, root, txid, _target, kwargs = _fixture(tmp_path)

    def crash(phase: str, index: int | None) -> None:
        if phase == "snapshot" and index == 0:
            raise _Crash(phase)

    with pytest.raises(_Crash):
        _manager(_Helper(), crash_hook=crash).prepare(**kwargs)
    journal = root / "transactions" / txid / "journal.json"
    value = json.loads(journal.read_text(encoding="utf-8"))
    value["snapshotDigests"][2] = value["snapshotDigests"][0]
    journal.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    journal.chmod(0o600)

    with pytest.raises(ProfileHostTransactionError, match="failed closed"):
        _manager(_Helper()).prepare(**kwargs)


def test_public_results_and_errors_do_not_render_private_transaction_paths(
    tmp_path: Path,
) -> None:
    _authorities, root, _txid, _target, kwargs = _fixture(tmp_path)
    result = _manager(_Helper()).prepare(**kwargs)
    rendered = json.dumps(result.public_payload(), sort_keys=True) + repr(result)
    assert str(root) not in rendered
    assert str(kwargs["intent_path"]) not in rendered

    with pytest.raises(ProfileHostTransactionError) as captured:
        _manager(_Helper()).prepare(**{**kwargs, "confirmation": "wrong"})
    assert str(root) not in str(captured.value)
