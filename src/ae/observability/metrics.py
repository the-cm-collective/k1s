"""Metrics helpers derived from state store snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from ae.controller.state import SQLiteStateStore


@dataclass(slots=True)
class MetricsSnapshot:
    total_apps: int
    ready_apps: int
    progressing_apps: int
    degraded_apps: int
    total_replicas: int
    ready_replicas: int
    live_replicas: int


class MetricsService:
    """Aggregates metrics from application status records."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self._store = store

    def snapshot(self) -> MetricsSnapshot:
        statuses = self._store.list_status()
        total_apps = len(statuses)
        ready_apps = sum(1 for status in statuses if status.revision_status == "ready")
        progressing_apps = sum(
            1 for status in statuses if status.revision_status == "progressing"
        )
        degraded_apps = total_apps - ready_apps - progressing_apps
        total_replicas = sum(status.desired_replicas for status in statuses)
        ready_replicas = sum(status.ready_replicas for status in statuses)
        live_replicas = sum(status.live_replicas for status in statuses)
        return MetricsSnapshot(
            total_apps=total_apps,
            ready_apps=ready_apps,
            progressing_apps=progressing_apps,
            degraded_apps=degraded_apps,
            total_replicas=total_replicas,
            ready_replicas=ready_replicas,
            live_replicas=live_replicas,
        )
