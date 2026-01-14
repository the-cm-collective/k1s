from ae.apishim.store import ObjectStore


def test_watch_metrics_drop(monkeypatch, tmp_path):
    monkeypatch.setenv("AE_APISHIM_WATCH_QUEUE_SIZE", "1")
    store = ObjectStore(db_path=tmp_path / "shim.db")
    gen = store.watch("", "v1", "configmaps", "default", heartbeat_seconds=1)
    try:
        store.upsert("", "v1", "configmaps", "default", "c1", {"name": "c1", "namespace": "default"}, {})
        store.upsert("", "v1", "configmaps", "default", "c1", {"name": "c1", "namespace": "default"}, {"k": "v"})
        metrics = store.render_metrics()
        assert "apishim_watch_events_dropped_total" in metrics
        assert 'resource="configmaps"' in metrics
    finally:
        gen.close()
