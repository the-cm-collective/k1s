"""Podman-backed runtime adapter using OCI runtimes via Podman CLI.

This avoids the Docker daemon and talks to the system's OCI runtime through Podman.
It implements the same labels and behaviors used by DockerRuntime so the rest
of the system (ingress, status, events) continues to work unchanged.
"""

# ruff: noqa: E501,S110,S112,S603,S607,SIM105,SIM118,UP022,UP028
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime

from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    app_key_for_manifest,
    runtime_labels_for_manifest,
    split_app_key,
)

from .base import ReplicaState, RuntimeAdapter, RuntimeResult
from .ports import choose_host_port


@dataclass
class _RunResult:
    code: int
    out: str
    err: str


LOGGER = logging.getLogger(__name__)


class PodmanRuntime(RuntimeAdapter):
    APP_LABEL = "ae.app"
    REPLICA_LABEL = "ae.replica_id"
    REVISION_LABEL = "ae.revision"
    CONTAINER_LABEL = "ae.container"
    JOB_ATTEMPT_LABEL = "ae.job_attempt"

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
        replica_ids: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
        app = app_key_for_manifest(manifest)
        desired_ids = (
            list(replica_ids)
            if replica_ids is not None
            else [f"{app}-rev{revision}-{i}" for i in range(manifest.spec.replicas)]
        )
        self._current_node_id = node_id
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
            return (obj.get("Config") or {}).get("Labels") or {}

        by_replica = {_labels(c).get(self.REPLICA_LABEL): c for c in existing if _labels(c)}
        old = [c for c in existing if _labels(c).get(self.REVISION_LABEL) != str(revision)]

        created = updated = removed = 0

        # Ensure image is available locally (best-effort). Avoid unconditional pulls
        # which can block startup when registry access is slow/unavailable. Mirror
        # DockerRuntime behavior: pull only if the image is not present locally.
        image = manifest.spec.image
        if not self._image_exists(image):
            self._run_ok([self._bin, "pull", image], allow_fail=True)
        if not self._image_exists(image):
            # import from Docker daemon if present
            try:
                import shutil as _sh

                if _sh.which("docker") is not None:
                    di = subprocess.run(
                        ["docker", "image", "inspect", image],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    if di.returncode == 0:
                        self._run_ok([self._bin, "pull", f"docker-daemon:{image}"], allow_fail=True)
            except Exception:
                pass
        if not self._image_exists(image) and "/" not in image:
            # try localhost/<image> (podman local)
            self._run_ok([self._bin, "pull", f"localhost/{image}"], allow_fail=True)

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
            c = by_replica.get(rid)
            if c is None:
                if limit_create is not None and created >= int(limit_create):
                    continue
                self._create_container(
                    manifest,
                    rid,
                    revision,
                    service=(svc_port, svc_target, svc_ports_list),
                    node_id=node_id,
                )
                # If a shared network is configured, connect the new container to it
                if self._network_name:
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
                                manifest,
                                rid,
                                revision,
                                service=(svc_port, svc_target, svc_ports_list),
                                node_id=node_id,
                                attempt=attempt + 1,
                            )
                            if self._network_name:
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
                self._ensure_sidecars(manifest, rid, revision)
            except Exception:
                pass

        if not keep_old:
            for c in old:
                self._stop_and_remove(c.get("Id", ""))
                removed += 1

        # Compose replica states
        final = self._list_app_containers(app)
        states: list[ReplicaState] = []
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
            rid = labs.get(self.REPLICA_LABEL) or ""
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
                            loop_host = (
                                "[::1]" if hip.startswith("[") or hip == "::" else "127.0.0.1"
                            )
                            endpoint = f"{loop_host}:{hp}"
                # 2) common HTTP ports
                if endpoint is None:
                    for cp in (80, 8080):
                        binds = (pmap or {}).get(f"{int(cp)}/tcp")
                        if binds:
                            b0 = binds[0] or {}
                            hp = b0.get("HostPort")
                            if hp:
                                hip = (b0.get("HostIp") or "").strip()
                                loop_host = (
                                    "[::1]" if hip.startswith("[") or hip == "::" else "127.0.0.1"
                                )
                                endpoint = f"{loop_host}:{hp}"
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
                            loop_host = (
                                "[::1]" if hip.startswith("[") or hip == "::" else "127.0.0.1"
                            )
                            endpoint = f"{loop_host}:{hp}"
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
                                if host:
                                    # host may be "0.0.0.0:PORT" or "[::]:PORT"; use 127.0.0.1 or ::1 accordingly
                                    hp = host.split(":")[-1].strip()
                                    if hp.isdigit():
                                        loop_host = "[::1]" if host.startswith("[") else "127.0.0.1"
                                        endpoint = f"{loop_host}:{hp}"
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
                ReplicaState(
                    replica_id=rid,
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
            replica_states=states,
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
                    labels = runtime_labels_for_manifest(manifest, app_name=app)
                    labels.update(
                        {
                            self.REPLICA_LABEL: replica_id,
                            self.REVISION_LABEL: str(revision),
                            self.CONTAINER_LABEL: cname,
                        }
                    )
                    cmd = [
                        self._bin,
                        "run",
                        "-d",
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
                            cmd += ["-v", f"{vol_name}:{getattr(s, 'mount_path', '')}:rw"]
                    for v in getattr(manifest.spec, "volumes", []) or []:
                        host = getattr(v, "host_path", None)
                        mnt = getattr(v, "mount_path", None)
                        ro = bool(getattr(v, "read_only", False))
                        if host and mnt:
                            if host and not os.path.isabs(host):
                                host = os.path.abspath(host)
                            cmd += ["-v", f"{host}:{mnt}:{'ro' if ro else 'rw'}"]
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
                    for item in getattr(csp, "env", []) or []:
                        if isinstance(item, dict) and "name" in item and "value" in item:
                            cmd += ["-e", f"{item['name']}={item['value']}"]
                    # Image and command
                    img = getattr(csp, "image")  # noqa: B009
                    cmd += [img]
                    combined: list[str] = []
                    combined += [str(x) for x in (getattr(csp, "command", []) or [])]  # noqa: B009
                    combined += [str(x) for x in (getattr(csp, "args", []) or [])]  # noqa: B009
                    cmd += combined
                    self._run_ok(cmd, allow_fail=True)
            except Exception:
                continue

    def read_logs(
        self,
        replica_id: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        # Find container by label
        cid = self._find_by_label(self.REPLICA_LABEL, replica_id)
        if not cid:
            # Fallback: scan ps JSON and match Config.Labels
            try:
                r = self._run_ok([self._bin, "ps", "-a", "--format", "json"], allow_fail=True)
                arr = json.loads(r.out or "[]")
                for it in arr:
                    labels = (it.get("Config") or {}).get("Labels") or {}
                    if labels.get(self.REPLICA_LABEL) == replica_id:
                        cid = it.get("Id") or it.get("Names", [None])[0]
                        break
            except Exception:
                pass
        # Fallback to well-known container name if label lookup fails
        if not cid:
            cid = f"ae-{replica_id}"
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
        replica_id: str,
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
        # Locate container by replica_id label
        cid = None
        try:
            labels = [f"label={self.REPLICA_LABEL}={replica_id}"]
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
            if debug:
                LOGGER.warning("podman exec_attach lookup %s => %s", cmd_list, cid)
        except Exception as exc:
            if debug:
                LOGGER.warning("podman exec_attach lookup failed: %s", exc)
            cid = None
        if not cid:
            raise RuntimeError("Replica not found for exec")

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
                except BlockingIOError:
                    raise TimeoutError

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
    def run_init_containers(self, manifest):  # type: ignore[override]
        """Run initContainers sequentially with optional timeouts.

        Returns a list of tuples: (name, rc, message).
        """
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
                for item in (
                    getattr(c, "env", None) or (c.get("env") if isinstance(c, dict) else []) or []
                ):
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        argv += ["-e", f"{item['name']}={item['value']}"]
            except Exception:
                pass
            # Volumes: mount app storage and hostPath volumes, plus projected config root when present
            try:
                if getattr(manifest.spec, "storage", None):
                    for s in manifest.spec.storage:
                        vol_name = self._storage_volume_name(
                            app_key_for_manifest(manifest), getattr(s, "name", "")
                        )
                        mnt = getattr(s, "mount_path", None)
                        if vol_name and mnt:
                            argv += ["-v", f"{vol_name}:{mnt}:rw"]
                for v in getattr(manifest.spec, "volumes", []) or []:
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

    def exec(self, replica_id: str, command: list[str], *, timeout: int | None = None) -> int:  # type: ignore[override]
        # Locate container by label
        cid = self._find_by_label(self.REPLICA_LABEL, replica_id)
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
                self.REPLICA_LABEL: replica_id,
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
        for item in manifest.spec.env or []:
            if "name" in item and "value" in item:
                cmd += ["-e", f"{item['name']}={item['value']}"]

        # Ports: publish service stable port if replicas==1. If no Service is defined
        # but the manifest declares ports, publish ephemeral host ports for exposed
        # container ports (Docker parity: docker-py maps {"8080/tcp": None}). This
        # allows the controller (host) to probe readiness via 127.0.0.1:ephemeral and
        # lets Caddy proxy via host alias inside the container.
        svc_port, svc_target, svc_ports_list = service
        published_any = False
        reserved_ports: set[int] = set()
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
                    portnum = getattr(sp, "port", None)
                    tgt = getattr(sp, "target_port", None)
                    name = getattr(sp, "name", None)
                    if tgt is None:
                        tgt = by_name.get(name) or (
                            by_num.get(int(portnum)) if portnum is not None else None
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
                cmd += ["-v", f"{vol_name}:{s.mount_path}:rw"]
        if manifest.spec.volumes:
            for v in manifest.spec.volumes:
                mode = "ro" if v.read_only else "rw"
                host = v.host_path
                if host and not os.path.isabs(host):
                    host = os.path.abspath(host)
                cmd += ["-v", f"{host}:{v.mount_path}:{mode}"]

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
