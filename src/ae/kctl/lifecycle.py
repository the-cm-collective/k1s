"""Deterministic lifecycle contracts for supported k1s runtime profiles."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TextIO

ProfileName = Literal["core", "edge", "core-edge", "edge-core"]
LifecycleAction = Literal["start", "status", "stop", "restart"]

PROFILE_NAMES: tuple[ProfileName, ...] = ("core", "edge", "core-edge", "edge-core")
LIFECYCLE_ACTIONS: tuple[LifecycleAction, ...] = ("start", "status", "stop", "restart")


@dataclass(frozen=True, slots=True)
class ProfileContract:
    """Stable mapping from a public profile name to the existing runtime helper."""

    name: ProfileName
    runtime_profile: str
    environment: tuple[tuple[str, str], ...] = ()


PROFILE_CONTRACTS: Mapping[ProfileName, ProfileContract] = {
    "core": ProfileContract(name="core", runtime_profile="k1s-core"),
    "edge": ProfileContract(name="edge", runtime_profile="k1s-edge"),
    "core-edge": ProfileContract(
        name="core-edge",
        runtime_profile="k1s-core",
        environment=(("AE_TRANSPORT_BACKEND", "nats-core"),),
    ),
    "edge-core": ProfileContract(
        name="edge-core",
        runtime_profile="k1s-edge",
        environment=(("EDGE_PROFILE", "k1s-core"),),
    ),
}


def profile_contract(name: str) -> ProfileContract:
    """Return the canonical profile contract or reject an unknown public name."""

    if name not in PROFILE_NAMES:
        choices = ", ".join(PROFILE_NAMES)
        raise ValueError(f"unsupported profile {name!r}; expected one of: {choices}")
    return PROFILE_CONTRACTS[name]


class ProcessBackend(Protocol):
    """Process boundary used by lifecycle handlers and deterministic unit fakes."""

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log: TextIO,
    ) -> int: ...

    def is_running(self, pid: int) -> bool: ...

    def terminate(self, pid: int, *, timeout_seconds: float) -> None: ...


class SubprocessBackend:
    """Host process implementation used only when an operator invokes the CLI."""

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log: TextIO,
    ) -> int:
        process = subprocess.Popen(  # noqa: S603
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        return int(process.pid)

    def is_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def terminate(self, pid: int, *, timeout_seconds: float) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.is_running(pid):
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class LifecycleState:
    """Minimal non-secret state required to address one managed process."""

    schema_version: int
    profile: ProfileName
    pid: int
    command: tuple[str, ...]
    started_at: str


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Stable result returned by every lifecycle action."""

    action: LifecycleAction
    profile: ProfileName
    status: Literal["started", "running", "stopped", "restarted"]
    exit_code: int
    pid: int | None = None
    command: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class LifecycleManager:
    """Manage supported profiles through an injectable process boundary."""

    def __init__(
        self,
        *,
        repo_root: Path,
        state_root: Path,
        process: ProcessBackend,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
        stop_timeout_seconds: float = 10.0,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.state_root = state_root.resolve()
        self.process = process
        self.environ = dict(environ if environ is not None else os.environ)
        self.clock = clock or _utc_now
        self.stop_timeout_seconds = stop_timeout_seconds

    @classmethod
    def from_environment(cls) -> LifecycleManager:
        repo_root = Path(__file__).resolve().parents[3]
        configured = os.getenv("K1S_PROFILE_LIFECYCLE_DIR")
        state_root = (
            Path(configured).expanduser() if configured else repo_root / "state/k1s-lifecycle"
        )
        return cls(repo_root=repo_root, state_root=state_root, process=SubprocessBackend())

    def perform(self, action: str, profile: str) -> LifecycleResult:
        if action not in LIFECYCLE_ACTIONS:
            choices = ", ".join(LIFECYCLE_ACTIONS)
            raise ValueError(f"unsupported lifecycle action {action!r}; expected one of: {choices}")
        canonical_action = action
        if canonical_action == "start":
            return self.start(profile)
        if canonical_action == "status":
            return self.status(profile)
        if canonical_action == "stop":
            return self.stop(profile)
        return self.restart(profile)

    def start(self, profile: str) -> LifecycleResult:
        contract = profile_contract(profile)
        state = self._read_state(contract)
        if state is not None and self.process.is_running(state.pid):
            return LifecycleResult(
                action="start",
                profile=contract.name,
                status="running",
                exit_code=0,
                pid=state.pid,
                command=state.command,
            )
        if state is not None:
            self._clear_state(contract)

        command = (str(self.repo_root / "scripts/dev/run_profile.sh"), contract.runtime_profile)
        environment = dict(self.environ)
        environment.update(dict(contract.environment))
        log_path = self._log_path(contract)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            pid = self.process.spawn(
                command,
                cwd=self.repo_root,
                environment=environment,
                log=log,
            )
        new_state = LifecycleState(
            schema_version=1,
            profile=contract.name,
            pid=pid,
            command=command,
            started_at=self.clock(),
        )
        self._write_state(contract, new_state)
        return LifecycleResult(
            action="start",
            profile=contract.name,
            status="started",
            exit_code=0,
            pid=pid,
            command=command,
        )

    def status(self, profile: str) -> LifecycleResult:
        contract = profile_contract(profile)
        state = self._read_state(contract)
        if state is not None and self.process.is_running(state.pid):
            return LifecycleResult(
                action="status",
                profile=contract.name,
                status="running",
                exit_code=0,
                pid=state.pid,
                command=state.command,
            )
        if state is not None:
            self._clear_state(contract)
        return LifecycleResult(
            action="status",
            profile=contract.name,
            status="stopped",
            exit_code=3,
        )

    def stop(self, profile: str) -> LifecycleResult:
        contract = profile_contract(profile)
        state = self._read_state(contract)
        if state is not None and self.process.is_running(state.pid):
            self.process.terminate(state.pid, timeout_seconds=self.stop_timeout_seconds)
        if state is not None:
            self._clear_state(contract)
        return LifecycleResult(
            action="stop",
            profile=contract.name,
            status="stopped",
            exit_code=0,
            pid=state.pid if state is not None else None,
            command=state.command if state is not None else (),
        )

    def restart(self, profile: str) -> LifecycleResult:
        contract = profile_contract(profile)
        self.stop(contract.name)
        result = self.start(contract.name)
        return LifecycleResult(
            action="restart",
            profile=contract.name,
            status="restarted",
            exit_code=result.exit_code,
            pid=result.pid,
            command=result.command,
        )

    def _state_path(self, contract: ProfileContract) -> Path:
        return self.state_root / f"{contract.name}.json"

    def _log_path(self, contract: ProfileContract) -> Path:
        return self.state_root / f"{contract.name}.log"

    def _read_state(self, contract: ProfileContract) -> LifecycleState | None:
        path = self._state_path(contract)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 1 or raw.get("profile") != contract.name:
                return None
            return LifecycleState(
                schema_version=1,
                profile=contract.name,
                pid=int(raw["pid"]),
                command=tuple(str(part) for part in raw["command"]),
                started_at=str(raw["started_at"]),
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_state(self, contract: ProfileContract, state: LifecycleState) -> None:
        path = self._state_path(contract)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(state), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _clear_state(self, contract: ProfileContract) -> None:
        self._state_path(contract).unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
