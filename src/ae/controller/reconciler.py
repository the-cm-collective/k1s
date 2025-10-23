"""Reconcile loop skeleton for the application engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ae.controller.health import HealthManager
from ae.controller.spec import AppManifest, load_manifest
from ae.runtime import RuntimeAdapter

from .state import SQLiteStateStore


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
    ) -> None:
        self._runtime = runtime
        self._state_store = state_store
        self._health_manager = health_manager or HealthManager()

    def reconcile_manifest_path(self, path: Path) -> ReconcileReport:
        """Load a manifest from disk and reconcile it."""

        manifest = load_manifest(path)
        return self.reconcile(manifest)

    def reconcile(self, manifest: AppManifest) -> ReconcileReport:
        """Reconcile the runtime to match the manifest."""

        result = self._runtime.ensure_app(manifest)
        health_report = self._health_manager.evaluate(manifest, result)
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
