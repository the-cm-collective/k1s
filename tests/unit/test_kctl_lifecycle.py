from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

import pytest

from ae.kctl.lifecycle import LifecycleManager, profile_contract


class FakeProcessBackend:
    def __init__(self) -> None:
        self.next_pid = 4100
        self.running: set[int] = set()
        self.spawns: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.terminations: list[tuple[int, float]] = []

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log: TextIO,
    ) -> int:
        del log
        pid = self.next_pid
        self.next_pid += 1
        self.running.add(pid)
        self.spawns.append((tuple(command), cwd, dict(environment)))
        return pid

    def is_running(self, pid: int) -> bool:
        return pid in self.running

    def terminate(self, pid: int, *, timeout_seconds: float) -> None:
        self.terminations.append((pid, timeout_seconds))
        self.running.discard(pid)


def _manager(tmp_path: Path, process: FakeProcessBackend) -> LifecycleManager:
    return LifecycleManager(
        repo_root=tmp_path / "repo",
        state_root=tmp_path / "state",
        process=process,
        environ={"BASELINE": "kept"},
        clock=lambda: "2026-11-04T00:00:00+00:00",
        stop_timeout_seconds=2.5,
    )


@pytest.mark.parametrize(
    ("profile", "runtime_profile", "overrides"),
    [
        ("core", "k1s-core", {}),
        ("edge", "k1s-edge", {}),
        ("core-edge", "k1s-core", {"AE_TRANSPORT_BACKEND": "nats-core"}),
        ("edge-core", "k1s-edge", {"EDGE_PROFILE": "k1s-core"}),
    ],
)
def test_profile_start_contracts_are_deterministic(
    tmp_path: Path,
    profile: str,
    runtime_profile: str,
    overrides: dict[str, str],
) -> None:
    process = FakeProcessBackend()
    manager = _manager(tmp_path, process)

    result = manager.start(profile)

    assert result.status == "started"
    assert result.exit_code == 0
    assert result.pid == 4100
    command, cwd, environment = process.spawns[0]
    assert command == (
        str((tmp_path / "repo/scripts/dev/run_profile.sh").resolve()),
        runtime_profile,
    )
    assert cwd == (tmp_path / "repo").resolve()
    assert environment == {"BASELINE": "kept", **overrides}
    state = json.loads((tmp_path / f"state/{profile}.json").read_text(encoding="utf-8"))
    assert state == {
        "command": list(command),
        "pid": 4100,
        "profile": profile,
        "schema_version": 1,
        "started_at": "2026-11-04T00:00:00+00:00",
    }


def test_start_is_idempotent_and_status_reports_running(tmp_path: Path) -> None:
    process = FakeProcessBackend()
    manager = _manager(tmp_path, process)

    first = manager.start("core")
    second = manager.start("core")
    status = manager.status("core")

    assert first.status == "started"
    assert second.status == "running"
    assert status.status == "running"
    assert status.exit_code == 0
    assert len(process.spawns) == 1


def test_stop_is_idempotent_and_clears_profile_state(tmp_path: Path) -> None:
    process = FakeProcessBackend()
    manager = _manager(tmp_path, process)
    started = manager.start("edge")
    assert started.pid is not None

    stopped = manager.stop("edge")
    stopped_again = manager.stop("edge")
    status = manager.status("edge")

    assert process.terminations == [(started.pid, 2.5)]
    assert stopped.status == "stopped"
    assert stopped.exit_code == 0
    assert stopped_again.status == "stopped"
    assert status.status == "stopped"
    assert status.exit_code == 3
    assert not (tmp_path / "state/edge.json").exists()


def test_restart_terminates_the_previous_process_and_spawns_again(tmp_path: Path) -> None:
    process = FakeProcessBackend()
    manager = _manager(tmp_path, process)
    first = manager.start("core-edge")
    assert first.pid is not None

    restarted = manager.restart("core-edge")

    assert process.terminations == [(first.pid, 2.5)]
    assert len(process.spawns) == 2
    assert restarted.status == "restarted"
    assert restarted.pid == 4101


def test_stale_state_is_replaced_without_terminating_an_unrelated_pid(tmp_path: Path) -> None:
    process = FakeProcessBackend()
    manager = _manager(tmp_path, process)
    first = manager.start("edge-core")
    process.running.discard(first.pid or 0)

    replacement = manager.start("edge-core")

    assert process.terminations == []
    assert replacement.pid == 4101
    assert len(process.spawns) == 2


def test_unknown_profile_and_action_are_rejected(tmp_path: Path) -> None:
    process = FakeProcessBackend()
    manager = _manager(tmp_path, process)

    with pytest.raises(ValueError, match="unsupported profile"):
        profile_contract("unknown")
    with pytest.raises(ValueError, match="unsupported lifecycle action"):
        manager.perform("destroy", "core")
