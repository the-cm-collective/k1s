from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from ae.kctl.profile_paths import ProfileRoots
from ae.kctl.profile_state import (
    ComponentState,
    FailureRecord,
    ProcessIdentity,
    ProfileBusyError,
    ProfileStateError,
    ProfileStateStore,
    ResourceReceipt,
    RunState,
    StateConflictError,
    _atomic_json_write,
    _validate_existing_state_target,
    command_digest,
)

NOW = "2026-11-07T12:00:00+00:00".replace("+00:00", "Z")


def _roots(tmp_path: Path) -> ProfileRoots:
    return ProfileRoots.from_environment(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        owner_uid=os.geteuid(),
    )


def _store(tmp_path: Path, profile: str = "local") -> ProfileStateStore:
    counter = 0

    def random_bytes(length: int) -> bytes:
        nonlocal counter
        counter += 1
        return bytes([counter]) * length

    return ProfileStateStore(
        _roots(tmp_path),
        profile,
        clock=lambda: NOW,
        random_bytes=random_bytes,
    )


def _identity(pid: int = 42) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        process_group_id=pid,
        session_id=pid,
        boot_id="11111111-2222-3333-4444-555555555555",
        start_time_ticks=12345,
        executable_device=99,
        executable_inode=100,
    )


class _Cleaner:
    def cleanup_resource(self, receipt: ResourceReceipt) -> None:
        del receipt


def test_begin_run_persists_canonical_private_current_and_run_state(tmp_path: Path) -> None:
    store = _store(tmp_path, "core-edge")
    state = store.begin_run()

    assert state.profile == "core"
    assert state.requested_name == "core-edge"
    assert state.run_id == "r1-01010101010101010101010101010101"
    assert state.generation == state.revision == 1
    assert state.phase == "preparing"
    assert store.inspect() == state

    run_path = store.layout.for_run(state.run_id).run_state
    for path in (store.layout.current, run_path, store.layout.lock):
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_uid == os.geteuid()
    for path in (store.layout.current, run_path):
        raw = path.read_text(encoding="utf-8")
        canonical = json.dumps(json.loads(raw), ensure_ascii=False, indent=2, sort_keys=True)
        assert raw == canonical + "\n"


def test_portable_backend_and_plan_identity_are_persisted_as_one_pair(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = "sha256:" + "a" * 64

    state = store.begin_run(backend_id="podman", plan_digest=digest)

    assert state.backend_id == "podman"
    assert state.plan_digest == digest
    payload = json.loads(store.layout.for_run(state.run_id).run_state.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 3
    assert payload["backendId"] == "podman"
    assert payload["planDigest"] == digest


def test_transaction_recovery_advances_current_after_run_only_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    original = store._write_current

    def crash_before_current(_state: object) -> None:
        raise RuntimeError("simulated current write crash")

    monkeypatch.setattr(store, "_write_current", crash_before_current)
    with pytest.raises(RuntimeError, match="simulated current"):
        store.begin_run(backend_id="podman", plan_digest="sha256:" + "b" * 64)
    monkeypatch.setattr(store, "_write_current", original)

    assert store.layout.transaction.is_file()
    with pytest.raises(StateConflictError, match="transaction requires explicit recovery"):
        store.inspect()
    assert store.reconcile_transaction() == "advanced-current"
    recovered = store.inspect()
    assert recovered is not None and recovered.backend_id == "podman"
    assert not store.layout.transaction.exists()


def test_transaction_recovery_seals_completed_pair_after_unlink_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    original = store._remove_transaction

    def crash_before_unlink() -> None:
        raise RuntimeError("simulated transaction unlink crash")

    monkeypatch.setattr(store, "_remove_transaction", crash_before_unlink)
    with pytest.raises(RuntimeError, match="simulated transaction unlink"):
        store.begin_run(backend_id="docker", plan_digest="sha256:" + "c" * 64)
    monkeypatch.setattr(store, "_remove_transaction", original)

    assert store.reconcile_transaction() == "completed"
    recovered = store.inspect()
    assert recovered is not None and recovered.backend_id == "docker"
    assert not store.layout.transaction.exists()


def test_legacy_schema_is_read_and_next_mutation_migrates_transaction_safely(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    current_path = store.layout.current
    run_path = store.layout.for_run(state.run_id).run_state
    current_payload = json.loads(current_path.read_text(encoding="utf-8"))
    run_payload = json.loads(run_path.read_text(encoding="utf-8"))
    current_payload["schemaVersion"] = 1
    run_payload["schemaVersion"] = 1
    run_payload.pop("backendId")
    run_payload.pop("planDigest")
    run_payload.pop("bootstrapId")
    run_payload.pop("privateBootstrapDigest")
    for path, payload in ((current_path, current_payload), (run_path, run_payload)):
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    legacy = store.inspect()
    assert legacy is not None and legacy.backend_id is None and legacy.plan_digest is None
    migrated = store.transition(
        legacy.run_id,
        "starting",
        expected_revision=legacy.revision,
    )

    assert migrated.phase == "starting"
    assert json.loads(current_path.read_text(encoding="utf-8"))["schemaVersion"] == 3
    migrated_payload = json.loads(run_path.read_text(encoding="utf-8"))
    assert migrated_payload["schemaVersion"] == 3
    assert migrated_payload["backendId"] is None
    assert migrated_payload["planDigest"] is None
    assert migrated_payload["bootstrapId"] is None
    assert migrated_payload["privateBootstrapDigest"] is None


def test_bootstrap_private_identity_is_persisted_but_never_required_for_local(
    tmp_path: Path,
) -> None:
    digest = "sha256:" + "d" * 64
    store = _store(tmp_path, "node")

    state = store.begin_run(
        backend_id="podman",
        plan_digest="sha256:" + "a" * 64,
        bootstrap_id="b1-" + "1" * 32,
        private_bootstrap_digest=digest,
    )
    loaded = store.inspect()

    assert loaded == state
    assert loaded is not None and loaded.bootstrap_id == "b1-" + "1" * 32
    assert loaded.private_bootstrap_digest == digest
    payload = json.loads(store.layout.for_run(state.run_id).run_state.read_text(encoding="utf-8"))
    assert payload["bootstrapId"] == state.bootstrap_id
    assert payload["privateBootstrapDigest"] == digest

    with pytest.raises(ProfileStateError, match="bootstrap ID and private digest"):
        _store(tmp_path / "other").begin_run(
            backend_id="podman",
            plan_digest="sha256:" + "b" * 64,
            bootstrap_id="b1-" + "2" * 32,
        )


def test_empty_inspection_is_read_only_and_creates_no_xdg_paths(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.inspect() is None
    assert not store.layout.roots.config.exists()
    assert not store.layout.roots.state.exists()
    assert not store.layout.roots.data.exists()
    assert not store.layout.roots.cache.exists()


def test_existing_state_without_authoritative_lock_fails_instead_of_repairing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.begin_run()
    store.layout.lock.unlink()

    with pytest.raises(ProfileStateError, match="lock is missing"):
        store.inspect()
    assert not store.layout.lock.exists()


def test_state_machine_requires_legal_transition_and_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    starting = store.transition(
        state.run_id,
        "starting",
        expected_revision=state.revision,
    )
    running = store.transition(
        state.run_id,
        "running",
        expected_revision=starting.revision,
    )

    assert (starting.revision, running.revision) == (2, 3)
    with pytest.raises(StateConflictError, match="revision changed"):
        store.transition(state.run_id, "stopping", expected_revision=starting.revision)
    with pytest.raises(StateConflictError, match="illegal run transition"):
        store.transition(state.run_id, "stopped", expected_revision=running.revision)


def test_component_identity_and_command_are_stored_without_raw_argv(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    component = ComponentState(
        component_id="controller",
        command_digest=command_digest(("python", "-m", "ae.controller")),
        log_path="logs/controller.log",
        status="running",
        process_identity=_identity(),
    )
    updated = store.put_component(state.run_id, component, expected_revision=state.revision)

    assert updated.components == (component,)
    persisted = store.layout.for_run(state.run_id).run_state.read_text(encoding="utf-8")
    assert "ae.controller" not in persisted
    assert component.command_digest in persisted
    assert updated.components[0].process_identity == _identity()


@pytest.mark.parametrize(
    ("status", "identity"),
    [
        ("starting", None),
        ("running", None),
        ("stopping", None),
        ("prepared", _identity()),
        ("stopped", _identity()),
        ("failed", _identity()),
    ],
)
def test_component_status_requires_consistent_process_authority(
    status: str, identity: ProcessIdentity | None
) -> None:
    with pytest.raises(ProfileStateError, match="component"):
        ComponentState(
            component_id="controller",
            command_digest=command_digest(("controller",)),
            log_path="logs/controller.log",
            status=status,  # type: ignore[arg-type]
            process_identity=identity,
        )


def test_command_digest_is_length_delimited_and_rejects_invalid_argv() -> None:
    assert command_digest(("ab", "c")) != command_digest(("a", "bc"))
    with pytest.raises(ProfileStateError, match="must not be empty"):
        command_digest(())
    with pytest.raises(ProfileStateError, match="NUL-free"):
        command_digest(("bad\x00argument",))


def test_lock_contention_fails_closed_and_different_profiles_are_independent(
    tmp_path: Path,
) -> None:
    first = _store(tmp_path, "local")
    second = _store(tmp_path, "local")
    independent = _store(tmp_path, "core")

    with first.locked(), pytest.raises(ProfileBusyError, match="profile_busy"), second.locked():
        pass
    with independent.locked():
        pass


def test_malformed_noncanonical_or_unknown_state_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    path = store.layout.for_run(state.run_id).run_state
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["unknown"] = True
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ProfileStateError, match="canonical JSON|unknown keys"):
        store.inspect()


@pytest.mark.parametrize("target_name", ["current", "state"])
@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory", "mode", "foreign-owner"])
def test_atomic_replacement_rejects_unsafe_existing_targets(
    tmp_path: Path, target_name: str, unsafe_kind: str
) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    target = (
        store.layout.current
        if target_name == "current"
        else store.layout.for_run(state.run_id).run_state
    )
    payload = json.loads(target.read_text(encoding="utf-8"))

    if unsafe_kind == "symlink":
        target.unlink()
        replacement = tmp_path / f"{target_name}-replacement"
        replacement.write_text("{}\n", encoding="utf-8")
        replacement.chmod(0o600)
        target.symlink_to(replacement)
    elif unsafe_kind == "directory":
        target.unlink()
        target.mkdir(mode=0o700)
    elif unsafe_kind == "mode":
        target.chmod(0o644)
    else:
        with pytest.raises(ProfileStateError, match="unsafe"):
            _validate_existing_state_target(target, owner_uid=os.geteuid() + 1)
        return

    with pytest.raises(ProfileStateError, match="unsafe"):
        _atomic_json_write(
            target,
            payload,
            owner_uid=os.geteuid(),
            random_bytes=lambda length: b"x" * length,
        )


def test_state_file_symlink_or_unsafe_mode_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    run_path = store.layout.for_run(state.run_id).run_state
    run_path.chmod(0o644)
    with pytest.raises(ProfileStateError, match="unsafe ownership or mode"):
        store.inspect()

    run_path.chmod(0o600)
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    run_path.unlink()
    run_path.symlink_to(target)
    with pytest.raises(ProfileStateError, match="cannot be opened safely"):
        store.inspect()


def test_new_generation_requires_clean_terminal_current_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.begin_run()
    with pytest.raises(StateConflictError, match="not cleanly terminal"):
        store.begin_run()

    failed = store.transition(
        first.run_id,
        "failed",
        expected_revision=first.revision,
        failure=FailureRecord("launch-failed", "The component failed before launch."),
    )
    stopping = store.transition(
        first.run_id,
        "stopping",
        expected_revision=failed.revision,
    )
    stopped = store.transition(
        first.run_id,
        "stopped",
        expected_revision=stopping.revision,
        cleanup_complete=True,
    )
    assert stopped.cleanup_complete

    second = store.begin_run()
    assert second.generation == 2
    assert second.run_id != first.run_id


def _stopped_run(store: ProfileStateStore) -> RunState:
    state = store.begin_run(backend_id="podman", plan_digest="sha256:" + "d" * 64)
    state = store.transition(state.run_id, "stopping", expected_revision=state.revision)
    state = store.cleanup_owned(state.run_id, _Cleaner())
    state = store.settle_components(state.run_id, expected_revision=state.revision)
    return store.transition(
        state.run_id,
        "stopped",
        expected_revision=state.revision,
        cleanup_complete=True,
    )


def test_exact_component_log_snapshot_is_bounded_and_descriptor_owned(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    component = ComponentState(
        component_id="controller",
        command_digest=command_digest(("controller",)),
        log_path="logs/controller.log",
        status="prepared",
    )
    state = store.put_component(state.run_id, component, expected_revision=state.revision)
    log = store.layout.for_run(state.run_id).log_path("controller")
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    log.chmod(0o600)

    snapshot = store.read_log_snapshot(state.run_id, "controller", tail=2)

    assert snapshot.lines == ("two", "three")
    assert snapshot.truncated is True
    assert snapshot.run_id == state.run_id
    with pytest.raises(StateConflictError, match="not recorded"):
        store.read_log_snapshot(state.run_id, "node-local")
    with pytest.raises(ProfileStateError, match="0 through 10000"):
        store.read_log_snapshot(state.run_id, "controller", tail=10_001)


def test_log_snapshot_uses_fstat_bounded_suffix_for_oversize_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    state = store.put_component(
        state.run_id,
        ComponentState(
            component_id="controller",
            command_digest=command_digest(("controller",)),
            log_path="logs/controller.log",
            status="prepared",
        ),
        expected_revision=state.revision,
    )
    log = store.layout.for_run(state.run_id).log_path("controller")
    log.write_bytes((b"old-line\n" * 140_000) + b"final-line\n")
    log.chmod(0o600)

    snapshot = store.read_log_snapshot(state.run_id, "controller", tail=1)

    assert snapshot.lines == ("final-line",)
    assert snapshot.truncated is True


def test_log_snapshot_rejects_symlink_mode_and_hardlink_substitution(tmp_path: Path) -> None:
    for case in ("symlink", "mode", "hardlink"):
        store = _store(tmp_path / case)
        state = store.begin_run()
        state = store.put_component(
            state.run_id,
            ComponentState(
                component_id="controller",
                command_digest=command_digest(("controller",)),
                log_path="logs/controller.log",
                status="prepared",
            ),
            expected_revision=state.revision,
        )
        log = store.layout.for_run(state.run_id).log_path("controller")
        log.write_text("safe\n", encoding="utf-8")
        log.chmod(0o600)
        if case == "symlink":
            target = tmp_path / "foreign.log"
            target.write_text("foreign\n", encoding="utf-8")
            log.unlink()
            log.symlink_to(target)
        elif case == "mode":
            log.chmod(0o644)
        else:
            os.link(log, log.with_name("other.log"))
        with pytest.raises(ProfileStateError):
            store.read_log_snapshot(state.run_id, "controller")


def test_log_directory_device_must_match_validated_run_parent(tmp_path: Path) -> None:
    import ae.kctl.profile_state as profile_state

    store = _store(tmp_path)
    state = store.begin_run()
    layout = store.layout.for_run(state.run_id)
    parent_fd = profile_state._open_owned_directory(
        layout.run_state.parent,
        owner_uid=os.geteuid(),
    )
    try:
        parent_info = os.fstat(parent_fd)
        with pytest.raises(ProfileStateError, match="child directory identity"):
            profile_state._open_owned_child_directory(
                parent_fd,
                "logs",
                owner_uid=os.geteuid(),
                expected_device=parent_info.st_dev + 1,
            )
    finally:
        os.close(parent_fd)


def test_owned_tree_device_must_match_validated_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ae.kctl.profile_state as profile_state

    parent = tmp_path / "parent"
    target = parent / "target"
    parent.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    payload = target / "owned.txt"
    payload.write_text("owned\n", encoding="utf-8")
    payload.chmod(0o600)
    original_stat = profile_state.os.stat

    def mismatched_target_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        info = original_stat(path, *args, **kwargs)
        if path == target.name and kwargs.get("dir_fd") is not None:
            values = list(info)
            values[2] = info.st_dev + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(profile_state.os, "stat", mismatched_target_stat)
    with pytest.raises(ProfileStateError, match="deletion target is unsafe"):
        profile_state._remove_owned_tree(target, owner_uid=os.geteuid())

    assert target.is_dir()
    assert payload.read_text(encoding="utf-8") == "owned\n"


def test_delete_run_is_digest_bound_idempotent_and_preserves_authority(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = _stopped_run(store)
    layout = store.layout.for_run(state.run_id)
    payload = layout.run_data / "controller.db"
    payload.write_bytes(b"state")
    payload.chmod(0o600)
    log = layout.log_path("controller")
    log.write_text("done\n", encoding="utf-8")
    log.chmod(0o600)

    deleted = store.delete_run(
        state.run_id,
        confirmation=f"{state.profile}/{state.run_id}",
    )
    repeated = store.delete_run(
        state.run_id,
        confirmation=f"{state.profile}/{state.run_id}",
    )

    assert deleted == repeated
    assert deleted.status == "complete"
    assert not layout.run_data.exists()
    assert not layout.run_cache.exists()
    assert not layout.run_logs.exists()
    assert layout.run_state.is_file()
    assert store.layout.current.is_file()
    record_path = layout.run_state.parent / "deletion.json"
    assert record_path.stat().st_mode & 0o777 == 0o600
    assert store.inspect() == state
    next_run = store.begin_run(backend_id="podman", plan_digest="sha256:" + "e" * 64)
    assert next_run.generation == state.generation + 1


def test_delete_run_pending_intent_rolls_forward_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    state = _stopped_run(store)
    import ae.kctl.profile_state as profile_state

    original = profile_state._remove_owned_tree
    calls = 0

    def crash_once(path: Path, *, owner_uid: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated deletion crash")
        original(path, owner_uid=owner_uid)

    monkeypatch.setattr(profile_state, "_remove_owned_tree", crash_once)
    confirmation = f"{state.profile}/{state.run_id}"
    with pytest.raises(RuntimeError, match="simulated deletion crash"):
        store.delete_run(state.run_id, confirmation=confirmation)
    authority = json.loads(store.layout.transaction.read_text(encoding="utf-8"))
    assert authority["kind"] == "ProfileRunDeletion"
    assert authority["status"] == "pending"
    with pytest.raises(StateConflictError, match="roll-forward"):
        store.inspect()

    monkeypatch.setattr(profile_state, "_remove_owned_tree", original)
    record = store.delete_run(state.run_id, confirmation=confirmation)
    assert record.status == "complete"
    assert store.inspect() == state


def test_completed_delete_revalidates_exact_target_absence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = _stopped_run(store)
    confirmation = f"{state.profile}/{state.run_id}"
    store.delete_run(state.run_id, confirmation=confirmation)
    layout = store.layout.for_run(state.run_id)
    layout.run_cache.mkdir(mode=0o700)

    with pytest.raises(StateConflictError, match="unexpectedly exists"):
        store.delete_run(state.run_id, confirmation=confirmation)


def test_delete_run_requires_exact_confirmation_and_rejects_unsafe_target(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = _stopped_run(store)
    with pytest.raises(StateConflictError, match="confirmation"):
        store.delete_run(state.run_id, confirmation="local/wrong")

    layout = store.layout.for_run(state.run_id)
    layout.run_secrets.rmdir()
    layout.run_data.rmdir()
    layout.run_data.symlink_to(tmp_path)
    with pytest.raises(ProfileStateError, match="target is unsafe"):
        store.delete_run(state.run_id, confirmation=f"local/{state.run_id}")


def test_repeated_random_run_id_collision_fails_closed(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    store = ProfileStateStore(
        roots,
        "local",
        clock=lambda: NOW,
        random_bytes=lambda length: b"z" * length,
    )
    first = store.begin_run()
    failed = store.transition(first.run_id, "failed", expected_revision=first.revision)
    stopping = store.transition(first.run_id, "stopping", expected_revision=failed.revision)
    stopped = store.transition(
        first.run_id,
        "stopped",
        expected_revision=stopping.revision,
        cleanup_complete=True,
    )
    assert stopped.phase == "stopped"

    with pytest.raises(StateConflictError, match="run ID already exists"):
        store.begin_run()


@pytest.mark.parametrize("phase", ["preparing", "starting", "running"])
def test_cleanup_is_rejected_before_stopping_or_failure(tmp_path: Path, phase: str) -> None:
    store = _store(tmp_path / phase)
    state = store.begin_run()
    if phase in {"starting", "running"}:
        state = store.transition(state.run_id, "starting", expected_revision=state.revision)
    if phase == "running":
        state = store.transition(state.run_id, "running", expected_revision=state.revision)

    with pytest.raises(StateConflictError, match="cleanup is not authorized"):
        store.cleanup_owned(state.run_id, _Cleaner())
    with pytest.raises(StateConflictError, match="only while the run is stopping"):
        store.settle_components(state.run_id, expected_revision=state.revision)


def test_stopped_run_rejects_every_mutating_entry_point(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    state = store.transition(state.run_id, "stopping", expected_revision=state.revision)
    state = store.cleanup_owned(state.run_id, _Cleaner())
    state = store.settle_components(state.run_id, expected_revision=state.revision)
    state = store.transition(
        state.run_id,
        "stopped",
        expected_revision=state.revision,
        cleanup_complete=True,
    )
    component = ComponentState(
        component_id="controller",
        command_digest=command_digest(("controller",)),
        log_path="logs/controller.log",
        status="prepared",
    )

    mutations = (
        lambda: store.transition(state.run_id, "failed"),
        lambda: store.put_component(state.run_id, component),
        lambda: store.create_secret(state.run_id, "token", b"secret"),
        lambda: store.add_resource(
            state.run_id,
            resource_type="socket",
            resource_id="controller",
            cleanup_handler_id="unlink-socket",
        ),
        lambda: store.cleanup_owned(state.run_id, _Cleaner()),
        lambda: store.settle_components(state.run_id, expected_revision=state.revision),
    )
    for mutation in mutations:
        with pytest.raises(StateConflictError, match="stopped run is immutable"):
            mutation()


def test_stopped_transition_rejects_live_component_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    state = store.put_component(
        state.run_id,
        ComponentState(
            component_id="controller",
            command_digest=command_digest(("controller",)),
            log_path="logs/controller.log",
            status="running",
            process_identity=_identity(),
        ),
        expected_revision=state.revision,
    )
    state = store.transition(state.run_id, "stopping", expected_revision=state.revision)
    state = store.cleanup_owned(state.run_id, _Cleaner())

    with pytest.raises(ProfileStateError, match="live component authority"):
        store.transition(
            state.run_id,
            "stopped",
            expected_revision=state.revision,
            cleanup_complete=True,
        )

    state = store.settle_components(state.run_id, expected_revision=state.revision)
    stopped = store.transition(
        state.run_id,
        "stopped",
        expected_revision=state.revision,
        cleanup_complete=True,
    )
    assert stopped.phase == "stopped"


def test_persisted_stopped_state_with_live_component_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    state = store.transition(state.run_id, "stopping", expected_revision=state.revision)
    state = store.cleanup_owned(state.run_id, _Cleaner())
    state = store.settle_components(state.run_id, expected_revision=state.revision)
    state = store.transition(
        state.run_id,
        "stopped",
        expected_revision=state.revision,
        cleanup_complete=True,
    )
    run_path = store.layout.for_run(state.run_id).run_state
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["components"] = [
        ComponentState(
            component_id="controller",
            command_digest=command_digest(("controller",)),
            log_path="logs/controller.log",
            status="running",
            process_identity=_identity(),
        ).to_payload()
    ]
    run_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileStateError, match="live component authority"):
        store.inspect()


@pytest.mark.parametrize(
    "phases",
    [
        ("starting", "running", "stopping", "stopped"),
        ("failed", "stopping", "stopped"),
        ("stopping", "stopped"),
        ("starting", "orphaned", "stopping", "stopped"),
    ],
)
def test_property_legal_transition_sequences_are_durable(
    tmp_path: Path, phases: tuple[str, ...]
) -> None:
    store = _store(tmp_path / "-".join(phases))
    state = store.begin_run()
    for phase in phases:
        state = store.transition(
            state.run_id,
            phase,  # type: ignore[arg-type]
            expected_revision=state.revision,
            cleanup_complete=True if phase == "stopped" else None,
        )
        assert store.inspect() == state


def test_concurrent_readers_never_observe_partial_atomic_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.begin_run()
    barrier = threading.Barrier(2)
    observations: list[RunState] = []
    failures: list[BaseException] = []

    def reader() -> None:
        try:
            barrier.wait()
            while len(observations) < 25:
                try:
                    observed = store.inspect()
                except ProfileBusyError:
                    time.sleep(0)
                    continue
                assert observed is not None
                observations.append(observed)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            failures.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    barrier.wait()

    def transition(phase: str) -> None:
        nonlocal state
        while True:
            try:
                state = store.transition(
                    state.run_id,
                    phase,  # type: ignore[arg-type]
                    expected_revision=state.revision,
                )
                return
            except ProfileBusyError:
                time.sleep(0)

    transition("starting")
    transition("running")
    transition("stopping")
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    assert observations
    assert {item.phase for item in observations} <= {
        "preparing",
        "starting",
        "running",
        "stopping",
    }


def test_resource_receipt_validates_exact_ownership() -> None:
    with pytest.raises(ProfileStateError, match="receipt ownership"):
        RunState(
            profile="local",
            requested_name="local",
            run_id="r1-0123456789abcdef0123456789abcdef",
            generation=1,
            revision=1,
            owner_uid=os.geteuid(),
            phase="preparing",
            created_at=NOW,
            updated_at=NOW,
            resource_receipts=(
                ResourceReceipt(
                    resource_type="socket",
                    resource_id="controller",
                    cleanup_handler_id="unlink-socket",
                    ordinal=0,
                    owner_profile="core",
                    owner_run_id="r1-0123456789abcdef0123456789abcdef",
                    created_at=NOW,
                ),
            ),
        )
