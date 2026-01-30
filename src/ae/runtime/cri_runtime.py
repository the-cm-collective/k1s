"""CRI-backed runtime adapter for managing application pods."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc

from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    app_key_for_manifest,
    runtime_labels_for_manifest,
    split_app_key,
)
from ae.runtime.ports import choose_host_port

from .base import PodState, RuntimeAdapter, RuntimeResult
from .registry import RegistryAuthProvider

LOGGER = logging.getLogger(__name__)


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
            pod = by_replica.get(rid)
            if pod is None:
                if limit_create is not None and created >= int(limit_create):
                    continue
                self._run_pod(manifest, rid, revision, node_id=node_id, attempt=0)
                created += 1
                continue
            if self._ensure_main_container(
                manifest,
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

    # Exec by container name (best-effort)
    def exec_for_container(
        self, app_name: str, container_name: str, command: list[str], *, timeout: int | None = None
    ) -> int:  # type: ignore[override]
        self._ensure_clients()
        pb2 = self._pb2()
        selector = {self.APP_LABEL: app_name, self.CONTAINER_LABEL: container_name}
        flt = pb2.ContainerFilter(label_selector=selector)
        req = pb2.ListContainersRequest(filter=flt)
        resp = self._runtime_call("ListContainers", req)
        items = getattr(resp, "containers", None)
        if items is None:
            items = getattr(resp, "items", None)
        containers = list(items or [])
        if not containers:
            return 127
        container_id = getattr(containers[0], "id", None)
        if not container_id:
            return 127
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

    def list_container_stats(self, label_selector: dict[str, str] | None = None) -> list[Any]:
        self._ensure_clients()
        pb2 = self._pb2()
        selector = label_selector or {}
        if selector:
            flt = pb2.ContainerStatsFilter(label_selector=selector)
        else:
            flt = pb2.ContainerStatsFilter()
        req = pb2.ListContainerStatsRequest(filter=flt)
        resp = self._runtime_call("ListContainerStats", req)
        items = getattr(resp, "stats", None)
        return list(items or [])

    # Internal helpers -------------------------------------------------
    def _ensure_clients(self) -> None:
        if self._runtime and self._images:
            return
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
        resp = self._runtime_call("RunPodSandbox", req)
        pod_id = getattr(resp, "pod_sandbox_id", None)
        if not pod_id:
            raise RuntimeError("CRI RunPodSandbox returned no pod_sandbox_id")
        self._port_assignments[str(replica_id)] = port_map
        self._create_main_container(manifest, pod_id, replica_id, revision, attempt=attempt)
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
            self._create_main_container(
                manifest,
                pod_id,
                replica_id,
                revision,
                attempt=0,
            )
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
        self._create_main_container(
            manifest,
            pod_id,
            replica_id,
            revision,
            attempt=attempt + 1,
        )
        try:
            if replica_id:
                self._ensure_sidecars(manifest, pod_id, replica_id, revision)
        except Exception as exc:
            LOGGER.warning("Failed to ensure sidecars for %s: %s", replica_id, exc)
        return True

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
        resp = self._runtime_call("CreateContainer", req)
        container_id = getattr(resp, "container_id", None)
        if not container_id:
            raise RuntimeError("CRI CreateContainer returned no container_id")
        self._runtime_call("StartContainer", pb2.StartContainerRequest(container_id=container_id))

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
        labels = {}
        with contextlib.suppress(Exception):
            labels = self._pod_labels(pod) or {}
        app_name = labels.get(self.APP_LABEL)
        replica_id = labels.get(self.REPLICA_LABEL) or labels.get(self.LEGACY_REPLICA_LABEL)
        if app_name and replica_id:
            with contextlib.suppress(Exception):
                self._cleanup_empty_dirs(str(app_name), str(replica_id))

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
        mounts = self._build_mounts_for_container(manifest, app_name, spec, replica_id)
        devices = self._build_devices_for_container(manifest)
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
        if devices:
            kwargs["devices"] = devices
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
        self,
        manifest: AppManifest,
        app_name: str,
        spec: Any,
        replica_id: str,
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
        empty_dirs = list(getattr(manifest.spec, "empty_dirs", []) or [])
        if empty_dirs:
            for ed in empty_dirs:
                with contextlib.suppress(Exception):
                    name = getattr(ed, "name", None) if not isinstance(ed, dict) else ed.get("name")
                    mount_path = (
                        getattr(ed, "mount_path", None)
                        if not isinstance(ed, dict)
                        else (ed.get("mountPath") or ed.get("mount_path"))
                    )
                    if not name or not mount_path:
                        continue
                    medium = (
                        getattr(ed, "medium", None)
                        if not isinstance(ed, dict)
                        else ed.get("medium")
                    )
                    root = self._empty_dir_root(str(medium) if medium is not None else None)
                    host_path = root / app_name / str(replica_id) / str(name)
                    host_path.mkdir(parents=True, exist_ok=True)
                    mounts.append(
                        pb2.Mount(
                            host_path=str(host_path),
                            container_path=str(mount_path),
                            readonly=False,
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

    def _empty_dir_root(self, medium: str | None) -> Path:
        root = Path(os.getenv("AE_CRI_EMPTYDIR_ROOT", "/var/lib/ae/emptydirs"))
        if medium and str(medium).lower() == "memory":
            tmpfs_root = Path(
                os.getenv("AE_CRI_EMPTYDIR_TMPFS_ROOT", "/dev/shm/ae-emptydir")
            )
            if tmpfs_root.exists():
                return tmpfs_root
        return root

    def _cleanup_empty_dirs(self, app_name: str, replica_id: str) -> None:
        roots = {
            Path(os.getenv("AE_CRI_EMPTYDIR_ROOT", "/var/lib/ae/emptydirs")),
            Path(os.getenv("AE_CRI_EMPTYDIR_TMPFS_ROOT", "/dev/shm/ae-emptydir")),
        }
        for root in roots:
            path = root / app_name / replica_id
            with contextlib.suppress(Exception):
                if path.exists():
                    shutil.rmtree(path)

    def _build_devices_for_container(self, manifest: AppManifest) -> list[Any]:
        pb2 = self._pb2()
        devices: list[Any] = []
        for d in getattr(manifest.spec, "volume_devices", []) or []:
            with contextlib.suppress(Exception):
                host_path = getattr(d, "host_path", None)
                if host_path and not os.path.isabs(host_path):
                    host_path = os.path.abspath(host_path)
                device_path = getattr(d, "device_path", None)
                if not host_path or not device_path:
                    continue
                perms = "r" if bool(getattr(d, "read_only", False)) else "rwm"
                devices.append(
                    pb2.Device(
                        host_path=str(host_path),
                        container_path=str(device_path),
                        permissions=str(perms),
                    )
                )
        return devices

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
