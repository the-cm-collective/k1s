"""Fakeable exact-identity process supervision for installed k1s profiles."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ae.kctl.profile_state import (
    ComponentState,
    FailureRecord,
    ProcessIdentity,
    ProfileStateError,
    ProfileStateStore,
    ResourceCleaner,
    RunState,
    command_digest,
)

ProcessDisposition = Literal["running", "exited", "orphaned"]
StopDisposition = Literal["stopped", "term-sent", "kill-sent", "orphaned"]
ForegroundDisposition = Literal["stop-requested", "component-exited", "orphaned", "stopped"]


class SupervisorError(ProfileStateError):
    """A process cannot be inspected or controlled without ambiguity."""


class ProcInspector(Protocol):
    """Read exact process identity without treating a PID as authority."""

    def inspect(self, pid: int) -> ProcessIdentity | None: ...


class ProcessLauncher(Protocol):
    """Spawn boundary injected into tests and later portable backends."""

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> int: ...


class GatedProcess(Protocol):
    """One child that cannot run its component before durable activation."""

    pid: int

    def activate(self) -> None: ...

    def cancel(self) -> None: ...


class GatedProcessLauncher(ProcessLauncher, Protocol):
    def spawn_gated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> GatedProcess: ...


class SignalBackend(Protocol):
    """Signal boundary used only after complete identity revalidation."""

    def signal_group(self, process_group_id: int, signal_number: int) -> None: ...


class MonotonicClock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class StopResult:
    disposition: StopDisposition
    term_sent: bool = False
    kill_sent: bool = False


@dataclass(frozen=True, slots=True)
class ForegroundWaitResult:
    disposition: ForegroundDisposition
    signal_number: int | None = None
    component_id: str | None = None


@dataclass(slots=True)
class PendingGatedProcess:
    pid: int
    activation_fd: int
    activated: bool = False

    def activate(self) -> None:
        if self.activated or self.activation_fd < 0:
            raise SupervisorError("component activation gate is already closed")
        try:
            if os.write(self.activation_fd, b"1") != 1:
                raise SupervisorError("component activation gate write was incomplete")
        except OSError as exc:
            raise SupervisorError("component activation gate cannot be written") from exc
        finally:
            os.close(self.activation_fd)
            self.activation_fd = -1
        self.activated = True

    def cancel(self) -> None:
        if self.activation_fd >= 0:
            os.close(self.activation_fd)
            self.activation_fd = -1


class SystemMonotonicClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class OSSignalBackend:
    def signal_group(self, process_group_id: int, signal_number: int) -> None:
        try:
            os.killpg(process_group_id, signal_number)
        except ProcessLookupError:
            return


class SubprocessLauncher:
    """Linux launcher; no command is started unless an explicit caller invokes it."""

    def __init__(self, *, owner_uid: int | None = None) -> None:
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> int:
        command_digest(argv)
        if not cwd.is_absolute():
            raise SupervisorError("component working directory must be absolute")
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = log_path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != self.owner_uid
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise SupervisorError("component log directory is not owner-private")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(log_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise SupervisorError("component log is not owner-private")
            with os.fdopen(fd, "ab", closefd=False) as log:
                process = subprocess.Popen(  # noqa: S603
                    list(argv),
                    cwd=cwd,
                    env=dict(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                    umask=0o077,
                )
        finally:
            os.close(fd)
        return int(process.pid)

    def spawn_gated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> PendingGatedProcess:
        command_digest(argv)
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        child_environment = dict(environment)
        child_environment["K1S_COMPONENT_GATE_FD"] = str(read_fd)
        try:
            pid = self._spawn_with_fds(
                argv,
                cwd=cwd,
                environment=child_environment,
                log_path=log_path,
                pass_fds=(read_fd,),
            )
        except Exception:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        return PendingGatedProcess(pid=pid, activation_fd=write_fd)

    def _spawn_with_fds(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
        pass_fds: tuple[int, ...],
    ) -> int:
        command_digest(argv)
        if not cwd.is_absolute():
            raise SupervisorError("component working directory must be absolute")
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = log_path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != self.owner_uid
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise SupervisorError("component log directory is not owner-private")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(log_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise SupervisorError("component log is not owner-private")
            with os.fdopen(fd, "ab", closefd=False) as log:
                process = subprocess.Popen(  # noqa: S603
                    list(argv),
                    cwd=cwd,
                    env=dict(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    pass_fds=pass_fds,
                    start_new_session=True,
                    umask=0o077,
                )
        finally:
            os.close(fd)
        return int(process.pid)


class LinuxProcInspector:
    """Read process identity from procfs with a parser safe for parenthesized names."""

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        if not proc_root.is_absolute():
            raise SupervisorError("procfs root must be absolute")
        self.proc_root = proc_root

    def inspect(self, pid: int) -> ProcessIdentity | None:
        if type(pid) is not int or pid <= 0:
            raise SupervisorError("PID must be a positive integer")
        process_root = self.proc_root / str(pid)
        try:
            stat_text = (process_root / "stat").read_text(encoding="utf-8")
            boot_id = (self.proc_root / "sys/kernel/random/boot_id").read_text(encoding="utf-8")
            executable = (process_root / "exe").stat()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise SupervisorError("process identity cannot be inspected safely") from exc
        parsed_pid, state, process_group_id, session_id, start_time_ticks = parse_proc_stat(
            stat_text
        )
        if parsed_pid != pid:
            raise SupervisorError("procfs PID differs from the requested process")
        if state == "Z":
            return None
        boot_id = boot_id.strip()
        return ProcessIdentity(
            pid=pid,
            process_group_id=process_group_id,
            session_id=session_id,
            boot_id=boot_id,
            start_time_ticks=start_time_ticks,
            executable_device=executable.st_dev,
            executable_inode=executable.st_ino,
        )


def parse_proc_stat(value: str) -> tuple[int, str, int, int, int]:
    """Parse the identity fields from Linux ``/proc/<pid>/stat``."""

    if not isinstance(value, str) or "\x00" in value or "\n" in value.strip("\n"):
        raise SupervisorError("procfs stat record is malformed")
    open_index = value.find("(")
    close_index = value.rfind(")")
    if open_index <= 0 or close_index <= open_index or close_index + 2 >= len(value):
        raise SupervisorError("procfs stat record is malformed")
    try:
        pid = int(value[:open_index].strip())
    except ValueError as exc:
        raise SupervisorError("procfs stat PID is malformed") from exc
    fields = value[close_index + 2 :].strip().split()
    if len(fields) < 20:
        raise SupervisorError("procfs stat record is incomplete")
    state = fields[0]
    if len(state) != 1:
        raise SupervisorError("procfs process state is malformed")
    try:
        process_group_id = int(fields[2])
        session_id = int(fields[3])
        start_time_ticks = int(fields[19])
    except ValueError as exc:
        raise SupervisorError("procfs identity field is malformed") from exc
    if pid <= 0 or process_group_id <= 0 or session_id <= 0 or start_time_ticks < 0:
        raise SupervisorError("procfs identity field is outside the accepted range")
    return pid, state, process_group_id, session_id, start_time_ticks


def classify_identity(
    expected: ProcessIdentity, observed: ProcessIdentity | None
) -> ProcessDisposition:
    """Classify an observation without ever adopting a PID-only match."""

    if observed is None:
        return "exited"
    return "running" if observed == expected else "orphaned"


class ProfileSupervisor:
    """Launch, stop, and recover components through injected boundaries."""

    def __init__(
        self,
        *,
        inspector: ProcInspector,
        launcher: ProcessLauncher,
        signals: SignalBackend,
        monotonic: MonotonicClock,
        stop_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if stop_timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise SupervisorError("supervisor timing values must be positive")
        self.inspector = inspector
        self.launcher = launcher
        self.signals = signals
        self.monotonic = monotonic
        self.stop_timeout_seconds = stop_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    @classmethod
    def system(cls) -> ProfileSupervisor:
        return cls(
            inspector=LinuxProcInspector(),
            launcher=SubprocessLauncher(),
            signals=OSSignalBackend(),
            monotonic=SystemMonotonicClock(),
        )

    def launch_component(
        self,
        component_id: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> ComponentState:
        digest = command_digest(argv)
        pid = self.launcher.spawn(
            argv,
            cwd=cwd,
            environment=environment,
            log_path=log_path,
        )
        identity = self.inspector.inspect(pid)
        if identity is None:
            raise SupervisorError("component exited before exact identity capture")
        return ComponentState(
            component_id=component_id,
            command_digest=digest,
            log_path=f"logs/{log_path.name}",
            status="running",
            process_identity=identity,
        )

    def prepare_component(
        self,
        component_id: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> tuple[ComponentState, GatedProcess]:
        """Spawn a blocked child and return its exact identity before activation."""

        spawn_gated = getattr(self.launcher, "spawn_gated", None)
        if spawn_gated is None:
            raise SupervisorError("portable components require an activation-gated launcher")
        pending = spawn_gated(
            argv,
            cwd=cwd,
            environment=environment,
            log_path=log_path,
        )
        try:
            identity = self.inspector.inspect(pending.pid)
            if identity is None:
                raise SupervisorError("gated component exited before exact identity capture")
            component = ComponentState(
                component_id=component_id,
                command_digest=command_digest(argv),
                log_path=f"logs/{log_path.name}",
                status="starting",
                process_identity=identity,
            )
        except Exception:
            pending.cancel()
            raise
        return component, pending

    def classify_exact(self, identity: ProcessIdentity) -> ProcessDisposition:
        """Revalidate one recorded identity without adopting a PID-only match."""

        return classify_identity(identity, self.inspector.inspect(identity.pid))

    def stop_exact(self, expected: ProcessIdentity) -> StopResult:
        disposition = classify_identity(expected, self.inspector.inspect(expected.pid))
        if disposition == "exited":
            return StopResult("stopped")
        if disposition == "orphaned":
            return StopResult("orphaned")

        self.signals.signal_group(expected.process_group_id, signal.SIGTERM)
        deadline = self.monotonic.now() + self.stop_timeout_seconds
        while self.monotonic.now() < deadline:
            disposition = classify_identity(expected, self.inspector.inspect(expected.pid))
            if disposition == "exited":
                return StopResult("stopped", term_sent=True)
            if disposition == "orphaned":
                return StopResult("orphaned", term_sent=True)
            self.monotonic.sleep(self.poll_interval_seconds)

        disposition = classify_identity(expected, self.inspector.inspect(expected.pid))
        if disposition == "exited":
            return StopResult("stopped", term_sent=True)
        if disposition == "orphaned":
            return StopResult("orphaned", term_sent=True)
        self.signals.signal_group(expected.process_group_id, signal.SIGKILL)
        return StopResult("kill-sent", term_sent=True, kill_sent=True)

    def wait_foreground(
        self,
        run: RunState,
        *,
        state_reader: Callable[[], RunState | None],
        stop_requested: Callable[[], int | None],
    ) -> ForegroundWaitResult:
        """Wait on recorded identities only; never discover or adopt a process."""

        expected = {
            component.component_id: component.process_identity
            for component in run.components
            if component.process_identity is not None
        }
        if not expected and run.phase != "stopping":
            raise SupervisorError("foreground run has no recorded process identities")
        stopping_deadline: float | None = None
        while True:
            requested = stop_requested()
            if requested is not None:
                if requested not in {signal.SIGINT, signal.SIGTERM}:
                    raise SupervisorError("foreground stop request is unsupported")
                return ForegroundWaitResult("stop-requested", signal_number=requested)
            current = state_reader()
            if current is None or current.run_id != run.run_id:
                return ForegroundWaitResult("orphaned")
            if current.phase == "stopped":
                return ForegroundWaitResult("stopped")
            if current.phase == "stopping":
                if stopping_deadline is None:
                    stopping_deadline = self.monotonic.now() + self.stop_timeout_seconds
                if self.monotonic.now() >= stopping_deadline:
                    raise SupervisorError("external stop did not settle within its deadline")
            else:
                stopping_deadline = None
            current_components = {
                component.component_id: component for component in current.components
            }
            for component_id, identity in expected.items():
                component = current_components.get(component_id)
                if component is None:
                    return ForegroundWaitResult("orphaned", component_id=component_id)
                if (
                    current.phase == "stopping"
                    and component.status == "stopped"
                    and component.process_identity is None
                ):
                    continue
                if component.process_identity != identity:
                    return ForegroundWaitResult("orphaned", component_id=component_id)
                disposition = self.classify_exact(identity)
                if disposition == "exited":
                    if current.phase == "stopping":
                        continue
                    return ForegroundWaitResult("component-exited", component_id=component_id)
                if disposition == "orphaned":
                    return ForegroundWaitResult("orphaned", component_id=component_id)
            self.monotonic.sleep(self.poll_interval_seconds)

    def recover(self, store: ProfileStateStore, cleaner: ResourceCleaner) -> RunState | None:
        """Recover one current run without process discovery or ownership guessing."""

        state = store.inspect()
        if state is None or state.phase == "stopped":
            return state

        observations: list[tuple[ComponentState, ProcessDisposition]] = []
        for component in state.components:
            if component.process_identity is None:
                continue
            observations.append(
                (
                    component,
                    classify_identity(
                        component.process_identity,
                        self.inspector.inspect(component.process_identity.pid),
                    ),
                )
            )

        all_running = (
            bool(observations)
            and len(observations) == len(state.components)
            and all(disposition == "running" for _, disposition in observations)
        )
        if all_running:
            if state.phase == "starting":
                return store.transition(
                    state.run_id,
                    "running",
                    expected_revision=state.revision,
                )
            if state.phase == "running":
                return state

        if any(disposition == "orphaned" for _, disposition in observations):
            state = self._enter_orphaned(store, state)
        elif state.phase not in {"failed", "stopping", "orphaned"}:
            state = store.transition(
                state.run_id,
                "failed",
                expected_revision=state.revision,
                failure=FailureRecord(
                    code="component-exited",
                    summary="A recorded component is no longer running.",
                ),
            )
        state = self._enter_stopping(store, state)

        became_orphaned = False
        for component, disposition in observations:
            if disposition != "running":
                continue
            assert component.process_identity is not None
            result = self.stop_exact(component.process_identity)
            if result.disposition in {"kill-sent", "term-sent"}:
                return store.inspect()
            if result.disposition == "orphaned":
                became_orphaned = True
        if became_orphaned:
            state = self._enter_orphaned(store, state)
            state = self._enter_stopping(store, state)

        state = store.cleanup_owned(state.run_id, cleaner)
        state = store.settle_components(state.run_id, expected_revision=state.revision)
        return store.transition(
            state.run_id,
            "stopped",
            expected_revision=state.revision,
            cleanup_complete=True,
        )

    @staticmethod
    def _enter_orphaned(store: ProfileStateStore, state: RunState) -> RunState:
        if state.phase == "orphaned":
            return state
        if state.phase == "failed":
            return state
        return store.transition(
            state.run_id,
            "orphaned",
            expected_revision=state.revision,
            failure=FailureRecord(
                code="process-identity-mismatch",
                summary="A PID exists but its exact process identity no longer matches.",
            ),
        )

    @staticmethod
    def _enter_stopping(store: ProfileStateStore, state: RunState) -> RunState:
        if state.phase == "stopping":
            return state
        return store.transition(
            state.run_id,
            "stopping",
            expected_revision=state.revision,
            failure=state.failure,
        )


__all__ = [
    "ForegroundDisposition",
    "ForegroundWaitResult",
    "LinuxProcInspector",
    "MonotonicClock",
    "OSSignalBackend",
    "ProcInspector",
    "ProcessDisposition",
    "ProcessLauncher",
    "GatedProcess",
    "GatedProcessLauncher",
    "PendingGatedProcess",
    "ProfileSupervisor",
    "SignalBackend",
    "StopDisposition",
    "StopResult",
    "SubprocessLauncher",
    "SupervisorError",
    "SystemMonotonicClock",
    "classify_identity",
    "parse_proc_stat",
]
