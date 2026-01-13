import types

from ae.apishim.adapter import AdapterWorker
from ae.apishim.store import ObjectStore
from ae.controller.health import HealthManager, HealthReport
from ae.controller.reconciler import Reconciler
from ae.controller.spec import ServiceSpec
from ae.controller.state import SQLiteStateStore
from ae.runtime import RuntimeResult, StubRuntime
from ae.network.service_controller import ServiceController
import pytest


def _make_adapter(tmp_path):
    store = ObjectStore(tmp_path / "apishim.db")
    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=StubRuntime(), state_store=state, health_manager=HealthManager())
    adapter = AdapterWorker(store, state, reconciler)
    adapter._hpa_cooldown_seconds = 0
    return store, state, adapter


def test_hpa_scales_up_on_cpu(tmp_path, monkeypatch):
    store, _state, adapter = _make_adapter(tmp_path)

    dep_md = {"name": "demo", "namespace": "default", "labels": {"app": "demo"}}
    dep_spec = {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "demo"}},
        "template": {"metadata": {"labels": {"app": "demo"}}, "spec": {"containers": [{"name": "demo", "image": "busybox"}]}},
    }
    dep_obj = store.upsert("apps", "v1", "deployments", "default", "demo", dep_md, dep_spec, {})

    hpa_spec = {
        "minReplicas": 1,
        "maxReplicas": 5,
        "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "demo"},
        "metrics": [
            {
                "type": "Resource",
                "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 50}},
            }
        ],
    }
    hpa_md = {"name": "demo-hpa", "namespace": "default"}
    hpa_obj = store.upsert("autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa", hpa_md, hpa_spec, {})

    monkeypatch.setattr(
        adapter,
        "_collect_metrics_for_app",
        lambda app_name: {"cpu_util": 80.0, "mem_util": None, "mem_bytes": None},
    )

    adapter._apply_hpa(hpa_obj)

    updated_dep = store.get("apps", "v1", "deployments", "default", "demo")
    assert updated_dep.spec.get("replicas") == 2

    updated_hpa = store.get("autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa")
    assert updated_hpa.status.get("desiredReplicas") == 2
    metrics = updated_hpa.status.get("currentMetrics", [])
    assert metrics and metrics[0]["resource"]["current"]["averageUtilization"] == 80


def test_hpa_scales_on_memory_average_value(tmp_path, monkeypatch):
    store, _state, adapter = _make_adapter(tmp_path)

    dep_md = {"name": "demo", "namespace": "default", "labels": {"app": "demo"}}
    dep_spec = {
        "replicas": 2,
        "selector": {"matchLabels": {"app": "demo"}},
        "template": {"metadata": {"labels": {"app": "demo"}}, "spec": {"containers": [{"name": "demo", "image": "busybox"}]}},
    }
    store.upsert("apps", "v1", "deployments", "default", "demo", dep_md, dep_spec, {})

    hpa_spec = {
        "minReplicas": 1,
        "maxReplicas": 6,
        "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "demo"},
        "metrics": [
            {
                "type": "Resource",
                "resource": {"name": "memory", "target": {"type": "AverageValue", "averageValue": "200Mi"}},
            }
        ],
    }
    hpa_md = {"name": "demo-hpa", "namespace": "default"}
    hpa_obj = store.upsert("autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa", hpa_md, hpa_spec, {})

    # 400Mi average usage across 2 pods -> desired 4 (ceil(2*400/200))
    monkeypatch.setattr(
        adapter,
        "_collect_metrics_for_app",
        lambda app_name: {"cpu_util": None, "mem_util": None, "mem_bytes": 400 * 1024 * 1024},
    )

    adapter._apply_hpa(hpa_obj)

    updated_dep = store.get("apps", "v1", "deployments", "default", "demo")
    assert updated_dep.spec.get("replicas") == 4

    updated_hpa = store.get("autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa")
    assert updated_hpa.status.get("desiredReplicas") == 4
    mem_metric = next(m for m in updated_hpa.status.get("currentMetrics", []) if m["resource"]["name"] == "memory")
    assert mem_metric["resource"]["current"]["averageValue"].endswith("Mi")


def test_hpa_cooldown_blocks_rapid_scale(tmp_path, monkeypatch):
    store, _state, adapter = _make_adapter(tmp_path)
    adapter._hpa_cooldown_seconds = 300

    dep_md = {"name": "demo", "namespace": "default", "labels": {"app": "demo"}}
    dep_spec = {
        "replicas": 2,
        "selector": {"matchLabels": {"app": "demo"}},
        "template": {"metadata": {"labels": {"app": "demo"}}, "spec": {"containers": [{"name": "demo", "image": "busybox"}]}},
    }
    store.upsert("apps", "v1", "deployments", "default", "demo", dep_md, dep_spec, {})

    hpa_spec = {
        "minReplicas": 1,
        "maxReplicas": 5,
        "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "demo"},
        "metrics": [
            {
                "type": "Resource",
                "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 50}},
            }
        ],
    }
    hpa_md = {"name": "demo-hpa", "namespace": "default"}
    hpa_obj = store.upsert("autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa", hpa_md, hpa_spec, {})

    monkeypatch.setattr(
        adapter,
        "_collect_metrics_for_app",
        lambda app_name: {"cpu_util": 90.0, "mem_util": None, "mem_bytes": None},
    )
    # Pretend we just scaled a moment ago
    import time as _t

    adapter._hpa_last_scale[_app_name := "default--demo"] = _t.time()

    adapter._apply_hpa(hpa_obj)

    updated_dep = store.get("apps", "v1", "deployments", "default", "demo")
    # Scale should be blocked by cooldown; still 2 replicas
    assert updated_dep.spec.get("replicas") == 2

    updated_hpa = store.get("autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa")
    conds = {c["type"]: c["status"] for c in updated_hpa.status.get("conditions", [])}
    assert conds.get("ScalingLimited") == "True"


class _OverlayProviderStub:
    def __init__(self):
        self.health = {"ok": False, "peers": 0}

    def ensure_network(self):
        return

    def ensure_service(self, app_name: str, ports: dict) -> str:
        return "10.0.0.2"

    def update_service_endpoints(self, app_name: str, backends: dict):
        return

    def remove_service(self, app_name: str):
        return

    def overlay_health(self):
        return dict(self.health)


def test_overlay_events_emitted_on_status_change(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    provider = _OverlayProviderStub()
    svc_controller = ServiceController(provider, store)

    svc_spec = ServiceSpec(port=80, target_port=80)
    manifest = types.SimpleNamespace()
    manifest.metadata = types.SimpleNamespace(name="demo-app")
    manifest.spec = types.SimpleNamespace(service=svc_spec)
    runtime_result = RuntimeResult(revision=1, created=0, updated=0, removed=0, replica_states=[])
    health_report = HealthReport(ready_replicas=0, live_replicas=0, replicas=[])

    svc_controller.reconcile(manifest, runtime_result, health_report)
    events = store.list_events("demo-app")
    assert any(e.event_type == "OverlayDegraded" for e in events)

    provider.health = {"ok": True, "peers": 1, "latest_handshake_seconds": 2}
    svc_controller.reconcile(manifest, runtime_result, health_report)
    events = store.list_events("demo-app")
    assert any(e.event_type == "OverlayReady" for e in events)

    # K8s Event projection carries namespace/name
    from ae.apishim.server import _to_event  # type: ignore

    ev_obj = _to_event("default", "demo", events[0])
    assert ev_obj["metadata"]["namespace"] == "default"
    assert ev_obj["reason"] in {"OverlayReady", "OverlayDegraded"}
