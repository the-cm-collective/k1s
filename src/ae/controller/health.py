"""Health probe evaluation utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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
    """Evaluates readiness and liveness based on probe specs with basic windowing.

    Tracks per-replica probe streaks to honor successThreshold/failureThreshold and
    avoids probing more frequently than periodSeconds by reusing the last decision
    within the period window. State is in-memory and resets on process restart.
    """

    def __init__(self) -> None:
        # (replica_id, probe_type) -> state
        self._state: dict[tuple[str, str], dict] = {}
        self._exec_cb = None

    def set_exec_callback(self, fn):  # type: ignore[no-untyped-def]
        """Inject a callback taking (replica_id, command:list[str], timeout:int|None)->int."""
        self._exec_cb = fn

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
        key = (replica.replica_id, probe_type)
        st = self._state.get(key, {"succ": 0, "fail": 0, "last": None, "effective": default_success, "ts": None})

        if probe is None:
            st.update({"succ": 1 if default_success else 0, "fail": 0 if default_success else 1, "last": default_success, "effective": default_success, "ts": datetime.now(timezone.utc)})
            self._state[key] = st
            return ProbeOutcome(success=default_success, message=f"{probe_type} default {'ok' if default_success else 'pending'}")

        if probe.initial_delay_seconds > 0 and replica.started_at is not None:
            now = datetime.now(timezone.utc)
            started_at = replica.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            elapsed = (now - started_at).total_seconds()
            if elapsed < probe.initial_delay_seconds:
                remaining = int(probe.initial_delay_seconds - elapsed)
                return ProbeOutcome(False, f"waiting initial delay ({remaining}s)")

        # Enforce minimal periodSeconds between probe attempts
        now = datetime.now(timezone.utc)
        if st.get("ts") and probe.period_seconds > 0:
            last_ts = st["ts"]
            if isinstance(last_ts, datetime):
                if (now - last_ts).total_seconds() < max(0, int(probe.period_seconds)):
                    # Reuse last effective decision
                    return ProbeOutcome(st.get("effective", default_success), f"{probe_type} cached")

        if probe.http_get:
            outcome = self._evaluate_http_probe(replica, probe.http_get.path, probe, probe_type)
        elif getattr(probe, "tcp_socket", None) is not None:
            outcome = self._evaluate_tcp_probe(replica, probe.tcp_socket.port, probe, probe_type)  # type: ignore[union-attr]
        elif getattr(probe, "exec", None) is not None:
            outcome = self._evaluate_exec_probe(replica, probe.exec.command, probe, probe_type)  # type: ignore[union-attr]
        else:
            outcome = ProbeOutcome(success=default_success, message=f"{probe_type} no-op {'ok' if default_success else 'pending'}")

        # Update streaks and compute effective result using thresholds
        if outcome.success:
            st["succ"] = int(st.get("succ", 0)) + 1
            st["fail"] = 0
            st["last"] = True
        else:
            st["fail"] = int(st.get("fail", 0)) + 1
            st["succ"] = 0
            st["last"] = False
        st["ts"] = now

        # Success requires successThreshold consecutive successes
        if outcome.success:
            if st["succ"] >= max(1, int(probe.success_threshold)):
                st["effective"] = True
                msg = outcome.message
            else:
                st["effective"] = False
                need = max(1, int(probe.success_threshold))
                msg = f"{probe_type} waiting successThreshold ({st['succ']}/{need})"
            self._state[key] = st
            return ProbeOutcome(st["effective"], msg)

        # Failure requires failureThreshold consecutive fails; otherwise retain previous effective
        need_fail = max(1, int(probe.failure_threshold))
        if st["fail"] >= need_fail:
            st["effective"] = False
            msg = outcome.message
        else:
            # Keep previous effective decision but annotate
            msg = f"{probe_type} transient fail ({st['fail']}/{need_fail})"
        self._state[key] = st
        return ProbeOutcome(bool(st["effective"]), msg)

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

    def _evaluate_tcp_probe(
        self,
        replica: ReplicaState,
        port: int,
        probe: ProbeSpec,
        probe_type: str,
    ) -> ProbeOutcome:
        import socket as _sock

        if not replica.endpoint:
            return ProbeOutcome(False, f"{probe_type} endpoint missing")
        # Prefer the replica's resolved endpoint; if it includes a host port mapping, use it.
        host, _, ep_port = str(replica.endpoint).partition(":")
        target_port = int(ep_port or port)
        try:
            timeout = max(probe.timeout_seconds, 1)
            with _sock.create_connection((host, int(target_port)), timeout=timeout):
                return ProbeOutcome(True, f"{probe_type} tcp ok")
        except OSError as exc:
            return ProbeOutcome(False, f"{probe_type} tcp error: {exc}")

    def _evaluate_exec_probe(
        self,
        replica: ReplicaState,
        command: list[str],
        probe: ProbeSpec,
        probe_type: str,
    ) -> ProbeOutcome:
        if not callable(getattr(self, "_exec_cb", None)):
            return ProbeOutcome(False, f"{probe_type} exec unsupported")
        try:
            timeout = max(probe.timeout_seconds, 1)
        except Exception:
            timeout = 1
        try:
            code = self._exec_cb(replica.replica_id, list(command), timeout)  # type: ignore[misc]
        except Exception as exc:  # pragma: no cover
            return ProbeOutcome(False, f"{probe_type} exec error: {exc}")
        return ProbeOutcome(code == 0, f"{probe_type} exec rc={code}")
