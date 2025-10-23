"""Health probe evaluation utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from requests import RequestException, get

from ae.controller.spec import AppManifest, ProbeSpec
from ae.runtime import ReplicaState, RuntimeResult


@dataclass(slots=True)
class ProbeOutcome:
    """Result of a single probe evaluation."""

    success: bool
    message: str


@dataclass(slots=True)
class ReplicaHealth:
    """Health status for a single replica."""

    replica_id: str
    ready: bool
    live: bool
    readiness_message: str
    liveness_message: str


@dataclass(slots=True)
class HealthReport:
    """Aggregated health across replicas."""

    ready_replicas: int
    live_replicas: int
    replicas: list[ReplicaHealth]


class HealthManager:
    """Evaluates readiness and liveness based on probe specs."""

    def evaluate(self, manifest: AppManifest, result: RuntimeResult) -> HealthReport:
        replicas: list[ReplicaHealth] = []

        readiness_spec = manifest.spec.health.readiness if manifest.spec.health else None
        liveness_spec = manifest.spec.health.liveness if manifest.spec.health else None

        for replica in result.replica_states:
            readiness = self._evaluate_probe(
                replica=replica,
                probe=readiness_spec,
                default_success=replica.ready,
                probe_type="readiness",
            )
            liveness = self._evaluate_probe(
                replica=replica,
                probe=liveness_spec,
                default_success=True,
                probe_type="liveness",
            )

            ready = readiness.success and liveness.success
            live = liveness.success

            replicas.append(
                ReplicaHealth(
                    replica_id=replica.replica_id,
                    ready=ready,
                    live=live,
                    readiness_message=readiness.message,
                    liveness_message=liveness.message,
                )
            )

        ready_count = sum(1 for replica in replicas if replica.ready)
        live_count = sum(1 for replica in replicas if replica.live)
        return HealthReport(ready_replicas=ready_count, live_replicas=live_count, replicas=replicas)

    def _evaluate_probe(
        self,
        replica: ReplicaState,
        probe: Optional[ProbeSpec],
        *,
        default_success: bool,
        probe_type: str,
    ) -> ProbeOutcome:
        if probe is None:
            return ProbeOutcome(
                success=default_success,
                message=f"{probe_type} default {'ok' if default_success else 'pending'}",
            )

        if probe.initial_delay_seconds > 0 and replica.started_at is not None:
            now = datetime.now(timezone.utc)
            started_at = replica.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            elapsed = (now - started_at).total_seconds()
            if elapsed < probe.initial_delay_seconds:
                remaining = int(probe.initial_delay_seconds - elapsed)
                return ProbeOutcome(False, f"waiting initial delay ({remaining}s)")

        if probe.http_get:
            return self._evaluate_http_probe(replica, probe.http_get.path, probe, probe_type)

        return ProbeOutcome(
            success=default_success,
            message=f"{probe_type} no-op {'ok' if default_success else 'pending'}",
        )

    def _evaluate_http_probe(
        self,
        replica: ReplicaState,
        path: str,
        probe: ProbeSpec,
        probe_type: str,
    ) -> ProbeOutcome:
        if not replica.endpoint:
            return ProbeOutcome(False, f"{probe_type} endpoint missing")

        url = f"http://{replica.endpoint}{path}"
        try:
            timeout = max(probe.timeout_seconds, 1)
            response = get(url, timeout=timeout)
        except RequestException as exc:  # pragma: no cover - network path depends on runtime
            return ProbeOutcome(False, f"{probe_type} http error: {exc}")

        if 200 <= response.status_code < 300:
            return ProbeOutcome(True, f"{probe_type} http {response.status_code}")
        return ProbeOutcome(False, f"{probe_type} http {response.status_code}")
