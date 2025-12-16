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
    total_nodes: int = 0
    ready_nodes: int = 0
    stale_nodes: int = 0
    total_services: int = 0


class MetricsService:
    """Aggregates metrics from application status records."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self._store = store

    def snapshot(self) -> MetricsSnapshot:
        statuses = self._store.list_status()
        total_apps = len(statuses)
        ready_apps = sum(1 for status in statuses if status.revision_status == "ready")
        progressing_apps = sum(1 for status in statuses if status.revision_status == "progressing")
        degraded_apps = total_apps - ready_apps - progressing_apps
        total_replicas = sum(status.desired_replicas for status in statuses)
        ready_replicas = sum(status.ready_replicas for status in statuses)
        live_replicas = sum(status.live_replicas for status in statuses)
        # Node and service counts (best-effort, optional for single-node)
        try:
            import os
            from datetime import datetime, timezone

            grace = int(os.getenv("AE_NODE_NOTREADY_AFTER", "40") or 40)
            now = datetime.now(timezone.utc)
            nodes = self._store.list_nodes()
            total_nodes = len(nodes)
            ready_nodes = 0
            stale_nodes = 0
            for node, status in nodes:
                if status is None:
                    stale_nodes += 1
                    continue
                try:
                    age = (now - status.seen_at).total_seconds()
                except Exception:
                    age = grace + 1
                stale = age > grace
                st = str(status.status or "").lower()
                if st == "ready" and not stale:
                    ready_nodes += 1
                if stale or st != "ready":
                    stale_nodes += 1
        except Exception:
            total_nodes = ready_nodes = stale_nodes = 0

        try:
            total_services = len(self._store.list_services())
        except Exception:
            total_services = 0

        return MetricsSnapshot(
            total_apps=total_apps,
            ready_apps=ready_apps,
            progressing_apps=progressing_apps,
            degraded_apps=degraded_apps,
            total_replicas=total_replicas,
            ready_replicas=ready_replicas,
            live_replicas=live_replicas,
            total_nodes=total_nodes,
            ready_nodes=ready_nodes,
            stale_nodes=stale_nodes,
            total_services=total_services,
        )
