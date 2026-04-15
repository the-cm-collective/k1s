from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ae.controller.hpa_authority import (
    HPAAuthorityController,
    HPAAuthorityControllerConfig,
    WorkloadMetricsCollector,
)
from ae.controller.spec import AppManifest
from ae.controller.state import SQLiteStateStore
from ae.runtime import WorkloadMetricSample


class _FakeAuthority:
    def __init__(self, *, is_leader: bool, controller_id: str = "ctrl-a", epoch: int = 7) -> None:
        self.is_leader = is_leader
        self.controller_id = controller_id
        self.epoch = epoch

    def snapshot(self):
        return SimpleNamespace(
            is_leader=self.is_leader,
            leader_info=SimpleNamespace(
                controller_id=self.controller_id,
                controller_epoch=self.epoch,
            ),
        )


def _manifest(
    *,
    replicas: int = 1,
    cpu_request: float | None = None,
    memory_request: str | None = None,
):
    resources: dict[str, dict[str, object]] = {}
    requests: dict[str, object] = {}
    if cpu_request is not None:
        requests["cpu"] = cpu_request
    if memory_request is not None:
        requests["memory"] = memory_request
    if requests:
        resources["requests"] = requests
    spec: dict[str, object] = {"image": "busybox", "replicas": replicas}
    if resources:
        spec["resources"] = resources
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": spec,
        }
    )


def _register_hpa(
    state: SQLiteStateStore,
    *,
    metrics: list[dict],
    status: dict | None = None,
) -> None:
    state.register_authority_object(
        "autoscaling",
        "v2",
        "horizontalpodautoscalers",
        "default",
        "demo-hpa",
        kind="HorizontalPodAutoscaler",
        metadata={"name": "demo-hpa", "namespace": "default", "generation": 3},
        spec={
            "minReplicas": 1,
            "maxReplicas": 6,
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "demo",
            },
            "metrics": metrics,
        },
        status=status or {},
        expected_resource_version=0,
    )


def test_workload_metrics_collector_persists_shared_snapshot(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    state.register_app(_manifest(replicas=3, cpu_request=0.5, memory_request="128Mi"), source="test")
    state.upsert_node("node-a", backend="cri", endpoint="http://node-a:9112")
    state.upsert_node("node-b", backend="cri", endpoint="http://node-b:9112")
    state.record_heartbeat("node-a", "Ready")
    state.record_heartbeat("node-b", "Ready")
    authority = _FakeAuthority(is_leader=True, epoch=9)

    samples_by_node = {
        "node-a": [
            WorkloadMetricSample(
                app_name="demo",
                node_id="node-a",
                collected_at=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
                cpu_cores=1.0,
                memory_bytes=256 * 1024 * 1024,
                pod_count=2,
            )
        ],
        "node-b": [
            WorkloadMetricSample(
                app_name="demo",
                node_id="node-b",
                collected_at=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
                cpu_cores=0.5,
                memory_bytes=128 * 1024 * 1024,
                pod_count=1,
            )
        ],
    }

    collector = WorkloadMetricsCollector(
        state,
        lambda node: list(samples_by_node.get(node.node_id, [])),
        authority=authority,
    )

    collector.run_once()

    snapshot = state.get_workload_metrics_snapshot("demo")
    assert snapshot is not None
    assert snapshot.controller_epoch == 9
    assert snapshot.pod_count == 3
    assert snapshot.node_count == 2
    assert snapshot.cpu_utilization == 100.0
    assert snapshot.memory_utilization == 100.0


def test_hpa_authority_controller_scales_on_cpu_utilization(monkeypatch, tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    state.register_app(_manifest(replicas=1, cpu_request=1.0), source="test")
    _register_hpa(
        state,
        metrics=[
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {"type": "Utilization", "averageUtilization": 50},
                },
            }
        ],
    )
    state.upsert_workload_metrics_snapshot(
        "demo",
        controller_id="ctrl-a",
        controller_epoch=7,
        collected_at=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
        cpu_utilization=80.0,
        memory_utilization=None,
        memory_bytes=0,
        pod_count=1,
        node_count=1,
    )
    controller = HPAAuthorityController(
        state,
        config=HPAAuthorityControllerConfig(interval_s=15, metrics_max_age_s=45, cooldown_s=0),
        authority=_FakeAuthority(is_leader=True, epoch=7),
    )
    monkeypatch.setattr(controller, "_now", lambda: datetime(2026, 3, 18, 12, 0, 5, tzinfo=timezone.utc))

    controller.run_once()

    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.manifest.spec.replicas == 2
    hpa = state.get_authority_object(
        "autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa"
    )
    assert hpa is not None
    assert hpa.status["desiredReplicas"] == 2
    assert hpa.status["currentMetrics"][0]["resource"]["current"]["averageUtilization"] == 80
    conditions = {c["type"]: c["status"] for c in hpa.status["conditions"]}
    assert conditions["AbleToScale"] == "True"
    assert conditions["ScalingActive"] == "True"


def test_hpa_authority_controller_scales_on_memory_average_value(monkeypatch, tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    state.register_app(_manifest(replicas=2, memory_request="256Mi"), source="test")
    _register_hpa(
        state,
        metrics=[
            {
                "type": "Resource",
                "resource": {
                    "name": "memory",
                    "target": {"type": "AverageValue", "averageValue": "100Mi"},
                },
            }
        ],
    )
    state.upsert_workload_metrics_snapshot(
        "demo",
        controller_id="ctrl-a",
        controller_epoch=7,
        collected_at=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
        cpu_utilization=None,
        memory_utilization=None,
        memory_bytes=400 * 1024 * 1024,
        pod_count=2,
        node_count=1,
    )
    controller = HPAAuthorityController(
        state,
        config=HPAAuthorityControllerConfig(interval_s=15, metrics_max_age_s=45, cooldown_s=0),
        authority=_FakeAuthority(is_leader=True, epoch=7),
    )
    monkeypatch.setattr(controller, "_now", lambda: datetime(2026, 3, 18, 12, 0, 5, tzinfo=timezone.utc))

    controller.run_once()

    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.manifest.spec.replicas == 4
    hpa = state.get_authority_object(
        "autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa"
    )
    assert hpa is not None
    mem_metric = next(
        metric for metric in hpa.status["currentMetrics"] if metric["resource"]["name"] == "memory"
    )
    assert mem_metric["resource"]["current"]["averageValue"].endswith("Mi")


def test_hpa_authority_controller_honors_shared_cooldown(monkeypatch, tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    state.register_app(_manifest(replicas=2, cpu_request=1.0), source="test")
    now = datetime(2026, 3, 18, 12, 0, 5, tzinfo=timezone.utc)
    _register_hpa(
        state,
        metrics=[
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {"type": "Utilization", "averageUtilization": 50},
                },
            }
        ],
        status={"lastScaleTime": (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")},
    )
    state.upsert_workload_metrics_snapshot(
        "demo",
        controller_id="ctrl-a",
        controller_epoch=7,
        collected_at=now,
        cpu_utilization=90.0,
        memory_utilization=None,
        memory_bytes=0,
        pod_count=2,
        node_count=1,
    )
    controller = HPAAuthorityController(
        state,
        config=HPAAuthorityControllerConfig(interval_s=15, metrics_max_age_s=45, cooldown_s=300),
        authority=_FakeAuthority(is_leader=True, epoch=7),
    )
    monkeypatch.setattr(controller, "_now", lambda: now)

    controller.run_once()

    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.manifest.spec.replicas == 2
    hpa = state.get_authority_object(
        "autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa"
    )
    assert hpa is not None
    conditions = {c["type"]: c["status"] for c in hpa.status["conditions"]}
    assert conditions["ScalingLimited"] == "True"


def test_hpa_authority_controller_ignores_stale_epoch_snapshot(monkeypatch, tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    state.register_app(_manifest(replicas=1, cpu_request=1.0), source="test")
    _register_hpa(
        state,
        metrics=[
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {"type": "Utilization", "averageUtilization": 50},
                },
            }
        ],
    )
    now = datetime(2026, 3, 18, 12, 0, 5, tzinfo=timezone.utc)
    state.upsert_workload_metrics_snapshot(
        "demo",
        controller_id="ctrl-a",
        controller_epoch=6,
        collected_at=now,
        cpu_utilization=90.0,
        memory_utilization=None,
        memory_bytes=0,
        pod_count=1,
        node_count=1,
    )
    controller = HPAAuthorityController(
        state,
        config=HPAAuthorityControllerConfig(interval_s=15, metrics_max_age_s=45, cooldown_s=0),
        authority=_FakeAuthority(is_leader=True, epoch=7),
    )
    monkeypatch.setattr(controller, "_now", lambda: now)

    controller.run_once()

    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.manifest.spec.replicas == 1
    hpa = state.get_authority_object(
        "autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa"
    )
    assert hpa is not None
    conditions = {c["type"]: c["status"] for c in hpa.status["conditions"]}
    assert conditions["ScalingActive"] == "False"
