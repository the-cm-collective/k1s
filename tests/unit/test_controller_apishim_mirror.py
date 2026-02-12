from ae.controller import __main__ as controller_main


def test_snapshot_apishim_manifests_closes_temp_store(monkeypatch):
    closed = {"count": 0}

    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

        def list_all(self, *_args, **_kwargs):
            return []

        def close(self):
            closed["count"] += 1

    class FakeAdapterWorker:
        def __init__(self, *_args, **_kwargs):
            self._service_specs = {}
            self._service_name_map = {}
            self._ingress_specs = {}
            self._ingress_owner_map = {}

    class DummyStateStore:
        pass

    import ae.apishim.adapter as adapter_mod
    import ae.apishim.store as store_mod

    monkeypatch.setenv("AE_APISHIM_DSN", "postgresql://shim:shim@127.0.0.1:5432/shim")
    monkeypatch.delenv("AE_APISHIM_SERVER", raising=False)
    monkeypatch.delenv("AE_APISHIM_DB", raising=False)
    monkeypatch.setattr(store_mod, "ObjectStore", FakeStore)
    monkeypatch.setattr(adapter_mod, "AdapterWorker", FakeAdapterWorker)
    monkeypatch.setattr(
        adapter_mod, "_manifest_from_deployment", lambda *_args, **_kwargs: None
    )

    manifests, reachable = controller_main._snapshot_apishim_manifests(DummyStateStore())

    assert manifests == {}
    assert reachable is True
    assert closed["count"] == 1
