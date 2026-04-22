from __future__ import annotations

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


def test_controller_ha_starts_and_stops_storage_authority(tmp_path, monkeypatch) -> None:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    db_path = tmp_path / "state.db"
    calls = {"started": 0, "stopped": 0}

    class _FakeStorageAuthorityRunner:
        def __init__(
            self, store, *, authority=None, poll_interval_s=1.0, close_store=False
        ) -> None:
            self.store = store
            self.authority = authority
            self.poll_interval_s = poll_interval_s
            self.close_store = close_store

        def start(self) -> None:
            calls["started"] += 1

        def stop(self) -> None:
            calls["stopped"] += 1

    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_CRONJOB_AUTHORITY_INTERVAL_S", "0")
    monkeypatch.setenv("AE_HPA_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: _FakeAuthority(is_leader=True),
    )
    monkeypatch.setattr(
        "ae.controller.__main__.build_storage_authority_store",
        lambda _store: object(),
    )
    monkeypatch.setattr(
        "ae.controller.__main__.StorageAuthorityRunner",
        _FakeStorageAuthorityRunner,
    )

    assert main(["--once", "--specs", str(specs_dir)]) == 0
    assert calls == {"started": 1, "stopped": 1}
