"""Runtime adapter interfaces for container orchestration."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime
from typing import Protocol

from ae.controller.spec import AppManifest


@dataclass(slots=True)
class PodState:
    """Status for an individual pod in the runtime."""

    ready: bool
    status: str = "running"
    pod_name: str = ""
    endpoint: str | None = None
    started_at: datetime | None = None
    exit_code: int | None = None
    finished_at: datetime | None = None
    replica_id: InitVar[str | None] = None

    def __post_init__(self, replica_id: str | None) -> None:
        if not self.pod_name and replica_id:
            self.pod_name = str(replica_id)

    @property
    def replica_id(self) -> str:
        return self.pod_name

    @replica_id.setter
    def replica_id(self, value: str) -> None:
        self.pod_name = value


ReplicaState = PodState


@dataclass(slots=True)
class RuntimeResult:
    """Result of reconciling containers for an application."""

    revision: int
    created: int
    updated: int
    removed: int
    pod_states: list[PodState] = field(default_factory=list)

    def __init__(
        self,
        revision: int,
        created: int,
        updated: int,
        removed: int,
        pod_states: list[PodState] | None = None,
        replica_states: list[PodState] | None = None,
    ) -> None:
        if pod_states is None:
            pod_states = list(replica_states) if replica_states is not None else []
        self.revision = revision
        self.created = created
        self.updated = updated
        self.removed = removed
        self.pod_states = list(pod_states)

    @property
    def replica_states(self) -> list[PodState]:
        return self.pod_states


class RuntimeAdapter(Protocol):
    """Adapter that drives container runtime operations."""

    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
        """Ensure the runtime matches the manifest."""

    def read_logs(
        self,
        pod_name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        """Yield log lines for a given pod identifier.

        Implementations should locate the container by the pod label and
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

    def exec(self, pod_name: str, command: list[str], *, timeout: int | None = None) -> int:
        """Execute a command inside the target pod's container.

        Returns the exit code. Implementations should locate the container by
        the pod label and run the command non-interactively.
        """

    # Optional API for container-scoped exec when multi-container is enabled.
    # Implementations may choose not to support it; callers should fall back
    # to `exec(replica_id, ...)` when unavailable.
    def exec_for_container(
        self, app_name: str, container_name: str, command: list[str], *, timeout: int | None = None
    ) -> int:  # pragma: no cover - optional; implementations provide coverage
        ...

    # Optional streaming exec (stdin/stdout/stderr) for kubectl exec
    def exec_attach(
        self,
        pod_name: str,
        command: list[str],
        *,
        container: str | None = None,
        tty: bool = False,
    ):  # pragma: no cover - optional; implementations provide coverage
        ...

    def exec_resize(
        self, exec_id: str, *, height: int | None = None, width: int | None = None
    ) -> None:  # pragma: no cover
        """Optionally resize a TTY exec session."""
        ...

    def exec_exit_code(self, exec_id: str) -> int:  # pragma: no cover
        """Optionally return exit code for a completed exec session."""
        _ = exec_id
        return 0
