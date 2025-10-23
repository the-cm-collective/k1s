"""Runtime adapter interfaces for container orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ae.controller.spec import AppManifest


@dataclass(slots=True)
class RuntimeResult:
    """Result of reconciling containers for an application."""

    created: int
    updated: int
    removed: int
    ready_replicas: int
    replica_ids: list[str]


class RuntimeAdapter(Protocol):
    """Adapter that drives container runtime operations."""

    def ensure_app(self, manifest: AppManifest) -> RuntimeResult:
        """Ensure the runtime matches the manifest."""
