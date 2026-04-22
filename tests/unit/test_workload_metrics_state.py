from __future__ import annotations

from datetime import datetime, timezone

from ae.controller.etcd_state import EtcdStateStore
from ae.controller.state import SQLiteStateStore


def test_sqlite_workload_metrics_snapshot_round_trip(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    collected_at = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)

    store.upsert_workload_metrics_snapshot(
        "default/demo",
        controller_id="ctrl-a",
        controller_epoch=7,
        collected_at=collected_at,
        cpu_utilization=82.5,
        memory_utilization=75.0,
        memory_bytes=268435456,
        pod_count=3,
        node_count=2,
    )

    snapshot = store.get_workload_metrics_snapshot("default/demo")

    assert snapshot is not None
    assert snapshot.app_name == "default/demo"
    assert snapshot.controller_id == "ctrl-a"
    assert snapshot.controller_epoch == 7
    assert snapshot.collected_at == collected_at
    assert snapshot.cpu_utilization == 82.5
    assert snapshot.memory_utilization == 75.0
    assert snapshot.memory_bytes == 268435456
    assert snapshot.pod_count == 3
    assert snapshot.node_count == 2
    assert [item.app_name for item in store.list_workload_metrics_snapshots()] == ["default/demo"]


def test_etcd_workload_metrics_snapshot_round_trip() -> None:
    store = object.__new__(EtcdStateStore)
    store._prefix = "k1s/v1"  # type: ignore[attr-defined]
    backing: dict[str, dict] = {}

    def _put_json(key: str, payload: dict, *, lease_id=None) -> None:
        _ = lease_id
        backing[key] = dict(payload)

    def _get_json(key: str):
        rec = backing.get(key)
        return (dict(rec) if rec is not None else None, 1 if rec is not None else 0)

    def _list_prefix(prefix: str):
        return [
            (key, dict(value), 1)
            for key, value in backing.items()
            if key.startswith(prefix)
        ]

    def _delete(key: str) -> None:
        backing.pop(key, None)

    store._put_json = _put_json  # type: ignore[method-assign]
    store._get_json = _get_json  # type: ignore[method-assign]
    store._list_prefix = _list_prefix  # type: ignore[method-assign]
    store._delete = _delete  # type: ignore[method-assign]

    collected_at = datetime(2026, 3, 18, 12, 5, tzinfo=timezone.utc)
    store.upsert_workload_metrics_snapshot(
        "default/demo",
        controller_id="ctrl-a",
        controller_epoch=8,
        collected_at=collected_at,
        cpu_utilization=None,
        memory_utilization=65.0,
        memory_bytes=134217728,
        pod_count=1,
        node_count=1,
    )

    snapshot = store.get_workload_metrics_snapshot("default/demo")

    assert snapshot is not None
    assert snapshot.controller_epoch == 8
    assert snapshot.memory_utilization == 65.0
    assert snapshot.cpu_utilization is None
    assert store.delete_workload_metrics_snapshot("default/demo") is True
    assert store.get_workload_metrics_snapshot("default/demo") is None
