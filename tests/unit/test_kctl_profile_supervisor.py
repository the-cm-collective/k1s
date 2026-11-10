from __future__ import annotations

import os
import signal
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from ae.kctl.profile_state import ComponentState, ProcessIdentity, RunState, command_digest
from ae.kctl.profile_supervisor import (
    ProfileSupervisor,
    StopResult,
    SubprocessLauncher,
    SupervisorError,
    classify_identity,
    parse_proc_stat,
)


def _identity(pid: int = 42, *, start: int = 123) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        process_group_id=pid,
        session_id=pid,
        boot_id="11111111-2222-3333-4444-555555555555",
        start_time_ticks=start,
        executable_device=9,
        executable_inode=10,
    )


class FakeInspector:
    def __init__(self, observations: Sequence[ProcessIdentity | None]) -> None:
        self.observations = list(observations)
        self.calls: list[int] = []

    def inspect(self, pid: int) -> ProcessIdentity | None:
        self.calls.append(pid)
        if not self.observations:
            raise AssertionError("unexpected process inspection")
        return self.observations.pop(0)


class FakeLauncher:
    def __init__(self, pid: int = 42) -> None:
        self.pid = pid
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str], Path]] = []

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> int:
        self.calls.append((tuple(argv), cwd, dict(environment), log_path))
        return self.pid


class FakePending:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.activated = False
        self.cancelled = False

    def activate(self) -> None:
        self.activated = True

    def cancel(self) -> None:
        self.cancelled = True


class FakeGatedLauncher(FakeLauncher):
    def __init__(self, pid: int = 42) -> None:
        super().__init__(pid)
        self.pending = FakePending(pid)

    def spawn_gated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> FakePending:
        self.calls.append((tuple(argv), cwd, dict(environment), log_path))
        return self.pending


class FakeSignals:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def signal_group(self, process_group_id: int, signal_number: int) -> None:
        self.calls.append((process_group_id, signal_number))


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _supervisor(
    observations: Sequence[ProcessIdentity | None], *, timeout: float = 0.1
) -> tuple[ProfileSupervisor, FakeSignals, FakeLauncher, FakeClock]:
    signals = FakeSignals()
    launcher = FakeLauncher()
    clock = FakeClock()
    supervisor = ProfileSupervisor(
        inspector=FakeInspector(observations),
        launcher=launcher,
        signals=signals,
        monotonic=clock,
        stop_timeout_seconds=timeout,
        poll_interval_seconds=0.05,
    )
    return supervisor, signals, launcher, clock


def _proc_stat(pid: int, name: str, *, state: str = "S", pgrp: int = 55) -> str:
    fields = [state, "1", str(pgrp), "66", *(["0"] * 15), "12345"]
    return f"{pid} ({name}) " + " ".join(fields) + "\n"


def test_proc_stat_parser_handles_spaces_and_parentheses_in_name() -> None:
    assert parse_proc_stat(_proc_stat(42, "worker (safe) name")) == (
        42,
        "S",
        55,
        66,
        12345,
    )


@pytest.mark.parametrize(
    "value",
    ["", "42 no-parentheses S 1", "x (name) S 1 2", "42 (name) S x 2 3"],
)
def test_proc_stat_parser_rejects_malformed_input(value: str) -> None:
    with pytest.raises(SupervisorError, match="procfs"):
        parse_proc_stat(value)


def test_identity_classification_never_accepts_pid_only_match() -> None:
    expected = _identity()
    assert classify_identity(expected, expected) == "running"
    assert classify_identity(expected, None) == "exited"
    assert classify_identity(expected, _identity(start=999)) == "orphaned"


def test_launch_captures_identity_before_returning_running_component(tmp_path: Path) -> None:
    expected = _identity()
    supervisor, signals, launcher, _ = _supervisor([expected])
    component = supervisor.launch_component(
        "controller",
        ("python", "-m", "ae.controller"),
        cwd=tmp_path,
        environment={"SAFE": "1"},
        log_path=tmp_path / "logs/controller.log",
    )

    assert component.status == "running"
    assert component.process_identity == expected
    assert component.log_path == "logs/controller.log"
    assert launcher.calls[0][0] == ("python", "-m", "ae.controller")
    assert not signals.calls


def test_launch_fails_if_process_exits_before_identity_capture(tmp_path: Path) -> None:
    supervisor, _, _, _ = _supervisor([None])
    with pytest.raises(SupervisorError, match="before exact identity capture"):
        supervisor.launch_component(
            "controller",
            ("python", "-m", "ae.controller"),
            cwd=tmp_path,
            environment={},
            log_path=tmp_path / "logs/controller.log",
        )


def test_prepare_component_captures_exact_identity_before_gated_activation(
    tmp_path: Path,
) -> None:
    expected = _identity()
    launcher = FakeGatedLauncher()
    supervisor = ProfileSupervisor(
        inspector=FakeInspector([expected]),
        launcher=launcher,
        signals=FakeSignals(),
        monotonic=FakeClock(),
    )

    component, pending = supervisor.prepare_component(
        "controller",
        ("/usr/bin/python3", "-m", "ae.kctl.profile_component_runner"),
        cwd=tmp_path,
        environment={"SAFE": "1"},
        log_path=tmp_path / "logs/controller.log",
    )

    assert component.status == "starting"
    assert component.process_identity == expected
    assert pending is launcher.pending
    assert not launcher.pending.activated
    assert not launcher.pending.cancelled


def test_gated_subprocess_creates_only_owner_private_content(tmp_path: Path) -> None:
    launcher = SubprocessLauncher(owner_uid=os.geteuid())
    script = (
        "from pathlib import Path; "
        "root = Path('child-output'); "
        "root.mkdir(); "
        "(root / 'payload').write_text('private\\n', encoding='utf-8')"
    )
    pending = launcher.spawn_gated(
        (sys.executable, "-c", script),
        cwd=tmp_path,
        environment={},
        log_path=tmp_path / "logs" / "component.log",
    )

    pending.activate()
    waited_pid, wait_status = os.waitpid(pending.pid, 0)

    assert waited_pid == pending.pid
    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert stat.S_IMODE((tmp_path / "child-output").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "child-output" / "payload").stat().st_mode) == 0o600


def test_classify_exact_never_adopts_reused_pid() -> None:
    expected = _identity()
    supervisor, _, _, _ = _supervisor([_identity(start=999)])

    assert supervisor.classify_exact(expected) == "orphaned"


def test_stop_sends_term_only_after_exact_identity_and_observes_exit() -> None:
    expected = _identity()
    supervisor, signals, _, _ = _supervisor([expected, None])

    assert supervisor.stop_exact(expected) == StopResult("stopped", term_sent=True)
    assert signals.calls == [(expected.process_group_id, signal.SIGTERM)]


def test_stop_never_signals_reused_pid() -> None:
    expected = _identity()
    supervisor, signals, _, _ = _supervisor([_identity(start=999)])

    assert supervisor.stop_exact(expected) == StopResult("orphaned")
    assert signals.calls == []


def test_kill_requires_second_exact_identity_revalidation() -> None:
    expected = _identity()
    supervisor, signals, _, clock = _supervisor([expected, expected, expected, expected])

    result = supervisor.stop_exact(expected)

    assert result == StopResult("kill-sent", term_sent=True, kill_sent=True)
    assert signals.calls == [
        (expected.process_group_id, signal.SIGTERM),
        (expected.process_group_id, signal.SIGKILL),
    ]
    assert clock.sleeps == [0.05, 0.05]


def test_identity_change_during_term_wait_suppresses_kill() -> None:
    expected = _identity()
    supervisor, signals, _, _ = _supervisor([expected, _identity(start=999)])

    assert supervisor.stop_exact(expected) == StopResult("orphaned", term_sent=True)
    assert signals.calls == [(expected.process_group_id, signal.SIGTERM)]


def _running_state(identity: ProcessIdentity) -> RunState:
    return RunState(
        profile="core",
        requested_name="core",
        run_id="r1-00000000000000000000000000000001",
        generation=1,
        revision=1,
        owner_uid=0,
        phase="running",
        created_at="2026-11-07T12:00:00Z",
        updated_at="2026-11-07T12:00:00Z",
        components=(
            ComponentState(
                component_id="controller",
                command_digest=command_digest(("controller",)),
                log_path="logs/controller.log",
                status="running",
                process_identity=identity,
            ),
        ),
    )


def test_foreground_wait_returns_recorded_signal_without_process_action() -> None:
    expected = _identity()
    supervisor, signals, _, clock = _supervisor([])
    state = _running_state(expected)

    result = supervisor.wait_foreground(
        state,
        state_reader=lambda: state,
        stop_requested=lambda: signal.SIGINT,
    )

    assert result.disposition == "stop-requested"
    assert result.signal_number == signal.SIGINT
    assert signals.calls == []
    assert clock.sleeps == []


def test_foreground_wait_detects_exit_and_identity_mismatch_without_signaling() -> None:
    expected = _identity()
    for observed, disposition in ((None, "component-exited"), (_identity(start=999), "orphaned")):
        supervisor, signals, _, _ = _supervisor([observed])
        state = _running_state(expected)
        result = supervisor.wait_foreground(
            state,
            state_reader=lambda current=state: current,
            stop_requested=lambda: None,
        )
        assert result.disposition == disposition
        assert result.component_id == "controller"
        assert signals.calls == []


def test_foreground_wait_tolerates_exact_same_run_external_stop_progress() -> None:
    expected = _identity()
    supervisor, signals, _, clock = _supervisor([])
    running = _running_state(expected)
    stopping = replace(
        running,
        phase="stopping",
        components=(replace(running.components[0], status="stopped", process_identity=None),),
    )
    stopped = replace(stopping, phase="stopped", cleanup_complete=True)
    states = iter((stopping, stopped))

    result = supervisor.wait_foreground(
        running,
        state_reader=lambda: next(states),
        stop_requested=lambda: None,
    )

    assert result.disposition == "stopped"
    assert signals.calls == []
    assert clock.sleeps == [0.05]


def test_foreground_wait_bounds_same_run_stuck_stopping_state() -> None:
    expected = _identity()
    supervisor, signals, _, clock = _supervisor([], timeout=0.1)
    running = _running_state(expected)
    stopping = replace(
        running,
        phase="stopping",
        components=(replace(running.components[0], status="stopped", process_identity=None),),
    )

    with pytest.raises(SupervisorError, match="did not settle"):
        supervisor.wait_foreground(
            stopping,
            state_reader=lambda: stopping,
            stop_requested=lambda: None,
        )

    assert signals.calls == []
    assert clock.sleeps == [0.05, 0.05]


def test_foreground_wait_rejects_identity_substitution_while_stopping() -> None:
    expected = _identity()
    replacement = _identity(start=999)
    supervisor, signals, _, _ = _supervisor([])
    running = _running_state(expected)
    stopping = replace(
        running,
        phase="stopping",
        components=(replace(running.components[0], process_identity=replacement),),
    )

    result = supervisor.wait_foreground(
        running,
        state_reader=lambda: stopping,
        stop_requested=lambda: None,
    )

    assert result.disposition == "orphaned"
    assert signals.calls == []
