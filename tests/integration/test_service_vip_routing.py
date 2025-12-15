"""Integration test that ensures Service endpoints exclude loopback and prefer pod/service IPs."""

from __future__ import annotations

import pytest

from ae.controller.health import HealthManager
from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, Metadata, ServiceSpec
from ae.controller.state import SQLiteStateStore
from ae.network.provider_docker import DockerBridgeProvider
from ae.network.service_controller import ServiceController
from ae.runtime.docker_stub import StubRuntime


def _manifest(name: str = "echo") -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name=name),
        spec=AppSpec(
            image="busybox",
            replicas=1,
            ports=[{"name": "http", "containerPort": 8080}],
            service=ServiceSpec(port=8080, target_port=8080),
        ),
    )


@pytest.mark.skipif(True, reason="Bridge provider needs Docker socket; run in docker-enabled env")
def test_service_endpoints_skip_loopback(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    runtime = StubRuntime()
    # StubRuntime uses loopback endpoints; inject a non-loopback endpoint manually
    manifest = _manifest()
    runtime.mock_endpoints = ["10.1.1.10:8080"]

    provider = DockerBridgeProvider(network_name="ae-test")
    svc = ServiceController(provider, state)
    reconciler = Reconciler(
        runtime=runtime,
        state_store=state,
        health_manager=HealthManager(),
        service_controller=svc,
    )

    report = reconciler.reconcile(manifest)
    assert report.ready_replicas == 1

    endpoints = state.list_service_endpoints(manifest.metadata.name)
    assert endpoints
    for ep in endpoints:
        assert not str(ep.ip).startswith("127.")
