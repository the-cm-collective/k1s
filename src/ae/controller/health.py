"""Health probe evaluation utilities."""

from __future__ import annotations

from dataclasses import InitVar, dataclass
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from requests import RequestException, get

from ae.controller.spec import AppManifest, ProbeSpec
from ae.runtime import PodState, RuntimeResult


@dataclass(slots=True)
class ProbeOutcome:
    """Result of a single probe evaluation."""

    success: bool
    message: str


@dataclass(slots=True)
class PodHealth:
    """Health status for a single pod."""

    ready: bool
    live: bool
    readiness_message: str
    liveness_message: str
    pod_name: str = ""
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


@dataclass(slots=True)
class HealthReport:
    """Aggregated health across replicas."""

    ready_replicas: int
    live_replicas: int
    pods: list[PodHealth]

    @property
    def replicas(self) -> list[PodHealth]:
        return self.pods


class HealthManager:
    """Evaluates readiness and liveness with thresholds, period, and backoff.

    - Honors successThreshold/failureThreshold using per-replica streaks
    - Enforces periodSeconds: within the period, reuse last effective result
    - Applies exponential backoff with jitter after sustained failures, capped by
      AE_PROBE_MAX_BACKOFF (seconds, default 30). Jitter is a small per-replica
      stable offset to avoid thundering herds.
    - State is in-memory and resets on process restart.
    """

    def __init__(self) -> None:
        # (replica_id, probe_type) -> state
        self._state: dict[tuple[str, str], dict] = {}
        self._exec_cb = None
        self._portforward_cb = None
        self._loopback_fallback = os.getenv("AE_PROBE_LOOPBACK_FALLBACK", "").strip()
        # optional callback: (replica_id, probe_type, success, message)
        self._event_cb = None

    def set_exec_callback(self, fn):  # type: ignore[no-untyped-def]
        """Inject a callback taking (replica_id, command:list[str], timeout:int|None)->int."""
        self._exec_cb = fn

    def set_portforward_callback(self, fn):  # type: ignore[no-untyped-def]
        """Inject a callback taking (pod_name, namespace, port)->socket-like object."""
        self._portforward_cb = fn

    def set_event_callback(self, fn):  # type: ignore[no-untyped-def]
        """Inject a callback taking (replica_id, probe_type, success, message)."""
        self._event_cb = fn

    def evaluate(self, manifest: AppManifest, result: RuntimeResult) -> HealthReport:
        pods: list[PodHealth] = []
        metadata = getattr(manifest, "metadata", None)
        namespace = getattr(metadata, "namespace", None)

        readiness_spec = manifest.spec.health.readiness if manifest.spec.health else None
        liveness_spec = manifest.spec.health.liveness if manifest.spec.health else None
        startup_spec = (
            getattr(manifest.spec.health, "startup", None) if manifest.spec.health else None
        )
        is_job = str(getattr(manifest.spec, "workload", "service")).lower() == "job"

        # Group runtime states by pod name to support multi-container pods
        groups: dict[str, list[PodState]] = {}
        for rs in result.pod_states:
            groups.setdefault(rs.pod_name, []).append(rs)

        for pod_name, members in groups.items():
            # Choose a primary state for probe evaluation: prefer the one with an endpoint
            primary = None
            for m in members:
                if getattr(m, "endpoint", None):
                    primary = m
                    break
            if primary is None:
                primary = members[0]
            # All containers in the replica must be healthy to be considered ready overall.
            if is_job:
                sidecars_ok = all(
                    (getattr(m, "status", "running") == "running")
                    or (getattr(m, "exit_code", None) == 0)
                    for m in members
                )
            else:
                sidecars_ok = all((getattr(m, "status", "running") == "running") for m in members)
            pod = primary
            # If startupProbe is defined, gate readiness/liveness until it succeeds.
            if startup_spec is not None:
                startup = self._evaluate_probe(
                    pod=pod,
                    probe=startup_spec,
                    default_success=False,
                    probe_type="startup",
                    namespace=namespace,
                )
                if not startup.success:
                    # While startup is pending/failing, treat liveness as OK and readiness as False.
                    pods.append(
                        PodHealth(
                            pod_name=pod_name,
                            ready=False,
                            live=True if sidecars_ok else False,
                            readiness_message=f"startup pending: {startup.message}",
                            liveness_message=(
                                "liveness gated by startup"
                                if sidecars_ok
                                else "sidecar not running"
                            ),
                        )
                    )
                    # Defer further probe work until startup succeeds
                    continue

            readiness = self._evaluate_probe(
                pod=pod,
                probe=readiness_spec,
                default_success=pod.ready,
                probe_type="readiness",
                namespace=namespace,
            )
            liveness = self._evaluate_probe(
                pod=pod,
                probe=liveness_spec,
                default_success=True,
                probe_type="liveness",
                namespace=namespace,
            )

            # Align with Kubernetes semantics:
            # - readiness gates traffic and is independent of liveness
            # - liveness indicates whether the container should be considered alive
            ready = readiness.success and sidecars_ok
            live = liveness.success and sidecars_ok

            pods.append(
                PodHealth(
                    pod_name=pod_name,
                    ready=ready,
                    live=live,
                    readiness_message=readiness.message,
                    liveness_message=(
                        liveness.message if sidecars_ok else "one or more sidecars not running"
                    ),
                )
            )

        ready_count = sum(1 for pod in pods if pod.ready)
        live_count = sum(1 for pod in pods if pod.live)
        return HealthReport(ready_replicas=ready_count, live_replicas=live_count, pods=pods)

    def _evaluate_probe(
        self,
        pod: PodState,
        probe: Optional[ProbeSpec],
        *,
        default_success: bool,
        probe_type: str,
        namespace: str | None,
    ) -> ProbeOutcome:
        key = (pod.pod_name, probe_type)
        st = self._state.get(
            key,
            {
                "succ": 0,
                "fail": 0,
                "last": None,
                "effective": default_success,
                "ts": None,
                "cooldown_until": None,
            },
        )
        now = datetime.now(timezone.utc)

        if probe is None:
            prev_effective = st.get("effective", default_success)
            st.update(
                {
                    "succ": 1 if default_success else 0,
                    "fail": 0 if default_success else 1,
                    "last": default_success,
                    "effective": default_success,
                    "ts": datetime.now(timezone.utc),
                }
            )
            self._state[key] = st
            if prev_effective != default_success:
                self._emit_probe_event(
                    pod.pod_name,
                    probe_type,
                    default_success,
                    f"{probe_type} default {'ok' if default_success else 'pending'}",
                )
            return ProbeOutcome(
                success=default_success,
                message=f"{probe_type} default {'ok' if default_success else 'pending'}",
            )

        # Enforce periodSeconds: reuse last decision within the window
        try:
            period = int(getattr(probe, "period_seconds", 10))
        except Exception:
            period = 10
        last_ts = st.get("ts")
        if last_ts is not None and period > 0:
            elapsed = (now - last_ts).total_seconds()
            if elapsed < period:
                remaining = int(max(0, period - elapsed))
                return ProbeOutcome(
                    bool(st.get("effective", default_success)),
                    f"{probe_type} waiting period ({remaining}s)",
                )

        # Respect backoff cooldown window after sustained failures
        cd_until = st.get("cooldown_until")
        if cd_until is not None and isinstance(cd_until, datetime) and now < cd_until:
            remaining = int((cd_until - now).total_seconds())
            return ProbeOutcome(
                bool(st.get("effective", default_success)), f"{probe_type} backoff ({remaining}s)"
            )

        if probe.initial_delay_seconds > 0 and pod.started_at is not None:
            started_at = pod.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            elapsed = (now - started_at).total_seconds()
            if elapsed < probe.initial_delay_seconds:
                remaining = int(probe.initial_delay_seconds - elapsed)
                return ProbeOutcome(False, f"waiting initial delay ({remaining}s)")

        # Period limiting: avoid probing more often than periodSeconds; reuse last decision
        try:
            raw_period = int(probe.period_seconds)
        except Exception:
            raw_period = 10
        period = raw_period if raw_period > 0 else 0
        last_ts = st.get("ts")
        if last_ts is not None and period > 0:
            try:
                # last_ts may be naive; treat as UTC
                if getattr(last_ts, "tzinfo", None) is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
            except Exception:
                pass
            age = (now - last_ts).total_seconds()
            if age < period:
                # Return cached effective result
                return ProbeOutcome(
                    bool(st.get("effective", default_success)),
                    f"{probe_type} cached ({int(period - age)}s)",
                )

        # Enforce minimal periodSeconds between probe attempts
        now = datetime.now(timezone.utc)
        if st.get("ts") and probe.period_seconds > 0:
            last_ts = st["ts"]
            if isinstance(last_ts, datetime):
                if (now - last_ts).total_seconds() < max(0, int(probe.period_seconds)):
                    # Reuse last effective decision
                    return ProbeOutcome(
                        st.get("effective", default_success), f"{probe_type} cached"
                    )

        if probe.http_get:
            outcome = self._evaluate_http_probe(
                pod,
                probe.http_get.path,
                probe,
                probe_type,
                namespace=namespace,
            )
        elif getattr(probe, "tcp_socket", None) is not None:
            outcome = self._evaluate_tcp_probe(
                pod,
                probe.tcp_socket.port,
                probe,
                probe_type,
                namespace=namespace,
            )  # type: ignore[union-attr]
        elif getattr(probe, "exec", None) is not None:
            outcome = self._evaluate_exec_probe(pod, probe.exec.command, probe, probe_type)  # type: ignore[union-attr]
        else:
            outcome = ProbeOutcome(
                success=default_success,
                message=f"{probe_type} no-op {'ok' if default_success else 'pending'}",
            )

        prev_effective = st.get("effective", default_success)
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
            if st["effective"] != prev_effective:
                self._emit_probe_event(pod.pod_name, probe_type, st["effective"], msg)
            return ProbeOutcome(st["effective"], msg)

        # Failure requires failureThreshold consecutive fails; otherwise retain previous effective
        need_fail = max(1, int(probe.failure_threshold))
        if st["fail"] >= need_fail:
            st["effective"] = False
            msg = outcome.message
            # Compute exponential backoff with small stable jitter
            try:
                max_backoff = int(os.getenv("AE_PROBE_MAX_BACKOFF", "30"))
            except Exception:
                max_backoff = 30
            base = max(1, period)
            # Exponent grows with extra failures beyond the threshold
            exponent = max(0, int(st["fail"]) - need_fail)
            backoff = min(max_backoff, int(base * (2**exponent)))
            # Stable jitter in [-10%, +10%] derived from key hash
            h = abs(hash(key)) % 1000
            jitter_pct = ((h / 999.0) - 0.5) * 0.2  # [-0.1 .. +0.1]
            jittered = max(1, int(backoff * (1.0 + jitter_pct)))
            st["cooldown_until"] = now + timedelta(seconds=jittered)
        else:
            # Keep previous effective decision but annotate. Optionally include detail.
            msg = f"{probe_type} transient fail ({st['fail']}/{need_fail})"
            verbose = os.getenv("AE_PROBE_VERBOSE", "").strip().lower() in {"1", "true", "yes"}
            if verbose or probe_type == "startup":
                msg = f"{msg}: {outcome.message}"
        self._state[key] = st
        if st.get("effective", False) != prev_effective:
            self._emit_probe_event(
                pod.pod_name, probe_type, bool(st.get("effective", False)), msg
            )
        return ProbeOutcome(bool(st["effective"]), msg)

    def _emit_probe_event(
        self, pod_name: str, probe_type: str, success: bool, message: str
    ) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(pod_name, probe_type, success, message)
        except Exception:
            pass

    def _split_endpoint(self, endpoint: str) -> tuple[str, Optional[str]]:
        text = endpoint or ""
        if text.startswith("["):
            end = text.find("]")
            if end != -1:
                host = text[1:end]
                rest = text[end + 1 :]
                if rest.startswith(":"):
                    return host, rest[1:]
                return host, None
        if ":" in text:
            host, port = text.rsplit(":", 1)
            return host, port
        return text, None

    def _format_endpoint(self, host: str, port: Optional[str]) -> str:
        if not host:
            return host if host else ""
        needs_brackets = ":" in host and not host.startswith("[") and not host.endswith("]")
        normalized = f"[{host}]" if needs_brackets else host
        if port:
            return f"{normalized}:{port}"
        return normalized

    def _rewrite_probe_host(self, host: str) -> str:
        if not self._loopback_fallback:
            return host
        norm = host.strip("[] ").lower()
        if not norm:
            return host
        loopback_hosts = {"", "localhost", "0.0.0.0", "::", "::1"}
        if norm in loopback_hosts or norm.startswith("127."):
            return self._loopback_fallback
        return host

    def _preserve_endpoint_port(self, host: str) -> bool:
        norm = host.strip("[] ").lower()
        if not norm:
            return False
        if norm.startswith("127."):
            return True
        return norm in {
            "localhost",
            "0.0.0.0",
            "::",
            "::1",
            "host.docker.internal",
            "host.containers.internal",
        }

    def _select_direct_probe_port(
        self, host: str, endpoint_port: str | None, probe_port: int | None
    ) -> int | None:
        if endpoint_port:
            if self._preserve_endpoint_port(host):
                try:
                    return int(endpoint_port)
                except Exception:
                    return probe_port
        if probe_port is not None:
            return probe_port
        if endpoint_port:
            try:
                return int(endpoint_port)
            except Exception:
                return None
        return None

    def _close_socket(self, sock) -> None:  # type: ignore[no-untyped-def]
        try:
            sock.close()
        except Exception:
            pass

    def _recv_until(self, sock, marker: bytes, *, limit: int = 65536) -> bytes:  # type: ignore[no-untyped-def]
        buf = b""
        while marker not in buf and len(buf) < limit:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    def _probe_via_portforward(self, pod_name: str, namespace: str | None, port: int):  # type: ignore[no-untyped-def]
        if not callable(getattr(self, "_portforward_cb", None)):
            raise RuntimeError("remote probe unsupported")
        return self._portforward_cb(pod_name, namespace, int(port))

    def _evaluate_http_probe_via_portforward(
        self,
        pod: PodState,
        path: str,
        *,
        port: int,
        timeout: int,
        probe_type: str,
        namespace: str | None,
    ) -> ProbeOutcome:
        request_path = path if path.startswith("/") else f"/{path}"
        try:
            sock = self._probe_via_portforward(pod.pod_name, namespace, port)
        except Exception as exc:
            return ProbeOutcome(
                False,
                f"{probe_type} remote http error: {exc} (pod={pod.pod_name} port={port} path={request_path})",
            )
        try:
            try:
                sock.settimeout(timeout)
            except Exception:
                pass
            request = (
                f"GET {request_path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{int(port)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii", "strict")
            sock.sendall(request)
            header = self._recv_until(sock, b"\r\n\r\n")
            if b"\r\n" not in header:
                return ProbeOutcome(
                    False,
                    f"{probe_type} remote http invalid response (pod={pod.pod_name} port={port} path={request_path})",
                )
            status_line = header.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
            parts = status_line.split()
            status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if 200 <= status_code < 300:
                return ProbeOutcome(
                    True,
                    f"{probe_type} remote http {status_code} (pod={pod.pod_name} port={port} path={request_path})",
                )
            return ProbeOutcome(
                False,
                f"{probe_type} remote http {status_code} (pod={pod.pod_name} port={port} path={request_path})",
            )
        except Exception as exc:
            return ProbeOutcome(
                False,
                f"{probe_type} remote http error: {exc} (pod={pod.pod_name} port={port} path={request_path})",
            )
        finally:
            self._close_socket(sock)

    def _evaluate_tcp_probe_via_portforward(
        self,
        pod: PodState,
        *,
        port: int,
        timeout: int,
        probe_type: str,
        namespace: str | None,
    ) -> ProbeOutcome:
        try:
            sock = self._probe_via_portforward(pod.pod_name, namespace, port)
        except Exception as exc:
            return ProbeOutcome(
                False,
                f"{probe_type} remote tcp error: {exc} (pod={pod.pod_name} port={port})",
            )
        try:
            try:
                sock.settimeout(timeout)
            except Exception:
                pass
            return ProbeOutcome(
                True,
                f"{probe_type} remote tcp ok (pod={pod.pod_name} port={port})",
            )
        finally:
            self._close_socket(sock)

    def _evaluate_http_probe(
        self,
        pod: PodState,
        path: str,
        probe: ProbeSpec,
        probe_type: str,
        *,
        namespace: str | None,
    ) -> ProbeOutcome:
        timeout = max(probe.timeout_seconds, 1)
        request_path = path if path.startswith("/") else f"/{path}"
        probe_port = None
        try:
            probe_port = int(probe.http_get.port) if probe.http_get else None
        except Exception:
            probe_port = None

        direct_error = None
        if pod.endpoint:
            host, endpoint_port = self._split_endpoint(str(pod.endpoint))
            host = self._rewrite_probe_host(host)
            target_port = self._select_direct_probe_port(host, endpoint_port, probe_port)
            if host and target_port is not None:
                endpoint = self._format_endpoint(host, str(target_port))
                url = f"http://{endpoint}{request_path}"
                try:
                    trust_env = os.getenv("AE_PROBE_TRUST_ENV", "").strip().lower() in {
                        "1",
                        "true",
                        "yes",
                    }
                    import requests as _requests

                    # If tests monkeypatch the module-level get, honor it regardless of trust_env.
                    if trust_env or get is not _requests.get:
                        response = get(url, timeout=timeout)
                    else:
                        sess = _requests.Session()
                        sess.trust_env = False
                        try:
                            response = sess.get(url, timeout=timeout)
                        finally:
                            sess.close()
                except RequestException as exc:  # pragma: no cover - network path depends on runtime
                    direct_error = f"{probe_type} http error: {exc} (url={url})"
                else:
                    if 200 <= response.status_code < 300:
                        return ProbeOutcome(
                            True,
                            f"{probe_type} http {response.status_code} (url={url})",
                        )
                    return ProbeOutcome(
                        False,
                        f"{probe_type} http {response.status_code} (url={url})",
                    )
            else:
                direct_error = f"{probe_type} endpoint missing"
        else:
            direct_error = f"{probe_type} endpoint missing"

        if probe_port is None:
            return ProbeOutcome(False, direct_error or f"{probe_type} endpoint missing")

        remote = self._evaluate_http_probe_via_portforward(
            pod,
            request_path,
            port=probe_port,
            timeout=timeout,
            probe_type=probe_type,
            namespace=namespace,
        )
        if remote.success or not direct_error:
            return remote
        return ProbeOutcome(False, f"{direct_error}; {remote.message}")

    def _evaluate_tcp_probe(
        self,
        pod: PodState,
        port: int,
        probe: ProbeSpec,
        probe_type: str,
        *,
        namespace: str | None,
    ) -> ProbeOutcome:
        import socket as _sock

        timeout = max(probe.timeout_seconds, 1)
        direct_error = None
        if pod.endpoint:
            host, endpoint_port = self._split_endpoint(str(pod.endpoint))
            host = self._rewrite_probe_host(host)
            target_port = self._select_direct_probe_port(host, endpoint_port, int(port))
            if target_port is None:
                direct_error = f"{probe_type} endpoint missing"
            else:
                try:
                    with _sock.create_connection((host, target_port), timeout=timeout):
                        return ProbeOutcome(True, f"{probe_type} tcp ok ({host}:{target_port})")
                except OSError as exc:
                    direct_error = f"{probe_type} tcp error: {exc} ({host}:{target_port})"
        else:
            direct_error = f"{probe_type} endpoint missing"

        remote = self._evaluate_tcp_probe_via_portforward(
            pod,
            port=int(port),
            timeout=timeout,
            probe_type=probe_type,
            namespace=namespace,
        )
        if remote.success or not direct_error:
            return remote
        return ProbeOutcome(False, f"{direct_error}; {remote.message}")

    def _evaluate_exec_probe(
        self,
        pod: PodState,
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
            code = self._exec_cb(pod.pod_name, list(command), timeout)  # type: ignore[misc]
        except Exception as exc:  # pragma: no cover
            return ProbeOutcome(False, f"{probe_type} exec error: {exc} (cmd={command})")
        return ProbeOutcome(code == 0, f"{probe_type} exec rc={code} (cmd={command})")


# ruff: noqa: E501,I001,S110,S112,SIM105,SIM102,SIM210,UP017,UP007,S104
