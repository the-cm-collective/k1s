"""HTTP agent exposing runtime operations and optional controller heartbeats."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ae.config.transport import TransportConfig, check_nats_connectivity
from ae.controller.spec import AppManifest
from ae.ha.fencing import SQLiteFenceStore, parse_envelope
from ae.runtime import PodState, RuntimeAdapter, RuntimeResult
import requests

LOGGER = logging.getLogger(__name__)


def _is_client_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, BrokenPipeError | ConnectionResetError | ConnectionAbortedError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {32, 103, 104}
    return False


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict) -> bool:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    try:
        handler.end_headers()
        handler.wfile.write(payload)
    except Exception as exc:  # noqa: BLE001
        if _is_client_disconnect(exc):
            LOGGER.debug(
                "agent client disconnected during response write path=%s status=%s",
                getattr(handler, "path", "<unknown>"),
                status,
            )
            return False
        raise
    return True


def _parse_manifest(payload: dict) -> AppManifest:
    return AppManifest.model_validate(payload)


def _ha_mode_enabled() -> bool:
    return str(os.getenv("AE_HA_MODE", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}


def _agent_fence_db_path() -> Path:
    raw = os.getenv("AE_AGENT_FENCE_DB")
    if raw:
        return Path(raw)
    return Path("/var/lib/ae/node/fence.db")


class AgentHandler(BaseHTTPRequestHandler):
    runtime: RuntimeAdapter = None  # type: ignore[assignment]
    volume_manager = None
    node_id: str | None = None
    fabric_sessions: dict[str, dict] = {}
    fabric_lock = threading.Lock()
    fence_store: SQLiteFenceStore | None = None

    def log_message(self, format: str, *args) -> None:  # noqa: A003 - BaseHTTPRequestHandler API
        LOGGER.info("%s - %s", self.address_string(), format % args)

    @classmethod
    def _ensure_fence_store(cls) -> SQLiteFenceStore:
        if cls.fence_store is None:
            cls.fence_store = SQLiteFenceStore(_agent_fence_db_path())
            cls.fence_store.init()
        return cls.fence_store

    def _fence_scope(self, kind: str) -> str:
        node_id = str(self.node_id or "").strip() or str(os.getenv("AE_NODE_ID") or "unknown-node")
        return f"{kind}:{node_id}"

    def _stale_response(self, decision) -> None:
        current = decision.current
        body = {"error": "stale_epoch"}
        if current is not None:
            body["controller_id"] = current.controller_id
            body["controller_epoch"] = current.controller_epoch
        _json_response(self, 409, body)

    def _begin_mutation(self, payload: dict, scope: str):
        if not _ha_mode_enabled():
            return "accept", None, None
        envelope = parse_envelope(payload)
        if envelope is None:
            _json_response(self, 409, {"error": "missing_fencing_envelope"})
            return "reject", None, None
        decision = self._ensure_fence_store().begin(scope, envelope)
        if decision.stale:
            self._stale_response(decision)
            return "reject", envelope, decision
        return decision.status, envelope, decision

    def _commit_mutation(self, scope: str, envelope) -> None:
        if envelope is None:
            return
        self._ensure_fence_store().commit(scope, envelope)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", "0"))
        payload = {}
        if length:
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                _json_response(self, 400, {"error": "invalid json"})
                return

        try:
            if self.path == "/v1/fabric/ensure_session":
                scope = self._fence_scope("fabric")
                status, envelope, _decision = self._begin_mutation(payload, scope)
                if status == "reject":
                    return
                session = payload.get("session")
                if not isinstance(session, dict):
                    _json_response(self, 400, {"error": "session payload required"})
                    return
                session_id = str(session.get("session_id") or "").strip()
                if not session_id:
                    _json_response(self, 400, {"error": "session_id required"})
                    return
                mode = str(session.get("mode") or "lan_direct").strip().lower()
                if mode not in {"lan_direct", "wg_ephemeral"}:
                    _json_response(self, 400, {"error": f"unsupported fabric mode {mode}"})
                    return
                if mode == "wg_ephemeral" and str(
                    os.getenv("AE_FABRIC_WG_ENABLE", "0")
                ).strip().lower() not in {"1", "true", "yes", "on"}:
                    _json_response(
                        self,
                        400,
                        {"error": "wg_ephemeral mode disabled (set AE_FABRIC_WG_ENABLE=1)"},
                    )
                    return
                if status == "duplicate":
                    with self.fabric_lock:
                        self.fabric_sessions[session_id] = {
                            "session_id": session_id,
                            "node_id": str(payload.get("node_id") or self.node_id or ""),
                            "mode": mode,
                            "policy_mode": str(session.get("policy_mode") or ""),
                            "ifname": str(session.get("ifname") or ""),
                            "member_ips": dict(session.get("member_ips") or {}),
                            "allowed_rules": list(session.get("allowed_rules") or []),
                            "expires_at": str(session.get("expires_at") or ""),
                            "updated_at": time.time(),
                        }
                    _json_response(
                        self,
                        200,
                        {"ok": True, "session_id": session_id, "mode": mode, "duplicate": True},
                    )
                    return
                with self.fabric_lock:
                    self.fabric_sessions[session_id] = {
                        "session_id": session_id,
                        "node_id": str(payload.get("node_id") or self.node_id or ""),
                        "mode": mode,
                        "policy_mode": str(session.get("policy_mode") or ""),
                        "ifname": str(session.get("ifname") or ""),
                        "member_ips": dict(session.get("member_ips") or {}),
                        "allowed_rules": list(session.get("allowed_rules") or []),
                        "expires_at": str(session.get("expires_at") or ""),
                        "updated_at": time.time(),
                    }
                self._commit_mutation(scope, envelope)
                _json_response(self, 200, {"ok": True, "session_id": session_id, "mode": mode})
                return
            if self.path == "/v1/fabric/teardown_session":
                scope = self._fence_scope("fabric")
                status, envelope, _decision = self._begin_mutation(payload, scope)
                if status == "reject":
                    return
                session_id = str(payload.get("session_id") or "").strip()
                if not session_id:
                    _json_response(self, 400, {"error": "session_id required"})
                    return
                if status == "duplicate":
                    _json_response(self, 200, {"ok": True, "removed": False, "duplicate": True})
                    return
                removed = False
                with self.fabric_lock:
                    removed = self.fabric_sessions.pop(session_id, None) is not None
                self._commit_mutation(scope, envelope)
                _json_response(self, 200, {"ok": True, "removed": removed})
                return
            if self.path == "/v1/ensure_app":
                scope = self._fence_scope("runtime")
                status, envelope, _decision = self._begin_mutation(payload, scope)
                if status == "reject":
                    return
                if status == "duplicate":
                    _json_response(
                        self,
                        200,
                        {
                            "revision": int(payload.get("revision", 0)),
                            "created": 0,
                            "updated": 0,
                            "removed": 0,
                            "pod_states": [],
                            "replica_states": [],
                            "ok": True,
                            "duplicate": True,
                        },
                    )
                    return
                manifest = _parse_manifest(payload.get("manifest", {}))
                pod_names = payload.get("pod_names") or payload.get("replica_ids")
                replica_id = None
                if isinstance(pod_names, list) and len(pod_names) == 1:
                    replica_id = pod_names[0]
                if self.volume_manager is not None:
                    try:
                        manifest = self.volume_manager.inject_pvc_mounts(  # type: ignore[attr-defined]
                            manifest,
                            node_id=payload.get("node_id") or self.node_id,
                            replica_id=replica_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        try:
                            from ae.storage.netfs import PvcNotReadyError
                        except Exception:
                            PvcNotReadyError = None  # type: ignore[assignment]
                        if PvcNotReadyError is not None and isinstance(exc, PvcNotReadyError):
                            names: list[str] = []
                            if isinstance(pod_names, list):
                                names = [str(n) for n in pod_names if n]
                            elif pod_names:
                                names = [str(pod_names)]
                            pending_states = [
                                PodState(ready=False, status="Pending", pod_name=name)
                                for name in names
                            ]
                            result = RuntimeResult(
                                revision=int(payload.get("revision", 0)),
                                created=0,
                                updated=0,
                                removed=0,
                                pod_states=pending_states,
                            )
                            self._commit_mutation(scope, envelope)
                            _json_response(self, 200, _result_to_dict(result))
                            return
                        raise
                result = self.runtime.ensure_app(
                    manifest,
                    int(payload.get("revision", 0)),
                    keep_old=bool(payload.get("keep_old", False)),
                    limit_create=payload.get("limit_create"),
                    pod_names=pod_names,
                    node_id=payload.get("node_id"),
                )
                self._commit_mutation(scope, envelope)
                _json_response(self, 200, _result_to_dict(result))
                return
            if self.path == "/v1/remove_app":
                scope = self._fence_scope("runtime")
                status, envelope, _decision = self._begin_mutation(payload, scope)
                if status == "reject":
                    return
                if status == "duplicate":
                    _json_response(self, 200, {"removed": 0, "ok": True, "duplicate": True})
                    return
                removed = self.runtime.remove_app(payload.get("app", ""))
                self._commit_mutation(scope, envelope)
                _json_response(self, 200, {"removed": removed})
                return
            if self.path == "/v1/remove_old":
                scope = self._fence_scope("runtime")
                status, envelope, _decision = self._begin_mutation(payload, scope)
                if status == "reject":
                    return
                if status == "duplicate":
                    _json_response(self, 200, {"removed": 0, "ok": True, "duplicate": True})
                    return
                removed = self.runtime.remove_old_revisions(
                    payload.get("app", ""), int(payload.get("keep_revision", 0))
                )
                self._commit_mutation(scope, envelope)
                _json_response(self, 200, {"removed": removed})
                return
            if self.path == "/v1/exec":
                pod_name = payload.get("pod_name") or payload.get("replica_id", "")
                rc = self.runtime.exec(
                    pod_name,
                    payload.get("command", []),
                    timeout=payload.get("timeout"),
                )
                _json_response(self, 200, {"exit_code": rc})
                return
            if self.path == "/v1/exec_attach":
                # Upgrade to raw stream and proxy between client and runtime exec socket
                rid = payload.get("pod_name") or payload.get("replica_id", "")
                cmd = payload.get("command", [])
                container = payload.get("container")
                tty = bool(payload.get("tty", False))
                try:
                    sock, exec_id = self.runtime.exec_attach(rid, cmd, container=container, tty=tty)
                except Exception as exc:  # noqa: BLE001
                    _json_response(self, 501, {"error": f"exec_attach unsupported: {exc}"})
                    return
                self.send_response(101, "Switching Protocols")
                self.send_header("Connection", "Upgrade")
                self.send_header("Upgrade", "ae-exec")
                if exec_id:
                    self.send_header("X-Exec-Id", str(exec_id))
                self.end_headers()
                client_sock = self.connection
                client_sock.settimeout(0.05)
                sock.settimeout(0.05)
                try:
                    while True:
                        try:
                            data = client_sock.recv(4096)
                        except socket.timeout:
                            data = None
                        if data:
                            sock.sendall(data)
                        try:
                            resp = sock.recv(4096)
                        except socket.timeout:
                            resp = None
                        if resp:
                            client_sock.sendall(resp)
                        if data == b"" or resp == b"":
                            break
                finally:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    try:
                        client_sock.close()
                    except Exception:
                        pass
                return
            if self.path == "/v1/portforward/attach":
                pod_id = payload.get("pod_id") or payload.get("uid")
                pod_name = payload.get("pod_name") or payload.get("replica_id")
                namespace = payload.get("namespace")
                try:
                    port = int(payload.get("port") or 0)
                except Exception:
                    port = 0
                if not port:
                    _json_response(self, 400, {"error": "port required"})
                    return
                target = self._resolve_portforward_target(
                    pod_id=str(pod_id) if pod_id else None,
                    pod_name=str(pod_name) if pod_name else None,
                    namespace=str(namespace) if namespace else None,
                    port=port,
                )
                if not target:
                    _json_response(self, 404, {"error": "pod target not found"})
                    return
                host, target_port = target
                try:
                    upstream = socket.create_connection((host, int(target_port)), timeout=5.0)
                    upstream.settimeout(0.05)
                except Exception as exc:  # noqa: BLE001
                    _json_response(self, 502, {"error": f"upstream connect failed: {exc}"})
                    return
                self.send_response(101, "Switching Protocols")
                self.send_header("Connection", "Upgrade")
                self.send_header("Upgrade", "ae-portforward")
                self.end_headers()
                client_sock = self.connection
                client_sock.settimeout(0.05)
                try:
                    while True:
                        try:
                            data = client_sock.recv(4096)
                        except socket.timeout:
                            data = None
                        if data:
                            upstream.sendall(data)
                        try:
                            resp = upstream.recv(4096)
                        except socket.timeout:
                            resp = None
                        if resp:
                            client_sock.sendall(resp)
                        if data == b"" or resp == b"":
                            break
                finally:
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    try:
                        client_sock.close()
                    except Exception:
                        pass
                return
            if self.path == "/v1/exec_resize":
                exec_id = payload.get("exec_id", "")
                h = payload.get("height")
                w = payload.get("width")
                try:
                    self.runtime.exec_resize(exec_id, height=h, width=w)
                except Exception:
                    pass
                _json_response(self, 200, {"ok": True})
                return
            if self.path == "/v1/exec_inspect":
                exec_id = payload.get("exec_id", "")
                code = 0
                try:
                    code = int(self.runtime.exec_exit_code(exec_id))
                except Exception:
                    code = 0
                _json_response(self, 200, {"exit_code": code})
                return
            if self.path == "/v1/ensure_volumes":
                self.runtime.ensure_storage_volumes(
                    payload.get("app", ""), payload.get("volumes", [])
                )
                _json_response(self, 200, {"ok": True})
                return
            if self.path == "/v1/remove_volumes":
                removed = self.runtime.remove_storage_volumes(
                    payload.get("app", ""), payload.get("names", [])
                )
                _json_response(self, 200, {"removed": removed})
                return
        except Exception as exc:  # noqa: BLE001
            if _is_client_disconnect(exc):
                LOGGER.debug("agent client disconnected path=%s", self.path)
                return
            LOGGER.exception("agent error on %s", self.path)
            _json_response(self, 500, {"error": str(exc)})
            return

        _json_response(self, 404, {"error": "unknown endpoint"})

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        try:
            if self.path.startswith("/v1/fabric/sessions"):
                with self.fabric_lock:
                    items = list(self.fabric_sessions.values())
                _json_response(self, 200, {"sessions": items})
                return
            if self.path.startswith("/v1/containers"):
                items = self.runtime.list_containers_info()
                _json_response(self, 200, {"containers": items})
                return
            if self.path.startswith("/v1/logs"):
                import urllib.parse as _u

                qs = _u.urlparse(self.path).query
                params = dict(_u.parse_qsl(qs))
                rid = params.get("pod_name") or params.get("replica_id", "")
                tail = params.get("tail")
                tail_i = int(tail) if tail else None
                since = params.get("since")
                since_i = int(since) if since else None
                lines = list(
                    self.runtime.read_logs(rid, follow=False, tail=tail_i, since=since_i) or []
                )
                _json_response(self, 200, {"lines": lines})
                return
            if self.path.startswith("/v1/volumes"):
                import urllib.parse as _u

                qs = _u.urlparse(self.path).query
                params = dict(_u.parse_qsl(qs))
                app = params.get("app")
                vols = self.runtime.list_storage_volumes(app)
                _json_response(self, 200, {"volumes": vols})
                return
        except Exception as exc:  # noqa: BLE001
            if _is_client_disconnect(exc):
                LOGGER.debug("agent client disconnected path=%s", self.path)
                return
            LOGGER.exception("agent error on %s", self.path)
            _json_response(self, 500, {"error": str(exc)})
            return
        _json_response(self, 404, {"error": "unknown endpoint"})

    def _resolve_portforward_target(
        self,
        *,
        pod_id: str | None,
        pod_name: str | None,
        namespace: str | None,
        port: int,
    ) -> tuple[str, int] | None:
        try:
            containers = self.runtime.list_containers_info()
        except Exception:
            containers = []
        target = None
        for c in containers:
            labels = c.get("labels", {}) or {}
            rid = labels.get("ae.pod_name") or labels.get("ae.replica_id") or c.get("name")
            c_ns = labels.get("ae.namespace") or "default"
            if pod_id and (c.get("uid") == pod_id or c.get("id") == pod_id):
                target = c
                break
            if pod_name and rid == pod_name and (not namespace or c_ns == namespace):
                target = c
                break
        if not target:
            return None
        pod_ip = target.get("pod_ip")
        host_ip = target.get("host_ip") or target.get("hostIP") or "127.0.0.1"
        host_ports = target.get("host_ports") or target.get("hostPorts") or []
        port_map = target.get("port_map") or target.get("portMap") or {}
        target_port = int(port)
        if not pod_ip:
            # If we only have host ports, try to map container port -> host port.
            if isinstance(port_map, dict):
                if port in port_map:
                    target_port = int(port_map.get(port) or port)
                elif str(port) in port_map:
                    target_port = int(port_map.get(str(port)) or port)
            if target_port == int(port) and host_ports:
                try:
                    target_port = int(host_ports[0])
                except Exception:
                    target_port = int(port)
        return (str(pod_ip) if pod_ip else str(host_ip), int(target_port))


def _result_to_dict(res: RuntimeResult) -> dict:
    pod_states = [
        {
            "pod_name": r.pod_name,
            "replica_id": r.pod_name,
            "ready": r.ready,
            "status": r.status,
            "endpoint": r.endpoint,
        }
        for r in res.pod_states
    ]
    return {
        "revision": res.revision,
        "created": res.created,
        "updated": res.updated,
        "removed": res.removed,
        "pod_states": pod_states,
        "replica_states": [
            {
                "replica_id": item["pod_name"],
                "ready": item["ready"],
                "status": item["status"],
                "endpoint": item["endpoint"],
            }
            for item in pod_states
        ],
    }


def _parse_labels(raw: str | None) -> dict:
    if not raw:
        return {}
    labels = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            labels[k.strip()] = v.strip()
        else:
            labels[part.strip()] = ""
    return labels


def _detect_gpu_labels() -> dict[str, str]:
    if str(os.getenv("AE_GPU_DISCOVERY_DISABLE", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return {}
    bin_name = str(os.getenv("AE_NVIDIA_SMI_BIN", "nvidia-smi")).strip() or "nvidia-smi"
    try:
        out = subprocess.check_output(  # noqa: S603
            [bin_name, "--query-gpu=name", "--format=csv,noheader"],  # noqa: S603
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        names = [line.strip() for line in out.splitlines() if line.strip()]
        if not names:
            return {"gpu.present": "false", "gpu.count": "0"}
        models = sorted(set(names))
        return {
            "gpu.present": "true",
            "gpu.count": str(len(names)),
            "gpu.models": ",".join(models),
        }
    except Exception:
        return {"gpu.present": "false", "gpu.count": "0"}


def _start_heartbeat_loop(
    controller_url: str | None,
    node_id: str,
    name: str,
    backend: str,
    endpoint: str | None,
    labels: dict | None,
    taints: list | None,
    token: str | None,
    interval: int,
    pod_cidr: str | None = None,
    wg_pubkey: str | None = None,
    rp_pubkey: str | None = None,
    ca_file: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> None:
    if not controller_url:
        LOGGER.info("heartbeat disabled (AE_CONTROLLER_URL not set)")
        return

    def _loop():
        while True:
            payload = {
                "node_id": node_id,
                "name": name,
                "backend": backend,
                "endpoint": endpoint,
                "labels": labels or {},
                "taints": taints or [],
                "status": "Ready",
                "pod_cidr": pod_cidr,
                "wg_pubkey": wg_pubkey,
                "rp_pubkey": rp_pubkey,
            }
            try:
                headers = {}
                if token:
                    headers["X-Agent-Token"] = token
                kwargs = {
                    "json": payload,
                    "headers": headers,
                    "timeout": 5,
                }
                if ca_file:
                    kwargs["verify"] = ca_file
                if client_cert and client_key:
                    kwargs["cert"] = (client_cert, client_key)
                requests.post(controller_url.rstrip("/") + "/v1/heartbeat", **kwargs)
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("heartbeat send failed: %s", exc)
            time.sleep(max(1, interval))

    threading.Thread(target=_loop, daemon=True, name="agent-heartbeat").start()


def _start_service_proxy_loop(
    *,
    controller_url: str | None,
    interval: int,
    state_db: str,
    token: str | None = None,
    ca_file: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> None:
    if os.getenv("AE_AGENT_SERVICE_PROXY", "0") != "1":
        return
    if not controller_url:
        LOGGER.warning("service proxy enabled but AE_CONTROLLER_URL is not set")
        return
    try:
        from pathlib import Path

        from ae.controller.state import ServiceEndpoint, SQLiteStateStore
        from ae.network import IptablesProvider
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("service proxy unavailable: %s", exc)
        return

    db_path = Path(state_db)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    store = SQLiteStateStore(db_path)
    provider = IptablesProvider(
        store,
        service_cidr=os.getenv("AE_SERVICE_IP_POOL", "10.241.0.0/16"),
        iptables_bin=os.getenv("AE_IPTABLES_BIN", "iptables"),
    )
    base = controller_url.rstrip("/")
    url = f"{base}/services"

    def _fetch_services() -> list[dict] | None:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        kwargs = {"headers": headers, "timeout": 5}
        if ca_file:
            kwargs["verify"] = ca_file
        if client_cert and client_key:
            kwargs["cert"] = (client_cert, client_key)
        try:
            resp = requests.get(url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("service proxy fetch failed: %s", exc)
            return None
        if resp.status_code != 200:
            LOGGER.debug("service proxy fetch status=%s", resp.status_code)
            return None
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("service proxy invalid json: %s", exc)
            return None
        if isinstance(payload, dict) and "items" in payload:
            items = payload.get("items") or []
            return list(items) if isinstance(items, list) else None
        if isinstance(payload, list):
            return payload
        return None

    def _apply_services(services: list[dict]) -> None:
        provider.ensure_network()
        seen: set[str] = set()
        for svc in services:
            if not isinstance(svc, dict):
                continue
            app = svc.get("app") or svc.get("app_name")
            cluster_ip = svc.get("cluster_ip") or svc.get("clusterIP")
            ports = svc.get("ports") or {}
            if not app or not cluster_ip or not ports:
                continue
            app = str(app)
            endpoints_raw = svc.get("endpoints") or []
            endpoints: list[ServiceEndpoint] = []
            backends_by_port: dict[int, list[tuple[str, int]]] = {}
            for ep in endpoints_raw:
                if not isinstance(ep, dict):
                    continue
                try:
                    port = int(ep.get("port") or 0)
                    target = int(ep.get("target_port") or ep.get("targetPort") or 0)
                except Exception:
                    continue
                ip = str(ep.get("ip") or "")
                ready = bool(ep.get("ready", True))
                if not ip or port <= 0 or target <= 0:
                    continue
                endpoints.append(
                    ServiceEndpoint(
                        app_name=app,
                        port=port,
                        ip=ip,
                        target_port=target,
                        ready=ready,
                    )
                )
                if ready:
                    backends_by_port.setdefault(port, []).append((ip, target))
            store.upsert_service_snapshot(app, str(cluster_ip), ports, endpoints)
            provider.update_service_endpoints(app, backends_by_port)
            seen.add(app)
        try:
            for rec in store.list_services():
                if rec.app_name not in seen:
                    provider.remove_service(rec.app_name)
                    store.delete_service(rec.app_name)
        except Exception:
            pass

    def _loop() -> None:
        while True:
            services = _fetch_services()
            if services is not None:
                try:
                    _apply_services(services)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug("service proxy apply failed: %s", exc)
            time.sleep(max(1, interval))

    LOGGER.info("service proxy loop enabled (controller=%s, interval=%ss)", base, interval)
    threading.Thread(target=_loop, daemon=True, name="agent-service-proxy").start()


def serve(
    runtime: RuntimeAdapter,
    host: str = "0.0.0.0",
    port: int = 9109,
    *,
    controller_url: str | None = None,
    node_id: str | None = None,
    node_name: str | None = None,
    node_labels: dict | None = None,
    node_taints: list | None = None,
    heartbeat_interval: int = 10,
    node_endpoint: str | None = None,
    pod_cidr: str | None = None,
    wg_pubkey: str | None = None,
    rp_pubkey: str | None = None,
    token: str | None = None,
    ensure_pod_net: bool = False,
    pod_bridge: str = "ae0",
    wg_iface: str = "wg0",
    wg_config: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    client_ca: str | None = None,
    require_client_cert: bool = False,
    controller_ca: str | None = None,
    controller_client_cert: str | None = None,
    controller_client_key: str | None = None,
) -> None:
    if ensure_pod_net and pod_cidr:
        try:
            from ae.node.net_helper import ensure_pod_bridge, apply_wireguard

            ensure_pod_bridge(pod_bridge, pod_cidr)
            if wg_config:
                apply_wireguard(wg_config, iface=wg_iface)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("overlay setup failed: %s", exc)

    _start_heartbeat_loop(
        controller_url,
        node_id or socket.gethostname(),
        node_name or node_id or socket.gethostname(),
        runtime.__class__.__name__.replace("Runtime", "").lower(),
        node_endpoint,
        node_labels,
        node_taints,
        token,
        heartbeat_interval,
        pod_cidr=pod_cidr,
        wg_pubkey=wg_pubkey,
        rp_pubkey=rp_pubkey,
        ca_file=controller_ca,
        client_cert=controller_client_cert,
        client_key=controller_client_key,
    )
    try:
        interval = int(os.getenv("AE_AGENT_SERVICE_PROXY_INTERVAL", "5") or 5)
    except Exception:
        interval = 5
    proxy_token = os.getenv("AE_AGENT_SERVICE_PROXY_TOKEN") or os.getenv("AE_API_READ_TOKEN")
    if not proxy_token:
        proxy_token = os.getenv("AE_API_ADMIN_TOKEN")
    proxy_url = os.getenv("AE_AGENT_SERVICE_PROXY_URL") or controller_url
    proxy_db = os.getenv("AE_AGENT_SERVICE_PROXY_DB", "/var/lib/ae/agent-services.db")
    _start_service_proxy_loop(
        controller_url=proxy_url,
        interval=interval,
        state_db=proxy_db,
        token=proxy_token,
        ca_file=controller_ca,
        client_cert=controller_client_cert,
        client_key=controller_client_key,
    )
    AgentHandler.runtime = runtime
    AgentHandler.node_id = node_id or socket.gethostname()
    AgentHandler.fence_store = SQLiteFenceStore(_agent_fence_db_path())
    AgentHandler.fence_store.init()
    if os.getenv("AE_ENABLE_NETFS", "0") == "1":
        try:
            from pathlib import Path

            from ae.apishim.store import ObjectStore
            from ae.storage import (
                ApishimStorageState,
                InMemoryStorageState,
                NetFSManager,
                NodeVolumeManager,
            )

            state = None
            dsn = os.getenv("AE_APISHIM_DSN")
            db_path = os.getenv("AE_APISHIM_DB")
            if dsn or db_path:
                store = ObjectStore(
                    db_path=Path(db_path) if db_path else Path("state/apishim.db"),
                    dsn=dsn,
                )
                state = ApishimStorageState(store)
            if state is None:
                state = InMemoryStorageState()
            netfs = NetFSManager(state)
            AgentHandler.volume_manager = NodeVolumeManager(netfs, node_id=AgentHandler.node_id)
            LOGGER.info("netfs volume manager enabled on node %s", AgentHandler.node_id)
        except Exception as exc:  # noqa: BLE001
            AgentHandler.volume_manager = None
            LOGGER.warning("failed to enable netfs volume manager: %s", exc)
    server = HTTPServer((host, port), AgentHandler)
    if tls_cert and tls_key:
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        if client_ca:
            ctx.load_verify_locations(client_ca)
            if require_client_cert:
                ctx.verify_mode = ssl.CERT_REQUIRED
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"
    LOGGER.info("ae.node agent listening on %s://%s:%s", scheme, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("shutdown requested (keyboard interrupt)")
    finally:
        try:
            server.server_close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ae.node", description="k1s node agent (phase 1 skeleton)"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port", type=int, default=int(__import__("os").getenv("AE_NODE_PORT", "9109"))
    )
    parser.add_argument("--controller-url", default=os.getenv("AE_CONTROLLER_URL"))
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=int(os.getenv("AE_AGENT_HEARTBEAT_SECONDS", "30") or 30),
        help="seconds between heartbeats to the controller",
    )
    parser.add_argument(
        "--advertise-endpoint",
        default=os.getenv("AE_AGENT_ENDPOINT"),
        help="optional controller-facing endpoint for this agent (host:port)",
    )
    parser.add_argument(
        "--runtime-backend",
        choices=["podman", "docker", "cri", "containerd"],
        default=__import__("os").getenv("AE_RUNTIME_BACKEND", "podman"),
    )
    parser.add_argument(
        "--ensure-pod-net",
        action="store_true",
        help="attempt to configure pod bridge/WireGuard from env",
    )
    parser.add_argument(
        "--pod-bridge",
        default=os.getenv("AE_POD_BRIDGE", "ae0"),
        help="bridge device for pod CIDR (used when --ensure-pod-net)",
    )
    parser.add_argument("--tls-cert", default=os.getenv("AE_AGENT_TLS_CERT"))
    parser.add_argument("--tls-key", default=os.getenv("AE_AGENT_TLS_KEY"))
    parser.add_argument("--client-ca", default=os.getenv("AE_AGENT_CLIENT_CA"))
    parser.add_argument(
        "--require-client-cert",
        action="store_true",
        default=os.getenv("AE_AGENT_REQUIRE_CLIENT_CERT", "0") == "1",
    )
    parser.add_argument("--controller-ca", default=os.getenv("AE_CONTROLLER_TLS_CA"))
    parser.add_argument("--controller-client-cert", default=os.getenv("AE_CONTROLLER_TLS_CERT"))
    parser.add_argument("--controller-client-key", default=os.getenv("AE_CONTROLLER_TLS_KEY"))
    args = parser.parse_args(argv)

    try:
        transport = TransportConfig.from_env()
        if transport.backend != "http":
            LOGGER.warning(
                "AE_TRANSPORT_BACKEND=%s configured; node agent still uses HTTP.",
                transport.backend,
            )
            if transport.nats_url:
                ok, detail = check_nats_connectivity(transport.nats_url)
                if ok:
                    LOGGER.info("nats connectivity ok (%s)", detail)
                else:
                    LOGGER.warning("nats connectivity failed (%s)", detail)
            else:
                LOGGER.warning("AE_NATS_URL not set; skipping nats connectivity check")
        else:
            LOGGER.info("transport backend=%s", transport.backend)
    except Exception:
        pass

    from ae.runtime import CRIRuntime, DockerRuntime, PodmanRuntime

    if args.runtime_backend in {"cri", "containerd"}:
        runtime = CRIRuntime()
    else:
        runtime = PodmanRuntime() if args.runtime_backend == "podman" else DockerRuntime()
    node_id = os.getenv("AE_NODE_ID", socket.gethostname())
    node_name = os.getenv("AE_NODE_NAME", node_id)
    node_labels = _parse_labels(os.getenv("AE_NODE_LABELS"))
    gpu_labels = _detect_gpu_labels()
    for k, v in gpu_labels.items():
        node_labels.setdefault(k, v)
    node_taints = []
    token = os.getenv("AE_AGENT_TOKEN")
    endpoint = args.advertise_endpoint or f"http://{socket.gethostname()}:{args.port}"
    pod_cidr = os.getenv("AE_POD_CIDR")
    wg_pubkey = os.getenv("AE_WG_PUBKEY")
    rp_pubkey = os.getenv("AE_ROSENPASS_PUBKEY") or os.getenv("AE_RP_PUBKEY")
    wg_config = os.getenv("AE_WG_CONFIG")
    wg_iface = os.getenv("AE_WG_INTERFACE", "wg0")
    if os.getenv("AE_ROSENPASS_ENABLED", "0") == "1":
        try:
            from pathlib import Path

            from ae.node.rosenpass import prepare_rosenpass

            base_dir = Path(os.getenv("AE_ROSENPASS_DIR", "/var/lib/ae/rosenpass"))
            cfg_raw = (os.getenv("AE_ROSENPASS_CONFIG") or "").strip()
            peers_from_controller = cfg_raw.lower() == "controller"
            cfg_path = None if peers_from_controller or not cfg_raw else Path(cfg_raw)
            backend = runtime.__class__.__name__.replace("Runtime", "").lower()
            refresh_raw = os.getenv("AE_ROSENPASS_PEER_REFRESH_SEC", "60")
            try:
                refresh_sec = float(refresh_raw)
            except Exception:
                refresh_sec = 60.0
            if refresh_sec < 0:
                refresh_sec = 0.0
            bootstrap_payload = {
                "node_id": node_id,
                "name": node_name,
                "backend": backend,
                "endpoint": endpoint,
                "labels": node_labels,
                "taints": node_taints,
                "status": "Ready",
                "pod_cidr": pod_cidr,
            }
            rp_runtime = prepare_rosenpass(
                config_path=cfg_path,
                base_dir=base_dir,
                controller_url=args.controller_url,
                token=token,
                node_id=node_id,
                pod_cidr=pod_cidr,
                peers_from_controller=peers_from_controller,
                bootstrap_payload=bootstrap_payload,
                peer_refresh_sec=refresh_sec if peers_from_controller else 0.0,
            )
            if rp_runtime:
                wg_iface = rp_runtime.interface or wg_iface
                if rp_runtime.wg_public_key:
                    wg_pubkey = rp_runtime.wg_public_key
                if rp_runtime.rp_public_key:
                    rp_pubkey = rp_runtime.rp_public_key
                if rp_runtime.wg_config:
                    try:
                        from ae.node.net_helper import apply_wireguard

                        apply_wireguard(rp_runtime.wg_config, iface=rp_runtime.interface)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning("wireguard apply failed: %s", exc)
                    wg_config = rp_runtime.wg_config
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("rosenpass setup failed: %s", exc)
    serve(
        runtime,
        host=args.host,
        port=args.port,
        controller_url=args.controller_url,
        node_id=node_id,
        node_name=node_name,
        node_labels=node_labels,
        node_taints=node_taints,
        heartbeat_interval=args.heartbeat_interval,
        node_endpoint=endpoint,
        token=token,
        pod_cidr=pod_cidr,
        wg_pubkey=wg_pubkey,
        rp_pubkey=rp_pubkey,
        ensure_pod_net=args.ensure_pod_net
        or bool(int(os.getenv("AE_AGENT_CONFIGURE_OVERLAY", "0"))),
        pod_bridge=args.pod_bridge,
        wg_iface=wg_iface,
        wg_config=wg_config,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        client_ca=args.client_ca,
        require_client_cert=args.require_client_cert,
        controller_ca=args.controller_ca,
        controller_client_cert=args.controller_client_cert,
        controller_client_key=args.controller_client_key,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
# ruff: noqa: E501,I001,F401,UP006,UP007,UP017,S104,S110,S112,SIM105,SIM108,UP041,S113
