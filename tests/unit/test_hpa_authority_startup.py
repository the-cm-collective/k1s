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
        return SimpleNamespace(
            is_leader=self._is_leader,
            leader_info=SimpleNamespace(controller_id="ctrl-a", controller_epoch=7),
        )

    def stop(self) -> None:
        return None


def test_controller_ha_starts_and_stops_hpa_authority(tmp_path: Path, monkeypatch) -> None:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    db_path = tmp_path / "state.db"
    calls = {"collector_started": 0, "collector_stopped": 0, "hpa_started": 0, "hpa_stopped": 0}

    class _FakeCollector:
        def __init__(self, store, reader, *, config=None, authority=None) -> None:
            self.store = store
            self.reader = reader
            self.config = config
            self.authority = authority

        def start(self) -> None:
            calls["collector_started"] += 1

        def stop(self) -> None:
            calls["collector_stopped"] += 1

    class _FakeController:
        def __init__(self, store, *, config=None, authority=None) -> None:
            self.store = store
            self.config = config
            self.authority = authority

        def start(self) -> None:
            calls["hpa_started"] += 1

        def stop(self) -> None:
            calls["hpa_stopped"] += 1

    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_CRONJOB_AUTHORITY_INTERVAL_S", "0")
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: _FakeAuthority(is_leader=True),
    )
    monkeypatch.setattr("ae.controller.__main__.WorkloadMetricsCollector", _FakeCollector)
    monkeypatch.setattr("ae.controller.__main__.HPAAuthorityController", _FakeController)

    assert main(["--once", "--specs", str(specs_dir)]) == 0
    assert calls == {
        "collector_started": 1,
        "collector_stopped": 1,
        "hpa_started": 1,
        "hpa_stopped": 1,
    }
