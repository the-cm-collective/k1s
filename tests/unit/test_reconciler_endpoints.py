"""Reconciler endpoint selection tests."""

from pathlib import Path

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest
from ae.controller.state import SQLiteStateStore
from ae.runtime.docker_stub import StubRuntime


def _reconciler() -> Reconciler:
    store = SQLiteStateStore(Path("/tmp/reconciler-endpoints.db"))
    return Reconciler(StubRuntime(), store)


def _manifest_with_readiness(port: int) -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "App",
            "metadata": {"name": "demo"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
                "ports": [{"name": "http", "containerPort": port}],
                "health": {"readiness": {"httpGet": {"path": "/", "port": port}}},
            },
        }
    )


def test_preferred_container_port_prefers_readiness():
    rec = _reconciler()
    manifest = _manifest_with_readiness(8080)
    assert rec._preferred_container_port(manifest) == 8080


def test_endpoint_from_container_info_prefers_pod_ip():
    rec = _reconciler()
    info = {
        "pod_ip": "10.0.0.2",
        "host_ip": "1.2.3.4",
        "port_map": {8080: 18080},
        "host_ports": [18080],
    }
    assert rec._endpoint_from_container_info(info, 8080) == ("10.0.0.2", 8080)


def test_endpoint_from_container_info_uses_port_map():
    rec = _reconciler()
    info = {"host_ip": "1.2.3.4", "port_map": {8080: 18080}, "host_ports": [18080]}
    assert rec._endpoint_from_container_info(info, 8080) == ("1.2.3.4", 18080)


def test_endpoint_from_container_info_uses_host_ports_fallback():
    rec = _reconciler()
    info = {"host_ports": [9090]}
    assert rec._endpoint_from_container_info(info, None) == ("127.0.0.1", 9090)
