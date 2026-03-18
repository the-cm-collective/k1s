from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ae.controller.__main__ import main


class _FakeAuthority:
    def __init__(self, *, is_leader: bool) -> None:
        self._is_leader = is_leader

    def start(self) -> None:
        return None

    def wait_until_ready(self, timeout=None) -> bool:
        return True

    def snapshot(self):
        return SimpleNamespace(is_leader=self._is_leader)

    def stop(self) -> None:
        return None


def test_controller_ha_starts_and_stops_cronjob_authority(tmp_path, monkeypatch) -> None:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    db_path = tmp_path / "state.db"
    calls = {"started": 0, "stopped": 0}

    class _FakeCronJobAuthorityController:
        def __init__(self, store, *, config=None, authority=None) -> None:
            self.store = store
            self.config = config
            self.authority = authority

        def start(self) -> None:
            calls["started"] += 1

        def stop(self) -> None:
            calls["stopped"] += 1

    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: _FakeAuthority(is_leader=True),
    )
    monkeypatch.setattr(
        "ae.controller.__main__.CronJobAuthorityController",
        _FakeCronJobAuthorityController,
    )

    assert main(["--once", "--specs", str(specs_dir)]) == 0
    assert calls == {"started": 1, "stopped": 1}
