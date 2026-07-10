"""Reconciler endpoint selection tests."""

from pathlib import Path

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest
from ae.controller.state import SQLiteStateStore
from ae.runtime import PodState, RuntimeResult
from ae.runtime.docker_stub import StubRuntime


def _reconciler() -> Reconciler:
    store = SQLiteStateStore(Path("/tmp/reconciler-endpoints.db"))
    return Reconciler(StubRuntime(), store)


def _manifest_with_readiness(port: int) -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
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


def test_hydrate_runtime_endpoints_from_container_info_uses_node_host_port(monkeypatch):
    rec = _reconciler()
    manifest = _manifest_with_readiness(5678)
    result = RuntimeResult(
        revision=1,
        created=0,
        updated=0,
        removed=0,
        pod_states=[
            PodState(
                pod_name="demo-rev1-0",
                ready=True,
                status="running",
                revision=1,
                endpoint=None,
            )
        ],
    )

    monkeypatch.setattr(
        rec,
        "_container_infos_by_pod",
        lambda *, require_direct_ingress=True, include_observation_runtimes=False: {
            "demo-rev1-0": {
                "name": "demo-rev1-0",
                "host_ip": "192.168.29.15",
                "port_map": {5678: 18087},
                "host_ports": [18087],
            }
        },
    )

    hydrated = rec._hydrate_runtime_endpoints_from_container_info(manifest, result)

    assert hydrated is result
    assert hydrated.pod_states[0].endpoint == "192.168.29.15:18087"


def test_hydrate_runtime_endpoints_preserves_existing_endpoint(monkeypatch):
    rec = _reconciler()
    manifest = _manifest_with_readiness(5678)
    result = RuntimeResult(
        revision=1,
        created=0,
        updated=0,
        removed=0,
        pod_states=[
            PodState(
                pod_name="demo-rev1-0",
                ready=True,
                status="running",
                revision=1,
                endpoint="192.168.29.20:18088",
            )
        ],
    )

    def _unexpected_container_info(**_kwargs):
        raise AssertionError("container info should not be read when endpoints are present")

    monkeypatch.setattr(rec, "_container_infos_by_pod", _unexpected_container_info)

    hydrated = rec._hydrate_runtime_endpoints_from_container_info(manifest, result)

    assert hydrated.pod_states[0].endpoint == "192.168.29.20:18088"


def test_hydrate_runtime_endpoints_replaces_loopback_endpoint(monkeypatch):
    rec = _reconciler()
    manifest = _manifest_with_readiness(5678)
    result = RuntimeResult(
        revision=1,
        created=0,
        updated=0,
        removed=0,
        pod_states=[
            PodState(
                pod_name="demo-rev1-0",
                ready=True,
                status="running",
                revision=1,
                endpoint="127.0.0.1:18087",
            )
        ],
    )

    monkeypatch.setattr(
        rec,
        "_container_infos_by_pod",
        lambda *, require_direct_ingress=True, include_observation_runtimes=False: {
            "demo-rev1-0": {
                "name": "demo-rev1-0",
                "host_ip": "192.168.29.15",
                "port_map": {5678: 18087},
                "host_ports": [18087],
            }
        },
    )

    hydrated = rec._hydrate_runtime_endpoints_from_container_info(manifest, result)

    assert hydrated.pod_states[0].endpoint == "192.168.29.15:18087"


def test_container_infos_by_pod_can_include_remote_observation_runtimes(monkeypatch):
    rec = _reconciler()

    class EmptyRuntime:
        def list_containers_info(self):
            return []

    class RemoteRuntime:
        def list_containers_info(self):
            return [
                {
                    "name": "demo-rev1-0",
                    "labels": {"ae.pod_name": "demo-rev1-0"},
                    "host_ip": "192.168.29.15",
                    "port_map": {5678: 18087},
                }
            ]

    monkeypatch.setattr(rec, "_runtime", EmptyRuntime())
    monkeypatch.setattr(rec, "_rollout_observation_runtimes", lambda: [rec._runtime, RemoteRuntime()])

    local_only = rec._container_infos_by_pod(require_direct_ingress=False)
    with_remote = rec._container_infos_by_pod(
        require_direct_ingress=False,
        include_observation_runtimes=True,
    )

    assert local_only == {}
    assert with_remote["demo-rev1-0"]["host_ip"] == "192.168.29.15"
