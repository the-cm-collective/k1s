from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from ae.kctl.profile_paths import ProfileRoots
from ae.kctl.profile_state import (
    ComponentState,
    ProcessIdentity,
    ProfileStateError,
    ProfileStateStore,
    ResourceReceipt,
    command_digest,
)
from ae.kctl.profile_supervisor import ProfileSupervisor

NOW = "2026-11-07T12:00:00Z"


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


def _store(tmp_path: Path) -> ProfileStateStore:
    counter = 0

    def random_bytes(length: int) -> bytes:
        nonlocal counter
        counter += 1
        return bytes([counter % 256]) * length

    return ProfileStateStore(
        _roots(tmp_path),
        "local",
        clock=lambda: NOW,
        random_bytes=random_bytes,
    )


def _identity(pid: int = 42, start: int = 100) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        process_group_id=pid,
        session_id=pid,
        boot_id="11111111-2222-3333-4444-555555555555",
        start_time_ticks=start,
        executable_device=9,
        executable_inode=10,
    )


class Inspector:
    def __init__(self, observations: Sequence[ProcessIdentity | None]) -> None:
        self.observations = list(observations)

    def inspect(self, pid: int) -> ProcessIdentity | None:
        del pid
        if not self.observations:
            raise AssertionError("unexpected process inspection")
        return self.observations.pop(0)


class Launcher:
    def spawn(self, *args: object, **kwargs: object) -> int:
        del args, kwargs
        raise AssertionError("recovery must not launch a component")


class Signals:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def signal_group(self, process_group_id: int, signal_number: int) -> None:
        self.calls.append((process_group_id, signal_number))


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class Cleaner:
    def __init__(self, *, fail_resource: str | None = None) -> None:
        self.fail_resource = fail_resource
        self.calls: list[str] = []

    def cleanup_resource(self, receipt: ResourceReceipt) -> None:
        self.calls.append(receipt.resource_id)
        if receipt.resource_id == self.fail_resource:
            raise RuntimeError("synthetic cleanup failure")


def _supervisor(
    observations: Sequence[ProcessIdentity | None], signals: Signals | None = None
) -> tuple[ProfileSupervisor, Signals]:
    actual_signals = signals or Signals()
    return (
        ProfileSupervisor(
            inspector=Inspector(observations),
            launcher=Launcher(),
            signals=actual_signals,
            monotonic=Clock(),
            stop_timeout_seconds=0.1,
            poll_interval_seconds=0.05,
        ),
        actual_signals,
    )


def _starting_component(store: ProfileStateStore, identity: ProcessIdentity) -> str:
    run = store.begin_run()
    run = store.transition(run.run_id, "starting", expected_revision=run.revision)
    run = store.put_component(
        run.run_id,
        ComponentState(
            component_id="controller",
            command_digest=command_digest(("python", "-m", "ae.controller")),
            log_path="logs/controller.log",
            status="running",
            process_identity=identity,
        ),
        expected_revision=run.revision,
    )
    return run.run_id


def _add_running_component(
    store: ProfileStateStore, run_id: str, component_id: str, identity: ProcessIdentity
) -> None:
    state = store.inspect()
    assert state is not None
    store.put_component(
        run_id,
        ComponentState(
            component_id=component_id,
            command_digest=command_digest((component_id,)),
            log_path=f"logs/{component_id}.log",
            status="running",
            process_identity=identity,
        ),
        expected_revision=state.revision,
    )


def test_recovery_adopts_only_exact_same_process_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    expected = _identity()
    run_id = _starting_component(store, expected)
    supervisor, signals = _supervisor([expected])

    recovered = supervisor.recover(store, Cleaner())

    assert recovered is not None
    assert recovered.run_id == run_id
    assert recovered.phase == "running"
    assert signals.calls == []


def test_recovery_fences_reused_pid_without_signaling_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = _starting_component(store, _identity())
    supervisor, signals = _supervisor([_identity(start=999)])

    recovered = supervisor.recover(store, Cleaner())

    assert recovered is not None
    assert recovered.run_id == run_id
    assert recovered.phase == "stopped"
    assert recovered.cleanup_complete
    assert recovered.failure is not None
    assert recovered.failure.code == "process-identity-mismatch"
    assert signals.calls == []


def test_recovery_of_exited_component_is_failed_cleaned_and_stopped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _starting_component(store, _identity())
    supervisor, signals = _supervisor([None])

    recovered = supervisor.recover(store, Cleaner())

    assert recovered is not None
    assert recovered.phase == "stopped"
    assert recovered.cleanup_complete
    assert recovered.failure is not None
    assert recovered.failure.code == "component-exited"
    assert signals.calls == []


def test_mixed_exited_and_exact_running_components_stop_live_identity_before_cleanup(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _identity(pid=41)
    second = _identity(pid=42)
    run_id = _starting_component(store, first)
    _add_running_component(store, run_id, "gateway", second)
    supervisor, signals = _supervisor([None, second, second, None])

    recovered = supervisor.recover(store, Cleaner())

    assert recovered is not None
    assert recovered.phase == "stopped"
    assert recovered.cleanup_complete
    assert all(component.status == "stopped" for component in recovered.components)
    assert all(component.process_identity is None for component in recovered.components)
    assert signals.calls == [(second.process_group_id, 15)]


def test_owned_resources_clean_in_reverse_order_and_secrets_are_exact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.begin_run()
    run, first = store.add_resource(
        run.run_id,
        resource_type="socket",
        resource_id="one",
        cleanup_handler_id="unlink-socket",
        expected_revision=run.revision,
    )
    run, second = store.add_resource(
        run.run_id,
        resource_type="socket",
        resource_id="two",
        cleanup_handler_id="unlink-socket",
        expected_revision=run.revision,
    )
    run, secret = store.create_secret(
        run.run_id,
        "join-token",
        b"not-reported",
        expected_revision=run.revision,
    )
    run = store.transition(run.run_id, "stopping", expected_revision=run.revision)
    secret_path = store.layout.roots.data / secret.relative_path
    cleaner = Cleaner()

    cleaned = store.cleanup_owned(run.run_id, cleaner)

    assert (first.ordinal, second.ordinal) == (0, 1)
    assert cleaner.calls == ["two", "one"]
    assert cleaned.cleanup_complete
    assert cleaned.resource_receipts == ()
    assert cleaned.secret_receipts == ()
    assert not secret_path.exists()


def test_partial_cleanup_is_durable_and_retries_same_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.begin_run()
    run, _ = store.add_resource(
        run.run_id,
        resource_type="socket",
        resource_id="one",
        cleanup_handler_id="unlink-socket",
        expected_revision=run.revision,
    )
    run, _ = store.add_resource(
        run.run_id,
        resource_type="socket",
        resource_id="two",
        cleanup_handler_id="unlink-socket",
        expected_revision=run.revision,
    )
    run = store.transition(run.run_id, "stopping", expected_revision=run.revision)
    failing = Cleaner(fail_resource="one")

    with pytest.raises(ProfileStateError, match="cleanup failed"):
        store.cleanup_owned(run.run_id, failing)

    persisted = store.inspect()
    assert persisted is not None
    assert persisted.run_id == run.run_id
    assert [receipt.resource_id for receipt in persisted.resource_receipts] == ["one"]
    assert persisted.failure is not None
    assert persisted.failure.code == "resource-cleanup-failed"

    retry = Cleaner()
    cleaned = store.cleanup_owned(run.run_id, retry)
    assert retry.calls == ["one"]
    assert cleaned.cleanup_complete


def test_foreign_secret_identity_is_never_deleted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.begin_run()
    run, receipt = store.create_secret(
        run.run_id,
        "join-token",
        b"original",
        expected_revision=run.revision,
    )
    run = store.transition(run.run_id, "stopping", expected_revision=run.revision)
    path = store.layout.roots.data / receipt.relative_path
    replacement = path.with_name("replacement")
    replacement.write_bytes(b"replacement")
    replacement.chmod(0o600)
    os.replace(replacement, path)

    with pytest.raises(ProfileStateError, match="identity differs"):
        store.cleanup_owned(run.run_id, Cleaner())
    assert path.read_bytes() == b"replacement"


def test_recovery_is_idempotent_for_terminal_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.begin_run()
    run = store.transition(run.run_id, "stopping", expected_revision=run.revision)
    run = store.cleanup_owned(run.run_id, Cleaner())
    run = store.transition(
        run.run_id,
        "stopped",
        expected_revision=run.revision,
        cleanup_complete=True,
    )
    supervisor, signals = _supervisor([])

    assert supervisor.recover(store, Cleaner()) == run
    assert signals.calls == []
