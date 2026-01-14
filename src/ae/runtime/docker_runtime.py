"""Docker-backed runtime adapter for managing application replicas."""

# ruff: noqa: E501,S110,S112,S603,S607,S104,SIM105,SIM118,UP022,UP028,B009
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from datetime import datetime

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from ae.controller.spec import AppManifest, PortSpec
from ae.runtime.ports import choose_host_port

from .base import ReplicaState, RuntimeAdapter, RuntimeResult
from .registry import RegistryAuthProvider

LOGGER = logging.getLogger(__name__)


class DockerRuntime(RuntimeAdapter):
    """Ensures Docker containers match the desired manifest state."""

    APP_LABEL = "ae.app"
    REPLICA_LABEL = "ae.replica_id"
    REVISION_LABEL = "ae.revision"
    CONTAINER_LABEL = "ae.container"

    def __init__(
        self,
        client: docker.DockerClient | None = None,
        registry_auth: RegistryAuthProvider | None = None,
    ) -> None:
        try:
            self._client = client or docker.from_env()
        except Exception as exc:  # pragma: no cover - defensive guard, validated in tests
            raise RuntimeError(f"Failed to initialize Docker client: {exc}") from exc
        self._registry = registry_auth or RegistryAuthProvider()
        # Optional shared network so that ingress (Caddy) can reach containers by name
        import os as _os

        self._network_name = _os.getenv("AE_DOCKER_NETWORK") or _os.getenv("AE_NETWORK_NAME")
        self._serial_service_rollout = _os.getenv("AE_SERIAL_SERVICE_ROLLOUT", "0") == "1"

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
        app_name = manifest.metadata.name
        desired_replica_ids = list(replica_ids) if replica_ids is not None else self._desired_replica_ids(manifest, revision)
        # Record node context so volume helpers can label ownership
        self._current_node_id = node_id

        try:
            existing_containers = self._client.containers.list(
                all=True, filters={"label": f"{self.APP_LABEL}={app_name}"}
            )
        except APIError as exc:  # pragma: no cover - network failure path hard to trigger in tests
            raise RuntimeError(f"Failed to list containers for {app_name}: {exc}") from exc

        containers_by_replica: dict[str, Container] = {}
        old_revision_containers: list[Container] = []
        for container in existing_containers:
            # Accessing .labels may trigger an inspect call; guard against races where
            # the container disappears between list() and inspect().
            try:
                labels = container.labels or {}
            except NotFound:
                continue
            replica_label = labels.get(self.REPLICA_LABEL)
            if not replica_label:
                continue
            if labels.get(self.REVISION_LABEL) == str(revision):
                containers_by_replica[replica_label] = container
            else:
                old_revision_containers.append(container)

        created = updated = removed = 0

        strict_service = (
            self._serial_service_rollout
            and not keep_old
            and getattr(manifest.spec, "service", None)
            and manifest.spec.replicas == 1
        )
        if strict_service and old_revision_containers:
            for container in list(old_revision_containers):
                self._stop_and_remove(container)
                removed += 1
            old_revision_containers = []

        if any(replica_id not in containers_by_replica for replica_id in desired_replica_ids):
            self._registry.ensure_login(self._client, manifest.spec.image)
            self._pull_image(manifest)

        for replica_id in desired_replica_ids:
            container = containers_by_replica.get(replica_id)
            if container is None:
                if limit_create is not None and created >= int(limit_create):
                    continue
                container = self._create_container(manifest, replica_id, revision, node_id=node_id)
                containers_by_replica[replica_id] = container
                created += 1
            else:
                self._reload(container)
                if container.status != "running":
                    try:
                        container.start()
                        updated += 1
                    except APIError as exc:
                        raise RuntimeError(
                            f"Failed to start container {container.name}: {exc}"
                        ) from exc

        if not keep_old:
            for container in old_revision_containers:
                self._stop_and_remove(container)
                removed += 1

        final_containers = self._client.containers.list(
            all=True, filters={"label": f"{self.APP_LABEL}={app_name}"}
        )
        replica_states = [
            self._build_state(manifest, container)
            for container in final_containers
            if container.labels.get(self.REPLICA_LABEL)
            and container.labels.get(self.REVISION_LABEL) == str(revision)
        ]

        return RuntimeResult(
            revision=revision,
            created=created,
            updated=updated,
            removed=removed,
            replica_states=replica_states,
        )

    def read_logs(
        self,
        replica_id: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        """Stream logs for a container labeled with the replica id."""
        try:
            containers = self._client.containers.list(
                all=True,
                filters={"label": f"{self.REPLICA_LABEL}={replica_id}"},
            )
        except APIError as exc:
            raise RuntimeError(f"Failed to query logs for {replica_id}: {exc}") from exc
        if not containers:
            return iter(())
        container = containers[0]
        try:
            # Include timestamps so the UI can render time-prefixed entries.
            if follow:
                for chunk in container.logs(
                    stdout=True,
                    stderr=True,
                    stream=True,
                    follow=True,
                    tail=tail or "all",
                    since=since,
                    timestamps=True,
                ):
                    yield chunk.decode("utf-8", "replace").rstrip("\n")
            else:
                output = container.logs(
                    stdout=True,
                    stderr=True,
                    stream=False,
                    tail=tail or 200,
                    since=since,
                    timestamps=True,
                )
                text = output.decode("utf-8", "replace")
                for line in text.splitlines():
                    yield line
        except APIError as exc:
            raise RuntimeError(f"Failed to read logs for {replica_id}: {exc}") from exc

    def exec(self, replica_id: str, command: list[str], *, timeout: int | None = None) -> int:  # type: ignore[override]
        _ = timeout
        try:
            containers = self._client.containers.list(
                all=True, filters={"label": f"{self.REPLICA_LABEL}={replica_id}"}
            )
        except APIError as exc:  # pragma: no cover
            raise RuntimeError(f"Failed to locate container for exec: {exc}") from exc
        if not containers:
            return 127
        c = containers[0]
        try:
            exec_id = self._client.api.exec_create(c.id, cmd=command)
            self._client.api.exec_start(exec_id, stream=False, detach=False, tty=False)
            # fetch exit code
            info = self._client.api.exec_inspect(exec_id)
            return int(info.get("ExitCode", 1))
        except APIError:
            return 1

    def remove_app(self, app_name: str) -> int:
        """Stop and remove all containers for a given app label."""
        try:
            containers = self._client.containers.list(
                all=True, filters={"label": f"{self.APP_LABEL}={app_name}"}
            )
        except APIError as exc:
            raise RuntimeError(f"Failed to list containers for {app_name}: {exc}") from exc
        removed = 0
        for c in containers:
            self._stop_and_remove(c)
            removed += 1
        return removed

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        try:
            containers = self._client.containers.list(
                all=True, filters={"label": f"{self.APP_LABEL}={app_name}"}
            )
        except APIError as exc:
            raise RuntimeError(f"Failed to list containers for {app_name}: {exc}") from exc
        removed = 0
        for c in containers:
            if c.labels.get(self.REVISION_LABEL) != str(keep_revision):
                self._stop_and_remove(c)
                removed += 1
        return removed

    # Internal helpers -------------------------------------------------

    def _desired_replica_ids(self, manifest: AppManifest, revision: int) -> list[str]:
        return [
            f"{manifest.metadata.name}-rev{revision}-{replica}"
            for replica in range(manifest.spec.replicas)
        ]

    def _pull_image(self, manifest: AppManifest) -> None:
        image_ref = manifest.spec.image
        try:
            self._client.images.get(image_ref)
            LOGGER.debug("Image %s already present locally; skipping pull", image_ref)
            return
        except NotFound:
            LOGGER.debug("Image %s not found locally; attempting pull", image_ref)
        try:
            LOGGER.debug("Pulling image %s", image_ref)
            self._client.images.pull(image_ref)
        except APIError as exc:
            raise RuntimeError(f"Failed to pull image {image_ref}: {exc}") from exc

    def _create_container(
        self,
        manifest: AppManifest,
        replica_id: str,
        revision: int,
        *,
        node_id: str | None = None,
    ) -> Container:
        # replica_id pattern: <app>-rev<revision>-<index>
        app_name = manifest.metadata.name
        replica_suffix = replica_id.split("-")[-1]
        name = f"ae-{app_name}-rev{revision}-{replica_suffix}"
        env = self._manifest_env(manifest)
        # Build port mapping; if a Service is specified and replicas==1, publish a stable host port
        svc_port = None
        svc_target = None
        svc_ports_list = None
        if getattr(manifest.spec, "service", None) and manifest.spec.replicas == 1:
            svc_port = getattr(manifest.spec.service, "port", None)
            svc_target = getattr(manifest.spec.service, "target_port", None)
            # Optional multi-port publishing for single-replica services
            if getattr(manifest.spec.service, "ports", None):
                svc_ports_list = list(manifest.spec.service.ports)
        ports, svc_bindings = self._port_mapping(
            manifest.spec.ports,
            app_name,
            service_port=svc_port,
            service_target=svc_target,
            service_ports=svc_ports_list,
        )
        # Pre-flight conflict check for any published service host ports. This only raises
        # when another app already owns the same host port, allowing single-app rollouts
        # to fall back to different ports when necessary.
        for host_port in {hp for hp in svc_bindings.values() if hp is not None}:
            self._ensure_host_port_free(app_name, int(host_port))

        # resource limits
        nano_cpus = None
        mem_limit = None
        cpu_shares = None
        mem_reservation = None
        if manifest.spec.resources and manifest.spec.resources.limits:
            limits = manifest.spec.resources.limits
            if limits.cpu is not None:
                try:
                    nano_cpus = int(float(limits.cpu) * 1_000_000_000)
                except ValueError:
                    nano_cpus = None
            if limits.memory is not None:
                mem_limit = self._parse_memory_bytes(str(limits.memory))
        # resource requests → soft reservations
        if manifest.spec.resources and manifest.spec.resources.requests:
            reqs = manifest.spec.resources.requests
            if reqs.cpu is not None:
                try:
                    # Docker cpu-shares: 1024 ≈ 1 CPU share
                    cpu_shares = max(2, int(float(reqs.cpu) * 1024))
                except ValueError:
                    cpu_shares = None
            if reqs.memory is not None:
                mem_reservation = self._parse_memory_bytes(str(reqs.memory))

        # volumes
        volumes = {}
        if manifest.spec.volumes:
            for v in manifest.spec.volumes:
                mode = "ro" if v.read_only else "rw"
                host_path = v.host_path
                # Docker bind mounts require absolute host paths; relative paths are treated
                # as named volumes and will be rejected if they contain '/'. Make absolute.
                if host_path and not os.path.isabs(host_path):
                    host_path = os.path.abspath(host_path)
                volumes[host_path] = {"bind": v.mount_path, "mode": mode}
        if getattr(manifest.spec, "storage", None):
            self.ensure_storage_volumes(
                manifest.metadata.name, [s.model_dump() for s in manifest.spec.storage]
            )
            for s in manifest.spec.storage:
                vol_name = self._storage_volume_name(manifest.metadata.name, s.name)
                volumes[vol_name] = {"bind": s.mount_path, "mode": "rw"}

        try:
            run_fn = self._client.containers.run
            # Build standard kwargs. Do NOT filter by signature; docker-py forwards **kwargs.
            # Filtering here accidentally dropped 'ports', preventing host port publishing.
            # Build command/args following K8s semantics:
            # - If both command and args are set, pass command + args
            # - If only args are set, pass args (ENTRYPOINT receives them)
            _cmd: list[str] | None = None
            if getattr(manifest.spec, "command", None) and getattr(manifest.spec, "args", None):
                _cmd = list(manifest.spec.command) + list(manifest.spec.args)
            elif getattr(manifest.spec, "command", None):
                _cmd = list(manifest.spec.command)
            elif getattr(manifest.spec, "args", None):
                _cmd = list(manifest.spec.args)
            kwargs = {
                "command": _cmd or None,
                "name": name,
                "detach": True,
                "environment": env if env else None,
                "labels": {
                    self.APP_LABEL: manifest.metadata.name,
                    self.REPLICA_LABEL: replica_id,
                    self.REVISION_LABEL: str(revision),
                    self.CONTAINER_LABEL: "main",
                    **({"ae.node": str(node_id)} if node_id else {}),
                },
                "ports": ports if ports else None,
                "restart_policy": {"Name": "unless-stopped"},
            }
            # Security context mapping
            sec = getattr(manifest.spec, "security", None)
            if sec is not None:
                user = None
                if getattr(sec, "run_as_user", None) is not None:
                    if getattr(sec, "run_as_group", None) is not None:
                        user = f"{int(sec.run_as_user)}:{int(sec.run_as_group)}"
                    else:
                        user = str(int(sec.run_as_user))
                if user:
                    kwargs["user"] = user
                if bool(getattr(sec, "read_only_root", False)):
                    kwargs["read_only"] = True
                drops = list(getattr(sec, "drop_caps", []) or [])
                if drops:
                    kwargs["cap_drop"] = drops
                # Map seccomp and AppArmor to Docker security_opt
                secopts: list[str] = []
                try:
                    s_type = getattr(sec, "seccomp_type", None)
                    s_local = getattr(sec, "seccomp_localhost_profile", None)
                    if s_type:
                        st = str(s_type)
                        if st == "RuntimeDefault":
                            # Let Docker apply its default seccomp; do NOT pass
                            # the Kubernetes token "runtime/default" which Docker
                            # interprets as a literal JSON path and fails to parse.
                            pass
                        elif st == "Unconfined":
                            secopts.append("seccomp=unconfined")
                        elif st == "Localhost" and s_local:
                            # Docker expects a path to a local profile JSON.
                            # Be defensive: only append if the file exists.
                            try:
                                if os.path.exists(str(s_local)):
                                    secopts.append(f"seccomp={s_local}")
                            except Exception:
                                pass
                    a_prof = getattr(sec, "apparmor_profile", None)
                    if a_prof:
                        ap = str(a_prof)
                        if ap.startswith("localhost/"):
                            ap = ap.split("/", 1)[1]
                        if ap == "runtime/default":
                            ap = "docker-default"
                        secopts.append(f"apparmor={ap}")
                except Exception:
                    # Be permissive if any mapping input is malformed
                    pass
                if secopts:
                    kwargs["security_opt"] = secopts
            if nano_cpus is not None:
                kwargs["nano_cpus"] = nano_cpus
            if mem_limit is not None:
                kwargs["mem_limit"] = mem_limit
            if cpu_shares is not None:
                kwargs["cpu_shares"] = cpu_shares
            if mem_reservation is not None:
                kwargs["mem_reservation"] = mem_reservation
            if volumes:
                kwargs["volumes"] = volumes
            if getattr(manifest.spec, "working_dir", None):
                kwargs["working_dir"] = str(manifest.spec.working_dir)

            container = run_fn(
                manifest.spec.image, **{k: v for k, v in kwargs.items() if v is not None}
            )
            # Attach to shared network if configured
            if self._network_name:
                try:
                    net = self._client.networks.get(self._network_name)
                    aliases = [
                        container.name,
                        f"app-{app_name}",
                        f"app-{app_name}-rev{revision}",
                    ]
                    net.connect(container, aliases=aliases)
                except Exception as _exc:  # pragma: no cover - optional path
                    LOGGER.warning(
                        "Failed to connect %s to network %s: %s", name, self._network_name, _exc
                    )
            # Record per-container stop timeout label for graceful shutdowns
            stop_timeout = int(getattr(manifest.spec, "termination_grace_period_seconds", 10) or 10)
            # docker-py doesn't allow setting custom labels post-create via kwargs; update label map
            try:
                lbls = container.labels or {}
                lbls["ae.stop_timeout"] = str(int(stop_timeout))
                container.update(labels=lbls)
            except Exception:
                pass
            self._reload(container)
            # Create/ensure sidecars if declared
            try:
                self._ensure_sidecars(manifest, replica_id, revision, volumes)
            except Exception:
                pass
            return container
        except APIError as exc:
            raise RuntimeError(f"Failed to create container {name}: {exc}") from exc

    def _ensure_host_port_free(self, app_name: str, host_port: int) -> None:
        try:
            all_containers = self._client.containers.list(all=True)
        except APIError as exc:  # pragma: no cover
            LOGGER.warning("Port check skipped; failed to list containers: %s", exc)
            return
        for c in all_containers:
            try:
                ports = (c.attrs or {}).get("NetworkSettings", {}).get("Ports", {}) or {}
            except Exception:  # pragma: no cover
                ports = {}
            for bindings in ports.values():
                if not bindings:
                    continue
                for b in bindings:
                    if b and str(b.get("HostPort")) == str(host_port):
                        other_app = (c.labels or {}).get(self.APP_LABEL, "")
                        if other_app != app_name:
                            raise RuntimeError(
                                f"service.port {host_port} is already in use by container {c.name} (app '{other_app}')"
                            )

    def _host_ports_in_use(self) -> set[int]:
        ports_in_use: set[int] = set()
        try:
            containers = self._client.containers.list(all=True)
        except APIError as exc:  # pragma: no cover - best effort
            LOGGER.debug("failed to list containers for host-port scan: %s", exc)
            return ports_in_use
        for container in containers:
            try:
                bindings = (container.attrs or {}).get("NetworkSettings", {}).get("Ports", {}) or {}
            except Exception:  # pragma: no cover - skip containers with unreadable attrs
                continue
            for values in bindings.values():
                if not values:
                    continue
                for item in values:
                    host_port = item.get("HostPort") if isinstance(item, dict) else None
                    if host_port is None:
                        continue
                    try:
                        ports_in_use.add(int(host_port))
                    except (TypeError, ValueError):
                        continue
        return ports_in_use

    def _stop_and_remove(self, container: Container) -> None:
        try:
            LOGGER.debug("Removing container %s", container.name)
            # Prefer per-container label timeout if present
            timeout = 10
            try:
                lbls = container.labels or {}
                if "ae.stop_timeout" in lbls:
                    timeout = int(lbls.get("ae.stop_timeout", "10"))
            except Exception:
                pass
            container.stop(timeout=timeout)
        except APIError as exc:  # pragma: no cover - protective guard
            LOGGER.warning("Failed to stop container %s: %s", container.name, exc)
        try:
            container.remove()
        except (APIError, NotFound) as exc:  # pragma: no cover - container already gone
            LOGGER.warning("Failed to remove container %s: %s", container.name, exc)

    def _ensure_sidecars(
        self,
        manifest: AppManifest,
        replica_id: str,
        revision: int,
        volumes: dict[str, dict],
    ) -> None:
        """Ensure declared sidecar containers (spec.containers) are running for a replica."""
        if not getattr(manifest.spec, "containers", None):
            return
        app_name = manifest.metadata.name
        try:
            existing = self._client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{self.APP_LABEL}={app_name}",
                        f"{self.REPLICA_LABEL}={replica_id}",
                        f"{self.REVISION_LABEL}={revision}",
                    ]
                },
            )
        except APIError:
            existing = []
        by_cname: dict[str, Container] = {}
        for c in existing:
            try:
                cname = (c.labels or {}).get(self.CONTAINER_LABEL)
                if cname and cname != "main":
                    by_cname[cname] = c
            except Exception:
                continue
        # Detect projection host root path
        proj_host_root = None
        for host_path, bind in (volumes or {}).items():
            try:
                b = bind or {}
                dest = b.get("bind") or ""
                if str(dest).startswith(f"/var/run/ae/config/{app_name}"):
                    proj_host_root = host_path
                    break
            except Exception:
                continue
        # Build common env from manifest defaults
        for csp in manifest.spec.containers:
            try:
                cname = str(getattr(csp, "name", "") or "").strip()
                if not cname:
                    continue
                c = by_cname.get(cname)
                if c is None:
                    env_map: dict[str, str] = {}
                    for item in (getattr(csp, "env", []) or []):
                        if isinstance(item, dict) and "name" in item and "value" in item:
                            env_map[item["name"]] = str(item.get("value", ""))
                    # Merge per-container projection mounts
                    vmap = dict(volumes or {})
                    try:
                        for pm in getattr(csp, "projection_mounts", []) or []:
                            p = getattr(pm, "path", None) if not isinstance(pm, dict) else pm.get("path")
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
                                vmap[host] = {"bind": str(mnt), "mode": ("ro" if ro else "rw")}
                    except Exception:
                        pass
                    name_suffix = replica_id.split("-")[-1]
                    full_name = f"ae-{app_name}-rev{revision}-{name_suffix}-{cname}"
                    try:
                        sc = self._client.containers.run(
                            getattr(csp, "image"),  # noqa: B009
                            command=(
                                list(getattr(csp, "command", []) or [])  # noqa: B009
                                + list(getattr(csp, "args", []) or [])  # noqa: B009
                            )
                            or None,
                            name=full_name,
                            detach=True,
                            environment=env_map or None,
                            volumes=vmap or None,
                            labels={
                                self.APP_LABEL: app_name,
                                self.REPLICA_LABEL: replica_id,
                                self.REVISION_LABEL: str(revision),
                                self.CONTAINER_LABEL: cname,
                            },
                            restart_policy={"Name": "unless-stopped"},
                        )
                        # Optional network join
                        if self._network_name:
                            try:
                                net = self._client.networks.get(self._network_name)
                                net.connect(sc)
                            except Exception:
                                pass
                    except APIError:
                        continue
                else:
                    try:
                        c.reload()
                        if c.status != "running":
                            c.start()
                    except Exception:
                        pass
            except Exception:
                continue

    # Logs by container (optional API used by HTTP UI)
    def read_logs_for_container(
        self,
        app_name: str,
        container_name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        try:
            containers = self._client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{self.APP_LABEL}={app_name}",
                        f"{self.CONTAINER_LABEL}={container_name}",
                    ]
                },
            )
        except APIError:
            containers = []
        if not containers:
            return iter(())
        container = containers[0]
        try:
            if follow:
                for chunk in container.logs(
                    stdout=True,
                    stderr=True,
                    stream=True,
                    follow=True,
                    tail=tail or "all",
                    since=since,
                    timestamps=True,
                ):
                    yield chunk.decode("utf-8", "replace").rstrip("\n")
            else:
                output = container.logs(
                    stdout=True,
                    stderr=True,
                    stream=False,
                    tail=tail or 200,
                    since=since,
                    timestamps=True,
                )
                text = output.decode("utf-8", "replace")
                for line in text.splitlines():
                    yield line
        except APIError:
            return iter(())

    # Exec by container name -------------------------------------------
    def exec_for_container(
        self, app_name: str, container_name: str, command: list[str], *, timeout: int | None = None
    ) -> int:  # type: ignore[override]
        _ = timeout
        try:
            containers = self._client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{self.APP_LABEL}={app_name}",
                        f"{self.CONTAINER_LABEL}={container_name}",
                    ]
                },
            )
        except APIError:
            return 127
        if not containers:
            return 127
        c = containers[0]
        try:
            exec_id = self._client.api.exec_create(c.id, cmd=command)
            _ = self._client.api.exec_start(exec_id, stream=False, detach=False, tty=False)
            info = self._client.api.exec_inspect(exec_id)
            return int(info.get("ExitCode", 1))
        except APIError:
            return 1

    # Streaming exec/attach for kubectl exec (SPDY)
    def exec_attach(
        self, replica_id: str, command: list[str], *, container: str | None = None, tty: bool = False
    ):
        """Return (socket, exec_id) for an attached exec session."""
        target = None
        try:
            filters = {"label": [f"{self.REPLICA_LABEL}={replica_id}"]}
            if container:
                filters["label"].append(f"{self.CONTAINER_LABEL}={container}")
            containers = self._client.containers.list(all=True, filters=filters)
            if containers:
                target = containers[0]
        except APIError:
            target = None
        if target is None:
            raise RuntimeError("Replica not found for exec")
        exec_id = self._client.api.exec_create(
            target.id,
            cmd=command,
            stdin=True,
            stdout=True,
            stderr=not tty,
            tty=tty,
        )
        sock = self._client.api.exec_start(exec_id, tty=tty, stream=True, socket=True, demux=False)
        return sock, exec_id

    def exec_resize(self, exec_id: str, *, height: int | None = None, width: int | None = None) -> None:
        try:
            self._client.api.exec_resize(exec_id, height=height, width=width)
        except APIError:
            return

    def exec_exit_code(self, exec_id: str) -> int:
        try:
            info = self._client.api.exec_inspect(exec_id)
            return int(info.get("ExitCode", 0))
        except APIError:
            return 0

    def _build_state(self, manifest: AppManifest, container: Container) -> ReplicaState:
        self._reload(container)
        labels = container.labels or {}
        replica_id = labels.get(self.REPLICA_LABEL, container.name)

        state = container.attrs.get("State", {})
        status = state.get("Status", container.status)
        ready = False
        if "Health" in state:
            ready = state["Health"].get("Status") == "healthy"
        else:
            ready = status == "running"

        # Prefer endpoint that matches readiness probe's declared port (http/tcp)
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
        endpoint = self._endpoint_from_ports(
            manifest.spec.ports, container, preferred=preferred_port
        )

        started_at = self._parse_datetime(state.get("StartedAt"))

        return ReplicaState(
            replica_id=replica_id,
            ready=ready,
            status=status,
            endpoint=endpoint,
            started_at=started_at,
        )

    def _port_mapping(
        self,
        ports: Iterable[PortSpec],
        app_name: str,
        *,
        service_port: int | None = None,
        service_target: int | None = None,
        service_ports: Iterable | None = None,
    ) -> tuple[dict[str, int | None], dict[int, int | None]]:
        mapping: dict[str, int | None] = {}
        first_port = None
        reserved: set[int] = set()
        blocked_ports: set[int] = set()
        if service_port is not None or service_ports is not None:
            blocked_ports = self._host_ports_in_use()
        # If multi-port service mapping is provided, build quick lookup from target->host
        svc_map: dict[int, int | None] = {}
        if service_ports is not None:
            # Build container port name/number map
            try:
                by_name = {p.name: int(p.container_port) for p in ports if getattr(p, "name", None)}
            except Exception:
                by_name = {}
            try:
                by_num = {int(p.container_port): int(p.container_port) for p in ports}
            except Exception:
                by_num = {}
            for sp in service_ports:
                try:
                    tgt = getattr(sp, "target_port", None)
                    name = getattr(sp, "name", None)
                    portnum = getattr(sp, "port", None)
                    if tgt is None:
                        tgt = by_name.get(name) or by_num.get(int(portnum))
                    if tgt is not None and portnum is not None:
                        chosen, used_preferred = choose_host_port(
                            int(portnum), reserved=reserved, blocked=blocked_ports
                        )
                        if chosen is None:
                            LOGGER.warning(
                                "service port %s for app %s is unavailable; skipping publish",
                                portnum,
                                app_name,
                            )
                            continue
                        if not used_preferred:
                            LOGGER.warning(
                                "service port %s for app %s already in use; assigning %s",
                                portnum,
                                app_name,
                                chosen,
                            )
                        svc_map[int(tgt)] = int(chosen)
                except Exception:
                    continue

        for port in ports:
            if first_port is None:
                first_port = port.container_port
            key = f"{port.container_port}/tcp"
            host_port: int | None = None
            if svc_map:
                host_port = svc_map.get(int(port.container_port))
            elif service_port is not None:
                target = service_target if service_target is not None else first_port
                if port.container_port == target:
                    preferred = int(service_port)
                    chosen, used = choose_host_port(
                        preferred, reserved=reserved, blocked=blocked_ports
                    )
                    if chosen is None:
                        LOGGER.warning(
                            "service port %s for app %s is unavailable; skipping publish",
                            preferred,
                            app_name,
                        )
                    else:
                        if not used:
                            LOGGER.warning(
                                "service port %s for app %s already in use; assigning %s",
                                preferred,
                                app_name,
                                chosen,
                            )
                        host_port = int(chosen)
                        svc_map[int(port.container_port)] = int(chosen)
            mapping[key] = host_port
        return mapping, svc_map

    def _endpoint_from_ports(
        self, ports: Iterable[PortSpec], container: Container, *, preferred: int | None = None
    ) -> str | None:
        # It is possible for images to expose ports that don't match the declared
        # manifest. For readiness probing we prefer, in order: a published host
        # port matching the preferred probe port; otherwise a published 80/tcp;
        # otherwise a published 8080/tcp; otherwise the first published non‑443
        # mapping; finally, if on a shared network, container IP/DNS.

        network_ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}

        def _binding_to_endpoint(binding: dict) -> str | None:
            host_ip = binding.get("HostIp", "127.0.0.1")
            # When containers run on remote nodes, loopback/0.0.0.0 is not reachable
            # from the controller. Prefer an advertised node IP when provided; otherwise
            # normalize wildcard/loopback to 127.0.0.1 for local access.
            if host_ip in ("0.0.0.0", "::", "[::]", "127.0.0.1", "::1", "[::1]"):
                host_ip = os.getenv("AE_NODE_ADVERTISE_IP") or "127.0.0.1"
            host_port = binding.get("HostPort")
            if host_port:
                return f"{host_ip}:{host_port}"
            return None

        # 0) Preferred probe port (host-published)
        if preferred is not None:
            binds = network_ports.get(f"{int(preferred)}/tcp")
            if binds:
                ep = _binding_to_endpoint(binds[0])
                if ep:
                    return ep

        # Convenience helper to pick a specific container port if published
        def _pick_port(container_port: int) -> str | None:
            binds = network_ports.get(f"{int(container_port)}/tcp")
            if binds:
                return _binding_to_endpoint(binds[0])
            return None

        # 1) Common HTTP ports
        for cp in (80, 8080):
            ep = _pick_port(cp)
            if ep:
                return ep

        # 2) First published non‑443 mapping
        for key, binds in network_ports.items():
            if not binds:
                continue
            try:
                cport = int(str(key).split("/")[0])
            except Exception:
                cport = None
            if cport == 443:
                continue
            ep = _binding_to_endpoint(binds[0])
            if ep:
                return ep

        # 3) Shared-network fallback using manifest-declared ports (same-host overlay only)
        if self._network_name:
            first = next(iter(ports or []), None)
            if first is not None:
                return f"{container.name}:{first.container_port}"
        return None

    # Init containers ----------------------------------------------------
    def run_init_containers(self, manifest):  # type: ignore[override]
        """Run initContainers sequentially with optional timeouts.

        Returns list of (name, rc, message).
        """
        results: list[tuple[str, int, str]] = []
        inits = getattr(manifest.spec, "init_containers", []) or []
        if not inits:
            return results

        # Build shared volume bindings: hostPath volumes and storage volumes
        volumes = {}
        try:
            if getattr(manifest.spec, "volumes", None):
                for v in manifest.spec.volumes:
                    mode = "ro" if getattr(v, "read_only", False) else "rw"
                    host_path = getattr(v, "host_path", None)
                    if host_path and not os.path.isabs(host_path):
                        host_path = os.path.abspath(host_path)
                    if host_path and getattr(v, "mount_path", None):
                        volumes[host_path] = {"bind": str(v.mount_path), "mode": mode}
        except Exception:
            pass
        try:
            if getattr(manifest.spec, "storage", None):
                self.ensure_storage_volumes(
                    manifest.metadata.name, [s.model_dump() for s in manifest.spec.storage]
                )
                for s in manifest.spec.storage:
                    vol_name = self._storage_volume_name(manifest.metadata.name, s.name)
                    volumes[vol_name] = {"bind": str(s.mount_path), "mode": "rw"}
        except Exception:
            pass

        for c in inits:
            name = (
                getattr(c, "name", None) if not isinstance(c, dict) else c.get("name")
            ) or "init"
            image = (
                getattr(c, "image", None) if not isinstance(c, dict) else c.get("image")
            )
            if not image:
                results.append((str(name), 1, "missing image"))
                continue
            # Timeout
            timeout: int | None = None
            try:
                raw = getattr(c, "timeout_seconds", None) if not isinstance(c, dict) else c.get("timeoutSeconds")
                if raw is not None:
                    timeout = int(raw)
            except Exception:
                timeout = None
            # Command + args
            try:
                command = [str(x) for x in (getattr(c, "command", None) or (c.get("command") if isinstance(c, dict) else []) or [])]
            except Exception:
                command = []
            try:
                args = [str(x) for x in (getattr(c, "args", None) or (c.get("args") if isinstance(c, dict) else []) or [])]
            except Exception:
                args = []
            env_map: dict[str, str] = {}
            try:
                for item in (getattr(c, "env", None) or (c.get("env") if isinstance(c, dict) else []) or []):
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        env_map[item["name"]] = str(item.get("value", ""))
            except Exception:
                env_map = {}

            # Create ephemeral container to enforce timeout via wait()
            cont = None
            try:
                self._pull_image(manifest)
            except Exception:
                # best-effort pull; continue
                pass
            try:
                cont = self._client.containers.create(
                    image,
                    command=(command + args) or None,
                    environment=env_map or None,
                    volumes=volumes or None,
                )
                cont.start()
                if timeout is not None and timeout > 0:
                    try:
                        res = cont.wait(timeout=timeout)
                    except Exception:
                        # Timeout or wait error; stop and remove
                        try:
                            cont.remove(force=True)
                        except Exception:
                            pass
                        results.append((str(name), 124, "timeout"))
                        continue
                else:
                    res = cont.wait()
                rc = int((res or {}).get("StatusCode", 1))
                try:
                    cont.remove(force=True)
                except Exception:
                    pass
                results.append((str(name), rc, "ok" if rc == 0 else "failed"))
            except Exception as exc:
                try:
                    if cont is not None:
                        cont.remove(force=True)
                except Exception:
                    pass
                results.append((str(name), 1, f"error: {exc}"))
        return results

    def _manifest_env(self, manifest: AppManifest) -> dict[str, str]:
        env_map: dict[str, str] = {}
        for item in manifest.spec.env:
            name = item.get("name")
            if not name:
                continue
            if "value" in item:
                env_map[name] = str(item.get("value", ""))
                continue
            # Minimal downward API: support metadata.name and metadata.namespace
            vf = item.get("valueFrom") if isinstance(item, dict) else None
            if isinstance(vf, dict) and isinstance(vf.get("fieldRef"), dict):
                fp = str(vf.get("fieldRef", {}).get("fieldPath", ""))
                if fp == "metadata.name":
                    env_map[name] = manifest.metadata.name
                elif fp == "metadata.namespace":
                    env_map[name] = "default"
                continue
            # Minimal resourceFieldRef support for cpu/memory requests/limits
            if isinstance(vf, dict) and isinstance(vf.get("resourceFieldRef"), dict):
                rfr = vf.get("resourceFieldRef", {}) or {}
                res = str(rfr.get("resource", ""))
                divisor_raw = str(rfr.get("divisor", "")) if rfr.get("divisor") is not None else ""
                try:
                    # CPU in millicores math: convert both base and divisor to mCPU and integer-divide
                    if res in {"limits.cpu", "requests.cpu"}:
                        rs = getattr(manifest.spec, "resources", None)
                        obj = rs.limits if res == "limits.cpu" else (rs.requests if rs else None)
                        cpuq = getattr(obj, "cpu", None) if obj else None
                        if isinstance(cpuq, int | float):
                            base_m = int(round(float(cpuq) * 1000))
                            if divisor_raw:
                                d = divisor_raw.strip().lower()
                                if d.endswith("m"):
                                    div_m = int(float(d[:-1] or 0)) or 1
                                else:
                                    # cores to mCPU
                                    div_m = int(round(float(d or "1") * 1000)) or 1
                            else:
                                # default divisor 1 core
                                div_m = 1000
                            if div_m <= 0:
                                div_m = 1
                            env_map[name] = str(int(base_m // div_m))
                        continue
                    # Memory bytes: convert both to bytes and integer-divide
                    if res in {"limits.memory", "requests.memory"}:
                        rs = getattr(manifest.spec, "resources", None)
                        obj = rs.limits if res == "limits.memory" else (rs.requests if rs else None)
                        memq = getattr(obj, "memory", None) if obj else None
                        if memq:
                            bytes_val = self._parse_memory_bytes(str(memq)) or 0
                            if divisor_raw:
                                div_bytes = self._parse_memory_bytes(str(divisor_raw)) or 1
                            else:
                                div_bytes = 1
                            if div_bytes <= 0:
                                div_bytes = 1
                            env_map[name] = str(int(bytes_val // div_bytes))
                        continue
                except Exception:
                    # ignore any parsing issues and skip resolution
                    pass
        return env_map

    def _reload(self, container: Container) -> None:
        try:
            container.reload()
        except APIError as exc:
            raise RuntimeError(f"Failed to reload container {container.name}: {exc}") from exc

    def _parse_datetime(self, raw: str | None) -> datetime | None:
        if not raw or raw == "0001-01-01T00:00:00Z":
            return None
        cleaned = str(raw)
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(cleaned)
        except ValueError:  # pragma: no cover - best effort parsing
            return None

    

    

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
        except Exception:  # pragma: no cover - forgiving
            return None

    def list_containers_info(self) -> list[dict]:  # type: ignore[override]
        """List running containers with published host ports and basic status."""
        out: list[dict] = []
        try:
            containers = self._client.containers.list(all=True)
        except APIError:
            return out
        for c in containers:
            try:
                ports: list[int] = []
                pmap = (c.attrs or {}).get("NetworkSettings", {}).get("Ports", {}) or {}
                for binds in pmap.values():
                    if not binds:
                        continue
                    for b in binds:
                        hp = b.get("HostPort")
                        if hp:
                            try:
                                ports.append(int(hp))
                            except ValueError:
                                continue
                state = (c.attrs or {}).get("State", {})
                restarts = (
                    int(state.get("RestartCount", 0))
                    if isinstance(state.get("RestartCount", 0), int | float)
                    else 0
                )
                started_at = state.get("StartedAt") or None
                running = state.get("Running", False)
                ip = (c.attrs or {}).get("NetworkSettings", {}).get("IPAddress") or None
                out.append(
                    {
                        "name": c.name,
                        "labels": c.labels or {},
                        "host_ports": ports,
                        "restart_count": restarts,
                        "started_at": started_at,
                        "running": bool(running),
                        "pod_ip": ip,
                    }
                )
            except Exception:
                out.append(
                    {
                        "name": getattr(c, "name", ""),
                        "labels": {},
                        "host_ports": [],
                        "restart_count": 0,
                        "started_at": None,
                        "running": False,
                        "pod_ip": None,
                    }
                )
        return out

    # Storage volumes ---------------------------------------------------

    def _storage_volume_name(self, app_name: str, vol_name: str) -> str:
        """Derive the Docker volume name for a given app + storage spec name."""
        return f"ae-{app_name}-{vol_name}"

    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:  # type: ignore[override]
        """Ensure Docker named volumes exist for each storage spec.

        Each volume is labeled with ae.app and ae.volume for discovery and pruning.
        """
        try:
            for v in volumes or []:
                name = (v or {}).get("name")
                if not name:
                    continue
                vol_name = self._storage_volume_name(app_name, str(name))
                try:
                    self._client.volumes.get(vol_name)
                    continue
                except NotFound:
                    pass
                self._client.volumes.create(
                    name=vol_name,
                    labels={
                        "ae.app": app_name,
                        "ae.volume": str(name),
                        **(
                            {"ae.node": str(getattr(self, "_current_node_id", None))}
                            if getattr(self, "_current_node_id", None)
                            else {}
                        ),
                    },
                )
        except APIError as exc:  # pragma: no cover - depends on docker daemon
            raise RuntimeError(f"Failed to ensure storage volumes for {app_name}: {exc}") from exc

    def remove_storage_volumes(self, app_name: str, names: list[str]) -> int:  # type: ignore[override]
        removed = 0
        for n in names or []:
            vol_name = self._storage_volume_name(app_name, str(n))
            try:
                vol = self._client.volumes.get(vol_name)
            except NotFound:
                continue
            try:
                vol.remove(force=True)
                removed += 1
            except APIError:
                # best-effort removal; continue
                continue
        return removed

    def list_storage_volumes(self, app_name: str | None = None) -> list[dict]:  # type: ignore[override]
        out: list[dict] = []
        try:
            vols = self._client.volumes.list()
        except APIError:
            return out
        for v in vols:
            try:
                attrs = getattr(v, "attrs", {}) or {}
                labels = attrs.get("Labels", {}) or {}
                app = labels.get("ae.app")
                if app_name is not None:
                    if app != app_name:
                        continue
                else:
                    # Only show volumes that belong to this system
                    if not app:
                        continue
                out.append(
                    {
                        "name": getattr(v, "name", attrs.get("Name", "")),
                        "labels": labels,
                        "driver": attrs.get("Driver", ""),
                        "mountpoint": attrs.get("Mountpoint", ""),
                    }
                )
            except Exception:
                # Skip any unexpected volume shape
                continue
        return out
