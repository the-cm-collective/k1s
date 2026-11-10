"""Fail-closed contracts for explicit stopped-profile migration."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ae.kctl import profile_migration as migration
from ae.kctl.profile_migration import (
    ProfileMigrationError,
    ProfileMigrationManager,
)
from ae.kctl.profile_state import (
    ProcessIdentity,
    ProfileStateError,
    ProfileStateStore,
    StateConflictError,
)

NOW = "2026-11-07T12:00:00Z"
LEGACY_TIME = "2026-11-07T11:00:00+00:00"


class _Inspector:
    def __init__(
        self,
        observed: ProcessIdentity | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.observed = observed
        self.failure = failure
        self.calls: list[int] = []

    def inspect(self, pid: int) -> ProcessIdentity | None:
        self.calls.append(pid)
        if self.failure is not None:
            raise self.failure
        return self.observed


def _canonical(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()


def _record_payload(profile: str) -> dict[str, object]:
    return {
        "command": [
            "/opt/k1s/scripts/dev/run_profile.sh",
            "k1s-core" if profile == "core-edge" else "k1s-edge",
        ],
        "pid": 4242,
        "profile": profile,
        "schema_version": 1,
        "started_at": LEGACY_TIME,
    }


def _fixture_roots(tmp_path: Path, profile: str = "edge") -> tuple[Path, Path, bytes]:
    legacy_root = tmp_path / "legacy"
    destination_root = tmp_path / "state-v1"
    legacy_root.mkdir(mode=0o700, parents=True)
    destination_root.mkdir(mode=0o700)
    payload = _canonical(_record_payload(profile))
    record = legacy_root / f"{profile}.json"
    record.write_bytes(payload)
    record.chmod(0o600)
    return legacy_root, destination_root, payload


def _manager(inspector: _Inspector | None = None) -> ProfileMigrationManager:
    counter = 0

    def random_bytes(length: int) -> bytes:
        nonlocal counter
        counter += 1
        return bytes([counter % 251 + 1]) * length

    return ProfileMigrationManager(
        inspector=_Inspector() if inspector is None else inspector,
        clock=lambda: NOW,
        random_bytes=random_bytes,
    )


def _plan_apply(
    tmp_path: Path,
    *,
    profile: str = "edge",
) -> tuple[
    ProfileMigrationManager,
    Path,
    Path,
    object,
    object,
]:
    legacy_root, destination_root, _ = _fixture_roots(tmp_path, profile)
    manager = _manager()
    destination_profile = "core" if profile == "core-edge" else "edge-dev"
    plan = manager.plan(
        profile,
        legacy_root=legacy_root,
        destination_profile=destination_profile,
        destination_root=destination_root,
    )
    result = manager.apply(
        profile,
        legacy_root=legacy_root,
        destination_profile=destination_profile,
        destination_root=destination_root,
        expected_plan_digest=plan.plan_digest,
    )
    return manager, legacy_root, destination_root, plan, result


@pytest.mark.parametrize(
    ("legacy_profile", "destination", "variant", "components"),
    [
        ("edge", "edge-dev", None, ("gateway", "worker-stub")),
        ("edge-core", "edge-dev", "core-peering", ("gateway", "worker-stub")),
        ("core-edge", "core", "nats-core", ("controller", "nats-core")),
    ],
)
def test_plan_is_pure_and_maps_only_frozen_legacy_aliases(
    tmp_path: Path,
    legacy_profile: str,
    destination: str,
    variant: str | None,
    components: tuple[str, ...],
) -> None:
    legacy_root, destination_root, source_bytes = _fixture_roots(tmp_path, legacy_profile)
    manager = _manager()
    before = tuple(destination_root.iterdir())

    plan = manager.plan(
        legacy_profile,
        legacy_root=legacy_root,
        destination_profile=destination,
        destination_root=destination_root,
    )

    assert tuple(destination_root.iterdir()) == before == ()
    assert (legacy_root / f"{legacy_profile}.json").read_bytes() == source_bytes
    assert plan.destination_profile == destination
    assert plan.compatibility_variant == variant
    assert plan.components == components
    assert plan.destination_status == "available"
    assert str(legacy_root) not in json.dumps(plan.public_payload())
    assert str(destination_root) not in json.dumps(plan.public_payload())


def test_active_reused_pid_and_inspector_ambiguity_fail_closed(tmp_path: Path) -> None:
    legacy_root, destination_root, _ = _fixture_roots(tmp_path)
    active = ProcessIdentity(
        pid=4242,
        process_group_id=4242,
        session_id=4242,
        boot_id="11111111-2222-3333-4444-555555555555",
        start_time_ticks=99,
        executable_device=1,
        executable_inode=2,
    )
    with pytest.raises(StateConflictError, match="PID is active"):
        _manager(_Inspector(observed=active)).plan(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
        )
    with pytest.raises(ProfileMigrationError, match="ambiguous"):
        _manager(_Inspector(failure=PermissionError("private proc detail"))).plan(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
        )


def test_apply_imports_stopped_evidence_without_touching_source_or_other_xdg_roots(
    tmp_path: Path,
) -> None:
    manager, legacy_root, destination_root, plan, result = _plan_apply(tmp_path)
    source = legacy_root / "edge.json"
    source_before = (source.read_bytes(), source.stat().st_ino, source.stat().st_mtime_ns)

    assert result.status == "applied"
    assert (source.read_bytes(), source.stat().st_ino, source.stat().st_mtime_ns) == source_before
    assert {item.name for item in destination_root.iterdir()} == {"profiles"}
    store = manager._state_store(destination_root, "edge-dev")
    state = store.inspect()
    assert state is not None
    assert state.run_id == plan.run_id
    assert state.phase == "stopped"
    assert state.cleanup_complete is True
    assert [item.status for item in state.components] == ["stopped", "stopped"]
    assert all(item.process_identity is None for item in state.components)
    assert all("legacy-stopped-evidence-v1" not in item.command_digest for item in state.components)


def test_apply_and_rollback_are_idempotent_only_for_exact_original_authority(
    tmp_path: Path,
) -> None:
    manager, legacy_root, destination_root, plan, applied = _plan_apply(tmp_path)

    replay = manager.apply(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        expected_plan_digest=plan.plan_digest,
    )
    assert replay.status == "already-applied"
    assert replay.receipt_digest == applied.receipt_digest

    rolled = manager.rollback(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        migration_id=plan.migration_id,
        expected_receipt_digest=applied.receipt_digest,
        confirmation=f"edge-dev/{plan.run_id}",
    )
    assert rolled.status == "rolled-back"
    assert rolled.receipt_digest != applied.receipt_digest

    replay_rolled = manager.rollback(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        migration_id=plan.migration_id,
        expected_receipt_digest=applied.receipt_digest,
        confirmation=f"edge-dev/{plan.run_id}",
    )
    assert replay_rolled.status == "already-rolled-back"
    assert replay_rolled.receipt_digest == rolled.receipt_digest
    with pytest.raises(StateConflictError, match="authorization digest differs"):
        manager.rollback(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
            migration_id=plan.migration_id,
            expected_receipt_digest=rolled.receipt_digest,
            confirmation=f"edge-dev/{plan.run_id}",
        )


def _crash_after_once(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    name: str,
) -> Callable[[], None]:
    original = getattr(target, name)
    crashed = False

    def wrapper(*args: object, **kwargs: object) -> object:
        nonlocal crashed
        result = original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError(f"crash after {name}")
        return result

    monkeypatch.setattr(target, name, wrapper)

    def restore() -> None:
        monkeypatch.setattr(target, name, original)

    return restore


@pytest.mark.parametrize(
    ("target", "name"),
    [
        (migration, "_write_migration_transaction"),
        (migration, "_write_receipt"),
        (ProfileStateStore, "_write_run"),
        (ProfileStateStore, "_write_current_payload"),
    ],
)
def test_identical_apply_recovers_after_every_journaled_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    name: str,
) -> None:
    legacy_root, destination_root, _ = _fixture_roots(tmp_path)
    manager = _manager()
    plan = manager.plan(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
    )
    restore = _crash_after_once(monkeypatch, target, name)

    with pytest.raises(RuntimeError, match=f"crash after {name}"):
        manager.apply(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
            expected_plan_digest=plan.plan_digest,
        )
    with pytest.raises(StateConflictError, match="transaction requires recovery"):
        manager.plan(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
        )
    store = manager._state_store(destination_root, "edge-dev")
    with pytest.raises(ProfileStateError):
        store.inspect()
    restore()

    recovered = manager.apply(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        expected_plan_digest=plan.plan_digest,
    )
    assert recovered.status == "applied"
    store = manager._state_store(destination_root, "edge-dev")
    assert not store.layout.transaction.exists()
    assert store.inspect() is not None


@pytest.mark.parametrize(
    ("target", "name"),
    [
        (migration, "_write_migration_transaction"),
        (ProfileStateStore, "remove_exact_current_payload"),
        (ProfileStateStore, "remove_exact_run_payload"),
        (migration, "_write_receipt"),
    ],
)
def test_identical_rollback_recovers_after_every_journaled_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    name: str,
) -> None:
    manager, legacy_root, destination_root, plan, applied = _plan_apply(tmp_path)
    restore = _crash_after_once(monkeypatch, target, name)

    with pytest.raises(RuntimeError, match=f"crash after {name}"):
        manager.rollback(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
            migration_id=plan.migration_id,
            expected_receipt_digest=applied.receipt_digest,
            confirmation=f"edge-dev/{plan.run_id}",
        )
    with pytest.raises(StateConflictError, match="transaction requires recovery"):
        manager.plan(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
        )
    restore()

    recovered = manager.rollback(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        migration_id=plan.migration_id,
        expected_receipt_digest=applied.receipt_digest,
        confirmation=f"edge-dev/{plan.run_id}",
    )
    assert recovered.status == "rolled-back"
    replay = manager.rollback(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        migration_id=plan.migration_id,
        expected_receipt_digest=applied.receipt_digest,
        confirmation=f"edge-dev/{plan.run_id}",
    )
    assert replay.status == "already-rolled-back"


def test_rollback_removes_only_the_exact_migration_created_runs_directory(
    tmp_path: Path,
) -> None:
    manager, legacy_root, destination_root, plan, applied = _plan_apply(tmp_path)
    store = manager._state_store(destination_root, "edge-dev")
    runs = store.layout.profile_state / "runs"

    manager.rollback(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        migration_id=plan.migration_id,
        expected_receipt_digest=applied.receipt_digest,
        confirmation=f"edge-dev/{plan.run_id}",
    )
    assert not runs.exists()

    other_root = tmp_path / "other"
    legacy_root, destination_root, _ = _fixture_roots(other_root)
    profiles = destination_root / "profiles"
    profile = profiles / "edge-dev"
    preexisting_runs = profile / "runs"
    profiles.mkdir(mode=0o700)
    profile.mkdir(mode=0o700)
    preexisting_runs.mkdir(mode=0o700)
    (preexisting_runs.parent / "migrations").mkdir(mode=0o700)
    (profile / "profile.lock").touch(mode=0o600)
    manager = _manager()
    plan = manager.plan(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
    )
    applied = manager.apply(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        expected_plan_digest=plan.plan_digest,
    )
    manager.rollback(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
        migration_id=plan.migration_id,
        expected_receipt_digest=applied.receipt_digest,
        confirmation=f"edge-dev/{plan.run_id}",
    )
    assert preexisting_runs.is_dir()


def test_replaced_migration_created_runs_directory_fails_before_state_removal(
    tmp_path: Path,
) -> None:
    manager, legacy_root, destination_root, plan, applied = _plan_apply(tmp_path)
    store = manager._state_store(destination_root, "edge-dev")
    runs = store.layout.profile_state / "runs"
    replacement = runs.with_name("runs-replaced")
    runs.rename(replacement)
    runs.mkdir(mode=0o700)
    (replacement / plan.run_id).rename(runs / plan.run_id)

    with pytest.raises(StateConflictError, match="runs directory differs"):
        manager.rollback(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
            migration_id=plan.migration_id,
            expected_receipt_digest=applied.receipt_digest,
            confirmation=f"edge-dev/{plan.run_id}",
        )
    assert store.layout.current.is_file()
    assert store.layout.for_run(plan.run_id).run_state.is_file()


def test_apply_serializes_concurrent_identical_requests(tmp_path: Path) -> None:
    legacy_root, destination_root, _ = _fixture_roots(tmp_path)
    first = _manager()
    second = _manager()
    plan = first.plan(
        "edge",
        legacy_root=legacy_root,
        destination_profile="edge-dev",
        destination_root=destination_root,
    )

    from concurrent.futures import ThreadPoolExecutor

    def apply(manager: ProfileMigrationManager) -> str:
        return manager.apply(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
            expected_plan_digest=plan.plan_digest,
        ).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(apply, (first, second)))
    assert statuses == ["already-applied", "applied"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: {**payload, "unknown": True},
        lambda payload: {**payload, "schema_version": 2},
        lambda payload: {**payload, "pid": 0},
        lambda payload: {**payload, "profile": "node"},
        lambda payload: {
            **payload,
            "command": ["/opt/k1s/scripts/run_profile.sh", "k1s-edge"],
        },
    ],
)
def test_unknown_partial_newer_or_noncanonical_legacy_records_fail_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    legacy_root, destination_root, _ = _fixture_roots(tmp_path)
    record = legacy_root / "edge.json"
    record.write_bytes(_canonical(mutation(_record_payload("edge"))))

    with pytest.raises(ProfileMigrationError):
        _manager().plan(
            "edge",
            legacy_root=legacy_root,
            destination_profile="edge-dev",
            destination_root=destination_root,
        )


def test_legacy_record_symlink_hardlink_and_unsafe_mode_are_rejected(tmp_path: Path) -> None:
    for kind in ("symlink", "hardlink", "mode"):
        root = tmp_path / kind
        legacy_root, destination_root, _ = _fixture_roots(root)
        record = legacy_root / "edge.json"
        if kind == "symlink":
            actual = legacy_root / "actual.json"
            record.rename(actual)
            record.symlink_to(actual.name)
        elif kind == "hardlink":
            os.link(record, legacy_root / "other.json")
        else:
            record.chmod(0o660)
        with pytest.raises(ProfileMigrationError, match="safely|unsafe"):
            _manager().plan(
                "edge",
                legacy_root=legacy_root,
                destination_profile="edge-dev",
                destination_root=destination_root,
            )
