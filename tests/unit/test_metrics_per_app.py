from __future__ import annotations

import io
from pathlib import Path

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata
from ae.controller.state import SQLiteStateStore
from ae.observability import MetricsService
from ae.observability.http_api import _ApiHandler, _HA_FENCE_METRICS, record_ha_fence_event
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
    # Status one-hot includes ready=1
    assert 'ae_app_status{app="demo",status="ready"} 1' in txt
    # Alias families kept for compatibility
    assert 'ae_ready_replicas{app="demo"} 2' in txt


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
