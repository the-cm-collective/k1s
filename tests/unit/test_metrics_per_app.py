from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata
from ae.controller.state import SQLiteStateStore
from ae.observability import MetricsService
from ae.observability.http_api import (
    _ApiHandler,
    _GATEWAY_WORK_METRICS,
    _HA_FENCE_METRICS,
    _HPA_ACTIVITY_METRICS,
    _ROUTE_BUNDLE_METRICS,
    _SITE_GATEWAY_BUILD_INFO,
    _SITE_GATEWAY_LAST_SEEN,
    record_gateway_metrics,
    record_gateway_identity,
    record_ha_fence_event,
    record_hpa_activity,
    record_route_bundle_publish_state,
    record_site_seen,
)
from ae.runtime.base import PodState, RuntimeAdapter, RuntimeResult


class _StubRuntime(RuntimeAdapter):
    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:  # type: ignore[override]
        _ = (keep_old, limit_create, node_id)
        # Return all replicas ready/live
        names = (
            pod_names
            if pod_names is not None
            else [
                f"{manifest.metadata.name}-rev{revision}-{i}"
                for i in range(int(manifest.spec.replicas))
            ]
        )
        reps = [
            PodState(pod_name=pod_name, ready=True, status="running") for pod_name in names
        ]
        return RuntimeResult(
            revision=revision, created=len(reps), updated=0, removed=0, pod_states=reps
        )


def _build_manifest(name: str = "demo", replicas: int = 2) -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=name),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=replicas,
            ingress=IngressSpec(host=f"{name}.local", path="/"),
        ),
    )


def test_metrics_expose_per_app_series(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=_StubRuntime(), state_store=store)

    man = _build_manifest("demo", replicas=2)
    report = reconciler.reconcile(man)
    assert report.revision_status == "ready"

    # Call the handler method directly without opening a socket
    # Build a fake handler instance with required attributes and stubs
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.metrics = MetricsService(store)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]

    _ApiHandler._handle_metrics(handler)  # type: ignore[arg-type]
    txt = handler.wfile.getvalue().decode("utf-8", "replace")
    # New per-app metrics
    assert 'ae_app_desired_replicas{app="demo"} 2' in txt
    assert 'ae_app_ready_replicas{app="demo"} 2' in txt
    assert 'ae_app_live_replicas{app="demo"} 2' in txt
    assert 'ae_app_current_revision_ready_replicas{app="demo"} 2' in txt
    assert 'ae_app_current_revision_live_replicas{app="demo"} 2' in txt
    assert 'ae_app_old_revision_ready_replicas{app="demo"} 0' in txt
    assert 'ae_app_old_revision_live_replicas{app="demo"} 0' in txt
    assert 'ae_app_overlap_ready_replicas{app="demo"} 0' in txt
    assert 'ae_app_overlap_live_replicas{app="demo"} 0' in txt
    # Status one-hot includes ready=1
    assert 'ae_app_status{app="demo",status="ready"} 1' in txt
    # Alias families kept for compatibility
    assert 'ae_ready_replicas{app="demo"} 2' in txt


def test_http_status_surfaces_include_rollout_overlap_fields(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=_StubRuntime(), state_store=store)
    man = _build_manifest("demo", replicas=2)
    report = reconciler.reconcile(man)
    assert report.current_revision_ready_replicas == 2

    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.path = "/status"  # type: ignore[attr-defined]
    handler._presented_role = lambda: None  # type: ignore[attr-defined]

    captured: dict[str, object] = {}

    def _json_ok(payload):  # noqa: ANN001
        captured["payload"] = payload

    handler._json_ok = _json_ok  # type: ignore[attr-defined]

    _ApiHandler._handle_status_list(handler)  # type: ignore[arg-type]
    payload = captured["payload"]
    assert isinstance(payload, dict)
    item = payload["items"][0]  # type: ignore[index]
    assert item["current_revision_ready_replicas"] == 2  # type: ignore[index]
    assert item["current_revision_live_replicas"] == 2  # type: ignore[index]
    assert item["old_revision_ready_replicas"] == 0  # type: ignore[index]
    assert item["old_revision_live_replicas"] == 0  # type: ignore[index]
    assert item["overlap_ready_replicas"] == 0  # type: ignore[index]
    assert item["overlap_live_replicas"] == 0  # type: ignore[index]

    captured.clear()
    _ApiHandler._handle_status_single(handler, "demo")  # type: ignore[arg-type]
    single = captured["payload"]
    assert isinstance(single, dict)
    assert single["current_revision_ready_replicas"] == 2  # type: ignore[index]
    assert single["current_revision_live_replicas"] == 2  # type: ignore[index]
    assert single["old_revision_ready_replicas"] == 0  # type: ignore[index]
    assert single["old_revision_live_replicas"] == 0  # type: ignore[index]
    assert single["overlap_ready_replicas"] == 0  # type: ignore[index]
    assert single["overlap_live_replicas"] == 0  # type: ignore[index]


def test_metrics_expose_ha_fence_counters(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.metrics = MetricsService(store)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]

    _HA_FENCE_METRICS.clear()
    record_ha_fence_event("gateway.lease_acquire", stale=True)
    record_ha_fence_event("gateway.lease_acquire", duplicate=True, epoch_advanced=True)
    try:
        _ApiHandler._handle_metrics(handler)  # type: ignore[arg-type]
        txt = handler.wfile.getvalue().decode("utf-8", "replace")
    finally:
        _HA_FENCE_METRICS.clear()

    assert 'ae_ha_fence_stale_total{surface="gateway.lease_acquire"} 1.0' in txt
    assert 'ae_ha_fence_duplicate_total{surface="gateway.lease_acquire"} 1.0' in txt
    assert 'ae_ha_fence_epoch_advance_total{surface="gateway.lease_acquire"} 1.0' in txt


def test_metrics_expose_gateway_replay_and_route_publish_series(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.metrics = MetricsService(store)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]

    _GATEWAY_WORK_METRICS.clear()
    _ROUTE_BUNDLE_METRICS.clear()
    record_gateway_metrics(
        "sea",
        work_stale_total=1,
        work_nak_total=2,
        lease_retry_total=3,
        result_replay_total=4,
        result_replay_fail_total=5,
        result_replay_backlog=6,
    )
    record_route_bundle_publish_state(
        "sea",
        pending=True,
        ack_age_seconds=7.5,
        publish_ok=True,
        publish_fail=True,
    )
    try:
        _ApiHandler._handle_metrics(handler)  # type: ignore[arg-type]
        txt = handler.wfile.getvalue().decode("utf-8", "replace")
    finally:
        _GATEWAY_WORK_METRICS.clear()
        _ROUTE_BUNDLE_METRICS.clear()

    assert 'ae_gateway_result_replay_total{site="sea"} 4.0' in txt
    assert 'ae_gateway_result_replay_fail_total{site="sea"} 5.0' in txt
    assert 'ae_gateway_result_replay_backlog{site="sea"} 6.0' in txt
    assert 'ae_route_bundle_publish_ok_total{site="sea"} 1.0' in txt
    assert 'ae_route_bundle_publish_fail_total{site="sea"} 1.0' in txt
    assert 'ae_route_bundle_pending{site="sea"} 1.0' in txt
    assert 'ae_route_bundle_ack_age_seconds{site="sea"} 7.5' in txt


def test_metrics_expose_site_gateway_last_seen_and_build_info(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.metrics = MetricsService(store)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]

    _SITE_GATEWAY_LAST_SEEN.clear()
    _SITE_GATEWAY_BUILD_INFO.clear()
    record_site_seen("sea", node_id="edge-1")
    record_gateway_identity(
        "sea",
        "edge-1",
        version="0.1.3.dev0",
        sha="sha-edge",
        date="2026-03-18",
    )
    try:
        _ApiHandler._handle_metrics(handler)  # type: ignore[arg-type]
        txt = handler.wfile.getvalue().decode("utf-8", "replace")
    finally:
        _SITE_GATEWAY_LAST_SEEN.clear()
        _SITE_GATEWAY_BUILD_INFO.clear()

    assert 'ae_site_gateway_last_seen_seconds{site="sea",node="edge-1"} ' in txt
    assert (
        'ae_site_gateway_build_info{site="sea",node="edge-1",version="0.1.3.dev0",sha="sha-edge",date="2026-03-18"} 1'
        in txt
    )


def test_metrics_expose_hpa_authority_series(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.metrics = MetricsService(store)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]

    original = dict(_HPA_ACTIVITY_METRICS)
    for key in list(_HPA_ACTIVITY_METRICS):
        _HPA_ACTIVITY_METRICS[key] = 0.0
    record_hpa_activity(reconcile=True, scale=True, metrics_stale=True, metrics_missing=True)
    record_hpa_activity(snapshot_age_seconds=12.5)
    try:
        _ApiHandler._handle_metrics(handler)  # type: ignore[arg-type]
        txt = handler.wfile.getvalue().decode("utf-8", "replace")
    finally:
        _HPA_ACTIVITY_METRICS.clear()
        _HPA_ACTIVITY_METRICS.update(original)

    assert "ae_hpa_reconcile_total 1.0" in txt
    assert "ae_hpa_scale_total 1.0" in txt
    assert "ae_hpa_metrics_stale_total 1.0" in txt
    assert "ae_hpa_metrics_missing_total 1.0" in txt
    assert "ae_hpa_snapshot_age_seconds 12.5" in txt


def test_metrics_expose_controller_authority_series(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.metrics = MetricsService(store)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]
    handler.authority_info_fn = staticmethod(  # type: ignore[attr-defined]
        lambda: SimpleNamespace(
            enabled=True,
            is_leader=True,
            leader_info=SimpleNamespace(controller_id="ctrl-a", controller_epoch=19),
            controller_epoch=19,
        )
    )

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]

    _ApiHandler._handle_metrics(handler)  # type: ignore[arg-type]
    txt = handler.wfile.getvalue().decode("utf-8", "replace")

    assert "ae_controller_is_leader 1" in txt
    assert "ae_controller_epoch 19" in txt
    assert "ae_controller_authority_healthy 1" in txt


def test_metrics_report_unhealthy_authority_when_no_leader_is_visible(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.metrics = MetricsService(store)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]
    handler.authority_info_fn = staticmethod(  # type: ignore[attr-defined]
        lambda: SimpleNamespace(
            enabled=True,
            is_leader=False,
            leader_info=None,
            controller_epoch=0,
        )
    )

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]

    _ApiHandler._handle_metrics(handler)  # type: ignore[arg-type]
    txt = handler.wfile.getvalue().decode("utf-8", "replace")

    assert "ae_controller_is_leader 0" in txt
    assert "ae_controller_epoch 0" in txt
    assert "ae_controller_authority_healthy 0" in txt


def test_metrics_expose_controller_build_info(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AE_BUILD_SHA", "abc123")
    monkeypatch.setenv("AE_BUILD_DATE", "2026-03-18")
    store = SQLiteStateStore(tmp_path / "state.db")
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.metrics = MetricsService(store)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]

    _ApiHandler._handle_metrics(handler)  # type: ignore[arg-type]
    txt = handler.wfile.getvalue().decode("utf-8", "replace")

    assert 'ae_controller_build_info{version="0.1.3.dev0",sha="abc123",date="2026-03-18"} 1' in txt
