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

    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        replica_ids: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
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

    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:
        """Ensure named storage volumes exist for the app.

        volumes: list of dicts with keys { name }
        """

    def remove_storage_volumes(self, app_name: str, names: list[str]) -> int:
        """Remove named storage volumes for the app when retention=Delete.

        Returns the number of volumes removed.
        """

    def list_storage_volumes(self, app_name: str | None = None) -> list[dict]:
        """List storage volumes known to the runtime.

        Returns a list of dicts with at least: { name, labels }.
        Implementations may include mountpoints and driver details when available.
        """

    def list_containers_info(self) -> list[dict]:
        """List running containers info for planning/conflict checks.

        Returns a list of dicts with at least: { name, labels, host_ports: [int] }.
        """
        return []

    def exec(self, replica_id: str, command: list[str], *, timeout: int | None = None) -> int:
        """Execute a command inside the target replica's container.

        Returns the exit code. Implementations should locate the container by
        the `ae.replica_id` label and run the command non-interactively.
        """

    # Optional API for container-scoped exec when multi-container is enabled.
    # Implementations may choose not to support it; callers should fall back
    # to `exec(replica_id, ...)` when unavailable.
    def exec_for_container(
        self, app_name: str, container_name: str, command: list[str], *, timeout: int | None = None
    ) -> int:  # pragma: no cover - optional; implementations provide coverage
        ...
