from __future__ import annotations

from pathlib import Path

from ae.apishim.store import ObjectStore
from ae.controller.state import SQLiteStateStore
from ae.observability import MetricsService


def test_metrics_volume_health_counts(tmp_path, monkeypatch) -> None:
    apishim_db = tmp_path / "apishim.db"
    store = ObjectStore(db_path=apishim_db)
    host_root = tmp_path / "storage"
    host_root.mkdir(parents=True, exist_ok=True)
    host_path = host_root / "vol-1"
    pv_meta = {
        "name": "pv-health",
        "annotations": {
            "k1s.io/local-host-root": str(host_root),
            "k1s.io/local-host-path": str(host_path),
        },
    }
    pv_spec = {"capacity": {"storage": "1Gi"}, "accessModes": ["ReadWriteOnce"]}
    store.upsert("", "v1", "persistentvolumes", None, "pv-health", pv_meta, pv_spec)

    monkeypatch.setenv("AE_APISHIM_DB", str(apishim_db))
    state_store = SQLiteStateStore(tmp_path / "state.db")
    service = MetricsService(state_store)

    snap = service.snapshot()
    assert snap.total_pvs == 1
    assert snap.unhealthy_pvs == 1
    assert snap.healthy_pvs == 0

    host_path.mkdir(parents=True, exist_ok=True)
    snap = service.snapshot()
    assert snap.total_pvs == 1
    assert snap.healthy_pvs == 1
    assert snap.unhealthy_pvs == 0


def test_metrics_storage_quota_usage(tmp_path, monkeypatch) -> None:
    apishim_db = tmp_path / "apishim.db"
    store = ObjectStore(db_path=apishim_db)
    pvc_spec = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "2Gi"}},
    }
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data",
        {"name": "data", "namespace": "default"},
        pvc_spec,
        status={"phase": "Pending"},
    )
    quota_path = tmp_path / "quotas.yaml"
    quota_path.write_text(
        "apiVersion: k1s.io/v1\n"
        "kind: StorageQuota\n"
        "metadata:\n"
        "  name: default\n"
        "spec:\n"
        "  namespace: default\n"
        "  hard:\n"
        "    requests.storage: 5Gi\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AE_APISHIM_DB", str(apishim_db))
    monkeypatch.setenv("AE_STORAGE_QUOTAS", str(quota_path))
    state_store = SQLiteStateStore(tmp_path / "state.db")
    service = MetricsService(state_store)

    snap = service.snapshot()
    assert snap.storage_used_bytes.get("default") == 2 * 1024**3
    assert snap.storage_quota_bytes.get("default") == 5 * 1024**3
