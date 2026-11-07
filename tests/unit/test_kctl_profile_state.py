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
