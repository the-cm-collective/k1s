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

    def ensure_app(self, manifest: AppManifest, revision: int) -> RuntimeResult:
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
        ports = self._port_mapping(manifest.spec.ports)

        try:
            container = self._client.containers.run(
                manifest.spec.image,
                command=manifest.spec.command or None,
                name=name,
                detach=True,
                environment=env if env else None,
                labels={
                    self.APP_LABEL: manifest.metadata.name,
                    self.REPLICA_LABEL: replica_id,
                    self.REVISION_LABEL: str(revision),
                },
                ports=ports if ports else None,
                restart_policy={"Name": "unless-stopped"},
            )
            self._reload(container)
            return container
        except APIError as exc:
            raise RuntimeError(f"Failed to create container {name}: {exc}") from exc

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

    def _port_mapping(self, ports: Iterable[PortSpec]) -> Dict[str, Optional[int]]:
        mapping: Dict[str, Optional[int]] = {}
        for port in ports:
            key = f"{port.container_port}/tcp"
            mapping[key] = None
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
        cleaned = raw
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(cleaned)
        except ValueError:  # pragma: no cover - best effort parsing
            return None
