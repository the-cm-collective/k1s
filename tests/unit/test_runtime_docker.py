"""Unit tests for the Docker runtime adapter with a fake docker client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from docker.errors import NotFound

from ae.controller.spec import AppManifest, AppSpec, Metadata, PortSpec
from ae.runtime.docker_runtime import DockerRuntime


@dataclass
class FakeContainer:
    client: "FakeDockerClient"
    name: str
    labels: Dict[str, str]
    host_port: int

    def __post_init__(self) -> None:
        self.status = "running"
        self.attrs = {
            "State": {
                "Status": self.status,
                "StartedAt": "2025-10-23T00:00:00+00:00",
            },
            "NetworkSettings": {
                "Ports": {
                    "8080/tcp": [
                        {
                            "HostIp": "127.0.0.1",
                            "HostPort": str(self.host_port),
                        }
                    ]
                }
            },
        }

    def reload(self) -> None:
        self.attrs["State"]["Status"] = self.status

    def start(self) -> None:
        self.status = "running"
        self.reload()

    def stop(self, timeout: int = 10) -> None:  # noqa: D401 - interface match
        self.status = "exited"
        self.reload()

    def remove(self) -> None:
        self.client.remove_container(self.labels["ae.replica_id"])


class FakeContainerManager:
    def __init__(self, client: "FakeDockerClient") -> None:
        self._client = client

    def list(self, all: bool = True, filters: Optional[Dict[str, str]] = None) -> List[FakeContainer]:
        containers = list(self._client.containers_by_replica.values())
        if not filters:
            return containers
        label_filter = filters.get("label") if isinstance(filters, dict) else None
        if label_filter:
            key, value = label_filter.split("=", maxsplit=1)
            filtered = []
            for container in containers:
                if container.labels.get(key) == value:
                    filtered.append(container)
            return filtered
        return containers

    def run(self, image: str, command=None, name: str = "", detach: bool = True, environment=None,
            labels=None, ports=None, restart_policy=None):  # noqa: ANN001,D401 - mimic docker
        replica_id = labels.get("ae.replica_id")
        host_port = self._client.allocate_port()
        container = FakeContainer(
            client=self._client,
            name=name,
            labels=labels,
            host_port=host_port,
        )
        self._client.register_container(replica_id, container)
        return container


class FakeImages:
    def __init__(self, client: "FakeDockerClient") -> None:
        self._client = client
        self.pulled: List[str] = []
        self._local: Dict[str, str] = {}

    def get(self, image: str):  # noqa: ANN001 - mimic docker
        if image not in self._local:
            raise NotFound(f"{image} not found")
        return self._local[image]

    def pull(self, image: str) -> None:
        self.pulled.append(image)
        self._local[image] = image


class FakeDockerClient:
    def __init__(self) -> None:
        self.images = FakeImages(self)
        self.containers = FakeContainerManager(self)
        self.containers_by_replica: Dict[str, FakeContainer] = {}
        self._next_port = 32000
        self.logins: list[tuple[str, str, str]] = []

    def allocate_port(self) -> int:
        port = self._next_port
        self._next_port += 1
        return port

    def register_container(self, replica_id: str, container: FakeContainer) -> None:
        self.containers_by_replica[replica_id] = container

    def remove_container(self, replica_id: str) -> None:
        self.containers_by_replica.pop(replica_id, None)

    def login(self, registry: str, username: str | None, password: str | None) -> None:
        self.logins.append((registry, username or "", password or ""))

    # helper for tests
    def seed_container(self, app_name: str, replica_suffix: int, revision: int) -> None:
        replica_id = f"{app_name}-rev{revision}-{replica_suffix}"
        container = FakeContainer(
            client=self,
            name=f"ae-{app_name}-rev{revision}-{replica_suffix}",
            labels={
                "ae.app": app_name,
                "ae.replica_id": replica_id,
                "ae.revision": str(revision),
            },
            host_port=self.allocate_port(),
        )
        self.register_container(replica_id, container)


def make_manifest(replica_count: int = 1, image: str = "alpine:3.20") -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image=image,
            replicas=replica_count,
            ports=[PortSpec(name="http", containerPort=8080)],
        ),
    )


def test_docker_runtime_creates_missing_replicas():
    client = FakeDockerClient()
    config = tmp_registry_config(client)
    runtime = DockerRuntime(client=client, registry_auth=config)

    manifest = make_manifest(replica_count=2)
    result = runtime.ensure_app(manifest, revision=1)

    assert result.revision == 1
    assert result.created == 2
    assert result.removed == 0
    assert len(result.replica_states) == 2
    assert client.images.pulled == ["alpine:3.20"]
    assert client.logins == [("ghcr.io", "user", "pass")]
    for state in result.replica_states:
        assert state.ready is True
        assert state.endpoint is not None


def test_docker_runtime_removes_extra_replicas():
    client = FakeDockerClient()
    client.seed_container("demo", 0, revision=0)
    client.seed_container("demo", 1, revision=0)

    runtime = DockerRuntime(client=client)
    manifest = make_manifest(replica_count=1)

    result = runtime.ensure_app(manifest, revision=1)

    assert result.removed == 2
    assert result.created == 1
    assert len(result.replica_states) == 1
    assert "demo-rev1-0" in client.containers_by_replica
    assert "demo-rev0-1" not in client.containers_by_replica


def test_docker_runtime_skips_pull_when_image_local():
    client = FakeDockerClient()
    client.images._local["demo-blue:latest"] = "demo-blue:latest"
    runtime = DockerRuntime(client=client)
    manifest = make_manifest(replica_count=1, image="demo-blue:latest")

    runtime.ensure_app(manifest, revision=1)

    assert client.images.pulled == []


def tmp_registry_config(client: FakeDockerClient):  # noqa: ANN001
    class StubAuth:
        def ensure_login(self, docker_client, image: str) -> None:  # noqa: ANN001
            docker_client.login(registry="ghcr.io", username="user", password="pass")

        def list_registries(self):  # noqa: D401
            return {"ghcr.io": {"username": "user", "password": "pass"}}

    return StubAuth()
