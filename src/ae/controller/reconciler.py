"""Reconcile loop skeleton for the application engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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


class Reconciler:
    """Coordinates manifest application across runtime and state store."""

    def __init__(self, runtime: RuntimeAdapter, state_store: SQLiteStateStore) -> None:
        self._runtime = runtime
        self._state_store = state_store

    def reconcile_manifest_path(self, path: Path) -> ReconcileReport:
        """Load a manifest from disk and reconcile it."""

        manifest = load_manifest(path)
        return self.reconcile(manifest)

    def reconcile(self, manifest: AppManifest) -> ReconcileReport:
        """Reconcile the runtime to match the manifest."""

        result = self._runtime.ensure_app(manifest)
        self._state_store.record_snapshot(
            manifest=manifest,
            ready_replicas=result.ready_replicas,
            replica_meta=result.replica_ids,
        )
        return ReconcileReport(
            app_name=manifest.metadata.name,
            created=result.created,
            updated=result.updated,
            removed=result.removed,
            ready_replicas=result.ready_replicas,
        )
