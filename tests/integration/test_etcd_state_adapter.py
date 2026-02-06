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
        kind="App",
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
        pod_states=[PodState(pod_name=pod_name, ready=True, status="running")],
    )
    health = HealthReport(
        ready_replicas=1,
        live_replicas=1,
        pods=[PodHealth(pod_name=pod_name, ready=True, live=True, readiness_message="", liveness_message="")],
    )
    store.record_snapshot(manifest, runtime, health, revision, "ready")
    store.record_event("etcd-demo", revision, "TestEvent", "hello")

    status = store.get_status("etcd-demo")
    assert status is not None
    assert status.ready_replicas == 1

    pods = store.list_pods("etcd-demo")
    assert pods and pods[0].pod_name == pod_name

    events = store.list_events("etcd-demo", limit=5)
    assert any(ev.event_type == "TestEvent" for ev in events)

    store.upsert_node("node-1", name="node-1", labels={"role": "worker"})
    store.record_heartbeat("node-1", "Ready")
    nodes = store.list_nodes()
    assert any(node.node_id == "node-1" for node, _ in nodes)

    store.delete_app_state("etcd-demo", purge_history=True)
    assert store.get_status("etcd-demo") is None
