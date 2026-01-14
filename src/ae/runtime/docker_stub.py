"""Stub runtime adapter for local testing without Docker."""

from __future__ import annotations

from datetime import datetime, timezone

from ae.controller.spec import AppManifest

from .base import ReplicaState, RuntimeAdapter, RuntimeResult


class StubRuntime(RuntimeAdapter):
    """Stubbed runtime; returns ready replicas without touching Docker."""

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
        _ = (keep_old, node_id)
        desired = len(replica_ids) if replica_ids is not None else manifest.spec.replicas
        # timezone.utc to remain compatible with current runtime; lint suppressed.
        now = datetime.now(timezone.utc)  # noqa: UP017
        count = desired if limit_create is None else max(0, min(desired, limit_create))
        replica_states = []
        rid_list = (
            list(replica_ids)
            if replica_ids is not None
            else [f"{manifest.metadata.name}-rev{revision}-{i}" for i in range(desired)]
        )
        for idx, rid in enumerate(rid_list[:count]):
            replica_states.append(
                ReplicaState(
                    replica_id=rid,
                    ready=True,
                    status="running",
                    endpoint=f"127.0.0.1:{9000 + idx}",
                    started_at=now,
                )
            )
        return RuntimeResult(
            revision=revision,
            created=count,
            updated=0,
            removed=0,
            replica_states=replica_states,
        )

    def read_logs(
        self,
        replica_id: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        _ = (tail, since)
        # Deterministic, small output for tests
        if follow:
            # emit a finite small stream for tests
            for i in range(3):
                yield f"{replica_id}: log line {i}"
        else:
            yield f"{replica_id}: recent log line"

    def remove_app(self, app_name: str) -> int:  # type: ignore[override]
        _ = app_name
        return 0

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:  # type: ignore[override]
        _ = (app_name, keep_revision)
        return 0
