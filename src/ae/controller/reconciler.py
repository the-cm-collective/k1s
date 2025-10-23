"""Reconcile loop skeleton for the application engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ae.controller.health import HealthManager, HealthReport
from ae.controller.spec import AppManifest, load_manifest
from ae.runtime import RuntimeAdapter, RuntimeResult

from .state import SQLiteStateStore
from ae.ingress.service import IngressService


@dataclass(slots=True)
class ReconcileReport:
    """Summary of a reconcile run."""

    app_name: str
    created: int
    updated: int
    removed: int
    ready_replicas: int
    live_replicas: int


class Reconciler:
    """Coordinates manifest application across runtime, health, and state store."""

    def __init__(
        self,
        runtime: RuntimeAdapter,
        state_store: SQLiteStateStore,
        health_manager: HealthManager | None = None,
        ingress_service: IngressService | None = None,
    ) -> None:
        self._runtime = runtime
        self._state_store = state_store
        self._health_manager = health_manager or HealthManager()
        self._ingress_service = ingress_service

    def reconcile_manifest_path(self, path: Path) -> ReconcileReport:
        """Load a manifest from disk and reconcile it."""

        manifest = load_manifest(path)
        return self.reconcile(manifest)

    def reconcile(self, manifest: AppManifest) -> ReconcileReport:
        """Reconcile the runtime to match the manifest."""

        result = self._runtime.ensure_app(manifest)
        health_report = self._health_manager.evaluate(manifest, result)
        if manifest.spec.ingress and self._ingress_service:
            upstream = self._select_upstream(manifest, result, health_report)
            if upstream:
                self._ingress_service.apply(manifest, upstream)
                self._ingress_service.reload()
        elif self._ingress_service and not manifest.spec.ingress:
            self._ingress_service.remove(manifest.metadata.name)
            self._ingress_service.reload()
        self._state_store.record_snapshot(
            manifest=manifest,
            runtime_result=result,
            health_report=health_report,
        )
        return ReconcileReport(
            app_name=manifest.metadata.name,
            created=result.created,
            updated=result.updated,
            removed=result.removed,
            ready_replicas=health_report.ready_replicas,
            live_replicas=health_report.live_replicas,
        )

    def _select_upstream(
        self,
        manifest: AppManifest,
        result: RuntimeResult,
        health_report: HealthReport,
    ) -> str | None:
        states_by_id = {state.replica_id: state for state in result.replica_states}

        for replica in health_report.replicas:
            if not replica.ready:
                continue
            state = states_by_id.get(replica.replica_id)
            if state and state.endpoint:
                return state.endpoint

        for state in result.replica_states:
            if state.endpoint:
                return state.endpoint

        if manifest.spec.ports:
            port = manifest.spec.ports[0].container_port
            return f"127.0.0.1:{port}"

        return None
