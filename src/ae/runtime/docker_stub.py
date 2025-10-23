"""Placeholder implementation of the runtime adapter using docker-py.

This module intentionally keeps logic minimal for Phase 1. Future phases will
replace stubbed operations with real docker/client interactions.
"""

from __future__ import annotations

from ae.controller.spec import AppManifest

from .base import ReplicaState, RuntimeAdapter, RuntimeResult


class DockerRuntime(RuntimeAdapter):
    """Stubbed Docker runtime; records intended operations."""

    def ensure_app(self, manifest: AppManifest) -> RuntimeResult:  # noqa: D401
        # Phase 1 stub: pretend one replica is ready. Later we will
        # reconcile containers via docker SDK and return precise counts.
        desired = manifest.spec.replicas
        replica_states = [
            ReplicaState(replica_id=f"{manifest.metadata.name}-{idx}", ready=True)
            for idx in range(desired)
        ]
        return RuntimeResult(
            created=desired,
            updated=0,
            removed=0,
            replica_states=replica_states,
        )
