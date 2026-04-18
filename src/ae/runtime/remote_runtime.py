"""Remote runtime shim that delegates RuntimeAdapter calls to an HTTP agent.

When `agent_url` is None, it falls back to the provided `local_runtime` to
preserve single-node behavior.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
from datetime import datetime
from urllib.parse import urlparse

import requests

from ae.ha.fencing import (
    delete_operation,
    ensure_operation,
    gc_operation,
    merge_envelope,
    resolve_controller_identity,
)
from ae.controller.spec import AppManifest

from .base import PodState, RuntimeAdapter, RuntimeResult, WorkloadMetricSample

LOGGER = logging.getLogger(__name__)


class RemoteRuntime(RuntimeAdapter):
    """RuntimeAdapter that forwards calls to an ae.node agent over HTTP."""

    def __init__(
        self,
        agent_url: str | None,
        local_runtime: RuntimeAdapter,
        *,
        authority=None,
        node_id: str | None = None,
    ) -> None:
        self._agent_url = agent_url.rstrip("/") if agent_url else None
        self._local = local_runtime
        self._authority = authority
        self._node_id = str(node_id or "")
        import os

        self._verify = os.getenv("AE_AGENT_CA_FILE") or True
        cert = os.getenv("AE_AGENT_CERT_FILE")
        key = os.getenv("AE_AGENT_KEY_FILE")
        self._cert = (cert, key) if cert and key else None

    def _agent_target(self) -> tuple[str, str, int, str]:
        if not self._agent_url:
            raise RuntimeError("agent_url not configured")
        parsed = urlparse(self._agent_url)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or ""
        if not host:
            raise RuntimeError("agent_url missing hostname")
        port = parsed.port or (443 if scheme == "https" else 80)
        base_path = parsed.path.rstrip("/")
        return scheme, host, port, base_path

    def _agent_ssl_context(self) -> ssl.SSLContext:
        verify = self._verify
        if verify is True:
            ctx = ssl.create_default_context()
        elif verify is False:
            ctx = ssl._create_unverified_context()  # noqa: S504 - explicit opt-out
        else:
            ctx = ssl.create_default_context(cafile=str(verify))
        if self._cert:
            cert, key = self._cert
            ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    def _open_upgrade(self, path: str, payload: dict, upgrade: str) -> tuple[socket.socket, dict]:
        scheme, host, port, base_path = self._agent_target()
        sock = socket.create_connection((host, port), timeout=10)
        if scheme == "https":
            ctx = self._agent_ssl_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        body = json.dumps(payload).encode("utf-8")
        req_path = f"{base_path}{path}" if base_path else path
        host_hdr = f"{host}:{port}" if port and port not in {80, 443} else host
        headers = [
            f"POST {req_path} HTTP/1.1",
            f"Host: {host_hdr}",
            "Connection: Upgrade",
            f"Upgrade: {upgrade}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "",
            "",
        ]
        sock.sendall("\r\n".join(headers).encode("utf-8") + body)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise RuntimeError("agent upgrade failed: no response")
            buf += chunk
            if len(buf) > 65536:
                sock.close()
                raise RuntimeError("agent upgrade failed: headers too large")
        header_blob, rest = buf.split(b"\r\n\r\n", 1)
        lines = header_blob.split(b"\r\n")
        status_line = lines[0].decode("utf-8", "ignore")
        parts = status_line.split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        resp_headers: dict[str, str] = {}
        for line in lines[1:]:
            if b":" not in line:
                continue
            key, val = line.split(b":", 1)
            resp_headers[key.decode("utf-8", "ignore").lower()] = val.decode(
                "utf-8", "ignore"
            ).strip()
        if status != 101:
            detail = rest.decode("utf-8", "ignore").strip()
            sock.close()
            raise RuntimeError(
                f"agent upgrade failed status={status} detail={detail or 'n/a'}"
            )

        if rest:

            class _BufferedSocket:
                def __init__(self, base_sock: socket.socket, initial: bytes) -> None:
                    self._sock = base_sock
                    self._buf = initial

                def recv(self, n: int) -> bytes:
                    if self._buf:
                        out, self._buf = self._buf[:n], self._buf[n:]
                        return out
                    return self._sock.recv(n)

                def sendall(self, data: bytes) -> None:
                    self._sock.sendall(data)

                def settimeout(self, value: float | None) -> None:
                    self._sock.settimeout(value)

                def shutdown(self, how: int) -> None:
                    self._sock.shutdown(how)

                def close(self) -> None:
                    self._sock.close()

                def fileno(self) -> int:
                    return self._sock.fileno()

            return _BufferedSocket(sock, rest), resp_headers
        return sock, resp_headers

    def _use_local(self) -> bool:
        return not self._agent_url

    def _request(self, method: str, path: str, *, timeout: int = 30, **kwargs):
        url = f"{self._agent_url}{path}"
        if self._cert:
            kwargs["cert"] = self._cert
        if self._verify is not True:
            kwargs["verify"] = self._verify
        resp = requests.request(method, url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp

    def _mutation_payload(
        self,
        payload: dict[str, object],
        operation_id: str,
        *,
        identity=None,
    ) -> dict[str, object]:
        identity = identity or resolve_controller_identity(self._authority)
        return merge_envelope(payload, identity.envelope(operation_id))

    # --- RuntimeAdapter API --------------------------------------------
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
        if self._use_local():
            return self._local.ensure_app(
                manifest,
                revision,
                keep_old=keep_old,
                limit_create=limit_create,
                pod_names=pod_names,
                node_id=node_id,
            )
        payload = {
            "manifest": manifest.model_dump(mode="json", by_alias=True),
            "revision": revision,
            "keep_old": keep_old,
            "limit_create": limit_create,
            "pod_names": pod_names,
            "replica_ids": pod_names,
            "node_id": node_id,
        }
        identity = resolve_controller_identity(self._authority)
        payload = self._mutation_payload(
            payload,
            ensure_operation(
                str(manifest.metadata.name),
                revision,
                str(node_id or self._node_id or ""),
            ),
            identity=identity,
        )
        resp = self._request("POST", "/v1/ensure_app", json=payload, timeout=30)
        data = resp.json()
        return _runtime_result_from_json(data)

    def remove_app(self, app_name: str) -> int:
        if self._use_local():
            return self._local.remove_app(app_name)
        identity = resolve_controller_identity(self._authority)
        payload = self._mutation_payload(
            {"app": app_name},
            delete_operation(app_name, self._node_id, identity.controller_epoch),
            identity=identity,
        )
        resp = self._request("POST", "/v1/remove_app", json=payload, timeout=15)
        return int(resp.json().get("removed", 0))

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        if self._use_local():
            return self._local.remove_old_revisions(app_name, keep_revision)
        identity = resolve_controller_identity(self._authority)
        payload = self._mutation_payload(
            {"app": app_name, "keep_revision": keep_revision},
            gc_operation(app_name, keep_revision, self._node_id),
            identity=identity,
        )
        resp = self._request(
            "POST",
            "/v1/remove_old",
            json=payload,
            timeout=15,
        )
        return int(resp.json().get("removed", 0))

    def list_containers_info(self) -> list[dict]:
        if self._use_local():
            return self._local.list_containers_info()
        resp = self._request("GET", "/v1/containers", timeout=10)
        return resp.json().get("containers", [])

    def list_workload_metrics(self) -> list[WorkloadMetricSample]:
        if self._use_local():
            return self._local.list_workload_metrics()
        resp = self._request("GET", "/v1/workload_metrics", timeout=10)
        payload = resp.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        out: list[WorkloadMetricSample] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            collected_at = item.get("collected_at")
            try:
                ts = datetime.fromisoformat(str(collected_at))
            except Exception:
                ts = datetime.now()
            try:
                memory_bytes = int(item.get("memory_bytes", 0) or 0)
            except Exception:
                memory_bytes = 0
            try:
                pod_count = int(item.get("pod_count", 0) or 0)
            except Exception:
                pod_count = 0
            cpu_raw = item.get("cpu_cores")
            try:
                cpu_cores = float(cpu_raw) if cpu_raw is not None else None
            except Exception:
                cpu_cores = None
            out.append(
                WorkloadMetricSample(
                    app_name=str(item.get("app_name") or ""),
                    node_id=str(item.get("node_id") or self._node_id or ""),
                    collected_at=ts,
                    cpu_cores=cpu_cores,
                    memory_bytes=memory_bytes,
                    pod_count=pod_count,
                )
            )
        return out

    def read_logs(
        self,
        pod_name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        if self._use_local():
            return self._local.read_logs(pod_name, follow=follow, tail=tail, since=since)
        params = {
            "pod_name": pod_name,
            "replica_id": pod_name,
            "follow": follow,
            "tail": tail,
            "since": since,
        }
        resp = self._request("GET", "/v1/logs", params=params, timeout=30)
        lines = resp.json().get("lines", [])
        return iter(lines)

    def exec(self, pod_name: str, command: list[str], *, timeout: int | None = None) -> int:
        if self._use_local():
            return self._local.exec(pod_name, command, timeout=timeout)
        payload = {
            "pod_name": pod_name,
            "replica_id": pod_name,
            "command": command,
            "timeout": timeout,
        }
        resp = self._request("POST", "/v1/exec", json=payload, timeout=timeout or 30)
        return int(resp.json().get("exit_code", 1))

    def exec_attach(
        self,
        pod_name: str,
        command: list[str],
        *,
        container: str | None = None,
        tty: bool = False,
    ):
        if self._use_local():
            return self._local.exec_attach(pod_name, command, container=container, tty=tty)
        payload = {
            "pod_name": pod_name,
            "replica_id": pod_name,
            "command": command,
            "container": container,
            "tty": bool(tty),
        }
        sock, headers = self._open_upgrade("/v1/exec_attach", payload, "ae-exec")
        exec_id = headers.get("x-exec-id")
        return sock, exec_id

    def exec_resize(
        self, exec_id: str, *, height: int | None = None, width: int | None = None
    ) -> None:
        if self._use_local():
            return self._local.exec_resize(exec_id, height=height, width=width)
        payload = {"exec_id": exec_id, "height": height, "width": width}
        self._request("POST", "/v1/exec_resize", json=payload, timeout=5)

    def exec_exit_code(self, exec_id: str) -> int:
        if self._use_local():
            return self._local.exec_exit_code(exec_id)
        resp = self._request("POST", "/v1/exec_inspect", json={"exec_id": exec_id}, timeout=10)
        return int(resp.json().get("exit_code", 0))

    def port_forward_socket(
        self,
        *,
        pod_id: str | None,
        pod_name: str | None,
        namespace: str | None,
        port: int,
    ):
        if self._use_local():
            return self._local.port_forward_socket(
                pod_id=pod_id,
                pod_name=pod_name,
                namespace=namespace,
                port=int(port),
            )
        payload = {
            "pod_id": pod_id,
            "pod_name": pod_name,
            "namespace": namespace,
            "port": int(port),
        }
        sock, _headers = self._open_upgrade("/v1/portforward/attach", payload, "ae-portforward")
        return sock

    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:
        if self._use_local():
            return self._local.ensure_storage_volumes(app_name, volumes)
        self._request(
            "POST",
            "/v1/ensure_volumes",
            json={"app": app_name, "volumes": volumes},
            timeout=20,
        )

    def remove_storage_volumes(self, app_name: str, names: list[str]) -> int:
        if self._use_local():
            return self._local.remove_storage_volumes(app_name, names)
        resp = self._request(
            "POST",
            "/v1/remove_volumes",
            json={"app": app_name, "names": names},
            timeout=20,
        )
        return int(resp.json().get("removed", 0))

    def list_storage_volumes(self, app_name: str | None = None) -> list[dict]:
        if self._use_local():
            return self._local.list_storage_volumes(app_name)
        resp = self._request("GET", "/v1/volumes", params={"app": app_name}, timeout=10)
        return resp.json().get("volumes", [])


def _runtime_result_from_json(data: dict) -> RuntimeResult:
    reps = []
    for item in data.get("pod_states", []) or data.get("replica_states", []):
        exit_code = item.get("exit_code", None)
        if exit_code is None:
            exit_code = item.get("exitCode", None)
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except Exception:
            exit_code = None
        finished_raw = item.get("finished_at", None)
        if finished_raw is None:
            finished_raw = item.get("finishedAt", None)
        finished_at = None
        if finished_raw:
            try:
                finished_at = datetime.fromisoformat(str(finished_raw).rstrip("Z"))
            except Exception:
                finished_at = None
        pod_name = item.get("pod_name")
        if not pod_name:
            pod_name = item.get("replica_id", "")
        reps.append(
            PodState(
                pod_name=pod_name,
                ready=bool(item.get("ready")),
                status=item.get("status", "unknown"),
                revision=(
                    int(item.get("revision"))
                    if str(item.get("revision", "")).isdigit()
                    else None
                ),
                endpoint=item.get("endpoint"),
                exit_code=exit_code,
                finished_at=finished_at,
            )
        )
    return RuntimeResult(
        revision=int(data.get("revision", 0)),
        created=int(data.get("created", 0)),
        updated=int(data.get("updated", 0)),
        removed=int(data.get("removed", 0)),
        pod_states=reps,
    )
