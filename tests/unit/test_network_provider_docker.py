import types

import pytest

from ae.network.provider_docker import DockerBridgeProvider


class _FakeStore:
    def __init__(self, services=None):
        self._services = services or []
        self.upserts = []
        self.deleted = []

    def list_services(self):
        return self._services

    def get_service(self, app_name: str):
        for s in self._services:
            if s.app_name == app_name:
                return s
        return None


def test_allocate_ip_skips_used_and_returns_first_free():
    used = [
        types.SimpleNamespace(app_name="a", cluster_ip="10.241.0.1"),
        types.SimpleNamespace(app_name="b", cluster_ip="10.241.0.2"),
    ]
    store = _FakeStore(used)
    prov = DockerBridgeProvider(store, service_cidr="10.241.0.0/29")
    ip = prov._allocate_ip()
    assert ip == "10.241.0.3"


def test_ensure_service_reuses_existing_ip():
    existing = types.SimpleNamespace(app_name="demo", cluster_ip="10.241.0.10")
    store = _FakeStore([existing])
    prov = DockerBridgeProvider(store)
    ip = prov.ensure_service("demo", {"ports": []})
    assert ip == existing.cluster_ip


def test_allocate_ip_exhaustion_raises():
    used = [types.SimpleNamespace(app_name="solo", cluster_ip="10.0.0.0")]
    store = _FakeStore(used)
    prov = DockerBridgeProvider(store, service_cidr="10.0.0.0/32")
    with pytest.raises(RuntimeError):
        prov._allocate_ip()
