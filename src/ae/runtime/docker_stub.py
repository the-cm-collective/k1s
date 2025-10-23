"""Stub runtime adapter for local testing without Docker."""

from __future__ import annotations

from datetime import datetime, timezone

from ae.controller.spec import AppManifest

from .base import ReplicaState, RuntimeAdapter, RuntimeResult


class StubRuntime(RuntimeAdapter):
    """Stubbed runtime; returns ready replicas without touching Docker."""

    def ensure_app(self, manifest: AppManifest, revision: int) -> RuntimeResult:
        desired = manifest.spec.replicas
        now = datetime.now(timezone.utc)
        replica_states = [
            ReplicaState(
                replica_id=f"{manifest.metadata.name}-rev{revision}-{idx}",
                ready=True,
                status="running",
                endpoint=f"127.0.0.1:{9000 + idx}",
                started_at=now,
            )
            for idx in range(desired)
        ]
        return RuntimeResult(
            revision=revision,
            created=desired,
            updated=0,
            removed=0,
            replica_states=replica_states,
        )
