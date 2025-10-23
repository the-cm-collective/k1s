"""Health probe evaluation utilities."""

from __future__ import annotations

from dataclasses import dataclass

from requests import RequestException, get

from ae.controller.spec import AppManifest
from ae.runtime import ReplicaState, RuntimeResult


@dataclass(slots=True)
class ReplicaHealth:
    """Health status for a single replica."""

    replica_id: str
    ready: bool
    message: str = ""


@dataclass(slots=True)
class HealthReport:
    """Aggregated health across replicas."""

    ready_replicas: int
    replicas: list[ReplicaHealth]


class HealthManager:
    """Evaluates readiness based on runtime status and probes."""

    def evaluate(self, manifest: AppManifest, result: RuntimeResult) -> HealthReport:
        replicas: list[ReplicaHealth] = []

        for replica in result.replica_states:
            ready, message = self._evaluate_replica(manifest, replica)
            replicas.append(
                ReplicaHealth(
                    replica_id=replica.replica_id,
                    ready=ready,
                    message=message,
                )
            )

        ready_count = sum(1 for replica in replicas if replica.ready)
        return HealthReport(ready_replicas=ready_count, replicas=replicas)

    def _evaluate_replica(self, manifest: AppManifest, replica: ReplicaState) -> tuple[bool, str]:
        # Phase 2 placeholder: rely on runtime readiness flag.
        # Later this will execute HTTP/TCP probes described in manifest.spec.health.
        readiness = manifest.spec.health.readiness if manifest.spec.health else None
        if readiness and readiness.http_get and replica.endpoint:
            url = f"http://{replica.endpoint}{readiness.http_get.path}"
            try:
                response = get(url, timeout=max(readiness.timeout_seconds, 1))
            except RequestException as exc:  # pragma: no cover - exception message path simple
                return False, f"http probe error: {exc}"  # type: ignore[str-format]
            if 200 <= response.status_code < 300:
                return True, f"http {response.status_code}"
            return False, f"http {response.status_code}"
        return replica.ready, "runtime ready" if replica.ready else "runtime pending"
