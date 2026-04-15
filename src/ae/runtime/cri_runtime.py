"""CRI-backed runtime adapter for managing application pods."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import grpc
    _grpc_import_error: Exception | None = None
except Exception as exc:  # pragma: no cover - optional dependency
    grpc = None
    _grpc_import_error = exc

from ae._utc import UTC
from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    app_key_for_manifest,
    runtime_labels_for_manifest,
    split_app_key,
)
from ae.runtime.ports import choose_host_port

from .base import PodState, RuntimeAdapter, RuntimeResult, WorkloadMetricSample
from .registry import RegistryAuthProvider

LOGGER = logging.getLogger(__name__)


class _StalePodSandboxError(RuntimeError):
    """Raised when CRI reports a dead or missing pod sandbox task."""

    def __init__(self, pod_id: str, message: str) -> None:
        super().__init__(message)
        self.pod_id = str(pod_id)


class CRIRuntime(RuntimeAdapter):
    """CRI gRPC-backed runtime adapter (containerd/kubelet)."""

    APP_LABEL = "ae.app"
    POD_LABEL = "ae.pod_name"
    LEGACY_REPLICA_LABEL = "ae.replica_id"
    REPLICA_LABEL = POD_LABEL
    REVISION_LABEL = "ae.revision"
    CONTAINER_LABEL = "ae.container"
    JOB_ATTEMPT_LABEL = "ae.job_attempt"

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        registry_auth: RegistryAuthProvider | None = None,
        sandbox_image: str | None = None,
        node_id: str | None = None,
    ) -> None:
        self._endpoint = endpoint or os.getenv(
            "AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock"
        )
        self._sandbox_image = sandbox_image or os.getenv(
            "AE_CRI_SANDBOX_IMAGE", "registry.k8s.io/pause:3.9"
        )
        self._registry = registry_auth or RegistryAuthProvider()
        self._current_node_id = node_id
        self._channel: Any | None = None
        self._runtime: Any | None = None
        self._images: Any | None = None
        self._port_assignments: dict[str, dict[int, int]] = {}
        self._exec_procs: dict[str, subprocess.Popen[bytes]] = {}
        self._exec_exit_codes: dict[str, int] = {}
        self._exec_lock = threading.Lock()
        self._volume_manager_checked = False
        self._volume_manager = None
        self._apishim_store_checked = False
        self._apishim_store = None
        self._apishim_state = None
        self._cpu_sample_cache: dict[str, tuple[int, int]] = {}

    # --- RuntimeAdapter API -----------------------------------------
    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
        app_name = app_key_for_manifest(manifest)
        desired_replica_ids = (
            list(pod_names)
            if pod_names is not None
            else self._desired_replica_ids(manifest, revision)
        )
        self._current_node_id = node_id
        self._ensure_clients()

        existing = self._list_pods(app_name)
        by_replica: dict[str, Any] = {}
        old: list[Any] = []
        for pod in existing:
            labels = self._pod_labels(pod)
            rid = labels.get(self.REPLICA_LABEL)
            if not rid:
                continue
            if labels.get(self.REVISION_LABEL) == str(revision):
                by_replica[rid] = pod
            else:
                old.append(pod)

        created = updated = removed = 0
        if any(rid not in by_replica for rid in desired_replica_ids):
            self._ensure_image(manifest.spec.image)

        is_job = str(getattr(manifest.spec, "workload", "service")).lower() == "job"
        job_backoff_limit = self._job_backoff_limit(manifest) if is_job else None

        for rid in desired_replica_ids:
            try:
                rep_manifest = self._maybe_inject_pvc_mounts(
                    manifest, node_id=node_id, replica_id=rid
                )
            except Exception as exc:  # noqa: BLE001
                try:
                    from ae.storage.netfs import PvcNotReadyError
                except Exception:
                    PvcNotReadyError = None  # type: ignore[assignment]
                if PvcNotReadyError is not None and isinstance(exc, PvcNotReadyError):
                    LOGGER.info("Skipping %s: PVCs not ready for mount injection", rid)
                    continue
                raise
            pod = by_replica.get(rid)
            if pod is None:
                if limit_create is not None and created >= int(limit_create):
                    continue
                self._run_pod(rep_manifest, rid, revision, node_id=node_id, attempt=0)
                created += 1
                continue
            if self._ensure_main_container(
                rep_manifest,
                pod,
                revision,
                is_job=is_job,
                job_backoff_limit=job_backoff_limit,
            ):
                updated += 1

        if not keep_old:
            for pod in old:
                try:
                    self._stop_and_remove_pod(manifest, pod)
                    removed += 1
                except Exception as exc:
                    LOGGER.warning("Failed to remove old pod: %s", exc)

        pod_states = self._build_states(manifest, revision)
        return RuntimeResult(
            revision=revision,
            created=created,
            updated=updated,
            removed=removed,
            pod_states=pod_states,
        )

    def read_logs(
        self,
        pod_name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        self._ensure_clients()
        container_id = self._container_id_for_replica(pod_name)
        if not container_id:
            return iter(())
        status = self._container_status(container_id)
        log_path = getattr(status, "log_path", None) if status else None
        if not log_path:
            return iter(())
        return self._iter_log_file(Path(str(log_path)), follow=follow, tail=tail, since=since)

    def remove_app(self, app_name: str) -> int:
        self._ensure_clients()
        removed = 0
        for pod in self._list_pods(app_name):
            with contextlib.suppress(Exception):
                self._stop_and_remove_pod(None, pod)
                removed += 1
        return removed

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        self._ensure_clients()
        removed = 0
        for pod in self._list_pods(app_name):
            labels = self._pod_labels(pod)
            if labels.get(self.REVISION_LABEL) == str(keep_revision):
                continue
            with contextlib.suppress(Exception):
                self._stop_and_remove_pod(None, pod)
                removed += 1
        return removed

    def list_containers_info(self) -> list[dict]:
        self._ensure_clients()
        out: list[dict] = []
        pods = self._list_pods()
        host_ip = os.getenv("AE_NODE_ADVERTISE_IP") or "127.0.0.1"
        for pod in pods:
            with contextlib.suppress(Exception):
                labels = self._pod_labels(pod)
                replica_id = labels.get(self.REPLICA_LABEL) or self._pod_name(pod)
                pod_id = getattr(pod, "id", None) or getattr(pod, "pod_sandbox_id", None)
                status = self._pod_status(pod_id) if pod_id else None
                pod_ip = None
                if status and getattr(status, "network", None):
                    pod_ip = getattr(status.network, "ip", None)
                container = self._find_container(pod_id, container_label="main") if pod_id else None
                c_status = self._container_status(container.id) if container else None
                running = False
                started_at = None
                if c_status:
                    running = self._is_container_running(c_status)
                    started_at = self._timestamp_iso(getattr(c_status, "started_at", None))
                port_map = self._port_assignments.get(str(replica_id), {})
                out.append(
                    {
                        "name": replica_id or "",
                        "labels": labels,
                        "uid": str(pod_id) if pod_id else None,
                        "host_ports": list(port_map.values()),
                        "port_map": port_map,
                        "host_ip": host_ip,
                        "restart_count": 0,
                        "started_at": started_at,
                        "running": bool(running),
                        "pod_ip": pod_ip,
                    }
                )
        return out

    def list_workload_metrics(self) -> list[WorkloadMetricSample]:
        self._ensure_clients()
        pb2 = self._pb2()
        req = pb2.ListPodSandboxStatsRequest()
        resp = self._runtime_call("ListPodSandboxStats", req)
        items = getattr(resp, "stats", None)
        if items is None:
            items = getattr(resp, "items", None)
        node_id = str(
            self._current_node_id or os.getenv("AE_NODE_ID") or socket.gethostname() or "unknown-node"
        )
        collected_at = datetime.now(UTC)
        aggregates: dict[str, dict[str, object]] = {}
        for item in list(items or []):
            labels = self._stats_labels(item)
            app_name = str(labels.get(self.APP_LABEL) or "").strip()
            if not app_name:
                continue
            aggregate = aggregates.setdefault(
                app_name,
                {
                    "cpu_cores": 0.0,
                    "cpu_seen": False,
                    "memory_bytes": 0,
                    "pod_count": 0,
                },
            )
            cpu_cores = self._stats_cpu_cores(item)
            if cpu_cores is not None:
                aggregate["cpu_cores"] = float(aggregate["cpu_cores"]) + float(cpu_cores)
                aggregate["cpu_seen"] = True
            aggregate["memory_bytes"] = int(aggregate["memory_bytes"]) + self._stats_memory_bytes(item)
            aggregate["pod_count"] = int(aggregate["pod_count"]) + 1
        out: list[WorkloadMetricSample] = []
        for app_name, aggregate in sorted(aggregates.items()):
            cpu_cores = (
                float(aggregate["cpu_cores"]) if bool(aggregate.get("cpu_seen")) else None
            )
            out.append(
                WorkloadMetricSample(
                    app_name=app_name,
                    node_id=node_id,
                    collected_at=collected_at,
                    cpu_cores=cpu_cores,
                    memory_bytes=int(aggregate["memory_bytes"]),
                    pod_count=int(aggregate["pod_count"]),
                )
            )
        return out

    def exec(self, pod_name: str, command: list[str], *, timeout: int | None = None) -> int:
        self._ensure_clients()
        container_id = self._container_id_for_replica(pod_name)
        if not container_id:
            return 127
        pb2 = self._pb2()
        timeout_seconds = int(timeout) if timeout else 0
        req = pb2.ExecSyncRequest(
            container_id=str(container_id), cmd=command, timeout=timeout_seconds
        )
        try:
            resp = self._runtime_call("ExecSync", req)
        except Exception as exc:
            LOGGER.warning("CRI exec failed: %s", exc)
            return 1
        return int(getattr(resp, "exit_code", 1))

    def exec_attach(
        self,
        pod_name: str,
        command: list[str],
        *,
        container: str | None = None,
        tty: bool = False,
    ):
        self._ensure_clients()
        if not command:
            raise RuntimeError("exec command is required")
        container_label = str(container or "main")
        container_id = self._container_id_for_replica(pod_name, container_label=container_label)
        if not container_id:
            raise RuntimeError("Replica not found for exec")
        crictl = os.getenv("CRICTL_BIN", "crictl")
        if shutil.which(crictl) is None:
            raise RuntimeError("crictl not found on PATH")
        endpoint = self._endpoint
        args = [crictl, "--runtime-endpoint", endpoint, "exec", "-i"]
        if tty:
            args.append("-t")
        args.append(str(container_id))
        args.extend([str(x) for x in command])
        if tty:
            # crictl requires a real TTY when -t is set; pipes fail with
            # "input is not a terminal". Use a PTY-backed attach path.
            import pty

            try:
                master_fd, slave_fd = pty.openpty()
            except Exception as exc:
                raise RuntimeError(f"failed to allocate pty for exec: {exc}") from exc
            try:
                proc = subprocess.Popen(  # noqa: S603 - crictl command with fixed args
                    args,  # noqa: S603
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                )
            except Exception:
                with contextlib.suppress(Exception):
                    os.close(master_fd)
                with contextlib.suppress(Exception):
                    os.close(slave_fd)
                raise
            with contextlib.suppress(Exception):
                os.close(slave_fd)

            exec_id = uuid.uuid4().hex
            with self._exec_lock:
                self._exec_procs[exec_id] = proc

            class _FDAsSocket:
                def __init__(self, fd: int) -> None:
                    self._fd = fd
                    with contextlib.suppress(Exception):
                        os.set_blocking(fd, False)

                def recv(self, n: int) -> bytes:
                    try:
                        return os.read(self._fd, n)
                    except BlockingIOError as err:
                        raise TimeoutError from err
                    except OSError:
                        return b""

                def sendall(self, data: bytes) -> None:
                    if not data:
                        return
                    view = memoryview(data)
                    while view:
                        try:
                            sent = os.write(self._fd, view)
                        except BlockingIOError as err:
                            raise TimeoutError from err
                        except OSError:
                            return
                        if sent <= 0:
                            return
                        view = view[sent:]

                def settimeout(self, _t: float) -> None:
                    return

                def shutdown(self, _how: int) -> None:
                    return

                def close(self) -> None:
                    with contextlib.suppress(Exception):
                        os.close(self._fd)

            def _watch_pty() -> None:
                code = 0
                try:
                    code = int(proc.wait(timeout=None))
                except Exception:
                    code = 0
                with self._exec_lock:
                    self._exec_exit_codes[exec_id] = code
                    self._exec_procs.pop(exec_id, None)
                with contextlib.suppress(Exception):
                    os.close(master_fd)

            threading.Thread(target=_watch_pty, daemon=True).start()
            return _FDAsSocket(master_fd), exec_id

        stderr = subprocess.STDOUT if tty else subprocess.PIPE
        proc = subprocess.Popen(  # noqa: S603 - crictl command with fixed args
            args,  # noqa: S603
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        parent_sock, child_sock = socket.socketpair()
        exec_id = uuid.uuid4().hex
        with self._exec_lock:
            self._exec_procs[exec_id] = proc

        def _mux(stream_type: int, payload: bytes) -> bytes:
            header = bytearray(8)
            header[0] = stream_type
            header[4:8] = len(payload).to_bytes(4, "big")
            return bytes(header) + payload

        def _pump_stdin() -> None:
            try:
                while True:
                    data = child_sock.recv(4096)
                    if not data:
                        break
                    if proc.stdin:
                        try:
                            proc.stdin.write(data)
                            proc.stdin.flush()
                        except Exception:
                            break
            finally:
                with contextlib.suppress(Exception):
                    if proc.stdin:
                        proc.stdin.close()

        def _pump_stream(stream, stream_type: int | None) -> None:
            if not stream:
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    try:
                        if stream_type is None:
                            child_sock.sendall(chunk)
                        else:
                            child_sock.sendall(_mux(stream_type, chunk))
                    except Exception:
                        break
            finally:
                with contextlib.suppress(Exception):
                    stream.close()

        def _watch() -> None:
            code = 0
            try:
                code = int(proc.wait(timeout=None))
            except Exception:
                code = 0
            with self._exec_lock:
                self._exec_exit_codes[exec_id] = code
                self._exec_procs.pop(exec_id, None)
            with contextlib.suppress(Exception):
                child_sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                child_sock.close()

        threading.Thread(target=_pump_stdin, daemon=True).start()
        threading.Thread(
            target=_pump_stream, args=(proc.stdout, None if tty else 1), daemon=True
        ).start()
        if not tty:
            threading.Thread(target=_pump_stream, args=(proc.stderr, 2), daemon=True).start()
        threading.Thread(target=_watch, daemon=True).start()
        return parent_sock, exec_id

    def exec_resize(
        self, exec_id: str, *, height: int | None = None, width: int | None = None
    ) -> None:
        _ = (exec_id, height, width)
        return

    def exec_exit_code(self, exec_id: str) -> int:
        with self._exec_lock:
            if exec_id in self._exec_exit_codes:
                return int(self._exec_exit_codes.get(exec_id, 0))
            proc = self._exec_procs.get(exec_id)
        if proc is None:
            return 0
        try:
            rc = proc.poll()
            return int(rc) if rc is not None else 0
        except Exception:
            return 0

    # Init containers --------------------------------------------------
    def run_init_containers(self, manifest: AppManifest):  # type: ignore[override]
        """Run initContainers sequentially with optional timeouts.

        Returns list of (name, rc, message).
        """
        results: list[tuple[str, int, str]] = []
        inits = list(getattr(manifest.spec, "init_containers", []) or [])
        if not inits:
            return results
        self._ensure_clients()

        with contextlib.suppress(Exception):
            if getattr(manifest.spec, "storage", None):
                self.ensure_storage_volumes(
                    app_key_for_manifest(manifest), [s.model_dump() for s in manifest.spec.storage]
                )

        app_name = app_key_for_manifest(manifest)
        ns, _ = split_app_key(app_name)
        pb2 = self._pb2()

        for spec in inits:
            name = self._spec_value(spec, "name") or "init"
            image = self._spec_value(spec, "image")
            if not image:
                results.append((str(name), 1, "missing image"))
                continue
            timeout = self._parse_timeout(
                self._spec_value(spec, "timeout_seconds", "timeoutSeconds")
            )
            with contextlib.suppress(Exception):
                self._ensure_image(str(image))

            pod_name = f"{app_name}-init-{name}-{uuid.uuid4().hex[:8]}"
            pod_uid = self._pod_uid(pod_name, ns)
            pod_meta = pb2.PodSandboxMetadata(
                name=str(pod_name),
                namespace=ns or DEFAULT_NAMESPACE,
                uid=str(pod_uid),
                attempt=0,
            )
            pod_config = pb2.PodSandboxConfig(
                metadata=pod_meta,
                labels=runtime_labels_for_manifest(manifest, app_name=app_name),
                log_directory=self._pod_log_dir(ns, pod_name, pod_uid),
            )
            try:
                resp = self._runtime_call(
                    "RunPodSandbox", pb2.RunPodSandboxRequest(config=pod_config)
                )
                pod_id = getattr(resp, "pod_sandbox_id", None)
                if not pod_id:
                    raise RuntimeError("RunPodSandbox returned no pod_sandbox_id")
            except Exception as exc:
                results.append((str(name), 1, f"sandbox error: {exc}"))
                continue

            container_id = None
            try:
                config = self._container_config_for_spec(
                    manifest,
                    spec,
                    name=str(name),
                    replica_id=str(pod_name),
                    revision=0,
                    attempt=0,
                    is_main=False,
                )
                req = pb2.CreateContainerRequest(
                    pod_sandbox_id=str(pod_id),
                    config=config,
                    sandbox_config=pod_config,
                )
                resp = self._runtime_call("CreateContainer", req)
                container_id = getattr(resp, "container_id", None)
                if not container_id:
                    raise RuntimeError("CreateContainer returned no container_id")
                self._runtime_call(
                    "StartContainer", pb2.StartContainerRequest(container_id=container_id)
                )
                exit_code = self._wait_container_exit(container_id, timeout)
                if exit_code is None:
                    with contextlib.suppress(Exception):
                        self._runtime_call(
                            "StopContainer",
                            pb2.StopContainerRequest(container_id=container_id, timeout=0),
                        )
                    results.append((str(name), 124, "timeout"))
                else:
                    msg = "ok" if exit_code == 0 else "failed"
                    results.append((str(name), int(exit_code), msg))
            except Exception as exc:
                results.append((str(name), 1, f"error: {exc}"))
            finally:
                with contextlib.suppress(Exception):
                    if container_id:
                        self._runtime_call(
                            "RemoveContainer", pb2.RemoveContainerRequest(container_id=container_id)
                        )
                with contextlib.suppress(Exception):
                    self._runtime_call(
                        "StopPodSandbox", pb2.StopPodSandboxRequest(pod_sandbox_id=str(pod_id))
                    )
                with contextlib.suppress(Exception):
                    self._runtime_call(
                        "RemovePodSandbox",
                        pb2.RemovePodSandboxRequest(pod_sandbox_id=str(pod_id)),
                    )
        return results

    # Storage lifecycle ------------------------------------------------
    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:
        root = Path(os.getenv("AE_CRI_VOLUME_ROOT", "/var/lib/ae/volumes"))
        for v in volumes or []:
            name = (v or {}).get("name")
            if not name:
                continue
            path = root / app_name / str(name)
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                LOGGER.warning("Failed to ensure storage volume %s: %s", path, exc)

    def remove_storage_volumes(self, app_name: str, names: list[str]) -> int:
        root = Path(os.getenv("AE_CRI_VOLUME_ROOT", "/var/lib/ae/volumes"))
        removed = 0
        for n in names or []:
            path = root / app_name / str(n)
            with contextlib.suppress(Exception):
                if not path.exists():
                    continue
                for child in path.rglob("*"):
                    with contextlib.suppress(Exception):
                        if child.is_file() or child.is_symlink():
                            child.unlink()
                        elif child.is_dir():
                            child.rmdir()
                removed_flag = False
                with contextlib.suppress(Exception):
                    path.rmdir()
                    removed_flag = True
                if removed_flag:
                    removed += 1
        return removed

    def list_storage_volumes(self, app_name: str | None = None) -> list[dict]:
        root = Path(os.getenv("AE_CRI_VOLUME_ROOT", "/var/lib/ae/volumes"))
        out: list[dict] = []
        if app_name:
            base = root / app_name
            if not base.exists():
                return out
            try:
                for entry in base.iterdir():
                    if not entry.is_dir():
                        continue
                    labels = {"ae.app": app_name, "ae.volume": entry.name}
                    out.append({"name": entry.name, "labels": labels, "path": str(entry)})
            except Exception:
                return out
            return out
        if not root.exists():
            return out
        try:
            for app_dir in root.iterdir():
                if not app_dir.is_dir():
                    continue
                for entry in app_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    labels = {"ae.app": app_dir.name, "ae.volume": entry.name}
                    out.append({"name": entry.name, "labels": labels, "path": str(entry)})
        except Exception:
            return out
        return out

    # Internal helpers -------------------------------------------------
    def _ensure_clients(self) -> None:
        if self._runtime and self._images:
            return
        if grpc is None:  # pragma: no cover - optional dependency
            detail = ""
            if _grpc_import_error is not None:
                detail = f": {type(_grpc_import_error).__name__}: {_grpc_import_error}"
            raise RuntimeError(f"grpc is required for CRI runtime (install grpcio){detail}")
        try:
            from ae.runtime.cri.api.runtime.v1 import api_pb2_grpc
        except Exception as exc:  # pragma: no cover - depends on generated stubs
            raise RuntimeError(
                "CRI stubs not available. Run scripts/cri_codegen.sh to generate them."
            ) from exc
        target = self._normalize_endpoint(self._endpoint)
        self._channel = grpc.insecure_channel(target)
        self._runtime = api_pb2_grpc.RuntimeServiceStub(self._channel)
        self._images = api_pb2_grpc.ImageServiceStub(self._channel)

    def _reset_clients(self) -> None:
        ch = self._channel
        self._channel = None
        self._runtime = None
        self._images = None
        if ch is not None:
            with contextlib.suppress(Exception):
                ch.close()

    def _runtime_call(self, method: str, req: Any):
        self._ensure_clients()
        fn = getattr(self._runtime, method)
        try:
            return fn(req)
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.UNAVAILABLE:
                raise
            LOGGER.warning("CRI call unavailable; resetting client and retrying once: %s", exc)
            self._reset_clients()
            self._ensure_clients()
            fn = getattr(self._runtime, method)
            return fn(req)

    def _images_call(self, method: str, req: Any):
        self._ensure_clients()
        fn = getattr(self._images, method)
        try:
            return fn(req)
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.UNAVAILABLE:
                raise
            LOGGER.warning("CRI call unavailable; resetting client and retrying once: %s", exc)
            self._reset_clients()
            self._ensure_clients()
            fn = getattr(self._images, method)
            return fn(req)

    def _pb2(self):
        try:
            from ae.runtime.cri.api.runtime.v1 import api_pb2

            return api_pb2
        except Exception as exc:  # pragma: no cover - depends on generated stubs
            raise RuntimeError(
                "CRI stubs not available. Run scripts/cri_codegen.sh to generate them."
            ) from exc

    def _normalize_endpoint(self, endpoint: str) -> str:
        raw = str(endpoint)
        if raw.startswith("unix://"):
            path = raw[len("unix://") :]
            if not path.startswith("/"):
                path = f"/{path}"
            return f"unix://{path}"
        if raw.startswith("tcp://"):
            return raw[len("tcp://") :]
        return raw

    def _desired_replica_ids(self, manifest: AppManifest, revision: int) -> list[str]:
        app_name = app_key_for_manifest(manifest)
        return [f"{app_name}-rev{revision}-{replica}" for replica in range(manifest.spec.replicas)]

    def _job_backoff_limit(self, manifest: AppManifest) -> int | None:
        try:
            raw_limit = getattr(manifest.spec, "job_backoff_limit", None)
            return int(raw_limit) if raw_limit is not None else 6
        except Exception:
            return 6

    def _list_pods(self, app_name: str | None = None) -> list[Any]:
        pb2 = self._pb2()
        selector = {self.APP_LABEL: app_name} if app_name else {}
        flt = pb2.PodSandboxFilter(label_selector=selector) if selector else pb2.PodSandboxFilter()
        req = pb2.ListPodSandboxRequest(filter=flt)
        resp = self._runtime_call("ListPodSandbox", req)
        items = getattr(resp, "items", None)
        if items is None:
            items = getattr(resp, "pod_sandboxes", None)
        return list(items or [])

    def _pod_name(self, pod: Any) -> str:
        meta = getattr(pod, "metadata", None)
        if meta and getattr(meta, "name", None):
            return str(meta.name)
        return str(getattr(pod, "id", ""))

    def _pod_labels(self, pod: Any) -> dict[str, str]:
        labels = getattr(pod, "labels", None)
        if labels:
            out = {str(k): str(v) for k, v in labels.items()}
            if self.POD_LABEL not in out and self.LEGACY_REPLICA_LABEL in out:
                out[self.POD_LABEL] = out[self.LEGACY_REPLICA_LABEL]
            return out
        meta = getattr(pod, "metadata", None)
        meta_labels = getattr(meta, "labels", None) if meta else None
        if meta_labels:
            out = {str(k): str(v) for k, v in meta_labels.items()}
            if self.POD_LABEL not in out and self.LEGACY_REPLICA_LABEL in out:
                out[self.POD_LABEL] = out[self.LEGACY_REPLICA_LABEL]
            return out
        return {}

    def _pod_status(self, pod_id: str | None):
        if not pod_id:
            return None
        pb2 = self._pb2()
        req = pb2.PodSandboxStatusRequest(pod_sandbox_id=str(pod_id), verbose=False)
        resp = self._runtime_call("PodSandboxStatus", req)
        return getattr(resp, "status", None)

    def _container_status(self, container_id: str | None):
        if not container_id:
            return None
        pb2 = self._pb2()
        req = pb2.ContainerStatusRequest(container_id=str(container_id), verbose=False)
        resp = self._runtime_call("ContainerStatus", req)
        return getattr(resp, "status", None)

    def _find_container(self, pod_id: str | None, *, container_label: str | None = None):
        if not pod_id:
            return None
        pb2 = self._pb2()
        selector = {}
        if container_label:
            selector[self.CONTAINER_LABEL] = container_label
        flt = pb2.ContainerFilter(pod_sandbox_id=str(pod_id), label_selector=selector)
        req = pb2.ListContainersRequest(filter=flt)
        resp = self._runtime_call("ListContainers", req)
        items = getattr(resp, "containers", None)
        if items is None:
            items = getattr(resp, "items", None)
        containers = list(items or [])
        return containers[0] if containers else None

    def _container_id_for_replica(
        self, replica_id: str, *, container_label: str = "main"
    ) -> str | None:
        pods = self._list_pods()
        for pod in pods:
            labels = self._pod_labels(pod)
            if labels.get(self.REPLICA_LABEL) != replica_id:
                continue
            pod_id = getattr(pod, "id", None) or getattr(pod, "pod_sandbox_id", None)
            container = self._find_container(pod_id, container_label=container_label)
            if container and getattr(container, "id", None):
                return str(container.id)
        return None

    def _pod_id_for_replica(self, replica_id: str) -> str | None:
        for pod in self._list_pods():
            labels = self._pod_labels(pod)
            if labels.get(self.REPLICA_LABEL) != replica_id:
                continue
            pod_id = getattr(pod, "id", None) or getattr(pod, "pod_sandbox_id", None)
            if pod_id:
                return str(pod_id)
        return None

    def _stats_labels(self, stats: Any) -> dict[str, str]:
        candidates = [
            getattr(getattr(stats, "attributes", None), "labels", None),
            getattr(stats, "labels", None),
        ]
        for labels in candidates:
            if labels:
                return {str(key): str(value) for key, value in labels.items()}
        return {}

    def _stats_cpu_cores(self, stats: Any) -> float | None:
        cpu = getattr(stats, "cpu", None)
        usage = getattr(cpu, "usage", None) if cpu is not None else None
        instant = getattr(usage, "usage_nano_cores", None) if usage is not None else None
        try:
            if instant is not None:
                return max(0.0, float(instant) / 1_000_000_000.0)
        except Exception:
            pass
        total = getattr(usage, "usage_core_nano_seconds", None) if usage is not None else None
        timestamp = getattr(cpu, "timestamp", None)
        if timestamp is None:
            timestamp = getattr(stats, "timestamp", None)
        sample_id = str(
            getattr(getattr(stats, "attributes", None), "id", None)
            or getattr(stats, "id", None)
            or getattr(stats, "pod_sandbox_id", None)
            or ""
        ).strip()
        try:
            if not sample_id or total is None or timestamp is None:
                return None
            total_i = int(total)
            ts_i = int(timestamp)
        except Exception:
            return None
        previous = self._cpu_sample_cache.get(sample_id)
        self._cpu_sample_cache[sample_id] = (ts_i, total_i)
        if previous is None:
            return None
        prev_ts, prev_total = previous
        delta_ts = ts_i - prev_ts
        delta_total = total_i - prev_total
        if delta_ts <= 0 or delta_total < 0:
            return None
        return max(0.0, float(delta_total) / float(delta_ts))

    def _stats_memory_bytes(self, stats: Any) -> int:
        memory = getattr(stats, "memory", None)
        for field in ("working_set_bytes", "usage_bytes"):
            value = getattr(memory, field, None) if memory is not None else None
            try:
                if value is not None:
                    return max(0, int(value))
            except Exception:
                continue
        return 0

    def _ensure_image(self, image_ref: str) -> None:
        pb2 = self._pb2()
        spec = pb2.ImageSpec(image=str(image_ref))
        with contextlib.suppress(Exception):
            status = self._images_call(
                "ImageStatus", pb2.ImageStatusRequest(image=spec, verbose=False)
            )
            if getattr(status, "image", None):
                return
        auth = self._image_pull_auth(image_ref)
        req = pb2.PullImageRequest(image=spec)
        if auth is not None:
            req.auth = auth
        try:
            self._images_call("PullImage", req)
        except Exception as exc:
            raise RuntimeError(f"Failed to pull image {image_ref}: {exc}") from exc

    def _image_pull_auth(self, image_ref: str):
        pb2 = self._pb2()
        creds = self._registry.list_registries()
        registry = self._extract_registry(image_ref) or "docker.io"
        candidates = [registry]
        if registry == "docker.io":
            candidates.extend(
                [
                    "index.docker.io",
                    "registry-1.docker.io",
                    "https://index.docker.io/v1/",
                ]
            )
        for host in candidates:
            entry = creds.get(host)
            if entry and entry.get("username") and entry.get("password"):
                return pb2.AuthConfig(
                    username=str(entry.get("username")),
                    password=str(entry.get("password")),
                    server_address=str(host),
                )
        return None

    def _get_apishim_store(self):
        if self._apishim_store_checked:
            return self._apishim_store
        self._apishim_store_checked = True
        try:
            from ae.apishim.store import ObjectStore
        except Exception:
            self._apishim_store = None
            return None
        dsn = os.getenv("AE_APISHIM_DSN")
        db_env = os.getenv("AE_APISHIM_DB")
        db_path = Path(db_env or "state/apishim.db")
        if not dsn and not db_path.exists():
            self._apishim_store = None
            return None
        try:
            self._apishim_store = ObjectStore(dsn=dsn) if dsn else ObjectStore(db_path=db_path)
        except Exception:
            self._apishim_store = None
        return self._apishim_store

    @staticmethod
    def _ha_mode_enabled() -> bool:
        return str(os.getenv("AE_HA_MODE", "0")).strip().lower() in {"1", "true", "yes", "on"}

    def _get_apishim_state(self):
        if self._apishim_state is not None:
            return self._apishim_state
        try:
            from ae.storage.state import ApishimHttpStorageState
        except Exception:
            ApishimHttpStorageState = None  # type: ignore[assignment]
        if self._ha_mode_enabled():
            if ApishimHttpStorageState is None:
                return None
            self._apishim_state = ApishimHttpStorageState.from_env()
            return self._apishim_state
        store = self._get_apishim_store()
        if store is not None:
            try:
                from ae.storage.state import ApishimStorageState
            except Exception:
                return None
            self._apishim_state = ApishimStorageState(store)
            return self._apishim_state
        if ApishimHttpStorageState is None:
            return None
        self._apishim_state = ApishimHttpStorageState.from_env()
        return self._apishim_state

    def _get_volume_manager(self):
        if self._volume_manager_checked:
            return self._volume_manager
        self._volume_manager_checked = True
        if os.getenv("AE_ENABLE_NETFS", "0") != "1":
            self._volume_manager = None
            return None
        state = self._get_apishim_state()
        if state is None:
            self._volume_manager = None
            return None
        try:
            from ae.storage import NetFSManager, NodeVolumeManager
        except Exception:
            self._volume_manager = None
            return None
        try:
            netfs = NetFSManager(state)
            self._volume_manager = NodeVolumeManager(netfs, node_id=self._current_node_id)
        except Exception:
            self._volume_manager = None
        return self._volume_manager

    def _maybe_inject_pvc_mounts(
        self,
        manifest: AppManifest,
        *,
        node_id: str | None = None,
        replica_id: str | None = None,
    ) -> AppManifest:
        mgr = self._get_volume_manager()
        if mgr is None:
            return manifest
        try:
            from ae.storage.netfs import PvcNotReadyError
        except Exception:
            PvcNotReadyError = None  # type: ignore[assignment]
        try:
            if replica_id is not None:
                return mgr.inject_pvc_mounts(
                    manifest,
                    node_id=node_id or self._current_node_id,
                    replica_id=replica_id,
                )
            return mgr.inject_pvc_mounts(
                manifest,
                node_id=node_id or self._current_node_id,
            )
        except Exception as exc:  # noqa: BLE001
            if PvcNotReadyError is not None and isinstance(exc, PvcNotReadyError):
                raise
            if isinstance(exc, TypeError):
                # Backward-compatible with older/partial managers that lack replica_id.
                try:
                    return mgr.inject_pvc_mounts(
                        manifest,
                        node_id=node_id or self._current_node_id,
                    )
                except Exception:
                    return manifest
            return manifest

    def _pull_secret_auths(self, manifest: AppManifest) -> dict[str, dict[str, str]]:
        state = self._get_apishim_state()
        if state is None:
            return {}
        secrets = self._image_pull_secret_names(manifest)
        if not secrets:
            secrets = self._service_account_pull_secrets(manifest)
        if not secrets:
            return {}
        namespace = getattr(getattr(manifest, "metadata", None), "namespace", None) or DEFAULT_NAMESPACE
        auths: dict[str, dict[str, str]] = {}
        for name, ns in secrets:
            if not name:
                continue
            data = state.get_secret(str(ns or namespace), str(name))
            if not data:
                continue
            raw = data.get(".dockerconfigjson") or data.get(".dockercfg")
            if raw:
                auths.update(self._parse_dockerconfigjson(str(raw)))
                continue
            # Fallback: allow simple secrets with username/password/registry keys
            host = data.get("registry") or data.get("host") or data.get("server")
            user = data.get("username")
            pw = data.get("password")
            if host and user and pw:
                auths[str(host)] = {"username": str(user), "password": str(pw)}
        return auths

    def _parse_dockerconfigjson(self, raw: str) -> dict[str, dict[str, str]]:
        try:
            payload = json.loads(raw)
        except Exception:
            return {}
        auths = payload.get("auths") if isinstance(payload, dict) else None
        if isinstance(auths, dict):
            entries = auths
        elif isinstance(payload, dict):
            entries = payload
        else:
            return {}
        out: dict[str, dict[str, str]] = {}
        for host, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            username = entry.get("username")
            password = entry.get("password")
            if (not username or not password) and entry.get("auth"):
                try:
                    decoded = base64.b64decode(str(entry["auth"]).encode("ascii")).decode("utf-8")
                    if ":" in decoded:
                        username, password = decoded.split(":", 1)
                except Exception:
                    username = None
                    password = None
            if username and password:
                out[str(host)] = {"username": str(username), "password": str(password)}
        return out

    def _image_pull_secret_names(self, manifest: AppManifest) -> list[tuple[str | None, str | None]]:
        secrets: list[tuple[str | None, str | None]] = []
        for sec in getattr(manifest.spec, "image_pull_secrets", []) or []:
            if isinstance(sec, dict):
                secrets.append((sec.get("name"), sec.get("namespace")))
            else:
                secrets.append((str(sec), None))
        return secrets

    def _service_account_pull_secrets(
        self, manifest: AppManifest
    ) -> list[tuple[str | None, str | None]]:
        state = self._get_apishim_state()
        if state is None:
            return []
        namespace = getattr(getattr(manifest, "metadata", None), "namespace", None) or DEFAULT_NAMESPACE
        sa_name = getattr(manifest.spec, "service_account_name", None)
        if not sa_name and not self._ha_mode_enabled():
            store = self._get_apishim_store()
            if store is not None:
                sa_name = self._service_account_name_from_store(manifest, store)
        sa_name = sa_name or "default"
        try:
            sa = state.get_service_account(namespace, str(sa_name))
        except Exception:
            sa = None
        if sa is None:
            return []
        if not isinstance(sa, dict):
            return []
        secrets = sa.get("imagePullSecrets") or []
        out: list[tuple[str | None, str | None]] = []
        for entry in secrets:
            if isinstance(entry, dict):
                out.append((entry.get("name"), None))
            else:
                out.append((str(entry), None))
        return out

    def _service_account_name_from_store(self, manifest: AppManifest, store: Any) -> str | None:
        name = getattr(getattr(manifest, "metadata", None), "name", None)
        namespace = getattr(getattr(manifest, "metadata", None), "namespace", None) or DEFAULT_NAMESPACE
        if not name:
            return None
        candidates = [
            ("apps", "v1", "deployments"),
            ("apps", "v1", "daemonsets"),
            ("apps", "v1", "statefulsets"),
            ("batch", "v1", "jobs"),
            ("batch", "v1", "cronjobs"),
        ]
        for group, version, resource in candidates:
            try:
                obj = store.get(group, version, resource, namespace, str(name))
            except Exception:
                obj = None
            if obj is None:
                continue
            spec = getattr(obj, "spec", None) or {}
            if not isinstance(spec, dict):
                continue
            template = None
            if resource == "cronjobs":
                template = (
                    (spec.get("jobTemplate") or {}).get("spec") or {}
                ).get("template")
            else:
                template = spec.get("template")
            if not isinstance(template, dict):
                continue
            tpl_spec = template.get("spec") or {}
            if not isinstance(tpl_spec, dict):
                continue
            sa = tpl_spec.get("serviceAccountName") or tpl_spec.get("serviceAccount")
            if sa:
                return str(sa)
        return None

    def _extract_registry(self, image: str) -> str | None:
        if "/" not in image:
            return None
        host = image.split("/", 1)[0]
        if "." not in host and ":" not in host:
            return None
        return host

    def _run_pod(
        self,
        manifest: AppManifest,
        replica_id: str,
        revision: int,
        *,
        node_id: str | None = None,
        attempt: int = 0,
        sandbox_recovery_attempt: int = 0,
    ) -> None:
        pb2 = self._pb2()
        app_name = app_key_for_manifest(manifest)
        labels = runtime_labels_for_manifest(manifest, app_name=app_name)
        labels.update(
            {
                self.POD_LABEL: replica_id,
                self.LEGACY_REPLICA_LABEL: replica_id,
                self.REVISION_LABEL: str(revision),
                **({"ae.node": str(node_id)} if node_id else {}),
            }
        )
        ns, _ = split_app_key(app_name)
        pod_uid = self._pod_uid(replica_id, ns)
        pod_meta = pb2.PodSandboxMetadata(
            name=replica_id,
            namespace=ns or DEFAULT_NAMESPACE,
            uid=str(pod_uid),
            attempt=int(attempt),
        )
        port_mappings, port_map = self._port_mappings(manifest)
        pod_config = pb2.PodSandboxConfig(
            metadata=pod_meta,
            labels=labels,
            log_directory=self._pod_log_dir(ns, replica_id, pod_uid),
            port_mappings=port_mappings,
        )
        runtime_handler = getattr(manifest.spec, "runtime_class_name", None)
        req = pb2.RunPodSandboxRequest(config=pod_config)
        if runtime_handler:
            req.runtime_handler = str(runtime_handler)
        try:
            resp = self._runtime_call("RunPodSandbox", req)
        except Exception as exc:
            if self._is_reserved_pod_sandbox_name_error(exc):
                self._recover_from_reserved_pod_sandbox_name(
                    manifest,
                    replica_id=replica_id,
                    revision=revision,
                    attempt=attempt,
                    sandbox_recovery_attempt=sandbox_recovery_attempt,
                    cause=exc,
                    node_id=node_id,
                )
                return
            raise
        pod_id = getattr(resp, "pod_sandbox_id", None)
        if not pod_id:
            raise RuntimeError("CRI RunPodSandbox returned no pod_sandbox_id")
        self._port_assignments[str(replica_id)] = port_map
        try:
            self._create_main_container(manifest, pod_id, replica_id, revision, attempt=attempt)
        except _StalePodSandboxError as exc:
            self._recover_from_stale_pod_sandbox(
                manifest,
                stale_pod_id=exc.pod_id,
                replica_id=replica_id,
                revision=revision,
                attempt=attempt,
                sandbox_recovery_attempt=sandbox_recovery_attempt,
                cause=exc,
                node_id=node_id,
            )
            return
        try:
            self._ensure_sidecars(manifest, pod_id, replica_id, revision)
        except Exception as exc:
            LOGGER.warning("Failed to ensure sidecars for %s: %s", replica_id, exc)

    def _ensure_main_container(
        self,
        manifest: AppManifest,
        pod: Any,
        revision: int,
        *,
        is_job: bool,
        job_backoff_limit: int | None,
    ) -> bool:
        pod_id = getattr(pod, "id", None) or getattr(pod, "pod_sandbox_id", None)
        if not pod_id:
            return False
        container = self._find_container(pod_id, container_label="main")
        replica_id = self._pod_labels(pod).get(self.REPLICA_LABEL, "")
        if not container:
            try:
                self._create_main_container(
                    manifest,
                    pod_id,
                    replica_id,
                    revision,
                    attempt=0,
                )
            except _StalePodSandboxError as exc:
                self._recover_from_stale_pod_sandbox(
                    manifest,
                    stale_pod_id=exc.pod_id,
                    replica_id=replica_id,
                    revision=revision,
                    attempt=0,
                    sandbox_recovery_attempt=0,
                    cause=exc,
                    node_id=self._current_node_id,
                )
                return True
            try:
                if replica_id:
                    self._ensure_sidecars(manifest, pod_id, replica_id, revision)
            except Exception as exc:
                LOGGER.warning("Failed to ensure sidecars for %s: %s", replica_id, exc)
            return True
        status = self._container_status(container.id)
        if status is None:
            return False
        if self._is_container_running(status):
            try:
                if replica_id:
                    self._ensure_sidecars(manifest, pod_id, replica_id, revision)
            except Exception as exc:
                LOGGER.warning("Failed to ensure sidecars for %s: %s", replica_id, exc)
            return False
        exit_code = getattr(status, "exit_code", None)
        labels = getattr(status, "labels", None) or {}
        attempt = 0
        try:
            attempt = int(labels.get(self.JOB_ATTEMPT_LABEL, 0))
        except Exception:
            attempt = 0
        if is_job:
            if exit_code == 0:
                return False
            if job_backoff_limit is not None and attempt >= job_backoff_limit:
                return False
        # Containers are single-use in CRI; recreate instead of restart.
        self._runtime_call(
            "RemoveContainer", self._pb2().RemoveContainerRequest(container_id=container.id)
        )
        try:
            self._create_main_container(
                manifest,
                pod_id,
                replica_id,
                revision,
                attempt=attempt + 1,
            )
        except _StalePodSandboxError as exc:
            self._recover_from_stale_pod_sandbox(
                manifest,
                stale_pod_id=exc.pod_id,
                replica_id=replica_id,
                revision=revision,
                attempt=attempt + 1,
                sandbox_recovery_attempt=0,
                cause=exc,
                node_id=self._current_node_id,
            )
            return True
        try:
            if replica_id:
                self._ensure_sidecars(manifest, pod_id, replica_id, revision)
        except Exception as exc:
            LOGGER.warning("Failed to ensure sidecars for %s: %s", replica_id, exc)
        return True

    def _grpc_error_details(self, exc: Exception) -> str:
        details = ""
        with contextlib.suppress(Exception):
            details = str(exc.details() or "")
        return details or str(exc)

    def _is_stale_pod_sandbox_error(self, exc: Exception) -> bool:
        if grpc is None or not isinstance(exc, grpc.RpcError):
            return False
        try:
            if exc.code() != grpc.StatusCode.NOT_FOUND:
                return False
        except Exception:
            return False
        details = self._grpc_error_details(exc).lower()
        if "sandbox" not in details:
            return False
        stale_markers = (
            "sandbox container task",
            "no running task found",
            "podsandbox not found",
            "pod sandbox not found",
            "sandbox not found",
        )
        return any(marker in details for marker in stale_markers)

    def _is_reserved_pod_sandbox_name_error(self, exc: Exception) -> bool:
        if grpc is None or not isinstance(exc, grpc.RpcError):
            return False
        try:
            code = exc.code()
        except Exception:
            return False
        if code not in {
            grpc.StatusCode.UNKNOWN,
            grpc.StatusCode.ALREADY_EXISTS,
            grpc.StatusCode.FAILED_PRECONDITION,
        }:
            return False
        details = self._grpc_error_details(exc).lower()
        return "failed to reserve sandbox name" in details and "reserved for" in details

    def _reserved_pod_sandbox_id_from_error(self, exc: Exception) -> str | None:
        details = self._grpc_error_details(exc)
        match = re.search(r'reserved for "([^"]+)"', details)
        if match:
            return str(match.group(1))
        return None

    def _wait_for_pod_sandbox_absent(self, pod_id: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            present = False
            with contextlib.suppress(Exception):
                for pod in self._list_pods():
                    current_id = getattr(pod, "id", None) or getattr(pod, "pod_sandbox_id", None)
                    if str(current_id or "") == str(pod_id):
                        present = True
                        break
            if not present:
                return
            time.sleep(0.2)

    def _remove_pod_sandbox(self, pod_id: str) -> None:
        pb2 = self._pb2()
        containers = []
        with contextlib.suppress(Exception):
            flt = pb2.ContainerFilter(pod_sandbox_id=str(pod_id))
            resp = self._runtime_call("ListContainers", pb2.ListContainersRequest(filter=flt))
            items = getattr(resp, "containers", None)
            if items is None:
                items = getattr(resp, "items", None)
            containers = list(items or [])
        for container in containers:
            container_id = getattr(container, "id", None)
            if not container_id:
                continue
            with contextlib.suppress(Exception):
                self._runtime_call(
                    "StopContainer", pb2.StopContainerRequest(container_id=container_id, timeout=0)
                )
            with contextlib.suppress(Exception):
                self._runtime_call(
                    "RemoveContainer", pb2.RemoveContainerRequest(container_id=container_id)
                )
        with contextlib.suppress(Exception):
            self._runtime_call(
                "StopPodSandbox", pb2.StopPodSandboxRequest(pod_sandbox_id=str(pod_id))
            )
        with contextlib.suppress(Exception):
            self._runtime_call(
                "RemovePodSandbox", pb2.RemovePodSandboxRequest(pod_sandbox_id=str(pod_id))
            )
        self._wait_for_pod_sandbox_absent(str(pod_id))

    def _recover_from_stale_pod_sandbox(
        self,
        manifest: AppManifest,
        *,
        stale_pod_id: str,
        replica_id: str,
        revision: int,
        attempt: int,
        sandbox_recovery_attempt: int,
        cause: _StalePodSandboxError,
        node_id: str | None,
    ) -> None:
        if sandbox_recovery_attempt >= 1:
            raise cause
        LOGGER.warning(
            "CRI sandbox stale for replica %s (pod_id=%s); recreating pod: %s",
            replica_id,
            stale_pod_id,
            cause,
        )
        self._remove_pod_sandbox(stale_pod_id)
        self._run_pod(
            manifest,
            replica_id,
            revision,
            node_id=node_id,
            attempt=attempt,
            sandbox_recovery_attempt=sandbox_recovery_attempt + 1,
        )

    def _recover_from_reserved_pod_sandbox_name(
        self,
        manifest: AppManifest,
        *,
        replica_id: str,
        revision: int,
        attempt: int,
        sandbox_recovery_attempt: int,
        cause: Exception,
        node_id: str | None,
    ) -> None:
        if sandbox_recovery_attempt >= 1:
            raise cause
        reserved_pod_id = self._reserved_pod_sandbox_id_from_error(cause)
        if not reserved_pod_id:
            reserved_pod_id = self._pod_id_for_replica(replica_id)
        LOGGER.warning(
            "CRI sandbox name still reserved for replica %s (pod_id=%s); retrying pod create: %s",
            replica_id,
            reserved_pod_id or "?",
            cause,
        )
        if reserved_pod_id:
            self._remove_pod_sandbox(reserved_pod_id)
        else:
            time.sleep(1.0)
        self._run_pod(
            manifest,
            replica_id,
            revision,
            node_id=node_id,
            attempt=attempt,
            sandbox_recovery_attempt=sandbox_recovery_attempt + 1,
        )

    def _ensure_sidecars(
        self,
        manifest: AppManifest,
        pod_id: str,
        replica_id: str,
        revision: int,
    ) -> None:
        sidecars = list(getattr(manifest.spec, "containers", []) or [])
        if not sidecars:
            return
        pb2 = self._pb2()
        existing: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            flt = pb2.ContainerFilter(pod_sandbox_id=str(pod_id))
            resp = self._runtime_call("ListContainers", pb2.ListContainersRequest(filter=flt))
            containers = list(getattr(resp, "containers", None) or [])
            for c in containers:
                labels = getattr(c, "labels", None) or {}
                cname = labels.get(self.CONTAINER_LABEL)
                if cname and cname != "main":
                    existing[str(cname)] = c

        for spec in sidecars:
            cname = self._spec_value(spec, "name")
            if not cname:
                continue
            container = existing.get(str(cname))
            if container:
                status = self._container_status(container.id)
                if status is not None and self._is_container_running(status):
                    continue
                with contextlib.suppress(Exception):
                    self._runtime_call(
                        "StopContainer",
                        pb2.StopContainerRequest(container_id=container.id, timeout=0),
                    )
                with contextlib.suppress(Exception):
                    self._runtime_call(
                        "RemoveContainer", pb2.RemoveContainerRequest(container_id=container.id)
                    )
            image = self._spec_value(spec, "image")
            if not image:
                continue
            with contextlib.suppress(Exception):
                self._ensure_image(str(image))
            config = self._container_config_for_spec(
                manifest,
                spec,
                name=str(cname),
                replica_id=replica_id,
                revision=revision,
                attempt=0,
                is_main=False,
            )
            pod_meta = None
            with contextlib.suppress(Exception):
                pod_status = self._pod_status(str(pod_id))
                pod_meta = getattr(pod_status, "metadata", None)
            if not pod_meta or not getattr(pod_meta, "uid", None):
                app_name = app_key_for_manifest(manifest)
                ns, _ = split_app_key(app_name)
                pod_uid = self._pod_uid(replica_id, ns)
                pod_meta = pb2.PodSandboxMetadata(
                    name=replica_id,
                    namespace=ns or DEFAULT_NAMESPACE,
                    uid=str(pod_uid),
                    attempt=0,
                )
            pod_config = pb2.PodSandboxConfig(metadata=pod_meta)
            req = pb2.CreateContainerRequest(
                pod_sandbox_id=str(pod_id),
                config=config,
                sandbox_config=pod_config,
            )
            resp = self._runtime_call("CreateContainer", req)
            container_id = getattr(resp, "container_id", None)
            if not container_id:
                continue
            self._runtime_call(
                "StartContainer", pb2.StartContainerRequest(container_id=container_id)
            )

    def _create_main_container(
        self,
        manifest: AppManifest,
        pod_id: str,
        replica_id: str,
        revision: int,
        *,
        attempt: int = 0,
    ) -> None:
        pb2 = self._pb2()
        config = self._container_config(manifest, replica_id, revision, attempt=attempt)
        pod_meta = None
        try:
            pod_status = self._pod_status(str(pod_id))
            pod_meta = getattr(pod_status, "metadata", None)
        except Exception:
            pod_meta = None
        if not pod_meta or not getattr(pod_meta, "uid", None):
            app_name = app_key_for_manifest(manifest)
            ns, _ = split_app_key(app_name)
            pod_uid = self._pod_uid(replica_id, ns)
            pod_meta = pb2.PodSandboxMetadata(
                name=replica_id,
                namespace=ns or DEFAULT_NAMESPACE,
                uid=str(pod_uid),
                attempt=0,
            )
        # containerd expects sandbox_config.metadata to be present.
        pod_config = pb2.PodSandboxConfig(metadata=pod_meta)
        req = pb2.CreateContainerRequest(
            pod_sandbox_id=str(pod_id),
            config=config,
            sandbox_config=pod_config,
        )
        try:
            resp = self._runtime_call("CreateContainer", req)
        except Exception as exc:
            if self._is_stale_pod_sandbox_error(exc):
                detail = self._grpc_error_details(exc)
                raise _StalePodSandboxError(str(pod_id), detail) from exc
            raise
        container_id = getattr(resp, "container_id", None)
        if not container_id:
            raise RuntimeError("CRI CreateContainer returned no container_id")
        try:
            self._runtime_call("StartContainer", pb2.StartContainerRequest(container_id=container_id))
        except Exception as exc:
            if self._is_stale_pod_sandbox_error(exc):
                detail = self._grpc_error_details(exc)
                raise _StalePodSandboxError(str(pod_id), detail) from exc
            raise

    def _stop_and_remove_pod(self, manifest: AppManifest | None, pod: Any) -> None:
        pb2 = self._pb2()
        pod_id = getattr(pod, "id", None) or getattr(pod, "pod_sandbox_id", None)
        if not pod_id:
            return
        containers = []
        try:
            flt = pb2.ContainerFilter(pod_sandbox_id=str(pod_id))
            resp = self._runtime_call("ListContainers", pb2.ListContainersRequest(filter=flt))
            containers = list(getattr(resp, "containers", None) or [])
        except Exception:
            containers = []
        timeout = 10
        if manifest is not None:
            try:
                timeout = int(getattr(manifest.spec, "termination_grace_period_seconds", 10) or 10)
            except Exception:
                timeout = 10
        for c in containers:
            with contextlib.suppress(Exception):
                self._runtime_call(
                    "StopContainer", pb2.StopContainerRequest(container_id=c.id, timeout=timeout)
                )
            with contextlib.suppress(Exception):
                self._runtime_call("RemoveContainer", pb2.RemoveContainerRequest(container_id=c.id))
        with contextlib.suppress(Exception):
            self._runtime_call(
                "StopPodSandbox", pb2.StopPodSandboxRequest(pod_sandbox_id=str(pod_id))
            )
        with contextlib.suppress(Exception):
            self._runtime_call(
                "RemovePodSandbox", pb2.RemovePodSandboxRequest(pod_sandbox_id=str(pod_id))
            )

    def _container_config(
        self,
        manifest: AppManifest,
        replica_id: str,
        revision: int,
        *,
        attempt: int = 0,
    ):
        return self._container_config_for_spec(
            manifest,
            manifest.spec,
            name="main",
            replica_id=replica_id,
            revision=revision,
            attempt=attempt,
            is_main=True,
        )

    def _container_config_for_spec(
        self,
        manifest: AppManifest,
        spec: Any,
        *,
        name: str,
        replica_id: str,
        revision: int,
        attempt: int = 0,
        is_main: bool = False,
    ):
        pb2 = self._pb2()
        app_name = app_key_for_manifest(manifest)
        labels = runtime_labels_for_manifest(manifest, app_name=app_name)
        labels.update(
            {
                self.POD_LABEL: replica_id,
                self.LEGACY_REPLICA_LABEL: replica_id,
                self.REVISION_LABEL: str(revision),
                self.CONTAINER_LABEL: str(name),
            }
        )
        if is_main and str(getattr(manifest.spec, "workload", "service")).lower() == "job":
            labels[self.JOB_ATTEMPT_LABEL] = str(int(attempt))

        image = self._spec_value(spec, "image") or manifest.spec.image
        command = list(self._spec_value(spec, "command") or [])
        args = list(self._spec_value(spec, "args") or [])
        env_items = self._spec_value(spec, "env") or []
        envs = [
            pb2.KeyValue(key=str(item.get("name")), value=str(item.get("value", "")))
            for item in env_items
            if isinstance(item, dict) and item.get("name")
        ]
        working_dir = self._spec_value(spec, "working_dir", "workingDir")
        mounts = self._build_mounts_for_container(manifest, app_name, spec)
        resources = self._build_resources_from_spec(
            self._spec_value(spec, "resources") if not is_main else manifest.spec.resources
        )
        sec_ctx = self._build_security_context_from_spec(
            self._spec_value(spec, "security") if not is_main else manifest.spec.security
        )
        kwargs: dict[str, Any] = {
            "metadata": pb2.ContainerMetadata(name=str(name)),
            "image": pb2.ImageSpec(image=str(image)),
            "command": command,
            "args": args,
            "envs": envs,
            "working_dir": str(working_dir or ""),
            "labels": labels,
            "mounts": mounts,
        }
        if sec_ctx is not None or resources is not None:
            linux_cfg = pb2.LinuxContainerConfig()
            if sec_ctx is not None:
                try:
                    linux_cfg.security_context.CopyFrom(sec_ctx)
                except Exception:
                    linux_cfg.security_context = sec_ctx
            if resources is not None:
                try:
                    linux_cfg.resources.CopyFrom(resources)
                except Exception:
                    linux_cfg.resources = resources
            kwargs["linux"] = linux_cfg
        return pb2.ContainerConfig(**kwargs)

    def _build_mounts_for_container(
        self, manifest: AppManifest, app_name: str, spec: Any
    ) -> list[Any]:
        pb2 = self._pb2()
        mounts: list[Any] = []
        for v in manifest.spec.volumes or []:
            with contextlib.suppress(Exception):
                host_path = getattr(v, "host_path", None)
                if host_path and not os.path.isabs(host_path):
                    host_path = os.path.abspath(host_path)
                mounts.append(
                    pb2.Mount(
                        host_path=str(host_path),
                        container_path=str(getattr(v, "mount_path", "")),
                        readonly=bool(getattr(v, "read_only", False)),
                    )
                )
        if getattr(manifest.spec, "storage", None):
            self.ensure_storage_volumes(app_name, [s.model_dump() for s in manifest.spec.storage])
            for s in manifest.spec.storage:
                host_path = self._storage_path(app_name, s.name)
                mounts.append(
                    pb2.Mount(
                        host_path=str(host_path),
                        container_path=str(s.mount_path),
                        readonly=False,
                    )
                )
        projection_root = self._projection_host_root(manifest, app_name)
        pmounts = self._spec_value(spec, "projection_mounts", "projectionMounts") or []
        if projection_root and pmounts:
            for pm in pmounts:
                with contextlib.suppress(Exception):
                    rel = self._spec_value(pm, "path")
                    mnt = self._spec_value(pm, "mount_path", "mountPath")
                    ro = self._spec_value(pm, "read_only", "readOnly")
                    if not rel or not mnt:
                        continue
                    host_path = os.path.join(str(projection_root), str(rel).lstrip("/"))
                    mounts.append(
                        pb2.Mount(
                            host_path=str(host_path),
                            container_path=str(mnt),
                            readonly=bool(ro if ro is not None else True),
                        )
                    )
        return mounts

    def _build_resources_from_spec(self, spec: Any):
        pb2 = self._pb2()
        resources = self._spec_value(spec, "limits") if spec else None
        requests = self._spec_value(spec, "requests") if spec else None
        cpu_shares = None
        cpu_quota = None
        cpu_period = None
        mem_limit = None
        if resources and self._spec_value(resources, "cpu") is not None:
            try:
                cpu = float(self._spec_value(resources, "cpu"))
                cpu_period = 100000
                cpu_quota = int(cpu * cpu_period)
            except Exception:
                cpu_quota = None
        if resources and self._spec_value(resources, "memory") is not None:
            mem_limit = self._parse_memory_bytes(str(self._spec_value(resources, "memory")))
        if requests and self._spec_value(requests, "cpu") is not None:
            try:
                cpu_shares = max(2, int(float(self._spec_value(requests, "cpu")) * 1024))
            except Exception:
                cpu_shares = None
        if not any(x is not None for x in (cpu_shares, cpu_quota, mem_limit)):
            return None
        return pb2.LinuxContainerResources(
            cpu_shares=cpu_shares or 0,
            cpu_quota=cpu_quota or 0,
            cpu_period=cpu_period or 0,
            memory_limit_in_bytes=mem_limit or 0,
        )

    def _build_security_context_from_spec(self, spec: Any):
        pb2 = self._pb2()
        sec = spec
        if sec is None:
            return None
        ctx = pb2.LinuxContainerSecurityContext()
        run_as_user = self._spec_value(sec, "run_as_user", "runAsUser")
        if run_as_user is not None:
            try:
                ctx.run_as_user.value = int(run_as_user)
            except Exception:
                with contextlib.suppress(Exception):
                    from google.protobuf.wrappers_pb2 import Int64Value

                    ctx.run_as_user.CopyFrom(Int64Value(value=int(run_as_user)))
        run_as_group = self._spec_value(sec, "run_as_group", "runAsGroup")
        if run_as_group is not None:
            try:
                ctx.run_as_group.value = int(run_as_group)
            except Exception:
                with contextlib.suppress(Exception):
                    from google.protobuf.wrappers_pb2 import Int64Value

                    ctx.run_as_group.CopyFrom(Int64Value(value=int(run_as_group)))
        if bool(self._spec_value(sec, "read_only_root", "readOnlyRootFilesystem")):
            ctx.readonly_rootfs = True
        drops = list(self._spec_value(sec, "drop_caps", "dropCapabilities") or [])
        if drops:
            ctx.capabilities.drop_capabilities.extend([str(c) for c in drops])
        return ctx

    def _build_states(self, manifest: AppManifest, revision: int) -> list[PodState]:
        states: list[PodState] = []
        app_name = app_key_for_manifest(manifest)
        for pod in self._list_pods(app_name):
            labels = self._pod_labels(pod)
            if labels.get(self.REVISION_LABEL) != str(revision):
                continue
            pod_name = (
                labels.get(self.POD_LABEL)
                or labels.get(self.LEGACY_REPLICA_LABEL)
                or self._pod_name(pod)
            )
            pod_id = getattr(pod, "id", None) or getattr(pod, "pod_sandbox_id", None)
            pod_status = self._pod_status(pod_id) if pod_id else None
            pod_ip = None
            if pod_status and getattr(pod_status, "network", None):
                pod_ip = getattr(pod_status.network, "ip", None)
            container = self._find_container(pod_id, container_label="main") if pod_id else None
            c_status = self._container_status(container.id) if container else None
            status = "unknown"
            exit_code = None
            started_at = None
            finished_at = None
            ready = False
            if c_status:
                status = self._container_state_name(c_status)
                exit_code = getattr(c_status, "exit_code", None)
                started_at = self._timestamp_dt(getattr(c_status, "started_at", None))
                finished_at = self._timestamp_dt(getattr(c_status, "finished_at", None))
                is_job = str(getattr(manifest.spec, "workload", "service")).lower() == "job"
                ready = (
                    exit_code == 0 and status == "exited" if is_job else status == "running"
                )
            endpoint = self._endpoint_for_manifest(manifest, pod_ip)
            states.append(
                PodState(
                    pod_name=str(pod_name),
                    ready=bool(ready),
                    status=status,
                    endpoint=endpoint,
                    started_at=started_at,
                    exit_code=int(exit_code) if exit_code is not None else None,
                    finished_at=finished_at,
                )
            )
        return states

    def _container_state_name(self, status: Any) -> str:
        pb2 = self._pb2()
        state = getattr(status, "state", None)
        if state == pb2.ContainerState.CONTAINER_RUNNING:
            return "running"
        if state == pb2.ContainerState.CONTAINER_EXITED:
            return "exited"
        if state == pb2.ContainerState.CONTAINER_CREATED:
            return "created"
        return "unknown"

    def _is_container_running(self, status: Any) -> bool:
        pb2 = self._pb2()
        return getattr(status, "state", None) == pb2.ContainerState.CONTAINER_RUNNING

    def _endpoint_for_manifest(self, manifest: AppManifest, pod_ip: str | None) -> str | None:
        if not pod_ip:
            return None
        preferred_port = None
        try:
            if manifest.spec.health and manifest.spec.health.readiness:
                r = manifest.spec.health.readiness
                if getattr(r, "http_get", None) is not None:
                    preferred_port = int(r.http_get.port)
                elif getattr(r, "tcp_socket", None) is not None:
                    preferred_port = int(r.tcp_socket.port)
        except Exception:
            preferred_port = None
        if preferred_port is None and manifest.spec.ports:
            try:
                preferred_port = int(manifest.spec.ports[0].container_port)
            except Exception:
                preferred_port = None
        if preferred_port is None:
            return None
        return f"{pod_ip}:{preferred_port}"

    def _port_mappings(self, manifest: AppManifest) -> tuple[list[Any], dict[int, int]]:
        pb2 = self._pb2()
        svc = getattr(manifest.spec, "service", None)
        if not svc or manifest.spec.replicas != 1:
            return [], {}
        reserved: set[int] = set()
        mappings: list[Any] = []
        port_map: dict[int, int] = {}

        ports_by_name = {
            p.name: int(p.container_port)
            for p in (manifest.spec.ports or [])
            if getattr(p, "name", None)
        }
        ports_by_number = {
            int(p.container_port): int(p.container_port) for p in (manifest.spec.ports or [])
        }

        def add_mapping(container_port: int, host_port: int, protocol: str = "TCP") -> None:
            proto_val = self._protocol_enum(pb2, protocol)
            mappings.append(
                pb2.PortMapping(
                    container_port=int(container_port),
                    host_port=int(host_port),
                    protocol=proto_val,
                )
            )
            port_map[int(container_port)] = int(host_port)

        if getattr(svc, "ports", None):
            for sp in svc.ports:
                with contextlib.suppress(Exception):
                    target = getattr(sp, "target_port", None)
                    if target is None:
                        name = getattr(sp, "name", None)
                        portnum = getattr(sp, "port", None)
                        if name and name in ports_by_name:
                            target = ports_by_name[name]
                        elif portnum is not None:
                            target = ports_by_number.get(int(portnum))
                    if target is None or getattr(sp, "port", None) is None:
                        continue
                    host_port, _ = choose_host_port(int(sp.port), reserved=reserved)
                    if host_port is None:
                        continue
                    add_mapping(int(target), int(host_port), str(getattr(sp, "protocol", "TCP")))
        elif getattr(svc, "port", None) is not None:
            target = getattr(svc, "target_port", None)
            if target is None:
                try:
                    target = int(manifest.spec.ports[0].container_port)
                except Exception:
                    target = None
            if target is not None:
                host_port, _ = choose_host_port(int(svc.port), reserved=reserved)
                if host_port is not None:
                    add_mapping(int(target), int(host_port), "TCP")

        return mappings, port_map

    def _pod_uid(self, replica_id: str, namespace: str | None) -> str:
        ns = namespace or DEFAULT_NAMESPACE
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{ns}/{replica_id}"))

    def _protocol_enum(self, pb2: Any, protocol: str):
        try:
            return pb2.Protocol.Value(str(protocol or "TCP").upper())
        except Exception:
            return str(protocol or "TCP")

    def _projection_host_root(self, manifest: AppManifest, app_name: str) -> str | None:
        mount_root = f"/var/run/ae/config/{app_name}"
        for v in manifest.spec.volumes or []:
            with contextlib.suppress(Exception):
                if str(getattr(v, "mount_path", "")).startswith(mount_root):
                    return str(getattr(v, "host_path", ""))
        return None

    def _pod_log_dir(self, namespace: str | None, replica_id: str, uid: str) -> str:
        ns = namespace or DEFAULT_NAMESPACE
        return f"/var/log/pods/{ns}_{replica_id}_{uid}"

    def _storage_path(self, app_name: str, volume_name: str) -> Path:
        root = Path(os.getenv("AE_CRI_VOLUME_ROOT", "/var/lib/ae/volumes"))
        return root / app_name / str(volume_name)

    def _spec_value(self, spec: Any, name: str, alt: str | None = None):
        if isinstance(spec, dict):
            if name in spec:
                return spec.get(name)
            if alt and alt in spec:
                return spec.get(alt)
        return getattr(spec, name, None) if spec is not None else None

    def _parse_timeout(self, raw: Any) -> int | None:
        if raw is None:
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def _timestamp_dt(self, raw: int | None) -> datetime | None:
        if raw is None:
            return None
        try:
            return datetime.fromtimestamp(int(raw) / 1_000_000_000, tz=UTC)
        except Exception:
            return None

    def _timestamp_iso(self, raw: int | None) -> str | None:
        dt = self._timestamp_dt(raw)
        return dt.isoformat() if dt else None

    def _wait_container_exit(self, container_id: str, timeout: int | None) -> int | None:
        start = time.monotonic()
        while True:
            status = self._container_status(container_id)
            if status is not None:
                state = getattr(status, "state", None)
                pb2 = self._pb2()
                if state == pb2.ContainerState.CONTAINER_EXITED:
                    try:
                        return int(getattr(status, "exit_code", 1))
                    except Exception:
                        return 1
            if timeout is not None and timeout > 0 and time.monotonic() - start >= timeout:
                return None
            time.sleep(0.5)

    def _iter_log_file(
        self, path: Path, *, follow: bool = False, tail: int | None = None, since: int | None = None
    ):
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except Exception:
            return iter(())

        def parse_time(line: str) -> float | None:
            try:
                ts = line.split(" ", 1)[0]
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                return datetime.fromisoformat(ts).timestamp()
            except Exception:
                return None

        def reader():
            with fh:
                lines = fh.readlines()[-int(tail) :] if tail is not None else fh.readlines()
                for line in lines:
                    if since is not None:
                        ts = parse_time(line)
                        if ts is not None and ts < since:
                            continue
                    yield line.rstrip("\n")
                if not follow:
                    return
                while True:
                    line = fh.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    if since is not None:
                        ts = parse_time(line)
                        if ts is not None and ts < since:
                            continue
                    yield line.rstrip("\n")

        return reader()

    def _parse_memory_bytes(self, raw: str) -> int | None:
        try:
            s = raw.strip()
            suffixes = {
                "b": 1,
                "k": 1024,
                "kb": 1024,
                "m": 1024**2,
                "mb": 1024**2,
                "mi": 1024**2,
                "g": 1024**3,
                "gb": 1024**3,
                "gi": 1024**3,
            }
            if s.isdigit():
                return int(s)
            num = ""
            unit = ""
            for ch in s:
                if ch.isdigit() or ch == ".":
                    num += ch
                else:
                    unit += ch
            factor = suffixes.get(unit.strip().lower())
            if factor is None:
                return None
            return int(float(num) * factor)
        except Exception:
            return None
