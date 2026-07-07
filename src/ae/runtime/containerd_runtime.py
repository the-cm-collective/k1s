"""Direct containerd runtime adapter backed by nerdctl."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    app_key_for_manifest,
    runtime_labels_for_manifest,
    split_app_key,
)
from ae.runtime.command_args import kubernetes_command_parts

from .base import RuntimeResult
from .podman_runtime import PodmanRuntime, _RunResult
from .registry import RegistryAuthProvider

LOGGER = logging.getLogger(__name__)


def _prefer_direct_endpoint_default() -> bool:
    raw = os.getenv("AE_CONTAINERD_ENDPOINT_PREFER_DIRECT")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _normalize_registry_host(raw: str) -> str:
    value = (raw or "").strip().lower().strip("/")
    if not value:
        return ""
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
    return value.split("/", 1)[0]


def _registry_host_from_ref(ref: str) -> str:
    first = _normalize_registry_host(ref)
    if not first:
        return ""
    if first == "localhost" or "." in first or ":" in first:
        return first
    return ""


def _insecure_registry_hosts_from_env() -> set[str]:
    raw = (
        os.getenv("AE_CONTAINERD_INSECURE_REGISTRIES")
        or os.getenv("AE_NERDCTL_INSECURE_REGISTRIES")
        or ""
    )
    hosts: set[str] = set()
    for item in re.split(r"[\s,]+", raw):
        host = _normalize_registry_host(item)
        if host:
            hosts.add(host)
    return hosts


def _safe_path_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    return safe.strip(".-") or "default"


class ContainerdRuntime(PodmanRuntime):
    """Containerd-backed runtime adapter using nerdctl."""

    _NERDCTL_NAME_MAX = 120
    _SERVICE_PORT_RETRY_ATTEMPTS = 30
    _SERVICE_PORT_RETRY_DELAY_SECONDS = 0.5

    def __init__(
        self,
        *,
        registry_auth: RegistryAuthProvider | None = None,
        address: str | None = None,
        namespace: str | None = None,
        data_root: str | None = None,
        cni_path: str | None = None,
        cni_netconfpath: str | None = None,
    ) -> None:
        configured_bin = os.getenv("AE_NERDCTL_BIN", "nerdctl")
        self._bin = shutil.which(configured_bin) or configured_bin
        self._address = address or os.getenv("AE_CONTAINERD_ADDRESS") or os.getenv(
            "AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock"
        )
        self._namespace = namespace or os.getenv("AE_CONTAINERD_NAMESPACE", "ae")
        self._data_root = data_root or os.getenv("AE_CONTAINERD_DATA_ROOT", "/var/lib/ae/nerdctl")
        self._cni_path = cni_path or os.getenv("AE_CONTAINERD_CNI_BIN_DIR") or os.getenv(
            "CNI_PATH", "/opt/cni/bin"
        )
        self._cni_netconfpath = cni_netconfpath or os.getenv("AE_CONTAINERD_CNI_CONF_DIR") or os.getenv(
            "NETCONFPATH", "/etc/cni/net.d"
        )
        self._registry = registry_auth or RegistryAuthProvider()
        self._network_name = os.getenv("AE_CONTAINERD_NETWORK") or os.getenv(
            "AE_NETWORK_NAME", "ae-net"
        )
        self._prefer_direct_endpoint = _prefer_direct_endpoint_default()
        self._serial_service_rollout = os.getenv("AE_SERIAL_SERVICE_ROLLOUT", "0") == "1"
        self._podman_retry_max = 0
        self._podman_retry_delay = 0.0
        self._crun_path_re = re.compile(r"$^")
        raw = os.getenv("AE_OCI_RUNTIME", "").strip()
        self._oci_runtime = (
            raw if raw and all(ch.isalnum() or ch in ("-", "_", ".") for ch in raw) else None
        )
        self._apishim_state_checked = False
        self._apishim_state = None
        self._apishim_store_checked = False
        self._apishim_store = None
        self._volume_manager_checked = False
        self._volume_manager = None
        self._exec_procs: dict[str, subprocess.Popen[bytes]] = {}
        self._exec_exit_codes: dict[str, int] = {}
        self._gpu_preflight_ready = False
        self._insecure_registries = _insecure_registry_hosts_from_env()

    def _service_account_name_for_manifest(self, manifest: AppManifest) -> str | None:
        sa_name = getattr(manifest.spec, "service_account_name", None)
        if sa_name:
            return str(sa_name)
        store = self._get_apishim_store()
        if store is None:
            return None
        return self._service_account_name_from_store(manifest, store)

    def _service_account_token(self, namespace: str, name: str) -> str | None:
        store = self._get_apishim_store()
        if store is None:
            return None
        try:
            obj = store.get("", "v1", "serviceaccounts", namespace, name)
        except Exception:
            return None
        if obj is None:
            return None
        metadata = getattr(obj, "metadata", None) or {}
        annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
        if not isinstance(annotations, dict):
            return None
        token = str(annotations.get("ae.apishim/token") or "").strip()
        return token or None

    def _write_service_account_projection(
        self,
        *,
        namespace: str,
        service_account: str,
        token: str,
        revision: int,
    ) -> Path:
        root = Path(os.getenv("AE_PROJECTION_ROOT", "state/projections"))
        projection_dir = (
            root
            / "serviceaccounts"
            / _safe_path_segment(namespace)
            / _safe_path_segment(service_account)
            / f"rev{int(revision)}"
        )
        projection_dir.mkdir(parents=True, exist_ok=True)
        token_path = projection_dir / "token"
        token_path.write_text(token, encoding="utf-8")
        token_path.chmod(0o644)
        namespace_path = projection_dir / "namespace"
        namespace_path.write_text(namespace, encoding="utf-8")
        namespace_path.chmod(0o644)
        return projection_dir

    def _service_account_projection_args(
        self,
        manifest: AppManifest,
        revision: int,
    ) -> list[str]:
        service_account = self._service_account_name_for_manifest(manifest)
        if not service_account:
            return []
        namespace = getattr(getattr(manifest, "metadata", None), "namespace", None) or DEFAULT_NAMESPACE
        token = self._service_account_token(str(namespace), service_account)
        if not token:
            return []
        api_base = (
            os.getenv("AE_APISHIM_PUBLIC_BASE")
            or os.getenv("AE_APISHIM_SERVER")
            or ""
        ).strip()
        parsed = urlsplit(api_base)
        host = parsed.hostname or ""
        if not host:
            return []
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        projection_dir = self._write_service_account_projection(
            namespace=str(namespace),
            service_account=service_account,
            token=token,
            revision=revision,
        )
        args = [
            "-e",
            f"KUBERNETES_SERVICE_HOST={host}",
            "-e",
            f"KUBERNETES_SERVICE_PORT={port}",
            "-e",
            f"KUBERNETES_SERVICE_PORT_HTTPS={port}",
        ]
        alias_ip = str(os.getenv("AE_WORKLOAD_INGRESS_HOST_ALIAS") or "").strip()
        if alias_ip and host not in {"localhost", "127.0.0.1", "::1"}:
            args += ["--add-host", f"{host}:{alias_ip}"]
        args += [
            "-v",
            f"{projection_dir}:/var/run/secrets/kubernetes.io/serviceaccount:ro",
        ]
        return args

    def _nerdctl_safe_name(self, *parts: object, prefix: str = "ae") -> str:
        raw = "-".join(str(part or "").strip() for part in parts if str(part or "").strip())
        raw = raw or "item"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)
        safe = re.sub(r"[._-]+", "-", safe).strip("-._") or "item"
        max_body = max(1, self._NERDCTL_NAME_MAX - len(prefix) - 1)
        if safe == raw and len(safe) <= max_body:
            return f"{prefix}-{safe}"

        digest = hashlib.blake2s(raw.encode("utf-8"), digest_size=5).hexdigest()
        max_body = max(1, self._NERDCTL_NAME_MAX - len(prefix) - len(digest) - 2)
        body = safe[:max_body].strip("-._") or "item"
        return f"{prefix}-{body}-{digest}"

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
        self._validated_gpu_request_count(manifest)
        if self._gpu_requested(manifest):
            self._ensure_gpu_runtime_ready()
        self._ensure_image(manifest.spec.image, manifest=manifest)
        if not bool(getattr(manifest.spec, "host_network", False)):
            self._ensure_network()
        return super().ensure_app(
            manifest,
            revision,
            keep_old=keep_old,
            limit_create=limit_create,
            pod_names=pod_names,
            node_id=node_id,
        )

    def list_containers_info(self) -> list[dict]:
        out: list[dict] = []
        for container in self._inspect_all_containers():
            labels = (container.get("Config") or {}).get("Labels") or {}
            host_ports: list[int] = []
            port_map: dict[int, int] = {}
            host_ip = None
            restarts = 0
            started_at = None
            running = False
            pod_ip = None
            try:
                state = container.get("State") or {}
                running = str(state.get("Status") or "").lower() == "running"
                rc = state.get("RestartCount", 0)
                if isinstance(rc, int | float):
                    restarts = int(rc)
                started_at = state.get("StartedAt")
                net_settings = container.get("NetworkSettings") or {}
                pod_ip = str(net_settings.get("IPAddress") or "").strip() or None
                pmap = net_settings.get("Ports") or {}
                for key, binds in (pmap or {}).items():
                    if not binds:
                        continue
                    try:
                        cport = int(str(key).split("/", 1)[0])
                    except Exception:
                        continue
                    for binding in binds:
                        if not isinstance(binding, dict):
                            continue
                        hp = binding.get("HostPort")
                        if hp:
                            try:
                                parsed = int(hp)
                            except Exception:
                                continue
                            host_ports.append(parsed)
                            port_map.setdefault(cport, parsed)
                        hip = binding.get("HostIp") or binding.get("HostIP")
                        if hip and host_ip is None:
                            host_ip = hip
            except Exception:
                pass
            if host_ip is not None:
                host_ip = self._normalize_host_ip(host_ip)
            elif host_ports or port_map:
                host_ip = self._normalize_host_ip(host_ip)
            out.append(
                {
                    "name": str(container.get("Name", "")).lstrip("/")
                    or str(container.get("Id", "")),
                    "labels": labels,
                    "uid": container.get("Id"),
                    "host_ports": host_ports,
                    "port_map": port_map,
                    "host_ip": host_ip,
                    "restart_count": restarts,
                    "started_at": started_at,
                    "running": bool(running),
                    "pod_ip": pod_ip,
                }
            )
        return out

    def _create_container(
        self,
        manifest: AppManifest,
        replica_id: str,
        revision: int,
        *,
        service: tuple[int | None, int | None, list | None] = (None, None, None),
        node_id: str | None = None,
        attempt: int = 0,
    ) -> None:
        app = app_key_for_manifest(manifest)
        suffix = replica_id.split("-")[-1]
        name = self._nerdctl_safe_name(app, f"rev{revision}", suffix)
        if self._container_exists(name):
            self._stop_and_remove(name)

        is_job = str(getattr(manifest.spec, "workload", "service")).lower() == "job"
        labels = runtime_labels_for_manifest(manifest, app_name=app)
        labels.update(
            {
                self.POD_LABEL: replica_id,
                self.LEGACY_REPLICA_LABEL: replica_id,
                self.REVISION_LABEL: str(revision),
                self.CONTAINER_LABEL: "main",
            }
        )
        if is_job:
            labels[self.JOB_ATTEMPT_LABEL] = str(int(attempt))
        if node_id:
            labels["ae.node"] = str(node_id)
        try:
            stop_timeout = int(getattr(manifest.spec, "termination_grace_period_seconds", 10) or 10)
        except Exception:
            stop_timeout = 10
        labels["ae.stop_timeout"] = str(int(stop_timeout))

        cmd = [
            self._bin,
            "run",
            "-d",
            "--name",
            name,
            *sum([["--label", f"{k}={v}"] for k, v in labels.items()], []),
        ]
        if not is_job:
            cmd += ["--restart", "always"]
        network_name = str(self._network_name or "").strip()
        if bool(getattr(manifest.spec, "host_network", False)):
            cmd += ["--net", "host"]
        elif network_name:
            cmd += ["--net", network_name]
        if bool(getattr(manifest.spec, "host_pid", False)):
            cmd += ["--pid", "host"]
        if bool(getattr(manifest.spec, "host_ipc", False)):
            cmd += ["--ipc", "host"]

        runtime_name = self._runtime_name_for_manifest(manifest)
        if runtime_name:
            cmd += ["--runtime", runtime_name]
        if self._gpu_requested(manifest):
            cmd += ["--gpus", "all"]

        try:
            lims = getattr(getattr(manifest.spec, "resources", None), "limits", None)
            if lims is not None and getattr(lims, "memory", None) is not None:
                raw_mem = str(getattr(lims, "memory"))
                mem = str(self._parse_memory_bytes(raw_mem) or raw_mem)
                cmd += ["--memory", mem]
            reqs = getattr(getattr(manifest.spec, "resources", None), "requests", None)
            if reqs is not None:
                if getattr(reqs, "cpu", None) is not None:
                    try:
                        shares = max(2, int(float(reqs.cpu) * 1024))
                        cmd += ["--cpu-shares", str(shares)]
                    except Exception:
                        pass
                if getattr(reqs, "memory", None) is not None:
                    raw_mem = str(getattr(reqs, "memory"))
                    mem = str(self._parse_memory_bytes(raw_mem) or raw_mem)
                    cmd += ["--memory-reservation", mem]
        except Exception:
            pass

        for item in manifest.spec.env or []:
            if "name" in item and "value" in item:
                cmd += ["-e", f"{item['name']}={item['value']}"]
        cmd += self._service_account_projection_args(manifest, revision)
        cmd += self._host_alias_args(manifest)
        cmd += self._dns_args(manifest)

        svc_port, svc_target, svc_ports_list = service
        published_any = False
        reserved_ports: set[int] = set()
        if not bool(getattr(manifest.spec, "host_network", False)):
            if svc_ports_list:
                try:
                    by_name = {
                        p.name: int(p.container_port)
                        for p in (manifest.spec.ports or [])
                        if getattr(p, "name", None)
                    }
                except Exception:
                    by_name = {}
                try:
                    by_num = {int(p.container_port): int(p.container_port) for p in (manifest.spec.ports or [])}
                except Exception:
                    by_num = {}
                for sp in svc_ports_list or []:
                    try:
                        portnum = getattr(sp, "port", None)
                        tgt = getattr(sp, "target_port", None)
                        s_name = getattr(sp, "name", None)
                        if tgt is None:
                            tgt = by_name.get(s_name) or (
                                by_num.get(int(portnum)) if portnum is not None else None
                            )
                        if portnum is not None and tgt is not None:
                            chosen, used_preferred = self._choose_service_host_port(
                                int(portnum),
                                reserved_ports,
                            )
                            if chosen is None:
                                raise RuntimeError(
                                    f"service.port {portnum} for app {app} is unavailable"
                                )
                            if not used_preferred:
                                LOGGER.warning(
                                    "service port %s for app %s already in use; assigning %s",
                                    portnum,
                                    app,
                                    chosen,
                                )
                            cmd += ["-p", f"{int(chosen)}:{int(tgt)}"]
                            published_any = True
                    except Exception:
                        continue
            elif svc_port is not None:
                target = int(svc_target) if svc_target is not None else int(svc_port)
                chosen, used_preferred = self._choose_service_host_port(
                    int(svc_port),
                    reserved_ports,
                )
                if chosen is None:
                    raise RuntimeError(f"service.port {svc_port} for app {app} is unavailable")
                else:
                    if not used_preferred:
                        LOGGER.warning(
                            "service port %s for app %s already in use; assigning %s",
                            svc_port,
                            app,
                            chosen,
                        )
                    cmd += ["-p", f"{int(chosen)}:{target}"]
                    published_any = True
            else:
                for p in manifest.spec.ports or []:
                    try:
                        host = int(getattr(p, "hostPort", 0) or 0)
                        cport = int(
                            getattr(p, "container_port", 0) or getattr(p, "containerPort", 0) or 0
                        )
                        if host and cport:
                            cmd += ["-p", f"{host}:{cport}"]
                            published_any = True
                    except Exception:
                        continue

        if getattr(manifest.spec, "storage", None):
            self.ensure_storage_volumes(app, [s.model_dump() for s in manifest.spec.storage])
            for s in manifest.spec.storage:
                vol_name = self._storage_volume_name(app, s.name)
                mode = "ro" if getattr(s, "read_only", False) else "rw"
                cmd += ["-v", f"{vol_name}:{s.mount_path}:{mode}"]
        if manifest.spec.volumes:
            for v in manifest.spec.volumes:
                mode = "ro" if v.read_only else "rw"
                host = v.host_path
                if host and not os.path.isabs(host):
                    host = os.path.abspath(host)
                cmd += ["-v", f"{host}:{v.mount_path}:{mode}"]
        if getattr(manifest.spec, "volume_devices", None):
            for d in manifest.spec.volume_devices:
                host = d.host_path
                dev = d.device_path
                if host and not os.path.isabs(host):
                    host = os.path.abspath(host)
                mode = "r" if d.read_only else "rwm"
                cmd += ["--device", f"{host}:{dev}:{mode}"]

        sec = getattr(manifest.spec, "security", None)
        if sec is not None:
            if getattr(sec, "run_as_user", None) is not None:
                if getattr(sec, "run_as_group", None) is not None:
                    cmd += ["--user", f"{int(sec.run_as_user)}:{int(sec.run_as_group)}"]
                else:
                    cmd += ["--user", str(int(sec.run_as_user))]
            if bool(getattr(sec, "read_only_root", False)):
                cmd += ["--read-only"]
            for cap in list(getattr(sec, "drop_caps", []) or []):
                cmd += ["--cap-drop", str(cap)]
            try:
                s_type = getattr(sec, "seccomp_type", None)
                s_local = getattr(sec, "seccomp_localhost_profile", None)
                if s_type:
                    st = str(s_type)
                    if st == "Unconfined":
                        cmd += ["--security-opt", "seccomp=unconfined"]
                    elif st == "Localhost" and s_local:
                        cmd += ["--security-opt", f"seccomp={s_local}"]
                a_prof = getattr(sec, "apparmor_profile", None)
                if a_prof:
                    ap = str(a_prof)
                    if ap.startswith("localhost/"):
                        ap = ap.split("/", 1)[1]
                    if ap == "runtime/default":
                        ap = "nerdctl-default"
                    cmd += ["--security-opt", f"apparmor={ap}"]
            except Exception:
                pass

        if getattr(manifest.spec, "working_dir", None):
            cmd += ["--workdir", str(manifest.spec.working_dir)]

        entrypoint, runtime_args = kubernetes_command_parts(
            getattr(manifest.spec, "command", None),
            getattr(manifest.spec, "args", None),
        )
        if entrypoint:
            cmd += ["--entrypoint", entrypoint[0]]
        cmd += [self._runtime_image_ref(str(manifest.spec.image))]
        if runtime_args:
            cmd += runtime_args

        for run_attempt in range(2):
            try:
                self._run_ok(cmd)
                return
            except RuntimeError as exc:
                if run_attempt == 0 and self._is_containerd_name_conflict(str(exc)):
                    labels = self._container_labels(name)
                    if (
                        labels.get(self.APP_LABEL) == app
                        and labels.get(self.REVISION_LABEL) == str(revision)
                        and self._container_running(name)
                    ):
                        return
                    conflict_ids = self._containerd_conflict_ids(str(exc))
                    with suppress(Exception):
                        self._stop_and_remove(name)
                    for conflict_id in conflict_ids:
                        with suppress(Exception):
                            self._stop_and_remove(conflict_id)
                    time.sleep(0.2)
                    continue
                with suppress(Exception):
                    self._stop_and_remove(name)
                raise

    def _storage_volume_name(self, app_name: str, vol_name: str) -> str:
        return self._nerdctl_safe_name(app_name, vol_name)

    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:  # type: ignore[override]
        ns, base = split_app_key(app_name)
        std_labels = {
            "app": base,
            "app.kubernetes.io/name": base,
            "app.kubernetes.io/instance": base,
            "app.kubernetes.io/managed-by": "k1s",
            "ae.app": app_name,
            "ae.namespace": ns or DEFAULT_NAMESPACE,
        }
        for v in volumes or []:
            name = (v or {}).get("name")
            if not name:
                continue
            vol = self._storage_volume_name(app_name, str(name))
            if self._volume_exists(vol):
                continue
            labels = [f"{k}={value}" for k, value in std_labels.items()]
            labels.append(f"ae.volume={name}")
            node_label = getattr(self, "_current_node_id", None)
            if node_label:
                labels.append(f"ae.node={node_label}")
            res = self._run_ok(
                [
                    self._bin,
                    "volume",
                    "create",
                    *sum([["--label", lbl] for lbl in labels], []),
                    vol,
                ],
                allow_fail=True,
            )
            if res.code == 0 or self._volume_exists(vol):
                continue
            detail = (res.err or res.out or "").strip()
            lowered = detail.lower()
            if "file exists" in lowered or "already exists" in lowered:
                LOGGER.warning(
                    "containerd volume %s exists on disk but is not listed by nerdctl; reusing it",
                    vol,
                )
                continue
            raise RuntimeError(f"nerdctl failed: volume create {vol} => {detail}")

    def list_storage_volumes(self, app_name: str | None = None) -> list[dict]:  # type: ignore[override]
        out: list[dict] = []
        r = self._run_ok([self._bin, "volume", "ls", "--format", "json"], allow_fail=True)
        for it in self._parse_nerdctl_json_items(r.out):
            labels = it.get("Labels") or {}
            app = labels.get(self.APP_LABEL) if isinstance(labels, dict) else None
            if app_name is not None:
                if app != app_name:
                    continue
            elif not app:
                continue
            out.append(
                {
                    "name": it.get("Name", ""),
                    "labels": labels if isinstance(labels, dict) else {},
                    "driver": it.get("Driver", ""),
                    "mountpoint": it.get("Mountpoint", ""),
                }
            )
        return out

    def _volume_exists(self, name: str) -> bool:
        if self._run_ok([self._bin, "volume", "inspect", name], allow_fail=True).code == 0:
            return True
        ls = self._run_ok([self._bin, "volume", "ls", "--format", "json"], allow_fail=True)
        return any(
            str(item.get("Name") or "") == name
            for item in self._parse_nerdctl_json_items(ls.out)
        )

    def _parse_nerdctl_json_items(self, text: str) -> list[dict]:
        raw = (text or "").strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except Exception:
            items: list[dict] = []
            for line in raw.splitlines():
                item = None
                with suppress(Exception):
                    item = json.loads(line)
                if isinstance(item, dict):
                    items.append(item)
            return items
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            volumes = payload.get("Volumes")
            if isinstance(volumes, list):
                return [item for item in volumes if isinstance(item, dict)]
            return [payload]
        return []

    def _runtime_image_ref(self, image: str) -> str:
        if "/" not in image and not self._image_ref_exists_exact(image):
            local = f"localhost/{image}"
            if self._image_ref_exists_exact(local):
                return local
        return image

    def _image_ref_exists_exact(self, name: str) -> bool:
        return self._run_ok([self._bin, "image", "inspect", name], allow_fail=True).code == 0

    def _list_app_containers(self, app: str) -> list[dict]:
        ids = self._list_container_ids(label_filters=[f"{self.APP_LABEL}={app}"])
        return self._inspect_many(ids)

    def _find_by_label(self, key: str, value: str) -> str | None:
        ids = self._list_container_ids(label_filters=[f"{key}={value}"])
        if not ids and key == self.POD_LABEL:
            ids = self._list_container_ids(label_filters=[f"{self.LEGACY_REPLICA_LABEL}={value}"])
        return ids[0] if ids else None

    def _image_exists(self, name: str) -> bool:
        if self._run_ok([self._bin, "image", "inspect", name], allow_fail=True).code == 0:
            return True
        if "/" not in str(name):
            return self._run_ok(
                [self._bin, "image", "inspect", f"localhost/{name}"],
                allow_fail=True,
            ).code == 0
        return False

    def _ensure_network(self) -> None:
        network_name = str(self._network_name or "").strip()
        if not network_name or network_name in {"host", "none", "bridge"}:
            return
        exists = self._run_ok(
            [self._bin, "network", "inspect", network_name],
            allow_fail=True,
        )
        if exists.code == 0:
            return
        argv = [self._bin, "network", "create", network_name]
        subnet = os.getenv("AE_CONTAINERD_NETWORK_SUBNET") or os.getenv("AE_NETWORK_SUBNET")
        if subnet:
            argv = [self._bin, "network", "create", "--subnet", subnet, network_name]
        self._run_ok(argv, allow_fail=False)

    def _container_exists(self, name: str) -> bool:
        return self._run_ok([self._bin, "inspect", name], allow_fail=True).code == 0

    def _container_labels(self, name_or_id: str) -> dict[str, str]:
        inspected = self._inspect_many([name_or_id])
        if not inspected:
            return {}
        labels = ((inspected[0].get("Config") or {}).get("Labels") or {})
        if not isinstance(labels, dict):
            return {}
        return {str(key): str(value) for key, value in labels.items()}

    def _container_running(self, name_or_id: str) -> bool:
        inspected = self._inspect_many([name_or_id])
        if not inspected:
            return False
        state = inspected[0].get("State") or {}
        if not isinstance(state, dict):
            return False
        return str(state.get("Status") or "").lower() == "running" or bool(state.get("Running"))

    def _containerd_conflict_ids(self, stderr: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r'used by ID "([^"]+)"', stderr or "")))

    def _is_containerd_name_conflict(self, stderr: str) -> bool:
        lowered = (stderr or "").lower()
        return (
            "name" in lowered
            and (
                "name-store error" in lowered
                or "already used by id" in lowered
                or "already in use" in lowered
                or "conflict" in lowered
            )
        )

    def _list_container_ids(self, *, label_filters: list[str] | None = None) -> list[str]:
        argv = [self._bin, "ps", "-a", "-q"]
        for item in label_filters or []:
            argv += ["--filter", f"label={item}"]
        res = self._run_ok(argv, allow_fail=True)
        return [line.strip() for line in (res.out or "").splitlines() if line.strip()]

    def _inspect_many(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        res = self._run_ok([self._bin, "inspect", *ids], allow_fail=True)
        try:
            payload = json.loads(res.out or "[]")
        except Exception:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            return [payload]
        return []

    def _inspect_all_containers(self) -> list[dict]:
        return self._inspect_many(self._list_container_ids())

    def _runtime_name_for_manifest(self, manifest: AppManifest) -> str | None:
        runtime_name = self._requested_runtime_class_name(manifest)
        if runtime_name:
            if runtime_name.lower() == "nvidia":
                configured = str(
                    os.getenv("AE_NVIDIA_CONTAINER_RUNTIME_BIN", "nvidia-container-runtime") or ""
                ).strip()
                return configured or runtime_name
            return runtime_name
        if self._oci_runtime:
            return str(self._oci_runtime)
        return None

    def _requested_runtime_class_name(self, manifest: AppManifest) -> str | None:
        runtime_name = getattr(manifest.spec, "runtime_class_name", None)
        if runtime_name is None:
            return None
        value = str(runtime_name).strip()
        return value or None

    def _validated_gpu_request_count(self, manifest: AppManifest) -> int:
        runtime_name = str(self._requested_runtime_class_name(manifest) or "").strip().lower()
        requested = self._resource_quantity_value(manifest, "requests", "nvidia.com/gpu")
        limited = self._resource_quantity_value(manifest, "limits", "nvidia.com/gpu")
        if requested is None and limited is None:
            if runtime_name == "nvidia":
                raise RuntimeError(
                    "direct-containerd GPU lane requires matching requests/limits for nvidia.com/gpu when runtimeClassName=nvidia"
                )
            return 0
        if runtime_name != "nvidia":
            raise RuntimeError(
                "direct-containerd GPU lane requires runtimeClassName=nvidia when nvidia.com/gpu is requested"
            )
        if requested is None or limited is None:
            raise RuntimeError(
                "direct-containerd GPU lane requires nvidia.com/gpu to be set in both requests and limits"
            )
        request_count = self._parse_gpu_quantity(requested, field="requests")
        limit_count = self._parse_gpu_quantity(limited, field="limits")
        if request_count != limit_count:
            raise RuntimeError(
                "direct-containerd GPU lane requires matching requests/limits for nvidia.com/gpu"
            )
        if request_count != 1:
            raise RuntimeError(
                f"direct-containerd GPU lane currently supports exactly nvidia.com/gpu=1; got {request_count}"
            )
        return 1

    def _resource_quantity_value(
        self,
        manifest: AppManifest,
        field: str,
        resource_name: str,
    ) -> object | None:
        resources = getattr(manifest.spec, "resources", None)
        raw = getattr(resources, field, None) if resources is not None else None
        if raw is None:
            return None
        quantity_map = getattr(raw, "quantity_map", None)
        data = quantity_map() if callable(quantity_map) else None
        if not isinstance(data, dict):
            return None
        return data.get(resource_name)

    def _parse_gpu_quantity(self, value: object, *, field: str) -> int:
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"direct-containerd GPU lane requires integer nvidia.com/gpu values; got {value!r} in {field}"
            ) from exc
        if parsed != parsed.to_integral_value():
            raise RuntimeError(
                f"direct-containerd GPU lane requires integer nvidia.com/gpu values; got {value!r} in {field}"
            )
        count = int(parsed)
        if count <= 0:
            raise RuntimeError(
                f"direct-containerd GPU lane requires positive nvidia.com/gpu values; got {value!r} in {field}"
            )
        return count

    def _ensure_gpu_runtime_ready(self) -> None:
        if self._gpu_preflight_ready:
            return
        toolkit_dir = str(
            os.getenv("AE_NVIDIA_TOOLKIT_DIR", "/usr/local/nvidia/toolkit") or ""
        ).strip()
        if toolkit_dir and os.path.isdir(toolkit_dir):
            self._prepend_env_path("PATH", toolkit_dir)
            self._prepend_env_path("LD_LIBRARY_PATH", toolkit_dir)
        config_dir = str(
            os.getenv("AE_NVIDIA_RUNTIME_CONFIG_DIR", "/etc/nvidia-container-runtime") or ""
        ).strip()
        if config_dir and not os.path.isdir(config_dir):
            raise RuntimeError(
                f"direct-containerd GPU lane requires NVIDIA runtime config directory {config_dir}"
            )
        checks = [
            (
                "nvidia-container-cli",
                str(os.getenv("AE_NVIDIA_CONTAINER_CLI_BIN", "nvidia-container-cli") or "").strip()
                or "nvidia-container-cli",
                ["--version"],
            ),
            (
                "nvidia-container-runtime",
                str(
                    os.getenv("AE_NVIDIA_CONTAINER_RUNTIME_BIN", "nvidia-container-runtime") or ""
                ).strip()
                or "nvidia-container-runtime",
                ["--version"],
            ),
            (
                "nvidia-smi",
                str(os.getenv("AE_NVIDIA_SMI_BIN", "nvidia-smi") or "").strip() or "nvidia-smi",
                ["-L"],
            ),
        ]
        for label, command, args in checks:
            resolved = self._resolve_gpu_command(command)
            try:
                cp = subprocess.run(
                    [resolved, *args],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"direct-containerd GPU lane requires {label} to be present in the node runtime environment"
                ) from exc
            if cp.returncode != 0:
                detail = (cp.stderr or cp.stdout or "").strip()
                raise RuntimeError(
                    f"direct-containerd GPU preflight failed for {label}: {detail or f'exit {cp.returncode}'}"
                )
        self._gpu_preflight_ready = True

    def _prepend_env_path(self, key: str, prefix: str) -> None:
        if not prefix:
            return
        current = str(os.getenv(key, "") or "")
        parts = [item for item in current.split(":") if item]
        if prefix in parts:
            return
        os.environ[key] = ":".join([prefix, *parts]) if parts else prefix

    def _resolve_gpu_command(self, command: str) -> str:
        if os.path.isabs(command):
            if os.access(command, os.X_OK):
                return command
            raise RuntimeError(
                f"direct-containerd GPU lane requires executable command at {command}"
            )
        resolved = shutil.which(command)
        if resolved:
            return resolved
        raise RuntimeError(
            f"direct-containerd GPU lane requires {command} to be present in PATH"
        )

    def _gpu_requested(self, manifest: AppManifest) -> bool:
        return self._resource_quantity_value(manifest, "requests", "nvidia.com/gpu") is not None or (
            self._resource_quantity_value(manifest, "limits", "nvidia.com/gpu") is not None
        )

    def _choose_host_port(
        self,
        preferred: int,
        reserved_ports: set[int],
        *,
        allow_fallback: bool = True,
    ) -> tuple[int | None, bool]:
        from ae.runtime.ports import choose_host_port

        return choose_host_port(
            preferred,
            reserved=reserved_ports,
            allow_fallback=allow_fallback,
        )

    def _choose_service_host_port(
        self,
        preferred: int,
        reserved_ports: set[int],
    ) -> tuple[int | None, bool]:
        attempts = max(1, int(self._SERVICE_PORT_RETRY_ATTEMPTS))
        for attempt in range(attempts):
            chosen, used_preferred = self._choose_host_port(
                preferred,
                reserved_ports,
                allow_fallback=False,
            )
            if chosen is not None:
                return chosen, used_preferred
            if attempt + 1 < attempts:
                time.sleep(float(self._SERVICE_PORT_RETRY_DELAY_SECONDS))
        return None, False

    def _global_args(self) -> list[str]:
        args = [self._bin, "--address", self._address, "--namespace", self._namespace]
        if self._data_root:
            args += ["--data-root", self._data_root]
        if self._cni_path:
            args += ["--cni-path", self._cni_path]
        if self._cni_netconfpath:
            args += ["--cni-netconfpath", self._cni_netconfpath]
        return args

    def _runtime_cmd(self, cmd: list[str]) -> list[str]:
        subcommand = cmd[1:] if cmd and cmd[0] == self._bin else cmd
        global_args = self._global_args()
        if self._uses_insecure_registry(subcommand):
            global_args.append("--insecure-registry")
        if cmd and cmd[0] == self._bin:
            return [*global_args, *cmd[1:]]
        return [*global_args, *cmd]

    def _uses_insecure_registry(self, argv: list[str]) -> bool:
        if not self._insecure_registries or not argv:
            return False
        command = argv[0]
        if command not in {"login", "pull", "push"}:
            return False
        target = next((item for item in reversed(argv[1:]) if item and not item.startswith("-")), "")
        host = _registry_host_from_ref(target)
        return "*" in self._insecure_registries or bool(host and host in self._insecure_registries)

    def _run_ok(self, argv: list[str], *, allow_fail: bool = False) -> _RunResult:
        cmd = self._runtime_cmd(argv)
        try:
            cp = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "nerdctl binary not found. Install nerdctl or set AE_NERDCTL_BIN"
            ) from exc
        if cp.returncode == 0 or allow_fail:
            return _RunResult(cp.returncode, cp.stdout or "", cp.stderr or "")
        raise RuntimeError(f"nerdctl failed: {' '.join(cmd)} => {(cp.stderr or '').strip()}")
