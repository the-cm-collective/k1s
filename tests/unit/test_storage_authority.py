from __future__ import annotations

import time
from types import SimpleNamespace

from ae.controller.storage_authority import StorageAuthorityRunner


class _FakeAuthority:
    def __init__(self, *, is_leader: bool) -> None:
        self.is_leader = is_leader

    def snapshot(self):
        return SimpleNamespace(is_leader=self.is_leader)


def test_storage_authority_runner_starts_on_leadership_and_stops_on_loss() -> None:
    authority = _FakeAuthority(is_leader=True)
    calls: list[str] = []

    class _FakeStorageController:
        def sync(self) -> int:
            calls.append("sync")
            return 1

        def start(self) -> None:
            calls.append("start")

        def stop(self) -> None:
            calls.append("stop")

    runner = StorageAuthorityRunner(
        object(),
        authority=authority,
        poll_interval_s=0.05,
        controller_factory=lambda _store: _FakeStorageController(),
    )
    runner.start()
    time.sleep(0.12)
    authority.is_leader = False
    time.sleep(0.12)
    runner.stop()

    assert calls[:2] == ["sync", "start"]
    assert "stop" in calls
