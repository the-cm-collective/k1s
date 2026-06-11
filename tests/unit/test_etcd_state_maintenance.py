from __future__ import annotations

import json
from typing import Any

import pytest

from ae.controller.etcd_state import EtcdStateStore, _b64encode


class _FakeEtcdClient:
    def __init__(self) -> None:
        self.grants = 0
        self.next_lease = 100
        self.compact_calls: list[int] = []
        self.defrag_calls = 0
        self.alarm_deactivate_calls: list[tuple[str, str]] = []
        self.range_calls: list[tuple[str, dict[str, Any]]] = []
        self.delete_calls: list[str] = []
        self.timeouts: list[float | None] = []
        self.kvs: dict[str, dict[str, Any]] = {}
        self.status_payload: dict[str, Any] = {"header": {"revision": 0}, "dbSize": 0}
        self.alarms_payload: dict[str, Any] = {"alarms": []}

    def grant_lease(self, _ttl_seconds: int) -> int:
        self.grants += 1
        self.next_lease += 1
        return self.next_lease

    def range(
        self,
        key: str,
        *,
        range_end: str | bytes | None = None,
        limit: int | None = None,
        sort_order: str | None = None,
        sort_target: str | None = None,
        keys_only: bool = False,
        count_only: bool = False,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        self.range_calls.append(
            (
                key,
                {
                    "range_end": range_end,
                    "limit": limit,
                    "sort_order": sort_order,
                    "sort_target": sort_target,
                    "keys_only": keys_only,
                    "count_only": count_only,
                    "timeout_s": timeout_s,
                },
            )
        )
        end = range_end.decode("utf-8") if isinstance(range_end, bytes) else range_end
        keys = [item for item in self.kvs if item >= key and (end is None or item < end)]
        keys.sort(reverse=str(sort_order or "").upper() == "DESCEND")
        if count_only:
            return {"count": len(keys)}
        if limit is not None:
            keys = keys[: int(limit)]
        kvs = []
        for item in keys:
            payload = "" if keys_only else json.dumps(self.kvs[item])
            kvs.append(
                {
                    "key": _b64encode(item),
                    "value": _b64encode(payload),
                    "mod_revision": "1",
                }
            )
        return {"kvs": kvs}

    def delete(self, key: str) -> None:
        self.delete_calls.append(key)
        self.kvs.pop(key, None)

    def maintenance_status(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        self.timeouts.append(timeout_s)
        return self.status_payload

    def maintenance_alarms(
        self,
        *,
        action: str = "GET",
        member_id: str | None = None,
        alarm: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        self.timeouts.append(timeout_s)
        if action == "GET":
            return self.alarms_payload
        self.alarm_deactivate_calls.append((str(member_id or "0"), str(alarm or "")))
        return {"ok": True}

    def compact(
        self,
        revision: int,
        *,
        physical: bool = True,
        timeout_s: float | None = None,
    ) -> None:
        assert physical is True
        self.timeouts.append(timeout_s)
        self.compact_calls.append(revision)

    def defragment(self, *, timeout_s: float | None = None) -> None:
        self.timeouts.append(timeout_s)
        self.defrag_calls += 1


def _mk_store(client: _FakeEtcdClient) -> EtcdStateStore:
    store = object.__new__(EtcdStateStore)
    store._client = client  # type: ignore[attr-defined]
    store._prefix = "k1s/v1"  # type: ignore[attr-defined]
    store._site_id = "core"  # type: ignore[attr-defined]
    store._lease_ttl_seconds = 60  # type: ignore[attr-defined]
    store._lease_refresh_ratio = 0.5  # type: ignore[attr-defined]
    store._node_leases = {}  # type: ignore[attr-defined]
    store._last_maintenance_result = {}  # type: ignore[attr-defined]
    return store


def _seed_event(
    store: EtcdStateStore,
    fake: _FakeEtcdClient,
    *,
    app_name: str,
    ts: str,
    event_type: str,
) -> None:
    key = f"{store._event_prefix(app_name)}{ts}/{event_type.lower()}"
    fake.kvs[key] = {
        "app_name": app_name,
        "revision": 1,
        "event_type": event_type,
        "message": event_type,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


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


def test_list_events_uses_server_side_descending_limit() -> None:
    fake = _FakeEtcdClient()
    store = _mk_store(fake)
    _seed_event(store, fake, app_name="demo", ts="00000000000000000001", event_type="Old")
    _seed_event(store, fake, app_name="demo", ts="00000000000000000003", event_type="New")
    _seed_event(store, fake, app_name="demo", ts="00000000000000000002", event_type="Middle")

    events = store.list_events("demo", limit=2)

    assert [event.event_type for event in events] == ["New", "Middle"]
    key, kwargs = fake.range_calls[-1]
    assert key == store._event_prefix("demo")
    assert kwargs["limit"] == 2
    assert kwargs["sort_order"] == "DESCEND"
    assert kwargs["sort_target"] == "KEY"


def test_list_events_paginated_uses_count_and_capped_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeEtcdClient()
    store = _mk_store(fake)
    for idx in range(5):
        _seed_event(
            store,
            fake,
            app_name="demo",
            ts=f"{idx + 1:020d}",
            event_type=f"Event{idx + 1}",
        )
    monkeypatch.setenv("AE_ETCD_EVENT_PAGE_SCAN_MAX", "3")

    events, total = store.list_events_paginated("demo", limit=2, offset=1)

    assert total == 5
    assert [event.event_type for event in events] == ["Event4", "Event3"]
    assert fake.range_calls[0][1]["count_only"] is True
    assert fake.range_calls[1][1]["limit"] == 3


def test_event_app_names_uses_registered_apps_without_broad_event_scan() -> None:
    fake = _FakeEtcdClient()
    store = _mk_store(fake)
    store.list_registered_app_names = lambda: ["demo"]  # type: ignore[method-assign]
    store.list_status = lambda: []  # type: ignore[method-assign]
    _seed_event(store, fake, app_name="demo", ts="00000000000000000001", event_type="Old")

    assert store._event_app_names(page_size=10) == ["demo"]
    assert not any(call[0] == f"{store._k('events')}/" for call in fake.range_calls)


def test_prune_events_deletes_oldest_keys_within_batch() -> None:
    fake = _FakeEtcdClient()
    store = _mk_store(fake)
    store.list_registered_app_names = lambda: ["demo"]  # type: ignore[method-assign]
    for idx in range(5):
        _seed_event(
            store,
            fake,
            app_name="demo",
            ts=f"{idx + 1:020d}",
            event_type=f"Event{idx + 1}",
        )

    summary = store.prune_events(max_per_app=2, max_age_days=0, batch_size=10)

    assert summary["deleted"] == 3
    assert len(fake.delete_calls) == 3
    assert all("/events/demo/" in key for key in fake.delete_calls)
    assert [event.event_type for event in store.list_events("demo", limit=5)] == [
        "Event5",
        "Event4",
    ]


def test_prune_events_dry_run_does_not_delete() -> None:
    fake = _FakeEtcdClient()
    store = _mk_store(fake)
    store.list_registered_app_names = lambda: ["demo"]  # type: ignore[method-assign]
    for idx in range(3):
        _seed_event(
            store,
            fake,
            app_name="demo",
            ts=f"{idx + 1:020d}",
            event_type=f"Event{idx + 1}",
        )

    summary = store.prune_events(max_per_app=1, max_age_days=0, batch_size=10, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["eligible"] == 2
    assert summary["deleted"] == 0
    assert fake.delete_calls == []
    assert store._event_count("demo") == 3


def test_watchdog_noop_when_usage_low_and_no_alarm() -> None:
    fake = _FakeEtcdClient()
    fake.status_payload = {"header": {"revision": 10}, "dbSize": 1024}
    fake.alarms_payload = {"alarms": []}
    store = _mk_store(fake)

    triggered = store.run_maintenance_watchdog(threshold_pct=80, quota_backend_bytes=10_000_000)

    assert triggered is False
    assert fake.compact_calls == []
    assert fake.defrag_calls == 0
    assert store.last_maintenance_result()["skipped_reason"] == "below_threshold"


def test_watchdog_triggers_on_nospace_alarm_and_uses_maintenance_timeout() -> None:
    fake = _FakeEtcdClient()
    fake.status_payload = {"header": {"revision": 88}, "dbSize": 512}
    fake.alarms_payload = {"alarms": [{"memberID": "1234", "alarm": "NOSPACE"}]}
    store = _mk_store(fake)

    triggered = store.run_maintenance_watchdog(
        threshold_pct=80,
        quota_backend_bytes=10_000_000,
        maintenance_timeout_s=42,
    )

    assert triggered is True
    assert fake.compact_calls == [88]
    assert fake.defrag_calls == 1
    assert fake.alarm_deactivate_calls == [("1234", "NOSPACE")]
    assert all(timeout == 42 for timeout in fake.timeouts)
    assert store.last_maintenance_result()["defragged"] is True
