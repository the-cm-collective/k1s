"""Podman-backed runtime adapter using OCI runtimes via Podman CLI.

This avoids the Docker daemon and talks to the system's OCI runtime through Podman.
It implements the same labels and behaviors used by DockerRuntime so the rest
of the system (ingress, status, events) continues to work unchanged.
"""

# ruff: noqa: E501,S110,S112,S603,S607,SIM105,SIM118,UP022,UP028
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    app_key_for_manifest,
    runtime_labels_for_manifest,
    split_app_key,
)

from .base import PodState, RuntimeAdapter, RuntimeResult
from .ports import choose_host_port
from .projections import ensure_k8s_volume_projections
from .registry import RegistryAuthProvider


@dataclass
class _RunResult:
    code: int
    out: str
    err: str


LOGGER = logging.getLogger(__name__)


class PodmanRuntime(RuntimeAdapter):
    APP_LABEL = "ae.app"
    POD_LABEL = "ae.pod_name"
    LEGACY_REPLICA_LABEL = "ae.replica_id"
    REPLICA_LABEL = POD_LABEL
    REVISION_LABEL = "ae.revision"
    CONTAINER_LABEL = "ae.container"
    JOB_ATTEMPT_LABEL = "ae.job_attempt"
    POD_SANDBOX_LABEL = "pod-sandbox"

    def __init__(self) -> None:
        configured_bin = os.getenv("AE_PODMAN_BIN", "podman")
        self._bin = shutil.which(configured_bin) or configured_bin
        # Optional shared network for ingress to reach containers by DNS name
        self._network_name = os.getenv("AE_PODMAN_NETWORK") or os.getenv("AE_NETWORK_NAME")
        self._serial_service_rollout = os.getenv("AE_SERIAL_SERVICE_ROLLOUT", "0") == "1"
        self._podman_retry_max = max(0, int(os.getenv("AE_PODMAN_RETRY_MAX", "2")))
        try:
            self._podman_retry_delay = float(os.getenv("AE_PODMAN_RETRY_DELAY", "0.5"))
        except Exception:
            self._podman_retry_delay = 0.5
        self._crun_path_re = re.compile(r"(/run/user/\d+/crun/[A-Za-z0-9_.-]+)")
        # Optional explicit OCI runtime override (e.g., "crun" or "runc").
        # When set, the adapter passes "--runtime=<value>" to all `podman run` calls.
        raw = os.getenv("AE_OCI_RUNTIME", "").strip()
        # Guard against injection: allow alnum, dash, underscore
        self._oci_runtime = (
            raw if raw and all(ch.isalnum() or ch in ("-", "_") for ch in raw) else None
        )
        self._registry = RegistryAuthProvider()
        self._apishim_state_checked = False
        self._apishim_state = None
        self._apishim_store_checked = False
        self._apishim_store = None
        self._volume_manager_checked = False
        self._volume_manager = None

    def _maybe_inject_runtime(self, argv: list[str]) -> None:
        """Inject --runtime into a `podman run` argv in-place when AE_OCI_RUNTIME is set."""
        if not getattr(self, "_oci_runtime", None):
            return
        try:
            idx = argv.index("run")
        except ValueError:
            return
        if "--runtime" not in argv:
            argv[idx + 1 : idx + 1] = ["--runtime", str(self._oci_runtime)]

    # Core ops ---------------------------------------------------------
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
        app = app_key_for_manifest(manifest)
        desired_ids = (
            list(pod_names)
            if pod_names is not None
            else [f"{app}-rev{revision}-{i}" for i in range(manifest.spec.replicas)]
        )
        self._current_node_id = node_id
        manifest = ensure_k8s_volume_projections(
            manifest, revision, state=self._get_apishim_state(), logger=LOGGER
        )
        host_network = bool(getattr(manifest.spec, "host_network", False))
        is_job = str(getattr(manifest.spec, "workload", "service")).lower() == "job"
        job_backoff_limit = None
        if is_job:
            try:
                raw_limit = getattr(manifest.spec, "job_backoff_limit", None)
                job_backoff_limit = int(raw_limit) if raw_limit is not None else 6
            except Exception:
                job_backoff_limit = 6

        # Find existing containers for this app
        existing = self._list_app_containers(app)

        def _labels(obj: dict) -> dict:
            labels = (obj.get("Config") or {}).get("Labels") or {}
            if self.POD_LABEL not in labels and self.LEGACY_REPLICA_LABEL in labels:
                labels = {**labels, self.POD_LABEL: labels[self.LEGACY_REPLICA_LABEL]}
            return labels

        by_replica: dict[str, dict] = {}
        old: list[dict] = []
        for c in existing:
            labs = _labels(c)
            if not labs:
                continue
            if labs.get(self.REVISION_LABEL) != str(revision):
                old.append(c)
                continue
            if labs.get(self.CONTAINER_LABEL) == self.POD_SANDBOX_LABEL:
                continue
            rid = labs.get(self.POD_LABEL)
            if rid:
                by_replica[rid] = c

        created = updated = removed = 0

        # Only pull when we need to create a pod (k8s semantics).
        image = manifest.spec.image
        if any(rid not in by_replica for rid in desired_ids):
            self._ensure_image(image, manifest=manifest)

        svc_port = None
        svc_target = None
        svc_ports_list = None
        if getattr(manifest.spec, "service", None) and manifest.spec.replicas == 1:
            svc_port = getattr(manifest.spec.service, "port", None)
            svc_target = getattr(manifest.spec.service, "target_port", None)
            if getattr(manifest.spec.service, "ports", None):
                svc_ports_list = list(manifest.spec.service.ports)

        strict_service = self._serial_service_rollout and not keep_old and svc_port is not None
        if strict_service and old:
            for c in list(old):
                self._stop_and_remove(c.get("Id", ""))
                removed += 1
            old = []

        for rid in desired_ids:
            rep_manifest = self._maybe_inject_pvc_mounts(
                manifest, node_id=node_id, replica_id=rid
            )
            c = by_replica.get(rid)
            if c is None:
                if limit_create is not None and created >= int(limit_create):
                    continue
                self._create_container(
                    rep_manifest,
                    rid,
                    revision,
                    service=(svc_port, svc_target, svc_ports_list),
                    node_id=node_id,
                )
                # If a shared network is configured, connect the new container to it
                if self._network_name and not host_network:
                    cid = self._find_by_label(self.REPLICA_LABEL, rid)
                    if cid:
                        aliases = [
                            f"ae-{app}",
                            f"ae-{app}-rev{revision}",
                            f"ae-{app}-rep-{rid.split('-')[-1]}",
                        ]
                        for al in aliases:
                            self._run_ok(
                                [
                                    self._bin,
                                    "network",
                                    "connect",
                                    "--alias",
                                    al,
                                    self._network_name,
                                    cid,
                                ],
                                allow_fail=True,
                            )
                created += 1
            else:
                # Ensure running with proper handling of Podman states
                st = (c.get("State") or {}).get("Status") or ""
                cid = c.get("Id", "")
                restart_handled = False
                if is_job and st != "running":
                    exit_code = (c.get("State") or {}).get("ExitCode", None)
                    try:
                        exit_code = int(exit_code) if exit_code is not None else None
                    except Exception:
                        exit_code = None
                    attempt = 0
                    try:
                        attempt = int(_labels(c).get(self.JOB_ATTEMPT_LABEL, 0))
                    except Exception:
                        attempt = 0
                    if exit_code == 0:
                        restart_handled = True
                    elif exit_code is not None:
                        if job_backoff_limit is not None and attempt >= job_backoff_limit:
                            restart_handled = True
                        else:
                            # Retry by recreating the container with incremented attempt
                            self._stop_and_remove(cid)
                            self._create_container(
                                rep_manifest,
                                rid,
                                revision,
                                service=(svc_port, svc_target, svc_ports_list),
                                node_id=node_id,
                                attempt=attempt + 1,
                            )
                            if self._network_name and not host_network:
                                nid = self._find_by_label(self.REPLICA_LABEL, rid)
                                if nid:
                                    aliases = [
                                        f"ae-{app}",
                                        f"ae-{app}-rev{revision}",
                                        f"ae-{app}-rep-{rid.split('-')[-1]}",
                                    ]
                                    for al in aliases:
                                        self._run_ok(
                                            [
                                                self._bin,
                                                "network",
                                                "connect",
                                                "--alias",
                                                al,
                                                self._network_name,
                                                nid,
                                            ],
                                            allow_fail=True,
                                        )
                            updated += 1
                            restart_handled = True
                if st != "running" and not restart_handled:
                    # Unpause if needed
                    if st == "paused":
                        self._run_ok([self._bin, "unpause", cid], allow_fail=True)
                        updated += 1
                    # Initialize if container is in 'configured' state (Podman specific)
                    elif st == "configured":
                        self._run_ok([self._bin, "container", "init", cid], allow_fail=True)
                        self._run_ok([self._bin, "start", cid], allow_fail=True)
                        updated += 1
                    else:
                        # created/exited/stopped → start
                        self._run_ok([self._bin, "start", cid], allow_fail=True)
                        updated += 1

            # Ensure sidecars for this replica when declared
            try:
                self._ensure_sidecars(rep_manifest, rid, revision)
            except Exception:
                pass

        if not keep_old:
            for c in old:
                self._stop_and_remove(c.get("Id", ""))
                removed += 1

        # Compose replica states
        final = self._list_app_containers(app)
        states: list[PodState] = []
        # Preferred container port for readiness
        preferred_port: int | None = None
        try:
            if manifest.spec.health and manifest.spec.health.readiness:
                r = manifest.spec.health.readiness
                if getattr(r, "http_get", None) is not None:
                    preferred_port = int(r.http_get.port)
                elif getattr(r, "tcp_socket", None) is not None:
                    preferred_port = int(r.tcp_socket.port)
        except Exception:
            preferred_port = None

        for c in final:
            labs = (c.get("Config") or {}).get("Labels") or {}
            if labs.get(self.REVISION_LABEL) != str(revision):
                continue
            if labs.get(self.CONTAINER_LABEL) == self.POD_SANDBOX_LABEL:
                continue
            rid = labs.get(self.POD_LABEL) or labs.get(self.LEGACY_REPLICA_LABEL) or ""
            state = c.get("State") or {}
            st = state.get("Status", "")
            started = self._parse_dt(state.get("StartedAt"))
            exit_code = None
            try:
                raw_exit = state.get("ExitCode", None)
                exit_code = int(raw_exit) if raw_exit is not None else None
            except Exception:
                exit_code = None
            finished_at = self._parse_dt(state.get("FinishedAt"))
            endpoint = None
            if host_network:
                endpoint = self._endpoint_for_host_network(manifest, preferred_port)
            else:
                # Prefer published host ports (so Caddy can reach via host alias).
                # Selection order: preferred probe port; 80/tcp; 8080/tcp; first non‑443 mapping.
                try:
                    pmap = (c.get("NetworkSettings") or {}).get("Ports") or {}
                    # Prefer host-published ports
                    # 1) check preferred container port first
                    if preferred_port is not None:
                        binds = (pmap or {}).get(f"{int(preferred_port)}/tcp")
                        if binds:
                            b0 = binds[0] or {}
                            hp = b0.get("HostPort")
                            if hp:
                                hip = (b0.get("HostIp") or "").strip()
                                endpoint = f"{self._normalize_host_ip(hip)}:{hp}"
                    # 2) common HTTP ports
                    if endpoint is None:
                        for cp in (80, 8080):
                            binds = (pmap or {}).get(f"{int(cp)}/tcp")
                            if binds:
                                b0 = binds[0] or {}
                                hp = b0.get("HostPort")
                                if hp:
                                    hip = (b0.get("HostIp") or "").strip()
                                    endpoint = f"{self._normalize_host_ip(hip)}:{hp}"
                                    break
                    # 3) otherwise pick the first published host port that is not 443
                    if endpoint is None:
                        for k, binds in (pmap or {}).items():
                            if not binds:
                                continue
                            # Skip HTTPS container port to avoid http-over-https probe mismatch
                            port_key = str(k).split("/")[0]
                            if port_key.isdigit() and int(port_key) == 443:
                                continue
                            b0 = binds[0] or {}
                            hp = b0.get("HostPort")
                            if hp:
                                hip = (b0.get("HostIp") or "").strip()
                                endpoint = f"{self._normalize_host_ip(hip)}:{hp}"
                                break
                    if endpoint is None:
                        # Fallback to `podman port <id>` which reliably reports published mappings
                        cid = c.get("Id") or ""
                        if cid:
                            pr = self._run_ok([self._bin, "port", cid], allow_fail=True)
                            # Expected lines like: "8080/tcp -> 0.0.0.0:49213" or "8080/tcp -> [::]:49213"
                            for line in (pr.out or "").splitlines():
                                try:
                                    _lhs, _arrow, rhs = line.partition("->")
                                    host = rhs.strip()
                                    if not host:
                                        continue
                                    hip = ""
                                    hp = ""
                                    if host.startswith("["):
                                        end = host.find("]:")
                                        if end != -1:
                                            hip = host[1:end]
                                            hp = host[end + 2 :]
                                    else:
                                        parts = host.rsplit(":", 1)
                                        if len(parts) == 2:
                                            hip, hp = parts
                                    hip = hip.strip()
                                    hp = hp.strip()
                                    if hp.isdigit():
                                        endpoint = f"{self._normalize_host_ip(hip)}:{hp}"
                                        break
                                except Exception:
                                    continue
                    if endpoint is None and pmap:
                        # Last resort: container DNS name (only usable from other containers on the network)
                        for k in (pmap or {}).keys():
                            port = k.split("/")[0]
                            if port:
                                endpoint = f"{(c.get('Name', '').lstrip('/'))}:{port}"
                                break
                    if endpoint is None and self._network_name:
                        try:
                            nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
                            netinfo = nets.get(self._network_name) or {}
                            ipaddr = netinfo.get("IPAddress")
                            if ipaddr:
                                p = preferred_port or 0
                                if p == 0 and manifest.spec.ports:
                                    p = int(getattr(manifest.spec.ports[0], "container_port", 0))
                                if p:
                                    endpoint = f"{ipaddr}:{p}"
                        except Exception:
                            pass
                except Exception:
                    pass
            ready = st == "running"
            if is_job:
                ready = False if st == "running" else exit_code == 0
            states.append(
                PodState(
                    pod_name=rid,
                    ready=ready,
                    status=st or "",
                    endpoint=endpoint,
                    started_at=started,
                    exit_code=exit_code,
                    finished_at=finished_at,
                )
            )

        return RuntimeResult(
            revision=revision,
            created=created,
            updated=updated,
            removed=removed,
            pod_states=states,
        )

    def _ensure_sidecars(self, manifest: AppManifest, replica_id: str, revision: int) -> None:
        if not getattr(manifest.spec, "containers", None):
            return
        app = app_key_for_manifest(manifest)
        # Determine projection host root from manifest.spec.volumes
        proj_host_root = None
        try:
            for v in getattr(manifest.spec, "volumes", []) or []:
                try:
                    mpath = (
                        getattr(v, "mount_path", None)
                        if not isinstance(v, dict)
                        else v.get("mountPath")
                    )
                    hpath = (
                        getattr(v, "host_path", None)
                        if not isinstance(v, dict)
                        else v.get("hostPath")
                    )
                    if mpath and str(mpath).startswith(f"/var/run/ae/config/{app}") and hpath:
                        if hpath and not os.path.isabs(hpath):
                            hpath = os.path.abspath(hpath)
                        proj_host_root = hpath
                        break
                except Exception:
                    continue
        except Exception:
            pass
        # For each declared sidecar, create if missing for this replica
        for csp in manifest.spec.containers or []:
            try:
                cname = str(getattr(csp, "name", "") or "").strip()
                if not cname:
                    continue
                name_suffix = replica_id.split("-")[-1]
                full_name = f"ae-{app}-rev{revision}-{name_suffix}-{cname}"
                # Locate by labels
                r = self._run_ok(
                    [
                        self._bin,
                        "ps",
                        "-a",
                        "--filter",
                        f"label={self.APP_LABEL}={app}",
                        "--filter",
                        f"label={self.REPLICA_LABEL}={replica_id}",
                        "--filter",
                        f"label={self.REVISION_LABEL}={revision}",
                        "--filter",
                        f"label={self.CONTAINER_LABEL}={cname}",
                        "--format",
                        "{{.ID}}",
                    ],
                    allow_fail=True,
                )
                cid = (r.out or "").strip()
                if not cid:
                    # Build run args
                    img = getattr(csp, "image", None)  # noqa: B009
                    if not img:
                        continue
                    self._ensure_image(str(img), manifest=manifest, spec=csp)
                    labels = runtime_labels_for_manifest(manifest, app_name=app)
                    labels.update(
                        {
                            self.POD_LABEL: replica_id,
                            self.LEGACY_REPLICA_LABEL: replica_id,
                            self.REVISION_LABEL: str(revision),
                            self.CONTAINER_LABEL: cname,
                        }
                    )
                    cmd = [
                        self._bin,
                        "run",
                        "-d",
                        "--name",
                        full_name,
                        *sum(
                            [["--label", f"{k}={v}"] for k, v in labels.items()],
                            [],
                        ),
                        "--restart",
                        "unless-stopped",
                    ]
                    # Volumes from manifest
                    if getattr(manifest.spec, "storage", None):
                        for s in manifest.spec.storage:
                            vol_name = self._storage_volume_name(app, getattr(s, "name", ""))
                            mode = "ro" if getattr(s, "read_only", False) else "rw"
                            cmd += ["-v", f"{vol_name}:{getattr(s, 'mount_path', '')}:{mode}"]
                    if getattr(manifest.spec, "volumes", None):
                        for v in manifest.spec.volumes:
                            host = getattr(v, "host_path", None)
                            mnt = getattr(v, "mount_path", None)
                            ro = bool(getattr(v, "read_only", False))
                            if host and mnt:
                                if host and not os.path.isabs(host):
                                    host = os.path.abspath(host)
                                cmd += ["-v", f"{host}:{mnt}:{'ro' if ro else 'rw'}"]
                    host_vols = None
                    if isinstance(csp, dict):
                        if "volumeMounts" in csp or "volume_mounts" in csp:
                            host_vols = csp.get("volumeMounts") or csp.get("volume_mounts") or []
                    else:
                        fields = getattr(csp, "__pydantic_fields_set__", None)
                        if fields and ("volume_mounts" in fields or "volumeMounts" in fields):
                            host_vols = getattr(csp, "volume_mounts", []) or []
                    if host_vols is None:
                        host_vols = []
                    for v in host_vols or []:
                        host = getattr(v, "host_path", None)
                        mnt = getattr(v, "mount_path", None)
                        ro = bool(getattr(v, "read_only", False))
                        if host and mnt:
                            if host and not os.path.isabs(host):
                                host = os.path.abspath(host)
                            cmd += ["-v", f"{host}:{mnt}:{'ro' if ro else 'rw'}"]
                    devs = None
                    if isinstance(csp, dict):
                        if "volumeDevices" in csp or "volume_devices" in csp:
                            devs = csp.get("volumeDevices") or csp.get("volume_devices") or []
                    else:
                        fields = getattr(csp, "__pydantic_fields_set__", None)
                        if fields and ("volume_devices" in fields or "volumeDevices" in fields):
                            devs = getattr(csp, "volume_devices", []) or []
                    if devs is None:
                        devs = []
                    for d in devs or []:
                        host = getattr(d, "host_path", None)
                        dev = getattr(d, "device_path", None)
                        ro = bool(getattr(d, "read_only", False))
                        if host and dev:
                            if host and not os.path.isabs(host):
                                host = os.path.abspath(host)
                            mode = "r" if ro else "rwm"
                            cmd += ["--device", f"{host}:{dev}:{mode}"]
                    host_network = bool(getattr(manifest.spec, "host_network", False))
                    host_pid = bool(getattr(manifest.spec, "host_pid", False))
                    host_ipc = bool(getattr(manifest.spec, "host_ipc", False))
                    share_proc = bool(getattr(manifest.spec, "share_process_namespace", False))
                    if host_network:
                        cmd += ["--network", "host"]
                    if host_pid:
                        cmd += ["--pid", "host"]
                    elif share_proc:
                        sandbox = self._ensure_pod_sandbox(
                            manifest,
                            replica_id,
                            revision,
                            node_id=getattr(self, "_current_node_id", None),
                        )
                        if sandbox:
                            cmd += ["--pid", f"container:{sandbox}"]
                    if host_ipc:
                        cmd += ["--ipc", "host"]
                    # Per-container projection mounts
                    try:
                        for pm in getattr(csp, "projection_mounts", []) or []:
                            p = (
                                getattr(pm, "path", None)
                                if not isinstance(pm, dict)
                                else pm.get("path")
                            )
                            mnt = (
                                getattr(pm, "mount_path", None)
                                if not isinstance(pm, dict)
                                else pm.get("mountPath") or pm.get("mount_path")
                            )
                            ro = (
                                bool(getattr(pm, "read_only", True))
                                if not isinstance(pm, dict)
                                else bool(pm.get("readOnly", True))
                            )
                            if proj_host_root and p and mnt:
                                host = os.path.join(str(proj_host_root), str(p).lstrip("/"))
                                cmd += ["-v", f"{host}:{mnt}:{'ro' if ro else 'rw'}"]
                    except Exception:
                        pass
                    # Inject runtime override if requested
                    self._maybe_inject_runtime(cmd)
                    # Env
                    env_items = (
                        getattr(csp, "env", None)
                        or (csp.get("env") if isinstance(csp, dict) else [])
                        or []
                    )
                    resources = (
                        getattr(csp, "resources", None)
                        if not isinstance(csp, dict)
                        else csp.get("resources")
                    )
                    env_map = self._resolve_env_map(manifest, env_items, resources=resources)
                    for key, value in env_map.items():
                        cmd += ["-e", f"{key}={value}"]
                    cmd += self._host_alias_args(manifest)
                    cmd += self._dns_args(manifest)
                    # Image and command
                    cmd += [str(img)]
                    combined: list[str] = []
                    combined += [str(x) for x in (getattr(csp, "command", []) or [])]  # noqa: B009
                    combined += [str(x) for x in (getattr(csp, "args", []) or [])]  # noqa: B009
                    cmd += combined
                    res = self._run_ok(cmd, allow_fail=True)
                    if self._network_name and not host_network and res.code == 0:
                        self._run_ok(
                            [
                                self._bin,
                                "network",
                                "connect",
                                self._network_name,
                                full_name,
                            ],
                            allow_fail=True,
                        )
            except Exception:
                continue

    def read_logs(
        self,
        pod_name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        # Find container by label
        cid = self._find_by_label(self.POD_LABEL, pod_name)
        if not cid:
            # Fallback: scan ps JSON and match Config.Labels
            try:
                r = self._run_ok([self._bin, "ps", "-a", "--format", "json"], allow_fail=True)
                arr = json.loads(r.out or "[]")
                for it in arr:
                    labels = (it.get("Config") or {}).get("Labels") or {}
                    if (
                        labels.get(self.POD_LABEL) == pod_name
                        or labels.get(self.LEGACY_REPLICA_LABEL) == pod_name
                    ):
                        cid = it.get("Id") or it.get("Names", [None])[0]
                        break
            except Exception:
                pass
        # Fallback to well-known container name if label lookup fails
        if not cid:
            cid = f"ae-{pod_name}"
            if follow:
                try:
                    probe = self._run_ok([self._bin, "container", "exists", cid], allow_fail=True)
                    if probe.code != 0:
                        return
                except Exception:
                    return
        cmd = [self._bin, "logs"]
        if tail is not None:
            cmd += ["--tail", str(int(tail))]
        if since is not None and int(since) > 0:
            cmd += ["--since", str(int(since))]
        if follow:
            cmd += ["-f"]
        cmd += [cid]
        # Stream using Popen when following to avoid buffering until process exit
        if follow:
            try:
                with subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
                ) as proc:  # type: ignore
                    if proc.stdout is not None:
                        for line in proc.stdout:
                            if (
                                "no container with name or ID" in line
                                or "no such container" in line
                            ):
                                return
                            yield line.rstrip("\n")
            except Exception:
                return
        else:
            res = self._run_ok(cmd, allow_fail=True)
            for line in (res.out or "").splitlines():
                yield line

    # Optional API used by HTTP UI to route logs by container name
    def read_logs_for_container(
        self,
        app_name: str,
        container_name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        # Find container id by labels
        r = self._run_ok(
            [
                self._bin,
                "ps",
                "-a",
                "--filter",
                f"label={self.APP_LABEL}={app_name}",
                "--filter",
                f"label={self.CONTAINER_LABEL}={container_name}",
                "--format",
                "{{.ID}}",
            ],
            allow_fail=True,
        )
        cid = (r.out or "").strip().splitlines()
        if not cid:
            return iter(())
        cid0 = cid[0]
        cmd = [self._bin, "logs"]
        if tail is not None:
            cmd += ["--tail", str(int(tail))]
        if since is not None and int(since) > 0:
            cmd += ["--since", str(int(since))]
        if follow:
            cmd += ["-f"]
        cmd += [cid0]
        if follow:
            try:
                with subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
                ) as proc:  # type: ignore
                    if proc.stdout is not None:
                        for line in proc.stdout:
                            if (
                                "no container with name or ID" in line
                                or "no such container" in line
                            ):
                                return
                            yield line.rstrip("\n")
            except Exception:
                return iter(())
        else:
            res = self._run_ok(cmd, allow_fail=True)
            for line in (res.out or "").splitlines():
                yield line

    # Exec by container name (best-effort)
    def exec_for_container(
        self, app_name: str, container_name: str, command: list[str], *, timeout: int | None = None
    ) -> int:  # type: ignore[override]
        r = self._run_ok(
            [
                self._bin,
                "ps",
                "-a",
                "--filter",
                f"label={self.APP_LABEL}={app_name}",
                "--filter",
                f"label={self.CONTAINER_LABEL}={container_name}",
                "--format",
                "{{.ID}}",
            ],
            allow_fail=True,
        )
        cid = (r.out or "").strip().splitlines()
        if not cid:
            return 127
        cmd = [self._bin, "exec", cid[0], *[str(x) for x in (command or [])]]
        try:
            cp = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            return int(cp.returncode)
        except Exception:
            return 1

    # Streaming exec for Podman (uses `podman exec --interactive --tty --attach` via varlink-less HTTP API)
    def exec_attach(
        self,
        pod_name: str,
        command: list[str],
        *,
        container: str | None = None,
        tty: bool = False,
    ):
        debug = str(os.getenv("AE_PODMAN_DEBUG", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        # Locate container by pod label
        cid = None
        try:
            labels = [f"label={self.POD_LABEL}={pod_name}"]
            if container:
                labels.append(f"label={self.CONTAINER_LABEL}={container}")
            cmd_list = [
                self._bin,
                "ps",
                "-a",
                *sum([["--filter", x] for x in labels], []),
                "--format",
                "{{.ID}}",
            ]
            res = self._run_ok(cmd_list, allow_fail=True)
            cid = (res.out or "").strip().splitlines()[0]
            if not cid:
                labels[0] = f"label={self.LEGACY_REPLICA_LABEL}={pod_name}"
                cmd_list = [
                    self._bin,
                    "ps",
                    "-a",
                    *sum([["--filter", x] for x in labels], []),
                    "--format",
                    "{{.ID}}",
                ]
                res = self._run_ok(cmd_list, allow_fail=True)
                cid = (res.out or "").strip().splitlines()[0]
            if debug:
                LOGGER.warning("podman exec_attach lookup %s => %s", cmd_list, cid)
        except Exception as exc:
            if debug:
                LOGGER.warning("podman exec_attach lookup failed: %s", exc)
            cid = None
        if not cid:
            raise RuntimeError("Pod not found for exec")

        # Use podman-remote exec attach over `podman system service --time=0` (HTTP API)
        # Fallback to stdio hijack via `podman exec --interactive --tty` and a pty.
        # Here we rely on `podman exec` with `--interactive` and `--tty` when requested,
        # attaching to a pseudo-tty and returning its master fd as a socket-like object.
        import pty

        try:
            master, slave = pty.openpty()
        except Exception as exc:
            if debug:
                LOGGER.warning("podman exec_attach openpty failed: %s", exc)
            raise
        argv = [self._bin, "exec", "--interactive"]
        if tty:
            argv.append("--tty")
        argv.append(cid)
        argv.extend([str(x) for x in command])
        try:
            proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
            )
        except Exception as exc:
            if debug:
                LOGGER.warning("podman exec_attach spawn failed: %s", exc)
            try:
                os.close(master)
            except Exception:
                pass
            try:
                os.close(slave)
            except Exception:
                pass
            raise
        os.close(slave)

        class _FDAsSocket:
            def __init__(self, fd, proc):
                self._fd = fd
                self._proc = proc
                os.set_blocking(fd, False)

            def recv(self, n: int) -> bytes:
                try:
                    return os.read(self._fd, n)
                except BlockingIOError as err:
                    raise TimeoutError from err

            def sendall(self, data: bytes) -> None:
                os.write(self._fd, data)

            def settimeout(self, _t: float) -> None:
                return

            def close(self) -> None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                try:
                    self._proc.terminate()
                except Exception:
                    pass

        return _FDAsSocket(master, proc), f"{cid}:{proc.pid}"

    def exec_resize(
        self, exec_id: str, *, height: int | None = None, width: int | None = None
    ) -> None:
        # best-effort: send SIGWINCH to the exec process when using pty
        try:
            if ":" in exec_id:
                _cid, pid_s = exec_id.split(":", 1)
                pid = int(pid_s)
                import fcntl
                import struct
                import termios

                if height and width:
                    winsize = struct.pack("HHHH", height, width, 0, 0)
                    with open(f"/proc/{pid}/fd/0", "wb", closefd=False) as f:
                        fcntl.ioctl(f, termios.TIOCSWINSZ, winsize)
        except Exception:
            return

    def exec_exit_code(self, exec_id: str) -> int:
        try:
            if ":" in exec_id:
                _cid, pid_s = exec_id.split(":", 1)
                pid = int(pid_s)
                _, sts = os.waitpid(pid, os.WNOHANG)
                if sts == 0:
                    return 0
                if os.WIFEXITED(sts):
                    return int(os.WEXITSTATUS(sts))
                return 0
        except Exception:
            return 0
        return 0

    def remove_app(self, app_name: str) -> int:
        removed = 0
        for c in self._list_app_containers(app_name):
            self._stop_and_remove(c.get("Id", ""))
            removed += 1
        return removed

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        removed = 0
        for c in self._list_app_containers(app_name):
            labs = (c.get("Config") or {}).get("Labels") or {}
            if labs.get(self.REVISION_LABEL) != str(keep_revision):
                self._stop_and_remove(c.get("Id", ""))
                removed += 1
        return removed

    # Init containers --------------------------------------------------
    def run_init_containers(  # type: ignore[override]
        self,
        manifest,
        *,
        replica_id: str | None = None,
        revision: int | None = None,
        node_id: str | None = None,
    ):
        """Run initContainers sequentially with optional timeouts.

        Returns a list of tuples: (name, rc, message).
        """
        manifest = self._maybe_inject_pvc_mounts(
            manifest,
            node_id=node_id or getattr(self, "_current_node_id", None),
            replica_id=replica_id,
        )
        results: list[tuple[str, int, str]] = []
        try:
            inits = list(getattr(manifest.spec, "init_containers", []) or [])
        except Exception:
            inits = []
        if not inits:
            return results

        # Ensure storage volumes exist if referenced so we can mount them
        try:
            if getattr(manifest.spec, "storage", None):
                self.ensure_storage_volumes(
                    app_key_for_manifest(manifest), [s.model_dump() for s in manifest.spec.storage]
                )
        except Exception:
            pass

        for c in inits:
            # Extract fields supporting both dict and model forms
            name = (
                getattr(c, "name", None) if not isinstance(c, dict) else c.get("name")
            ) or "init"
            image = getattr(c, "image", None) if not isinstance(c, dict) else c.get("image")
            if not image:
                results.append((str(name), 1, "missing image"))
                continue
            try:
                self._ensure_image(str(image), manifest=manifest, spec=c)
            except Exception as exc:  # noqa: BLE001
                results.append((str(name), 1, f"error: {exc}"))
                continue
            timeout: int | None = None
            try:
                raw = (
                    getattr(c, "timeout_seconds", None)
                    if not isinstance(c, dict)
                    else c.get("timeoutSeconds")
                )
                if raw is not None:
                    timeout = int(raw)
            except Exception:
                timeout = None

            # Build command
            try:
                command = [
                    str(x)
                    for x in (
                        getattr(c, "command", None)
                        or (c.get("command") if isinstance(c, dict) else [])
                        or []
                    )
                ]
            except Exception:
                command = []
            try:
                args = [
                    str(x)
                    for x in (
                        getattr(c, "args", None)
                        or (c.get("args") if isinstance(c, dict) else [])
                        or []
                    )
                ]
            except Exception:
                args = []

            # Build podman run argv
            argv: list[str] = [self._bin, "run", "--rm"]
            # Respect AE_OCI_RUNTIME for init containers
            self._maybe_inject_runtime(argv)
            # Working dir if specified on init container
            try:
                wd = (
                    getattr(c, "working_dir", None)
                    if not isinstance(c, dict)
                    else c.get("workingDir")
                )
                if wd:
                    argv += ["--workdir", str(wd)]
            except Exception:
                pass
            # Env
            try:
                env_items = (
                    getattr(c, "env", None) or (c.get("env") if isinstance(c, dict) else []) or []
                )
                resources = (
                    getattr(c, "resources", None)
                    if not isinstance(c, dict)
                    else c.get("resources")
                )
                env_map = self._resolve_env_map(manifest, env_items, resources=resources)
                for key, value in env_map.items():
                    argv += ["-e", f"{key}={value}"]
            except Exception:
                pass
            argv += self._host_alias_args(manifest)
            argv += self._dns_args(manifest)
            host_network = bool(getattr(manifest.spec, "host_network", False))
            host_pid = bool(getattr(manifest.spec, "host_pid", False))
            host_ipc = bool(getattr(manifest.spec, "host_ipc", False))
            share_proc = bool(getattr(manifest.spec, "share_process_namespace", False))
            if host_network:
                argv += ["--network", "host"]
            if host_pid:
                argv += ["--pid", "host"]
            elif share_proc and replica_id and revision is not None:
                sandbox = self._ensure_pod_sandbox(
                    manifest,
                    replica_id,
                    int(revision),
                    node_id=node_id or getattr(self, "_current_node_id", None),
                )
                if sandbox:
                    argv += ["--pid", f"container:{sandbox}"]
            if host_ipc:
                argv += ["--ipc", "host"]
            # Volumes: mount app storage and hostPath volumes, plus projected config root when present
            try:
                if getattr(manifest.spec, "storage", None):
                    for s in manifest.spec.storage:
                        vol_name = self._storage_volume_name(
                            app_key_for_manifest(manifest), getattr(s, "name", "")
                        )
                        mnt = getattr(s, "mount_path", None)
                        if vol_name and mnt:
                            mode = "ro" if getattr(s, "read_only", False) else "rw"
                            argv += ["-v", f"{vol_name}:{mnt}:{mode}"]
                host_vols = None
                if isinstance(c, dict):
                    if "volumeMounts" in c or "volume_mounts" in c:
                        host_vols = c.get("volumeMounts") or c.get("volume_mounts") or []
                else:
                    fields = getattr(c, "__pydantic_fields_set__", None)
                    if fields and ("volume_mounts" in fields or "volumeMounts" in fields):
                        host_vols = getattr(c, "volume_mounts", []) or []
                if host_vols is None:
                    host_vols = []
                for v in host_vols or []:
                    host = (
                        getattr(v, "host_path", None)
                        if not isinstance(v, dict)
                        else v.get("hostPath")
                    )
                    mnt = (
                        getattr(v, "mount_path", None)
                        if not isinstance(v, dict)
                        else v.get("mountPath")
                    )
                    ro = bool(
                        getattr(v, "read_only", False)
                        if not isinstance(v, dict)
                        else v.get("readOnly", False)
                    )
                    if host and mnt:
                        if host and not os.path.isabs(host):
                            host = os.path.abspath(host)
                        argv += ["-v", f"{host}:{mnt}:{'ro' if ro else 'rw'}"]
                devs = None
                if isinstance(c, dict):
                    if "volumeDevices" in c or "volume_devices" in c:
                        devs = c.get("volumeDevices") or c.get("volume_devices") or []
                else:
                    fields = getattr(c, "__pydantic_fields_set__", None)
                    if fields and ("volume_devices" in fields or "volumeDevices" in fields):
                        devs = getattr(c, "volume_devices", []) or []
                if devs is None:
                    devs = []
                for d in devs or []:
                    host = (
                        getattr(d, "host_path", None)
                        if not isinstance(d, dict)
                        else d.get("hostPath")
                    )
                    dev = (
                        getattr(d, "device_path", None)
                        if not isinstance(d, dict)
                        else d.get("devicePath")
                    )
                    ro = bool(
                        getattr(d, "read_only", False)
                        if not isinstance(d, dict)
                        else d.get("readOnly", False)
                    )
                    if host and dev:
                        if host and not os.path.isabs(host):
                            host = os.path.abspath(host)
                        mode = "r" if ro else "rwm"
                        argv += ["--device", f"{host}:{dev}:{mode}"]
            except Exception:
                pass

            # Image and command
            argv += [image]
            if (
                "/" not in image
                and not self._image_exists(image)
                and self._image_exists(f"localhost/{image}")
            ):
                argv[-1] = f"localhost/{image}"
            argv += command + args

            # Execute with optional timeout
            try:
                cp = subprocess.run(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout or None,
                )
                rc = int(cp.returncode)
                msg = "ok" if rc == 0 else (cp.stderr.strip() or "failed")
                results.append((str(name), rc, msg))
            except subprocess.TimeoutExpired:
                results.append((str(name), 124, "timeout"))
            except Exception as exc:  # noqa: BLE001
                results.append((str(name), 1, f"error: {exc}"))

        return results

    def run_init_containers_for_pod(
        self,
        manifest: AppManifest,
        replica_id: str,
        revision: int,
        *,
        node_id: str | None = None,
    ):
        return self.run_init_containers(
            manifest,
            replica_id=replica_id,
            revision=revision,
            node_id=node_id,
        )

    # Volumes ----------------------------------------------------------
    def _storage_volume_name(self, app_name: str, vol_name: str) -> str:
        return f"ae-{app_name}-{vol_name}"

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
            # create if missing
            ls = self._run_ok([self._bin, "volume", "ls", "--format", "json"], allow_fail=True)
            exists = False
            try:
                for item in json.loads(ls.out or "[]"):
                    if item.get("Name") == vol:
                        exists = True
                        break
            except Exception:
                pass
            if not exists:
                labels = [f"{k}={v}" for k, v in std_labels.items()]
                labels.append(f"ae.volume={name}")
                node_label = getattr(self, "_current_node_id", None)
                if node_label:
                    labels.append(f"ae.node={node_label}")
                self._run_ok(
                    [
                        self._bin,
                        "volume",
                        "create",
                        *sum([["--label", lbl] for lbl in labels], []),
                        vol,
                    ],
                    allow_fail=False,
                )

    def remove_storage_volumes(self, app_name: str, names: list[str]) -> int:  # type: ignore[override]
        removed = 0
        for n in names or []:
            vol = self._storage_volume_name(app_name, str(n))
            r = self._run_ok([self._bin, "volume", "rm", "-f", vol], allow_fail=True)
            if r.code == 0:
                removed += 1
        return removed

    def list_storage_volumes(self, app_name: str | None = None) -> list[dict]:  # type: ignore[override]
        out: list[dict] = []
        r = self._run_ok([self._bin, "volume", "ls", "--format", "json"], allow_fail=True)
        try:
            items = json.loads(r.out or "[]")
        except Exception:
            items = []
        for it in items:
            labels = it.get("Labels") or {}
            app = labels.get(self.APP_LABEL)
            if app_name is not None:
                if app != app_name:
                    continue
            else:
                if not app:
                    continue
            out.append(
                {
                    "name": it.get("Name", ""),
                    "labels": labels,
                    "driver": it.get("Driver", ""),
                    "mountpoint": it.get("Mountpoint", ""),
                }
            )
        return out

    # Info -------------------------------------------------------------
    def list_containers_info(self) -> list[dict]:  # type: ignore[override]
        r = self._run_ok([self._bin, "ps", "-a", "--format", "json"], allow_fail=True)
        out: list[dict] = []
        try:
            items = json.loads(r.out or "[]")
        except Exception:
            return out
        for it in items:
            labels = it.get("Labels") or (it.get("Config") or {}).get("Labels") or {}
            host_ports: list[int] = []
            port_map: dict[int, int] = {}
            host_ip = None
            restarts = 0
            started_at = None
            running = (it.get("State") or "").lower() == "running"
            pod_ip = None
            try:
                insp = self._run_ok(
                    [self._bin, "inspect", it.get("Id", ""), "--format", "json"], allow_fail=True
                )
                arr = json.loads(insp.out or "[]")
                if arr:
                    pmap = (arr[0].get("NetworkSettings") or {}).get("Ports") or {}
                    for binds in (pmap or {}).values():
                        if not binds:
                            continue
                        for b in binds:
                            hp = b.get("HostPort")
                            if hp:
                                try:
                                    host_ports.append(int(hp))
                                except Exception:
                                    pass
                            hip = b.get("HostIp") or b.get("HostIP")
                            if hip and host_ip is None:
                                host_ip = hip
                    for key, binds in (pmap or {}).items():
                        if not binds:
                            continue
                        try:
                            cport = int(str(key).split("/", 1)[0])
                        except Exception:
                            continue
                        for b in binds:
                            hp = b.get("HostPort")
                            if hp:
                                try:
                                    port_map.setdefault(cport, int(hp))
                                except Exception:
                                    pass
                    try:
                        st = arr[0].get("State") or {}
                        rc = st.get("RestartCount", 0)
                        if isinstance(rc, int | float):
                            restarts = int(rc)
                        started_at = st.get("StartedAt")
                        pod_ip = (arr[0].get("NetworkSettings") or {}).get("IPAddress")
                    except Exception:
                        restarts = 0
            except Exception:
                pass
            out.append(
                {
                    "name": it.get("Names", [it.get("Id", "")])[0],
                    "labels": labels,
                    "uid": it.get("Id"),
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

    def exec(self, pod_name: str, command: list[str], *, timeout: int | None = None) -> int:  # type: ignore[override]
        # Locate container by label
        cid = self._find_by_label(self.POD_LABEL, pod_name)
        if not cid:
            return 127
        cmd = [self._bin, "exec"]
        if timeout is not None and int(timeout) > 0:
            # podman exec lacks a direct timeout; rely on caller to limit retries
            pass
        cmd += [cid] + [str(x) for x in command]
        r = self._run_ok(cmd, allow_fail=True)
        return int(r.code)

    # Helpers ----------------------------------------------------------
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
        name = f"ae-{app}-rev{revision}-{suffix}"

        # Ensure idempotency: if a container with this name already exists,
        # remove it first so we can reliably recreate the replica for this revision.
        # This mirrors Docker's --force recreate behavior and avoids Podman name
        # collisions across repeated applies of the same revision.
        exists = self._run_ok([self._bin, "container", "exists", name], allow_fail=True)
        if exists.code == 0:
            # Best-effort stop/remove by name
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
            *sum(
                [["--label", f"{k}={v}"] for k, v in labels.items()],
                [],
            ),
            "--restart",
            "no" if is_job else "unless-stopped",
        ]

        # Resources: limits/requests
        # - limits.memory → hard cap (--memory)
        # - requests.memory → soft reservation (--memory-reservation)
        try:
            lims = getattr(getattr(manifest.spec, "resources", None), "limits", None)  # noqa: B009
            if lims is not None and getattr(lims, "memory", None) is not None:  # noqa: B009
                try:
                    mem = str(getattr(lims, "memory"))  # noqa: B009
                    cmd += ["--memory", mem]
                except Exception:
                    pass
            reqs = getattr(getattr(manifest.spec, "resources", None), "requests", None)  # noqa: B009
            if reqs is not None:
                if getattr(reqs, "cpu", None) is not None:  # noqa: B009
                    try:
                        shares = max(2, int(float(reqs.cpu) * 1024))
                        cmd += ["--cpu-shares", str(shares)]
                    except Exception:
                        pass
                if getattr(reqs, "memory", None) is not None:  # noqa: B009
                    mem = str(getattr(reqs, "memory"))  # noqa: B009
                    cmd += ["--memory-reservation", mem]
        except Exception:
            pass

        # Env
        env_map = self._resolve_env_map(
            manifest, manifest.spec.env or [], resources=getattr(manifest.spec, "resources", None)
        )
        for key, value in env_map.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += self._host_alias_args(manifest)
        cmd += self._dns_args(manifest)
        host_network = bool(getattr(manifest.spec, "host_network", False))
        host_pid = bool(getattr(manifest.spec, "host_pid", False))
        host_ipc = bool(getattr(manifest.spec, "host_ipc", False))
        share_proc = bool(getattr(manifest.spec, "share_process_namespace", False))
        if host_network:
            cmd += ["--network", "host"]
        if host_pid:
            cmd += ["--pid", "host"]
        elif share_proc:
            sandbox = self._ensure_pod_sandbox(
                manifest, replica_id, revision, node_id=node_id
            )
            if sandbox:
                cmd += ["--pid", f"container:{sandbox}"]
        if host_ipc:
            cmd += ["--ipc", "host"]

        # Ports: publish service stable port if replicas==1. If no Service is defined
        # but the manifest declares ports, publish ephemeral host ports for exposed
        # container ports (Docker parity: docker-py maps {"8080/tcp": None}). This
        # allows the controller (host) to probe readiness via 127.0.0.1:ephemeral and
        # lets Caddy proxy via host alias inside the container.
        svc_port, svc_target, svc_ports_list = service
        published_any = False
        reserved_ports: set[int] = set()
        svc_type = ""
        if getattr(manifest.spec, "service", None):
            svc_type = str(getattr(manifest.spec.service, "type", "") or "").lower()
        if not host_network:
            if svc_ports_list:
                # Publish each declared service port as host:container mapping
                # Resolve targetPort similarly to the exporter rules
                try:
                    by_name = {
                        p.name: int(p.container_port)
                        for p in (manifest.spec.ports or [])
                        if getattr(p, "name", None)
                    }
                except Exception:
                    by_name = {}
                try:
                    by_num = {
                        int(p.container_port): int(p.container_port)
                        for p in (manifest.spec.ports or [])
                    }
                except Exception:
                    by_num = {}
                for sp in svc_ports_list or []:
                    try:
                        svc_port_num = getattr(sp, "port", None)
                        portnum = svc_port_num
                        if svc_type in {"nodeport", "loadbalancer"}:
                            node_port = getattr(sp, "node_port", None)
                            if node_port is not None:
                                portnum = node_port
                        tgt = getattr(sp, "target_port", None)
                        name = getattr(sp, "name", None)
                        if tgt is None:
                            tgt = by_name.get(name) or (
                                by_num.get(int(svc_port_num)) if svc_port_num is not None else None
                            )
                        if portnum is not None and tgt is not None:
                            chosen, used_preferred = choose_host_port(
                                int(portnum), reserved=reserved_ports
                            )
                            if chosen is None:
                                LOGGER.warning(
                                    "service port %s for app %s is unavailable; skipping publish",
                                    portnum,
                                    app,
                                )
                                continue
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
                chosen, used_preferred = choose_host_port(int(svc_port), reserved=reserved_ports)
                if chosen is None:
                    LOGGER.warning(
                        "service port %s for app %s is unavailable; skipping publish",
                        svc_port,
                        app,
                    )
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
                # If no explicit host mappings were given but ports exist, publish exposed
                # ports on ephemeral host ports (requires images to EXPOSE the ports).
            if not published_any and (manifest.spec.ports or []):
                cmd += ["-P"]

        # Volumes
        if getattr(manifest.spec, "storage", None):
            self.ensure_storage_volumes(app, [s.model_dump() for s in manifest.spec.storage])
            for s in manifest.spec.storage:
                vol_name = self._storage_volume_name(app, s.name)
                mode = "ro" if getattr(s, "read_only", False) else "rw"
                cmd += ["-v", f"{vol_name}:{s.mount_path}:{mode}"]
        proj_host_root = None
        try:
            for v in manifest.spec.volumes or []:
                mpath = getattr(v, "mount_path", None)
                hpath = getattr(v, "host_path", None)
                if mpath and str(mpath).startswith(f"/var/run/ae/config/{app}") and hpath:
                    proj_host_root = hpath
                    break
        except Exception:
            proj_host_root = None
        if manifest.spec.volumes:
            for v in manifest.spec.volumes:
                mode = "ro" if v.read_only else "rw"
                host = v.host_path
                if host and not os.path.isabs(host):
                    host = os.path.abspath(host)
                cmd += ["-v", f"{host}:{v.mount_path}:{mode}"]
        if proj_host_root and getattr(manifest.spec, "projection_mounts", None):
            for pm in getattr(manifest.spec, "projection_mounts", []) or []:
                try:
                    rel = getattr(pm, "path", None) if not isinstance(pm, dict) else pm.get("path")
                    mnt = (
                        getattr(pm, "mount_path", None)
                        if not isinstance(pm, dict)
                        else pm.get("mountPath") or pm.get("mount_path")
                    )
                    ro = (
                        bool(getattr(pm, "read_only", True))
                        if not isinstance(pm, dict)
                        else bool(pm.get("readOnly", True))
                    )
                    if not rel or not mnt:
                        continue
                    host = os.path.join(str(proj_host_root), str(rel).lstrip("/"))
                    if host and not os.path.isabs(host):
                        host = os.path.abspath(host)
                    cmd += ["-v", f"{host}:{mnt}:{'ro' if ro else 'rw'}"]
                except Exception:
                    continue
        if getattr(manifest.spec, "volume_devices", None):
            for d in manifest.spec.volume_devices:
                host = d.host_path
                dev = d.device_path
                if host and not os.path.isabs(host):
                    host = os.path.abspath(host)
                mode = "r" if d.read_only else "rwm"
                cmd += ["--device", f"{host}:{dev}:{mode}"]

        # Security context
        sec = getattr(manifest.spec, "security", None)
        if sec is not None:
            if getattr(sec, "run_as_user", None) is not None:
                if getattr(sec, "run_as_group", None) is not None:
                    cmd += ["--user", f"{int(sec.run_as_user)}:{int(sec.run_as_group)}"]
                else:
                    cmd += ["--user", str(int(sec.run_as_user))]
            if bool(getattr(sec, "read_only_root", False)):
                cmd += ["--read-only"]
            drops = list(getattr(sec, "drop_caps", []) or [])
            for cap in drops:
                cmd += ["--cap-drop", str(cap)]
            # seccomp and AppArmor via --security-opt
            try:
                s_type = getattr(sec, "seccomp_type", None)
                s_local = getattr(sec, "seccomp_localhost_profile", None)
                if s_type:
                    st = str(s_type)
                    if st == "RuntimeDefault":
                        # Let Podman apply its default seccomp profile; do not pass the Kubernetes token
                        # "runtime/default" because Podman expects an actual path.
                        pass
                    elif st == "Unconfined":
                        cmd += ["--security-opt", "seccomp=unconfined"]
                    elif st == "Localhost" and s_local:
                        # Podman accepts a path to a local profile JSON
                        cmd += ["--security-opt", f"seccomp={s_local}"]
                a_prof = getattr(sec, "apparmor_profile", None)
                if a_prof:
                    ap = str(a_prof)
                    if ap.startswith("localhost/"):
                        ap = ap.split("/", 1)[1]
                    if ap == "runtime/default":
                        # Podman uses "container-default" profile name typically; allow plain default mapping
                        ap = "container-default"
                    cmd += ["--security-opt", f"apparmor={ap}"]
            except Exception:
                pass

        # Working directory
        if getattr(manifest.spec, "working_dir", None):
            cmd += ["--workdir", str(manifest.spec.working_dir)]

        # Image and command
        cmd += [manifest.spec.image]
        # If unqualified name missing but localhost/<name> exists, use that
        if (
            "/" not in manifest.spec.image
            and not self._image_exists(manifest.spec.image)
            and self._image_exists(f"localhost/{manifest.spec.image}")
        ):
            cmd[-1] = f"localhost/{manifest.spec.image}"
        # Build command/args following K8s semantics
        combined: list[str] = []
        if getattr(manifest.spec, "command", None):
            combined += [str(x) for x in (manifest.spec.command or [])]
        if getattr(manifest.spec, "args", None):
            combined += [str(x) for x in (manifest.spec.args or [])]
        if combined:
            cmd += combined

        # Respect AE_OCI_RUNTIME for container creation
        self._maybe_inject_runtime(cmd)
        self._run_ok(cmd)

    def _stop_and_remove(self, cid: str) -> None:
        if not cid:
            return
        # Inspect label for per-container stop timeout
        timeout = 10
        try:
            r = self._run_ok(
                [self._bin, "inspect", cid, "--format", "{{json .Config.Labels}}"], allow_fail=True
            )
            import json as _json

            labels = _json.loads(r.out or "{}") or {}
            if isinstance(labels, dict) and labels.get("ae.stop_timeout"):
                timeout = int(str(labels.get("ae.stop_timeout")))
        except Exception:
            pass
        self._run_ok([self._bin, "stop", "-t", str(int(timeout)), cid], allow_fail=True)
        self._run_ok([self._bin, "rm", "-f", cid], allow_fail=True)

    def _list_app_containers(self, app: str) -> list[dict]:
        r = self._run_ok(
            [
                self._bin,
                "ps",
                "-a",
                "--filter",
                f"label={self.APP_LABEL}={app}",
                "--format",
                "json",
            ],
            allow_fail=True,
        )
        try:
            ids = [it.get("Id", "") for it in json.loads(r.out or "[]")]
        except Exception:
            ids = []
        items: list[dict] = []
        if not ids:
            return items
        insp = self._run_ok([self._bin, "inspect", "--format", "json", *ids], allow_fail=True)
        try:
            arr = json.loads(insp.out or "[]")
        except Exception:
            arr = []
        for it in arr:
            items.append(it)
        return items

    def _find_by_label(self, key: str, value: str) -> str | None:
        r = self._run_ok(
            [self._bin, "ps", "-a", "--filter", f"label={key}={value}", "--format", "{{.ID}}"],
            allow_fail=True,
        )
        cid = (r.out or "").strip().splitlines()
        if not cid and key == self.POD_LABEL:
            r = self._run_ok(
                [
                    self._bin,
                    "ps",
                    "-a",
                    "--filter",
                    f"label={self.LEGACY_REPLICA_LABEL}={value}",
                    "--format",
                    "{{.ID}}",
                ],
                allow_fail=True,
            )
            cid = (r.out or "").strip().splitlines()
        return cid[0] if cid else None

    def _parse_dt(self, raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            s = str(raw).rstrip("Z")
            return datetime.fromisoformat(s)
        except Exception:
            return None

    def _cleanup_crun_path(self, stderr: str) -> None:
        match = self._crun_path_re.search(stderr or "")
        if not match:
            return
        path = match.group(1)
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    def _should_retry_podman(self, stderr: str) -> bool:
        lowered = (stderr or "").lower()
        return "permission denied" in lowered and "crun" in lowered and "/run/user" in lowered

    def _run_ok(self, argv: list[str], *, allow_fail: bool = False) -> _RunResult:
        retries = self._podman_retry_max if not allow_fail else 0
        attempt = 0
        while True:
            try:
                cp = subprocess.run(
                    argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "podman binary not found. Install Podman or set AE_PODMAN_BIN"
                ) from exc

            if cp.returncode == 0 or allow_fail:
                return _RunResult(cp.returncode, cp.stdout or "", cp.stderr or "")

            stderr = cp.stderr or ""
            if attempt < retries and self._should_retry_podman(stderr):
                attempt += 1
                self._cleanup_crun_path(stderr)
                time.sleep(self._podman_retry_delay)
                continue

            raise RuntimeError(f"podman failed: {' '.join(argv)} => {stderr.strip()}")

    def _image_present(self, image_ref: str) -> bool:
        if self._image_exists(image_ref):
            return True
        if "/" not in str(image_ref) and self._image_exists(f"localhost/{image_ref}"):
            return True
        return False

    def _resolve_image_pull_policy(
        self,
        image_ref: str,
        *,
        manifest: AppManifest | None = None,
        spec: Any | None = None,
        explicit: str | None = None,
    ) -> str:
        raw = explicit
        if raw is None and spec is not None:
            if isinstance(spec, dict):
                raw = spec.get("imagePullPolicy") or spec.get("image_pull_policy")
            else:
                raw = getattr(spec, "image_pull_policy", None)
        if raw is None and manifest is not None:
            raw = getattr(manifest.spec, "image_pull_policy", None)
        if raw:
            val = str(raw).strip().lower()
            if val == "always":
                return "Always"
            if val == "ifnotpresent":
                return "IfNotPresent"
            if val == "never":
                return "Never"

        # Default Kubernetes semantics: :latest -> Always, otherwise IfNotPresent.
        if "@" in str(image_ref):
            return "IfNotPresent"
        last = str(image_ref).rsplit("/", 1)[-1]
        if ":" in last:
            tag = last.split(":", 1)[1]
            return "Always" if tag == "latest" else "IfNotPresent"
        return "Always"

    def _host_alias_args(self, manifest: AppManifest) -> list[str]:
        args: list[str] = []
        for entry in getattr(manifest.spec, "host_aliases", []) or []:
            if isinstance(entry, dict):
                ip = entry.get("ip")
                names = entry.get("hostnames") or entry.get("hostNames") or []
            else:
                ip = getattr(entry, "ip", None)
                names = getattr(entry, "hostnames", None) or []
            if not ip:
                continue
            for name in names or []:
                if name:
                    args += ["--add-host", f"{name}:{ip}"]
        return args

    def _dns_args(self, manifest: AppManifest) -> list[str]:
        cfg = getattr(manifest.spec, "dns_config", None)
        if not cfg:
            return []
        args: list[str] = []
        for ns in getattr(cfg, "nameservers", None) or []:
            if ns:
                args += ["--dns", str(ns)]
        for search in getattr(cfg, "searches", None) or []:
            if search:
                args += ["--dns-search", str(search)]
        for opt in getattr(cfg, "options", None) or []:
            if isinstance(opt, dict):
                name = opt.get("name")
                value = opt.get("value")
            else:
                name = getattr(opt, "name", None)
                value = getattr(opt, "value", None)
            if name:
                args += ["--dns-opt", f"{name}:{value}" if value else str(name)]
        return args

    def _endpoint_for_host_network(
        self, manifest: AppManifest, preferred: int | None = None
    ) -> str | None:
        port = None
        if preferred is not None:
            port = int(preferred)
        elif getattr(manifest.spec, "ports", None):
            try:
                port = int(manifest.spec.ports[0].container_port)
            except Exception:
                port = None
        if port is None and getattr(manifest.spec, "service", None):
            svc = manifest.spec.service
            try:
                target = getattr(svc, "target_port", None)
                port = int(target if target is not None else getattr(svc, "port", None))
            except Exception:
                port = None
        if port is None:
            return None
        host = os.getenv("AE_NODE_ADVERTISE_IP") or "127.0.0.1"
        return f"{host}:{port}"

    @staticmethod
    def _normalize_host_ip(host_ip: str | None) -> str:
        raw = (host_ip or "").strip()
        if raw in {"", "0.0.0.0", "::", "[::]", "127.0.0.1", "::1", "[::1]"}:
            return os.getenv("AE_NODE_ADVERTISE_IP") or "127.0.0.1"
        return raw

    def _extract_registry(self, image: str) -> str | None:
        if "/" not in image:
            return None
        host = image.split("/", 1)[0]
        if "." not in host and ":" not in host:
            return None
        return host

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
        store = self._get_apishim_store()
        if store is None:
            return []
        namespace = getattr(getattr(manifest, "metadata", None), "namespace", None) or DEFAULT_NAMESPACE
        sa_name = (
            getattr(manifest.spec, "service_account_name", None)
            or self._service_account_name_from_store(manifest, store)
            or "default"
        )
        try:
            sa = store.get("", "v1", "serviceaccounts", namespace, str(sa_name))
        except Exception:
            sa = None
        if sa is None:
            return []
        spec = getattr(sa, "spec", None) or {}
        if not isinstance(spec, dict):
            return []
        secrets = spec.get("imagePullSecrets") or []
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
                template = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")
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
            host = data.get("registry") or data.get("host") or data.get("server")
            user = data.get("username")
            pw = data.get("password")
            if host and user and pw:
                auths[str(host)] = {"username": str(user), "password": str(pw)}
        return auths

    def _image_pull_credentials(
        self,
        image_ref: str,
        *,
        manifest: AppManifest | None = None,
        spec: Any | None = None,
    ) -> tuple[str, str, str] | None:
        creds = self._registry.list_registries()
        secret_creds = self._pull_secret_auths(manifest) if manifest is not None else {}
        preferred: list[str] = []
        if manifest is not None:
            ref = getattr(manifest.spec, "registry_auth_ref", None)
            if ref:
                preferred.append(str(ref))
            for sec in getattr(manifest.spec, "image_pull_secrets", []) or []:
                if isinstance(sec, dict):
                    name = sec.get("name")
                    if name:
                        preferred.append(str(name))
                else:
                    preferred.append(str(sec))

        for host in preferred:
            entry = creds.get(host) or secret_creds.get(host)
            if entry and entry.get("username") and entry.get("password"):
                return str(host), str(entry.get("username")), str(entry.get("password"))

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
            entry = creds.get(host) or secret_creds.get(host)
            if entry and entry.get("username") and entry.get("password"):
                return str(host), str(entry.get("username")), str(entry.get("password"))
        return None

    def _podman_login(self, registry: str, username: str, password: str) -> None:
        cp = subprocess.run(
            [
                self._bin,
                "login",
                "--username",
                str(username),
                "--password-stdin",
                str(registry),
            ],
            input=str(password),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"podman login failed: {cp.stderr.strip()}")

    def _sandbox_image(self) -> str:
        return (
            os.getenv("AE_POD_SANDBOX_IMAGE")
            or os.getenv("AE_CRI_SANDBOX_IMAGE")
            or "registry.k8s.io/pause:3.9"
        )

    def _pod_sandbox_name(self, manifest: AppManifest, replica_id: str, revision: int) -> str:
        app_name = app_key_for_manifest(manifest)
        suffix = replica_id.split("-")[-1]
        return f"ae-{app_name}-rev{revision}-{suffix}-pod"

    def _ensure_pod_sandbox(
        self,
        manifest: AppManifest,
        replica_id: str,
        revision: int,
        *,
        node_id: str | None = None,
    ) -> str | None:
        if not bool(getattr(manifest.spec, "share_process_namespace", False)):
            return None
        name = self._pod_sandbox_name(manifest, replica_id, revision)
        app_name = app_key_for_manifest(manifest)
        labels = runtime_labels_for_manifest(manifest, app_name=app_name)
        labels.update(
            {
                self.POD_LABEL: replica_id,
                self.LEGACY_REPLICA_LABEL: replica_id,
                self.REVISION_LABEL: str(revision),
                self.CONTAINER_LABEL: self.POD_SANDBOX_LABEL,
                **({"ae.node": str(node_id)} if node_id else {}),
            }
        )
        try:
            exists = self._run_ok([self._bin, "container", "exists", name], allow_fail=True)
            if exists.code == 0:
                status = self._run_ok(
                    [self._bin, "inspect", name, "--format", "{{.State.Status}}"],
                    allow_fail=True,
                )
                if (status.out or "").strip() != "running":
                    self._run_ok([self._bin, "start", name], allow_fail=True)
                return name
        except Exception:
            pass
        try:
            self._ensure_image(self._sandbox_image(), manifest=manifest, policy="IfNotPresent")
            cmd = [
                self._bin,
                "run",
                "-d",
                "--name",
                name,
                *sum([["--label", f"{k}={v}"] for k, v in labels.items()], []),
                "--restart",
                "unless-stopped",
                self._sandbox_image(),
            ]
            self._run_ok(cmd, allow_fail=False)
            return name
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to create pod sandbox for %s: %s", replica_id, exc)
            return None

    def _get_volume_manager(self):
        if self._volume_manager_checked:
            return self._volume_manager
        self._volume_manager_checked = True
        if os.getenv("AE_ENABLE_NETFS", "0") != "1":
            self._volume_manager = None
            return None
        try:
            from pathlib import Path

            from ae.storage import (
                ApishimStorageState,
                InMemoryStorageState,
                NetFSManager,
                NodeVolumeManager,
            )
            from ae.apishim.store import ObjectStore
        except Exception:
            self._volume_manager = None
            return None
        state = None
        dsn = os.getenv("AE_APISHIM_DSN")
        db_path = os.getenv("AE_APISHIM_DB")
        if dsn or db_path:
            try:
                store = ObjectStore(
                    db_path=Path(db_path) if db_path else Path("state/apishim.db"),
                    dsn=dsn,
                )
                state = ApishimStorageState(store)
            except Exception:
                state = None
        if state is None:
            state = InMemoryStorageState()
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
        except TypeError:
            try:
                return mgr.inject_pvc_mounts(
                    manifest,
                    node_id=node_id or self._current_node_id,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("PVC mount injection failed: %s", exc)
                return manifest
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("PVC mount injection failed: %s", exc)
            return manifest

    def _ensure_image(
        self,
        image_ref: str,
        *,
        manifest: AppManifest | None = None,
        spec: Any | None = None,
        policy: str | None = None,
    ) -> None:
        policy_name = self._resolve_image_pull_policy(
            image_ref, manifest=manifest, spec=spec, explicit=policy
        )
        if policy_name == "Never":
            if not self._image_present(image_ref):
                raise RuntimeError(
                    f"imagePullPolicy=Never and image not present: {image_ref}"
                )
            return
        if policy_name != "Always" and self._image_present(image_ref):
            return
        try:
            creds = self._image_pull_credentials(image_ref, manifest=manifest, spec=spec)
            if creds:
                registry, username, password = creds
                self._podman_login(registry, username, password)
            self._run_ok([self._bin, "pull", image_ref], allow_fail=False)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to pull image {image_ref}: {exc}") from exc

    def _get_apishim_state(self):
        if self._apishim_state_checked:
            return self._apishim_state
        self._apishim_state_checked = True
        try:
            from ae.apishim.store import ObjectStore
            from ae.storage.state import ApishimHttpStorageState, ApishimStorageState
        except Exception:
            self._apishim_state = None
            return None
        dsn = os.getenv("AE_APISHIM_DSN")
        db_env = os.getenv("AE_APISHIM_DB")
        db_path = Path(db_env or "state/apishim.db")
        store = None
        if dsn or db_path.exists():
            try:
                store = ObjectStore(dsn=dsn) if dsn else ObjectStore(db_path=db_path)
            except Exception:
                store = None
        if store is not None:
            self._apishim_state = ApishimStorageState(store)
            return self._apishim_state
        self._apishim_state = ApishimHttpStorageState.from_env()
        return self._apishim_state

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

    def _resolve_env_map(
        self, manifest: AppManifest, env_items: list[dict] | list, *, resources=None
    ) -> dict[str, str]:
        env_map: dict[str, str] = {}
        if resources is None:
            resources = getattr(manifest.spec, "resources", None)
        namespace = getattr(manifest.metadata, "namespace", None) or DEFAULT_NAMESPACE
        needs_store = False
        for item in env_items or []:
            vf = item.get("valueFrom") if isinstance(item, dict) else None
            if isinstance(vf, dict) and (
                isinstance(vf.get("configMapKeyRef"), dict)
                or isinstance(vf.get("secretKeyRef"), dict)
            ):
                needs_store = True
                break
        state = self._get_apishim_state() if needs_store else None

        def _merge_env_from_ref(
            ref: dict | None, *, secret: bool, prefix: str | None = None
        ) -> None:
            if not ref or not isinstance(ref, dict):
                return
            name = ref.get("name")
            if not name or state is None:
                return
            data = (
                state.get_secret(namespace, str(name))
                if secret
                else state.get_config_map(namespace, str(name))
            )
            if not data:
                return
            for key, value in data.items():
                env_key = f"{prefix}{key}" if prefix else str(key)
                env_map[str(env_key)] = "" if value is None else str(value)

        for item in env_items or []:
            if not isinstance(item, dict):
                continue
            if item.get("name"):
                continue
            vf = item.get("valueFrom") if isinstance(item, dict) else None
            if not isinstance(vf, dict):
                continue
            cm_ref = (
                vf.get("configMapKeyRef") if isinstance(vf.get("configMapKeyRef"), dict) else None
            )
            if cm_ref is not None and not cm_ref.get("key"):
                _merge_env_from_ref(cm_ref, secret=False, prefix=cm_ref.get("prefix"))
            sec_ref = vf.get("secretKeyRef") if isinstance(vf.get("secretKeyRef"), dict) else None
            if sec_ref is not None and not sec_ref.get("key"):
                _merge_env_from_ref(sec_ref, secret=True, prefix=sec_ref.get("prefix"))

        for item in env_items or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            if "value" in item:
                env_map[str(name)] = str(item.get("value", ""))
                continue
            vf = item.get("valueFrom") if isinstance(item, dict) else None
            if isinstance(vf, dict) and isinstance(vf.get("fieldRef"), dict):
                fp = str(vf.get("fieldRef", {}).get("fieldPath", ""))
                if fp == "metadata.name":
                    env_map[str(name)] = str(manifest.metadata.name)
                elif fp == "metadata.namespace":
                    env_map[str(name)] = str(namespace)
                continue
            if isinstance(vf, dict) and isinstance(vf.get("resourceFieldRef"), dict):
                rfr = vf.get("resourceFieldRef", {}) or {}
                res = str(rfr.get("resource", ""))
                divisor_raw = str(rfr.get("divisor", "")) if rfr.get("divisor") is not None else ""
                try:
                    if res in {"limits.cpu", "requests.cpu"}:
                        obj = self._resource_quantities(
                            resources, "limits" if res == "limits.cpu" else "requests"
                        )
                        cpuq = self._resource_value(obj, "cpu")
                        if isinstance(cpuq, int | float):
                            base_m = int(round(float(cpuq) * 1000))
                            if divisor_raw:
                                d = divisor_raw.strip().lower()
                                if d.endswith("m"):
                                    div_m = int(float(d[:-1] or 0)) or 1
                                else:
                                    div_m = int(round(float(d or "1") * 1000)) or 1
                            else:
                                div_m = 1000
                            if div_m <= 0:
                                div_m = 1
                            env_map[str(name)] = str(int(base_m // div_m))
                        continue
                    if res in {"limits.memory", "requests.memory"}:
                        obj = self._resource_quantities(
                            resources, "limits" if res == "limits.memory" else "requests"
                        )
                        memq = self._resource_value(obj, "memory")
                        if memq:
                            bytes_val = self._parse_memory_bytes(str(memq)) or 0
                            if divisor_raw:
                                div_bytes = self._parse_memory_bytes(str(divisor_raw)) or 1
                            else:
                                div_bytes = 1
                            if div_bytes <= 0:
                                div_bytes = 1
                            env_map[str(name)] = str(int(bytes_val // div_bytes))
                        continue
                except Exception:
                    pass
            if isinstance(vf, dict) and isinstance(vf.get("configMapKeyRef"), dict):
                cm_ref = vf.get("configMapKeyRef") or {}
                key = cm_ref.get("key")
                if key and state is not None:
                    data = state.get_config_map(namespace, str(cm_ref.get("name") or ""))
                    if data and key in data:
                        env_map[str(name)] = str(data[key])
                continue
            if isinstance(vf, dict) and isinstance(vf.get("secretKeyRef"), dict):
                sec_ref = vf.get("secretKeyRef") or {}
                key = sec_ref.get("key")
                if key and state is not None:
                    data = state.get_secret(namespace, str(sec_ref.get("name") or ""))
                    if data and key in data:
                        env_map[str(name)] = str(data[key])
                continue
        return env_map

    @staticmethod
    def _resource_quantities(resources, field):  # noqa: ANN001
        if resources is None:
            return None
        if isinstance(resources, dict):
            return resources.get(field)
        return getattr(resources, field, None)

    @staticmethod
    def _resource_value(obj, field):  # noqa: ANN001
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(field)
        return getattr(obj, field, None)

    @staticmethod
    def _parse_memory_bytes(raw: str) -> int | None:
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
            if not num:
                return None
            factor = suffixes.get(unit.strip().lower(), 1)
            return int(float(num) * factor)
        except Exception:
            return None

    def _image_exists(self, name: str) -> bool:
        def _dockerhub_aliases(image: str) -> set[str]:
            aliases = {image}
            raw = image
            digest = None
            if "@" in raw:
                raw, digest = raw.split("@", 1)
            tag = None
            if ":" in raw and "/" not in raw.rsplit(":", 1)[1]:
                raw, tag = raw.rsplit(":", 1)
            if raw.startswith("docker.io/"):
                rest = raw[len("docker.io/") :]
                if rest.startswith("library/"):
                    repo = rest[len("library/") :]
                    alt = f"docker.io/{repo}"
                    if tag:
                        alt = f"{alt}:{tag}"
                    if digest:
                        alt = f"{alt}@{digest}"
                    aliases.add(alt)
                elif "/" not in rest:
                    alt = f"docker.io/library/{rest}"
                    if tag:
                        alt = f"{alt}:{tag}"
                    if digest:
                        alt = f"{alt}@{digest}"
                    aliases.add(alt)
            return aliases

        name_aliases = _dockerhub_aliases(name)
        try:
            r = self._run_ok([self._bin, "images", "--format", "json"], allow_fail=True)
            arr = json.loads(r.out or "[]")
            for it in arr:
                names = []
                repo = it.get("Repository") or it.get("Repositories") or []
                tag = it.get("Tag") or it.get("Tags") or []
                if isinstance(repo, list):
                    repos = repo
                elif repo:
                    repos = [repo]
                else:
                    repos = []
                if isinstance(tag, list):
                    tags = tag
                elif tag:
                    tags = [tag]
                else:
                    tags = []
                for rp in repos or []:
                    for tg in tags or []:
                        names.append(f"{rp}:{tg}")
                for n in it.get("Names") or []:
                    names.append(n)
                # Direct hit (including docker.io/library alias)
                if any(n in name_aliases for n in names):
                    return True
                # Treat default-registry qualified names as matching the unqualified input and vice versa
                # Examples: docker.io/mendhak/http-https-echo:37 ≈ mendhak/http-https-echo:37
                if "/" in name:
                    if any(n.endswith(f"/{name}") for n in names):
                        return True
                else:
                    if any(n.endswith(f"/{name}") for n in names):
                        return True
        except Exception:
            return False
        return False
