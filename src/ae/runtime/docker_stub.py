"""Stub runtime adapter for local testing without Docker."""

from __future__ import annotations

from datetime import datetime, timezone
import os

from ae.controller.spec import AppManifest

from .base import ReplicaState, RuntimeAdapter, RuntimeResult


class StubRuntime(RuntimeAdapter):
    """Stubbed runtime; returns ready replicas without touching Docker."""

    def __init__(self) -> None:
        # Track last ensured container metadata so apishim can project Pods
        self._containers: dict[str, list[dict]] = {}
        # Allow override so tests can point at a real loopback backend (e.g., http.server)
        self._backend_host = os.getenv("AE_STUB_BACKEND_HOST", "127.0.0.1")
        try:
            self._backend_port = int(os.getenv("AE_STUB_BACKEND_PORT", "8081"))
        except Exception:
            self._backend_port = 8081
        self._default_namespace = os.getenv("AE_STUB_NAMESPACE", "default")

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
        is_job = str(getattr(manifest.spec, "workload", "service")).lower() == "job"
        # timezone.utc to remain compatible with current runtime; lint suppressed.
        now = datetime.now(timezone.utc)  # noqa: UP017
        count = desired if limit_create is None else max(0, min(desired, limit_create))
        replica_states = []
        containers: list[dict] = []
        rid_list = (
            list(replica_ids)
            if replica_ids is not None
            else [f"{manifest.metadata.name}-rev{revision}-{i}" for i in range(desired)]
        )
        # Derive a Kubernetes-style app label (without namespace prefix)
        full_name = manifest.metadata.name
        if "--" in full_name:
            ns_part, base_name = full_name.split("--", 1)
            namespace_label = ns_part or self._default_namespace
        else:
            base_name = full_name
            namespace_label = self._default_namespace
        for idx, rid in enumerate(rid_list[:count]):
            host_port = self._backend_port + idx
            status = "exited" if is_job else "running"
            exit_code = 0 if is_job else None
            finished_at = now if is_job else None
            replica_states.append(
                ReplicaState(
                    replica_id=rid,
                    ready=True if is_job else True,
                    status=status,
                    endpoint=f"{self._backend_host}:{host_port}",
                    started_at=now,
                    exit_code=exit_code,
                    finished_at=finished_at,
                )
            )
            containers.append(
                {
                    "name": rid,
                    "labels": {
                        "ae.app": manifest.metadata.name,
                        "ae.namespace": namespace_label,
                        "app": base_name,
                        "ae.revision": str(revision),
                        "ae.replica_id": rid,
                        "ae.container": "main",
                    },
                    "running": True,
                    "restart_count": 0,
                    "started_at": now.isoformat(),
                    "pod_ip": self._backend_host,
                    "host_ip": self._backend_host,
                    "host_ports": [host_port],
                }
            )
        # Persist latest view for list_containers_info/pod projection
        self._containers[manifest.metadata.name] = containers
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
        self._containers.pop(app_name, None)
        return 0

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:  # type: ignore[override]
        _ = (app_name, keep_revision)
        return 0

    def list_containers_info(self) -> list[dict]:
        """Return synthetic container info for apishim pod projection."""
        containers: list[dict] = []
        for app_conts in self._containers.values():
            containers.extend(app_conts)
        return containers.copy()
