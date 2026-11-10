"""Bounded, explicitly selected engine capability inspection.

Importing this module never contacts Podman or Docker.  The system boundary is
behind :class:`CommandRunner` so source tests can prove the contract with
deterministic fakes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

EngineName = Literal["podman", "docker"]
QUALIFICATION_POSTURE = "source-validated/unqualified"
MAX_ENGINE_OUTPUT_BYTES = 256 * 1024


class EngineCapabilityError(RuntimeError):
    """An explicitly selected engine cannot be identified safely."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    """Injected subprocess boundary for a bounded read-only engine query."""

    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CommandResult: ...


class ExecutableResolver(Protocol):
    def resolve(self, name: str) -> Path: ...


class SystemExecutableResolver:
    """Resolve one executable to an absolute canonical regular file."""

    def resolve(self, name: str) -> Path:
        if name not in {"podman", "docker"}:
            raise EngineCapabilityError("unsupported engine executable")
        candidate = shutil.which(name)
        if candidate is None:
            raise EngineCapabilityError(f"selected {name} executable is unavailable")
        path = Path(candidate).resolve(strict=True)
        info = path.stat()
        if not path.is_absolute() or not stat.S_ISREG(info.st_mode):
            raise EngineCapabilityError("engine executable is not a regular absolute file")
        if info.st_mode & 0o111 == 0:
            raise EngineCapabilityError("engine executable is not executable")
        return path


class SystemCommandRunner:
    """Real boundary used only by an explicit doctor/start invocation."""

    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CommandResult:
        if not argv or not Path(argv[0]).is_absolute():
            raise EngineCapabilityError("engine command must use an absolute executable")
        try:
            completed = subprocess.run(  # noqa: S603
                list(argv),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EngineCapabilityError("selected engine capability query failed") from exc
        if len(completed.stdout) > output_limit or len(completed.stderr) > output_limit:
            raise EngineCapabilityError("selected engine capability output exceeded its limit")
        return CommandResult(
            returncode=int(completed.returncode),
            stdout=bytes(completed.stdout),
            stderr=bytes(completed.stderr),
        )


@dataclass(frozen=True, slots=True)
class EngineCapability:
    """Sanitized immutable identity for one explicitly selected engine."""

    backend: EngineName
    executable: Path
    executable_device: int
    executable_inode: int
    version: str
    rootless: bool
    qualification_posture: str = QUALIFICATION_POSTURE

    def __post_init__(self) -> None:
        if self.backend not in {"podman", "docker"}:
            raise EngineCapabilityError("unsupported engine backend")
        if not self.executable.is_absolute():
            raise EngineCapabilityError("engine executable identity must be absolute")
        if self.executable_device < 0 or self.executable_inode <= 0:
            raise EngineCapabilityError("engine executable identity is invalid")
        if (
            not self.version
            or self.version != self.version.strip()
            or "\x00" in self.version
            or "\n" in self.version
            or len(self.version) > 128
        ):
            raise EngineCapabilityError("engine version identity is invalid")
        if type(self.rootless) is not bool:
            raise EngineCapabilityError("engine rootless identity must be a bool")
        if self.qualification_posture != QUALIFICATION_POSTURE:
            raise EngineCapabilityError("live engine qualification cannot be claimed here")

    def identity_payload(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "executable": str(self.executable),
            "executableDevice": self.executable_device,
            "executableInode": self.executable_inode,
            "qualificationPosture": self.qualification_posture,
            "rootless": self.rootless,
            "version": self.version,
        }

    def digest(self) -> str:
        payload = json.dumps(
            self.identity_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _strict_json(payload: bytes, *, context: str) -> dict[str, object]:
    if not payload or len(payload) > MAX_ENGINE_OUTPUT_BYTES or b"\x00" in payload:
        raise EngineCapabilityError(f"{context} output is invalid")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineCapabilityError(f"{context} output is not JSON") from exc
    if type(value) is not dict:
        raise EngineCapabilityError(f"{context} output must be an object")
    return cast(dict[str, object], value)


def _nested(value: Mapping[str, object], *keys: str) -> object:
    current: object = value
    for key in keys:
        if type(current) is not dict or key not in current:
            return None
        current = cast(dict[str, object], current)[key]
    return current


class SelectedEngineProbe:
    """Probe exactly one requested engine; never fall back to the other."""

    def __init__(
        self,
        *,
        runner: CommandRunner,
        resolver: ExecutableResolver,
        timeout_seconds: float = 5.0,
        effective_uid: int | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise EngineCapabilityError("engine capability timeout is outside the safe range")
        self.runner = runner
        self.resolver = resolver
        self.timeout_seconds = timeout_seconds
        self.effective_uid = os.geteuid() if effective_uid is None else effective_uid
        if type(self.effective_uid) is not int or self.effective_uid < 0:
            raise EngineCapabilityError("effective user identity is invalid")

    @classmethod
    def system(cls) -> SelectedEngineProbe:
        return cls(runner=SystemCommandRunner(), resolver=SystemExecutableResolver())

    def probe(self, backend: str) -> EngineCapability:
        if backend not in {"podman", "docker"}:
            raise EngineCapabilityError("engine must be explicitly podman or docker")
        selected = cast(EngineName, backend)
        executable = self.resolver.resolve(selected)
        before = executable.stat()
        environment = self._environment()
        if selected == "podman":
            version_result = self._run(
                (str(executable), "version", "--format", "json"), environment
            )
            version_json = _strict_json(version_result.stdout, context="podman version")
            version = _nested(version_json, "Client", "Version") or version_json.get("Version")
            if type(version) is not str:
                raise EngineCapabilityError("podman capability fields are missing or ambiguous")
            rootless = self.effective_uid != 0
            if not rootless:
                raise EngineCapabilityError("selected Podman engine is not rootless")
        else:
            version_result = self._run(
                (str(executable), "version", "--format", "{{json .}}"), environment
            )
            version_json = _strict_json(version_result.stdout, context="docker version")
            version = _nested(version_json, "Server", "Version")
            operating_system = _nested(version_json, "Server", "Os") or _nested(
                version_json, "Server", "Platform", "Name"
            )
            if type(version) is not str or type(operating_system) is not str:
                raise EngineCapabilityError("docker capability fields are missing or ambiguous")
            if "linux" not in operating_system.lower():
                raise EngineCapabilityError("selected Docker server is not Linux")
            rootless = False
        after = executable.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise EngineCapabilityError("engine executable identity changed during inspection")
        return EngineCapability(
            backend=selected,
            executable=executable,
            executable_device=after.st_dev,
            executable_inode=after.st_ino,
            version=version,
            rootless=rootless,
        )

    def _run(self, argv: Sequence[str], environment: Mapping[str, str]) -> CommandResult:
        result = self.runner.run(
            argv,
            environment=environment,
            timeout_seconds=self.timeout_seconds,
            output_limit=MAX_ENGINE_OUTPUT_BYTES,
        )
        if result.returncode != 0:
            raise EngineCapabilityError("selected engine capability query returned nonzero")
        return result

    @staticmethod
    def _environment() -> dict[str, str]:
        # Version queries receive no ambient proxy, credential, endpoint, HOME,
        # XDG, or provider state.  They cannot initialize a machine or daemon.
        return {"PATH": os.defpath, "LC_ALL": "C.UTF-8", "TZ": "UTC"}


__all__ = [
    "CommandResult",
    "CommandRunner",
    "EngineCapability",
    "EngineCapabilityError",
    "EngineName",
    "ExecutableResolver",
    "MAX_ENGINE_OUTPUT_BYTES",
    "QUALIFICATION_POSTURE",
    "SelectedEngineProbe",
    "SystemCommandRunner",
    "SystemExecutableResolver",
]
