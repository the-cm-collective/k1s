"""Docker-backed runtime adapter for managing application replicas."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from ae.controller.spec import AppManifest, PortSpec

from .base import ReplicaState, RuntimeAdapter, RuntimeResult
from .registry import RegistryAuthProvider

LOGGER = logging.getLogger(__name__)


class DockerRuntime(RuntimeAdapter):
    """Ensures Docker containers match the desired manifest state."""

    APP_LABEL = "ae.app"
    REPLICA_LABEL = "ae.replica_id"
    REVISION_LABEL = "ae.revision"

    def __init__(
        self,
        client: Optional[docker.DockerClient] = None,
        registry_auth: Optional[RegistryAuthProvider] = None,
    ) -> None:
        try:
            self._client = client or docker.from_env()
        except Exception as exc:  # pragma: no cover - defensive guard, validated in tests
            raise RuntimeError(f"Failed to initialize Docker client: {exc}") from exc
        self._registry = registry_auth or RegistryAuthProvider()
        # Optional shared network so that ingress (Caddy) can reach containers by name
        import os as _os
        self._network_name = _os.getenv("AE_DOCKER_NETWORK")

    def ensure_app(self, manifest: AppManifest, revision: int, *, keep_old: bool = False, limit_create: int | None = None) -> RuntimeResult:
        app_name = manifest.metadata.name
        desired_replica_ids = self._desired_replica_ids(manifest, revision)

        try:
            existing_containers = self._client.containers.list(
                all=True, filters={"label": f"{self.APP_LABEL}={app_name}"}
            )
        except APIError as exc:  # pragma: no cover - network failure path hard to trigger in tests
            raise RuntimeError(f"Failed to list containers for {app_name}: {exc}") from exc

        containers_by_replica: Dict[str, Container] = {}
        old_revision_containers: List[Container] = []
        for container in existing_containers:
            replica_label = container.labels.get(self.REPLICA_LABEL)
            if not replica_label:
                continue
            if container.labels.get(self.REVISION_LABEL) == str(revision):
                containers_by_replica[replica_label] = container
            else:
                old_revision_containers.append(container)

        created = updated = removed = 0

        if any(replica_id not in containers_by_replica for replica_id in desired_replica_ids):
            self._registry.ensure_login(self._client, manifest.spec.image)
            self._pull_image(manifest)

        for replica_id in desired_replica_ids:
            container = containers_by_replica.get(replica_id)
            if container is None:
                if limit_create is not None and created >= int(limit_create):
                    continue
                container = self._create_container(manifest, replica_id, revision)
                containers_by_replica[replica_id] = container
                created += 1
            else:
                self._reload(container)
                if container.status != "running":
                    try:
                        container.start()
                        updated += 1
                    except APIError as exc:
                        raise RuntimeError(f"Failed to start container {container.name}: {exc}") from exc

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

    def read_logs(self, replica_id: str, *, follow: bool = False, tail: int | None = None, since: int | None = None):
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
            if follow:
                for chunk in container.logs(stdout=True, stderr=True, stream=True, follow=True, tail=tail or "all", since=since):
                    yield chunk.decode("utf-8", "replace").rstrip("\n")
            else:
                output = container.logs(stdout=True, stderr=True, stream=False, tail=tail or 200, since=since)
                text = output.decode("utf-8", "replace")
                for line in text.splitlines():
                    yield line
        except APIError as exc:
            raise RuntimeError(f"Failed to read logs for {replica_id}: {exc}") from exc

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

    def _desired_replica_ids(self, manifest: AppManifest, revision: int) -> List[str]:
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

    def _create_container(self, manifest: AppManifest, replica_id: str, revision: int) -> Container:
        # replica_id pattern: <app>-rev<revision>-<index>
        app_name = manifest.metadata.name
        replica_suffix = replica_id.split("-")[-1]
        name = f"ae-{app_name}-rev{revision}-{replica_suffix}"
        env = self._manifest_env(manifest)
        # Build port mapping; if a Service is specified and replicas==1, publish a stable host port
        svc_port = None
        svc_target = None
        if getattr(manifest.spec, "service", None) and manifest.spec.replicas == 1:
            svc_port = manifest.spec.service.port
            svc_target = manifest.spec.service.target_port
        ports = self._port_mapping(manifest.spec.ports, service_port=svc_port, service_target=svc_target)
        # Pre-flight conflict check for service stable port
        if svc_port is not None:
            self._ensure_host_port_free(app_name, int(svc_port))

        # resource limits
        nano_cpus = None
        mem_limit = None
        if manifest.spec.resources and manifest.spec.resources.limits:
            limits = manifest.spec.resources.limits
            if limits.cpu is not None:
                try:
                    nano_cpus = int(float(limits.cpu) * 1_000_000_000)
                except ValueError:
                    nano_cpus = None
            if limits.memory is not None:
                mem_limit = self._parse_memory_bytes(str(limits.memory))

        # volumes
        volumes = {}
        if manifest.spec.volumes:
            for v in manifest.spec.volumes:
                mode = "ro" if v.read_only else "rw"
                volumes[v.host_path] = {"bind": v.mount_path, "mode": mode}
        if getattr(manifest.spec, "storage", None):
            self.ensure_storage_volumes(manifest.metadata.name, [s.model_dump() for s in manifest.spec.storage])
            for s in manifest.spec.storage:
                vol_name = self._storage_volume_name(manifest.metadata.name, s.name)
                volumes[vol_name] = {"bind": s.mount_path, "mode": "rw"}

        try:
            run_fn = self._client.containers.run
            # Build standard kwargs. Do NOT filter by signature; docker-py forwards **kwargs.
            # Filtering here accidentally dropped 'ports', preventing host port publishing.
            kwargs = {
                "command": manifest.spec.command or None,
                "name": name,
                "detach": True,
                "environment": env if env else None,
                "labels": {
                    self.APP_LABEL: manifest.metadata.name,
                    self.REPLICA_LABEL: replica_id,
                    self.REVISION_LABEL: str(revision),
                },
                "ports": ports if ports else None,
                "restart_policy": {"Name": "unless-stopped"},
            }
            if nano_cpus is not None:
                kwargs["nano_cpus"] = nano_cpus
            if mem_limit is not None:
                kwargs["mem_limit"] = mem_limit
            if volumes:
                kwargs["volumes"] = volumes

            container = run_fn(
                manifest.spec.image,
                **{k: v for k, v in kwargs.items() if v is not None}
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
                    LOGGER.warning("Failed to connect %s to network %s: %s", name, self._network_name, _exc)
            self._reload(container)
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

    def _stop_and_remove(self, container: Container) -> None:
        try:
            LOGGER.debug("Removing container %s", container.name)
            container.stop(timeout=10)
        except APIError as exc:  # pragma: no cover - protective guard
            LOGGER.warning("Failed to stop container %s: %s", container.name, exc)
        try:
            container.remove()
        except (APIError, NotFound) as exc:  # pragma: no cover - container already gone
            LOGGER.warning("Failed to remove container %s: %s", container.name, exc)

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

        endpoint = self._endpoint_from_ports(manifest.spec.ports, container)

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
        *,
        service_port: Optional[int] = None,
        service_target: Optional[int] = None,
    ) -> Dict[str, Optional[int]]:
        mapping: Dict[str, Optional[int]] = {}
        first_port = None
        for port in ports:
            if first_port is None:
                first_port = port.container_port
            key = f"{port.container_port}/tcp"
            host_port: Optional[int] = None
            if service_port is not None:
                target = service_target if service_target is not None else first_port
                if port.container_port == target:
                    host_port = int(service_port)
            mapping[key] = host_port
        return mapping

    def _endpoint_from_ports(self, ports: Iterable[PortSpec], container: Container) -> Optional[str]:
        if not ports:
            return None
        network_ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        for port in ports:
            key = f"{port.container_port}/tcp"
            bindings = network_ports.get(key)
            if not bindings:
                continue
            binding = bindings[0]
            host_ip = binding.get("HostIp", "127.0.0.1")
            host_port = binding.get("HostPort")
            if host_port:
                return f"{host_ip}:{host_port}"
        # If no host port was published but we are on a shared network, use container DNS name
        if self._network_name:
            first = next(iter(ports), None)
            if first is not None:
                return f"{container.name}:{first.container_port}"
        return None

    def _manifest_env(self, manifest: AppManifest) -> Dict[str, str]:
        env_map: Dict[str, str] = {}
        for item in manifest.spec.env:
            if "name" in item and "value" in item:
                env_map[item["name"]] = item["value"]
        return env_map

    def _reload(self, container: Container) -> None:
        try:
            container.reload()
        except APIError as exc:
            raise RuntimeError(f"Failed to reload container {container.name}: {exc}") from exc

    def _parse_datetime(self, raw: Optional[str]) -> Optional[datetime]:
        if not raw or raw == "0001-01-01T00:00:00Z":
            return None

    def _parse_memory_bytes(self, raw: str) -> Optional[int]:
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
            # numeric only
            if s.isdigit():
                return int(s)
            # split number and unit
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
        cleaned = raw
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(cleaned)
        except ValueError:  # pragma: no cover - best effort parsing
            return None

    def list_containers_info(self) -> list[dict]:  # type: ignore[override]
        """List running containers with published host ports for conflict checks."""
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
                out.append({
                    "name": c.name,
                    "labels": c.labels or {},
                    "host_ports": ports,
                })
            except Exception:
                out.append({"name": getattr(c, 'name', ''), "labels": {}, "host_ports": []})
        return out
