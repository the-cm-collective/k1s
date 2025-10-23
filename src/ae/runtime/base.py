"""Runtime adapter interfaces for container orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ae.controller.spec import AppManifest


@dataclass(slots=True)
class ReplicaState:
    """Status for an individual replica in the runtime."""

    replica_id: str
    ready: bool
    status: str = "running"
    endpoint: str | None = None


@dataclass(slots=True)
class RuntimeResult:
    """Result of reconciling containers for an application."""

    created: int
    updated: int
    removed: int
    replica_states: list[ReplicaState]


class RuntimeAdapter(Protocol):
    """Adapter that drives container runtime operations."""

    def ensure_app(self, manifest: AppManifest) -> RuntimeResult:
        """Ensure the runtime matches the manifest."""
