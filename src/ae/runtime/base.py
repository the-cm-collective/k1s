"""Runtime adapter interfaces for container orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ae.controller.spec import AppManifest


@dataclass(slots=True)
class ReplicaState:
    """Status for an individual replica in the runtime."""

    replica_id: str
    ready: bool
    status: str = "running"
    endpoint: str | None = None
    started_at: datetime | None = None


@dataclass(slots=True)
class RuntimeResult:
    """Result of reconciling containers for an application."""

    revision: int
    created: int
    updated: int
    removed: int
    replica_states: list[ReplicaState]


class RuntimeAdapter(Protocol):
    """Adapter that drives container runtime operations."""

    def ensure_app(self, manifest: AppManifest, revision: int, *, keep_old: bool = False, limit_create: int | None = None) -> RuntimeResult:
        """Ensure the runtime matches the manifest."""

    def read_logs(
        self,
        replica_id: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        """Yield log lines for a given replica identifier.

        Implementations should locate the container by the `replica_id` label and
        yield decoded UTF-8 lines. If `follow` is True, continue streaming.
        """

    def remove_app(self, app_name: str) -> int:
        """Stop and remove all containers belonging to an app.

        Returns the number of containers removed.
        """

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        """Remove containers of older revisions for a given app, keeping the specified revision."""
