from __future__ import annotations

from typing import Any

import pytest

from ae.controller.etcd_state import EtcdStateStore


class _FakeEtcdClient:
    def __init__(self) -> None:
        self.grants = 0
        self.next_lease = 100
        self.compact_calls: list[int] = []
        self.defrag_calls = 0
        self.alarm_deactivate_calls: list[tuple[str, str]] = []
        self.status_payload: dict[str, Any] = {"header": {"revision": 0}, "dbSize": 0}
        self.alarms_payload: dict[str, Any] = {"alarms": []}

    def grant_lease(self, _ttl_seconds: int) -> int:
        self.grants += 1
        self.next_lease += 1
        return self.next_lease

    def maintenance_status(self) -> dict[str, Any]:
        return self.status_payload

    def maintenance_alarms(
        self,
        *,
        action: str = "GET",
        member_id: str | None = None,
        alarm: str | None = None,
    ) -> dict[str, Any]:
        if action == "GET":
            return self.alarms_payload
        self.alarm_deactivate_calls.append((str(member_id or "0"), str(alarm or "")))
        return {"ok": True}

    def compact(self, revision: int, *, physical: bool = True) -> None:
        assert physical is True
        self.compact_calls.append(revision)

    def defragment(self) -> None:
        self.defrag_calls += 1


def _mk_store(client: _FakeEtcdClient) -> EtcdStateStore:
    store = object.__new__(EtcdStateStore)
    store._client = client  # type: ignore[attr-defined]
    store._prefix = "k1s/v1"  # type: ignore[attr-defined]
    store._site_id = "core"  # type: ignore[attr-defined]
    store._lease_ttl_seconds = 60  # type: ignore[attr-defined]
    store._lease_refresh_ratio = 0.5  # type: ignore[attr-defined]
    store._node_leases = {}  # type: ignore[attr-defined]
    return store


def test_record_heartbeat_reuses_cached_lease_and_skips_node_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeEtcdClient()
    store = _mk_store(fake)

    puts: list[tuple[str, int | None]] = []
    node_key = store._k("nodes", "core", "n1")
    status_key = store._k("node_status", "core", "n1")

    def _get_json(key: str) -> tuple[dict | None, int]:
        if key == node_key:
            return (
                {
                    "node_id": "n1",
                    "name": "n1",
                    "labels": {},
                    "taints": [],
                    "backend": "docker",
                    "endpoint": "http://n1:9109",
                    "pod_cidr": "10.42.1.0/24",
                    "wg_pubkey": None,
                    "rp_pubkey": None,
                    "cordoned": False,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
                1,
            )
        return None, 0

    def _put_json(key: str, _payload: dict, *, lease_id: int | None = None) -> None:
        puts.append((key, lease_id))

    now = {"v": 0.0}
    monkeypatch.setattr("ae.controller.etcd_state.time.monotonic", lambda: now["v"])
    store._get_json = _get_json  # type: ignore[method-assign]
    store._put_json = _put_json  # type: ignore[method-assign]
    store._record_heartbeat_metrics = lambda **_kwargs: None  # type: ignore[method-assign]

    store.record_heartbeat("n1", "Ready")
    now["v"] = 5.0
    store.record_heartbeat("n1", "Ready")

    assert fake.grants == 1
    assert [key for key, _lease in puts].count(status_key) == 2
    assert [key for key, _lease in puts].count(node_key) == 1


def test_watchdog_noop_when_usage_low_and_no_alarm() -> None:
    fake = _FakeEtcdClient()
    fake.status_payload = {"header": {"revision": 10}, "dbSize": 1024}
    fake.alarms_payload = {"alarms": []}
    store = _mk_store(fake)

    triggered = store.run_maintenance_watchdog(threshold_pct=80, quota_backend_bytes=10_000_000)

    assert triggered is False
    assert fake.compact_calls == []
    assert fake.defrag_calls == 0


def test_watchdog_triggers_on_nospace_alarm() -> None:
    fake = _FakeEtcdClient()
    fake.status_payload = {"header": {"revision": 88}, "dbSize": 512}
    fake.alarms_payload = {"alarms": [{"memberID": "1234", "alarm": "NOSPACE"}]}
    store = _mk_store(fake)

    triggered = store.run_maintenance_watchdog(threshold_pct=80, quota_backend_bytes=10_000_000)

    assert triggered is True
    assert fake.compact_calls == [88]
    assert fake.defrag_calls == 1
    assert fake.alarm_deactivate_calls == [("1234", "NOSPACE")]
