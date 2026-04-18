"""Integration test for the etcd-backed state adapter."""

from __future__ import annotations

import hashlib
import json
import os
import uuid

import pytest

from ae.controller.etcd_state import EtcdStateStore
from ae.controller.health import HealthReport, PodHealth
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.runtime.base import PodState, RuntimeResult


def _spec_hash(manifest: AppManifest) -> str:
    payload = json.dumps(
        manifest.model_dump(by_alias=True, exclude_none=True),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.integration
def test_etcd_state_store_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoints = os.getenv("AE_ETCD_ENDPOINTS", "http://127.0.0.1:2379")
    prefix = f"k1s/test/{uuid.uuid4().hex}"
    monkeypatch.setenv("AE_ETCD_ENDPOINTS", endpoints)
    monkeypatch.setenv("AE_ETCD_PREFIX", prefix)
    monkeypatch.setenv("AE_SITE_ID", "test-site")

    try:
        store = EtcdStateStore()
    except Exception as exc:  # pragma: no cover - only when etcd is missing
        pytest.skip(f"etcd unavailable: {exc}")

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="etcd-demo"),
        spec=AppSpec(image="example.io/demo:latest", replicas=1),
    )

    store.register_app(manifest, source="test", labels={"env": "it"})
    revision, created = store.prepare_revision(manifest, _spec_hash(manifest))
    assert revision == 1
    assert created is True

    pod_name = "etcd-demo-rev1-0"
    runtime = RuntimeResult(
        revision=revision,
        created=1,
        updated=0,
        removed=0,
        pod_states=[PodState(pod_name=pod_name, ready=True, status="running", revision=revision)],
    )
    health = HealthReport(
        ready_replicas=1,
        live_replicas=1,
        pods=[PodHealth(pod_name=pod_name, ready=True, live=True, readiness_message="", liveness_message="")],
    )
    store.record_snapshot(
        manifest,
        runtime,
        health,
        revision,
        "ready",
        current_revision_ready_replicas=1,
        current_revision_live_replicas=1,
        old_revision_ready_replicas=0,
        old_revision_live_replicas=0,
        overlap_ready_replicas=0,
        overlap_live_replicas=0,
    )
    store.record_event("etcd-demo", revision, "TestEvent", "hello")

    status = store.get_status("etcd-demo")
    assert status is not None
    assert status.ready_replicas == 1
    assert status.current_revision_ready_replicas == 1
    assert status.current_revision_live_replicas == 1
    assert status.old_revision_ready_replicas == 0
    assert status.old_revision_live_replicas == 0
    assert status.overlap_ready_replicas == 0
    assert status.overlap_live_replicas == 0

    pods = store.list_pods("etcd-demo")
    assert pods and pods[0].pod_name == pod_name

    events = store.list_events("etcd-demo", limit=5)
    assert any(ev.event_type == "TestEvent" for ev in events)

    store.upsert_node("node-1", name="node-1", labels={"role": "worker"})
    store.record_heartbeat("node-1", "Ready")
    nodes = store.list_nodes()
    assert any(node.node_id == "node-1" for node, _ in nodes)

    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=2333,
    )
    endpoints = store.list_site_ingress_endpoints()
    assert any(
        item.site_id == "sea-edge-02"
        and item.mode == "core-proxy"
        and item.core_proxy_port == 2333
        for item in endpoints
    )

    route_doc = {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressRoute",
        "metadata": {"name": "app-core-proxy", "namespace": "default"},
        "spec": {
            "host": "app-core-proxy.home.arpa",
            "paths": [
                {
                    "path": "/",
                    "serviceRef": {"name": "etcd-demo", "namespace": "default", "port": 8080},
                }
            ],
            "exposure": {
                "mode": "core-proxy",
                "placement": {"site": "sea-edge-02"},
            },
        },
    }
    store.upsert_edge_ingress_route(
        name="app-core-proxy",
        namespace="default",
        site_id="sea-edge-02",
        policy_name=None,
        policy_namespace=None,
        document=route_doc,
    )
    route = store.get_edge_ingress_route(name="app-core-proxy", namespace="default")
    assert route is not None
    assert route.site_id == "sea-edge-02"
    assert route.spec == route_doc
    store.update_edge_ingress_route_status(
        name="app-core-proxy",
        namespace="default",
        status={"valid": True, "errors": []},
    )
    route = store.get_edge_ingress_route(name="app-core-proxy", namespace="default")
    assert route is not None and route.status == {"valid": True, "errors": []}

    policy_doc = {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressPolicy",
        "metadata": {"name": "allow-basic", "namespace": "default"},
        "spec": {"timeouts": {"requestMs": 1000}},
    }
    store.upsert_edge_ingress_policy(
        name="allow-basic",
        namespace="default",
        document=policy_doc,
    )
    policy = store.get_edge_ingress_policy(name="allow-basic", namespace="default")
    assert policy is not None
    assert policy.spec == policy_doc
    store.update_edge_ingress_policy_status(
        name="allow-basic",
        namespace="default",
        status={"valid": True},
    )
    policy = store.get_edge_ingress_policy(name="allow-basic", namespace="default")
    assert policy is not None and policy.status == {"valid": True}

    store.delete_app_state("etcd-demo", purge_history=True)
    assert store.get_status("etcd-demo") is None
