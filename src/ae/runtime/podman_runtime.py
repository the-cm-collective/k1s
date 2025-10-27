"""Podman-backed runtime adapter using OCI runtimes via Podman CLI.

This avoids the Docker daemon and talks to the system's OCI runtime through Podman.
It implements the same labels and behaviors used by DockerRuntime so the rest
of the system (ingress, status, events) continues to work unchanged.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ae.controller.spec import AppManifest

from .base import ReplicaState, RuntimeAdapter, RuntimeResult


@dataclass
class _RunResult:
    code: int
    out: str
    err: str


class PodmanRuntime(RuntimeAdapter):
    APP_LABEL = "ae.app"
    REPLICA_LABEL = "ae.replica_id"
    REVISION_LABEL = "ae.revision"

    def __init__(self) -> None:
        self._bin = os.getenv("AE_PODMAN_BIN", "podman")
        # Optional shared network for ingress to reach containers by DNS name
        self._network_name = os.getenv("AE_PODMAN_NETWORK")

    # Core ops ---------------------------------------------------------
    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
    ) -> RuntimeResult:
        app = manifest.metadata.name
        desired_ids = [f"{app}-rev{revision}-{i}" for i in range(manifest.spec.replicas)]

        # Find existing containers for this app
        existing = self._list_app_containers(app)
        def _labels(obj: dict) -> dict:
            return (obj.get("Config") or {}).get("Labels") or {}
        by_replica = { _labels(c).get(self.REPLICA_LABEL): c for c in existing if _labels(c) }
        old = [c for c in existing if _labels(c).get(self.REVISION_LABEL) != str(revision)]

        created = updated = removed = 0

        # Ensure image is available locally (best-effort)
        image = manifest.spec.image
        self._run_ok([self._bin, "pull", image], allow_fail=True)
        if not self._image_exists(image):
            # import from Docker daemon if present
            try:
                import shutil as _sh
                if _sh.which("docker") is not None:
                    di = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    if di.returncode == 0:
                        self._run_ok([self._bin, "pull", f"docker-daemon:{image}"], allow_fail=True)
            except Exception:
                pass
        if not self._image_exists(image) and "/" not in image:
            # try localhost/<image> (podman local)
            self._run_ok([self._bin, "pull", f"localhost/{image}"], allow_fail=True)

        svc_port = None
        svc_target = None
        if getattr(manifest.spec, "service", None) and manifest.spec.replicas == 1:
            svc_port = manifest.spec.service.port
            svc_target = manifest.spec.service.target_port

        for rid in desired_ids:
            c = by_replica.get(rid)
            if c is None:
                if limit_create is not None and created >= int(limit_create):
                    continue
                self._create_container(manifest, rid, revision, service=(svc_port, svc_target))
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
                            self._run_ok([self._bin, "network", "connect", "--alias", al, self._network_name, cid], allow_fail=True)
                created += 1
            else:
                # Ensure running
                if (c.get("State") or {}).get("Status") != "running":
                    self._run_ok([self._bin, "start", c.get("Id", "")])
                    updated += 1

        if not keep_old:
            for c in old:
                self._stop_and_remove(c.get("Id", ""))
                removed += 1

        # Compose replica states
        final = self._list_app_containers(app)
        states: list[ReplicaState] = []
        for c in final:
            labs = (c.get("Config") or {}).get("Labels") or {}
            if labs.get(self.REVISION_LABEL) != str(revision):
                continue
            rid = labs.get(self.REPLICA_LABEL) or ""
            st = (c.get("State") or {}).get("Status", "")
            started = self._parse_dt((c.get("State") or {}).get("StartedAt"))
            endpoint = None
            # Prefer published host ports (so Caddy can reach via host alias),
            # fall back to container DNS name only if no host port is published.
            try:
                pmap = (c.get("NetworkSettings") or {}).get("Ports") or {}
                # Prefer host-published ports
                for k, binds in (pmap or {}).items():
                    if not binds:
                        continue
                    hp = (binds[0] or {}).get("HostPort")
                    if hp:
                        endpoint = f"127.0.0.1:{hp}"
                        break
                if endpoint is None:
                    # Fallback to `podman port <id>` which reliably reports published mappings
                    cid = c.get("Id") or ""
                    if cid:
                        pr = self._run_ok([self._bin, "port", cid], allow_fail=True)
                        # Expected lines like: "8080/tcp -> 0.0.0.0:49213"
                        for line in (pr.out or "").splitlines():
                            try:
                                _lhs, _arrow, rhs = line.partition("->")
                                host = rhs.strip()
                                if host:
                                    # host may be "0.0.0.0:PORT" or "[::]:PORT"; use 127.0.0.1
                                    hp = host.split(":")[-1].strip()
                                    if hp.isdigit():
                                        endpoint = f"127.0.0.1:{hp}"
                                        break
                            except Exception:
                                continue
                if endpoint is None and pmap:
                    # Last resort: container DNS name (only usable from other containers on the network)
                    for k in (pmap or {}).keys():
                        port = k.split("/")[0]
                        if port:
                            endpoint = f"{(c.get('Name','').lstrip('/'))}:{port}"
                            break
            except Exception:
                pass
            states.append(ReplicaState(replica_id=rid, ready=(st == "running"), status=st or "", endpoint=endpoint, started_at=started))

        return RuntimeResult(revision=revision, created=created, updated=updated, removed=removed, replica_states=states)

    def read_logs(self, replica_id: str, *, follow: bool = False, tail: int | None = None, since: int | None = None):
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
                with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as proc:  # type: ignore
                    if proc.stdout is not None:
                        for line in proc.stdout:
                            yield line.rstrip("\n")
            except Exception:
                return
        else:
            res = self._run_ok(cmd, allow_fail=True)
            for line in (res.out or "").splitlines():
                yield line

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

    # Volumes ----------------------------------------------------------
    def _storage_volume_name(self, app_name: str, vol_name: str) -> str:
        return f"ae-{app_name}-{vol_name}"

    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:  # type: ignore[override]
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
                self._run_ok([self._bin, "volume", "create", "--label", f"{self.APP_LABEL}={app_name}", "--label", f"ae.volume={name}", vol], allow_fail=False)

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
            out.append({"name": it.get("Name", ""), "labels": labels, "driver": it.get("Driver", ""), "mountpoint": it.get("Mountpoint", "")})
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
            labels = (it.get("Config") or {}).get("Labels") or {}
            host_ports: list[int] = []
            try:
                insp = self._run_ok([self._bin, "inspect", it.get("Id", ""), "--format", "json"], allow_fail=True)
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
            except Exception:
                pass
            out.append({"name": it.get("Names", [it.get("Id", "")])[0], "labels": labels, "host_ports": host_ports})
        return out

    # Helpers ----------------------------------------------------------
    def _create_container(self, manifest: AppManifest, replica_id: str, revision: int, *, service: tuple[int | None, int | None] = (None, None)) -> None:
        app = manifest.metadata.name
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

        cmd = [self._bin, "run", "-d", "--name", name,
               "--label", f"{self.APP_LABEL}={app}",
               "--label", f"{self.REPLICA_LABEL}={replica_id}",
               "--label", f"{self.REVISION_LABEL}={revision}",
               "--restart", "unless-stopped"]
        try:
            stop_timeout = int(getattr(manifest.spec, "termination_grace_period_seconds", 10) or 10)
        except Exception:
            stop_timeout = 10
        cmd += ["--label", f"ae.stop_timeout={int(stop_timeout)}"]

        # Env
        for item in (manifest.spec.env or []):
            if "name" in item and "value" in item:
                cmd += ["-e", f"{item['name']}={item['value']}"]

        # Ports: publish service stable port if replicas==1. If no Service is defined
        # but the manifest declares ports, publish ephemeral host ports for exposed
        # container ports (Docker parity: docker-py maps {"8080/tcp": None}). This
        # allows the controller (host) to probe readiness via 127.0.0.1:ephemeral and
        # lets Caddy proxy via host alias inside the container.
        svc_port, svc_target = service
        published_any = False
        if svc_port is not None:
            target = int(svc_target) if svc_target is not None else int(svc_port)
            cmd += ["-p", f"{int(svc_port)}:{target}"]
            published_any = True
        else:
            for p in (manifest.spec.ports or []):
                try:
                    host = int(getattr(p, "hostPort", 0) or 0)
                    cport = int(getattr(p, "container_port", 0) or getattr(p, "containerPort", 0) or 0)
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

        # Image and command
        cmd += [manifest.spec.image]
        # If unqualified name missing but localhost/<name> exists, use that
        if "/" not in manifest.spec.image and not self._image_exists(manifest.spec.image) and self._image_exists(f"localhost/{manifest.spec.image}"):
            cmd[-1] = f"localhost/{manifest.spec.image}"
        if manifest.spec.command:
            if isinstance(manifest.spec.command, (list, tuple)):
                cmd += [str(x) for x in manifest.spec.command]
            else:
                cmd += shlex.split(str(manifest.spec.command))

        self._run_ok(cmd)

    def _stop_and_remove(self, cid: str) -> None:
        if not cid:
            return
        # Inspect label for per-container stop timeout
        timeout = 10
        try:
            r = self._run_ok([self._bin, "inspect", cid, "--format", "{{json .Config.Labels}}"], allow_fail=True)
            import json as _json

            labels = _json.loads(r.out or "{}") or {}
            if isinstance(labels, dict) and labels.get("ae.stop_timeout"):
                timeout = int(str(labels.get("ae.stop_timeout")))
        except Exception:
            pass
        self._run_ok([self._bin, "stop", "-t", str(int(timeout)), cid], allow_fail=True)
        self._run_ok([self._bin, "rm", "-f", cid], allow_fail=True)

    def _list_app_containers(self, app: str) -> list[dict]:
        r = self._run_ok([self._bin, "ps", "-a", "--filter", f"label={self.APP_LABEL}={app}", "--format", "json"], allow_fail=True)
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

    def _find_by_label(self, key: str, value: str) -> Optional[str]:
        r = self._run_ok([self._bin, "ps", "-a", "--filter", f"label={key}={value}", "--format", "{{.ID}}"], allow_fail=True)
        cid = (r.out or "").strip().splitlines()
        return cid[0] if cid else None

    def _parse_dt(self, raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        try:
            s = str(raw).rstrip("Z")
            return datetime.fromisoformat(s)
        except Exception:
            return None

    def _run_ok(self, argv: list[str], *, allow_fail: bool = False) -> _RunResult:
        try:
            cp = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if cp.returncode != 0 and not allow_fail:
                raise RuntimeError(f"podman failed: {' '.join(argv)} => {cp.stderr.strip()}")
            return _RunResult(cp.returncode, cp.stdout or "", cp.stderr or "")
        except FileNotFoundError:
            raise RuntimeError("podman binary not found. Install Podman or set AE_PODMAN_BIN")

    def _image_exists(self, name: str) -> bool:
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
                for n in (it.get("Names") or []):
                    names.append(n)
                if name in names:
                    return True
                if "/" not in name and any(n.endswith(f"/{name}") for n in names):
                    return True
        except Exception:
            return False
        return False
