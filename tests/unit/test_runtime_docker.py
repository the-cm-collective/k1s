"""Unit tests for the Docker runtime adapter with a fake docker client."""

from __future__ import annotations

from dataclasses import dataclass

from docker.errors import NotFound

from ae.controller.spec import AppManifest, AppSpec, Metadata, PortSpec, ServiceSpec
from ae.runtime.docker_runtime import DockerRuntime

try:
    from unittest import mock
except ImportError:  # pragma: no cover - <3.8 legacy
    from unittest import mock  # type: ignore


@dataclass
class FakeContainer:
    client: FakeDockerClient
    name: str
    labels: dict[str, str]
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
        _ = timeout
        self.status = "exited"
        self.reload()

    def remove(self) -> None:
        self.client.remove_container(self.labels["ae.pod_name"])


class FakeContainerManager:
    def __init__(self, client: FakeDockerClient) -> None:
        self._client = client

    def list(self, all: bool = True, filters: dict[str, str] | None = None) -> list[FakeContainer]:
        _ = all
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

    def run(
        self,
        image: str,
        command=None,
        name: str = "",
        detach: bool = True,
        environment=None,
        labels=None,
        ports=None,
        restart_policy=None,
    ):  # noqa: ANN001,D401 - mimic docker
        _ = (image, command, detach, environment, ports, restart_policy)
        pod_name = labels.get("ae.pod_name")
        host_port = self._client.allocate_port()
        container = FakeContainer(
            client=self._client,
            name=name,
            labels=labels,
            host_port=host_port,
        )
        self._client.register_container(pod_name, container)
        return container


class FakeImages:
    def __init__(self, client: FakeDockerClient) -> None:
        self._client = client
        self.pulled: list[str] = []
        self._local: dict[str, str] = {}

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
        self.containers_by_replica: dict[str, FakeContainer] = {}
        self._next_port = 32000
        self.logins: list[tuple[str, str, str]] = []

    def allocate_port(self) -> int:
        port = self._next_port
        self._next_port += 1
        return port

    def register_container(self, pod_name: str, container: FakeContainer) -> None:
        self.containers_by_replica[pod_name] = container

    def remove_container(self, pod_name: str) -> None:
        self.containers_by_replica.pop(pod_name, None)

    def login(self, registry: str, username: str | None, password: str | None) -> None:
        self.logins.append((registry, username or "", password or ""))

    # helper for tests
    def seed_container(self, app_name: str, replica_suffix: int, revision: int) -> None:
        pod_name = f"{app_name}-rev{revision}-{replica_suffix}"
        container = FakeContainer(
            client=self,
            name=f"ae-{app_name}-rev{revision}-{replica_suffix}",
            labels={
                "ae.app": app_name,
                "ae.pod_name": pod_name,
                "ae.revision": str(revision),
            },
            host_port=self.allocate_port(),
        )
        self.register_container(pod_name, container)


def make_manifest(replica_count: int = 1, image: str = "alpine:3.20") -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
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
    assert len(result.pod_states) == 2
    assert client.images.pulled == ["alpine:3.20"]
    assert client.logins == [("ghcr.io", "user", "pass")]
    for state in result.pod_states:
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
    assert len(result.pod_states) == 1
    assert "demo-rev1-0" in client.containers_by_replica
    assert "demo-rev0-1" not in client.containers_by_replica


def test_docker_runtime_skips_pull_when_image_local():
    client = FakeDockerClient()
    client.images._local["demo-blue:latest"] = "demo-blue:latest"
    runtime = DockerRuntime(client=client)
    manifest = make_manifest(replica_count=1, image="demo-blue:latest")

    runtime.ensure_app(manifest, revision=1)

    assert client.images.pulled == []


def test_port_mapping_with_multi_service_ports():
    client = FakeDockerClient()
    runtime = DockerRuntime(client=client)

    manifest = make_manifest(replica_count=1)
    # Add a metrics port and multi-port Service mapping
    manifest = manifest.model_copy(
        update={
            "spec": manifest.spec.model_copy(
                update={
                    "ports": manifest.spec.ports + [PortSpec(name="metrics", containerPort=9090)],
                    "service": ServiceSpec(
                        ports=[
                            ServiceSpec.ServicePort(name="http", port=8080, targetPort=8080),
                            ServiceSpec.ServicePort(name="metrics", port=9090, targetPort=9090),
                        ]
                    ),
                }
            )
        }
    )
    with (
        mock.patch.object(runtime, "_host_ports_in_use", return_value=set()),
        mock.patch(
            "ae.runtime.docker_runtime.choose_host_port",
            side_effect=lambda port, **_: (port, True),
        ),
    ):
        mapping, svc_map = runtime._port_mapping(  # type: ignore[attr-defined]
            manifest.spec.ports,
            manifest.metadata.name,
            service_ports=list(manifest.spec.service.ports),  # type: ignore[arg-type]
        )
    # Expect both container ports to be present with host ports matching service ports
    assert mapping.get("8080/tcp") == 8080
    assert mapping.get("9090/tcp") == 9090
    assert svc_map.get(8080) == 8080
    assert svc_map.get(9090) == 9090


def test_port_mapping_falls_back_when_port_busy():
    client = FakeDockerClient()
    runtime = DockerRuntime(client=client)

    manifest = make_manifest(replica_count=1)
    manifest = manifest.model_copy(
        update={
            "spec": manifest.spec.model_copy(
                update={
                    "service": ServiceSpec(port=18080, targetPort=8080),
                }
            )
        }
    )

    with (
        mock.patch.object(runtime, "_host_ports_in_use", return_value=set()),
        mock.patch("ae.runtime.docker_runtime.choose_host_port", return_value=(18123, False)),
    ):
        mapping, svc_map = runtime._port_mapping(  # type: ignore[attr-defined]
            manifest.spec.ports,
            manifest.metadata.name,
            service_port=manifest.spec.service.port,  # type: ignore[arg-type]
            service_target=manifest.spec.service.target_port,  # type: ignore[arg-type]
        )

    assert mapping.get("8080/tcp") == 18123
    assert svc_map.get(8080) == 18123


def test_port_mapping_skips_ports_already_in_use():
    client = FakeDockerClient()
    runtime = DockerRuntime(client=client)

    manifest = make_manifest(replica_count=1)
    manifest = manifest.model_copy(
        update={
            "spec": manifest.spec.model_copy(
                update={"service": ServiceSpec(port=18080, targetPort=8080)}
            )
        }
    )

    def fake_choose_host_port(port, **kwargs):  # noqa: ANN001
        blocked = kwargs.get("blocked") or set()
        assert 18080 in blocked
        if port in blocked:
            return port + 1, False
        return port, True

    with (
        mock.patch.object(runtime, "_host_ports_in_use", return_value={18080}),
        mock.patch(
            "ae.runtime.docker_runtime.choose_host_port",
            side_effect=fake_choose_host_port,
        ),
    ):
        mapping, svc_map = runtime._port_mapping(  # type: ignore[attr-defined]
            manifest.spec.ports,
            manifest.metadata.name,
            service_port=manifest.spec.service.port,  # type: ignore[arg-type]
            service_target=manifest.spec.service.target_port,  # type: ignore[arg-type]
        )

    assert mapping.get("8080/tcp") == 18081
    assert svc_map.get(8080) == 18081


def test_serial_service_rollout_removes_previous_revision(monkeypatch):
    monkeypatch.setenv("AE_SERIAL_SERVICE_ROLLOUT", "1")
    client = FakeDockerClient()
    client.seed_container("demo", 0, revision=0)

    runtime = DockerRuntime(client=client)
    manifest = make_manifest(replica_count=1)
    manifest = manifest.model_copy(
        update={
            "spec": manifest.spec.model_copy(
                update={"service": ServiceSpec(port=18080, targetPort=8080)}
            )
        }
    )

    result = runtime.ensure_app(manifest, revision=1)

    assert "demo-rev0-0" not in client.containers_by_replica
    assert result.removed >= 1


def test_endpoint_normalizes_anyaddr_to_loopback():
    """HostIp 0.0.0.0 (wildcard) should be treated as 127.0.0.1 for probes."""
    client = FakeDockerClient()
    runtime = DockerRuntime(client=client)

    manifest = make_manifest(replica_count=1)

    # Seed a container and rewrite HostIp to the wildcard address that Docker uses
    client.seed_container("demo", 0, revision=1)
    container = client.containers_by_replica["demo-rev1-0"]
    container.attrs["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostIp"] = "0.0.0.0"  # noqa: S104

    endpoint = runtime._endpoint_from_ports(manifest.spec.ports, container)  # type: ignore[attr-defined]

    assert endpoint is not None
    # Should map to loopback, not 0.0.0.0
    assert endpoint.startswith("127.0.0.1:")


def tmp_registry_config(_client: FakeDockerClient):  # noqa: ANN001
    class StubAuth:
        def ensure_login(self, docker_client, _image: str) -> None:  # noqa: ANN001
            docker_client.login(registry="ghcr.io", username="user", password="pass")  # noqa: S106

        def list_registries(self):  # noqa: D401
            return {"ghcr.io": {"username": "user", "password": "pass"}}

    return StubAuth()


def test_build_state_prefers_readiness_port_for_endpoint():
    # Minimal fake container with two published ports
    class C:
        def __init__(self) -> None:
            self.name = "ae-echo-rev1-0"
            self.labels = {"ae.pod_name": "echo-rev1-0"}
            self.status = "running"
            self.attrs = {
                "State": {"Status": "running", "StartedAt": "2025-10-23T00:00:00+00:00"},
                "NetworkSettings": {
                    "Ports": {
                        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32001"}],
                        "9090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "40000"}],
                    }
                },
            }

        def reload(self) -> None:  # needed by _build_state
            pass

    runtime = DockerRuntime(client=FakeDockerClient())
    # Manifest with readiness on 9090
    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="echo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            ports=[
                PortSpec(name="http", containerPort=8080),
                PortSpec(name="metrics", containerPort=9090),
            ],
            health={
                "readiness": {"httpGet": {"path": "/healthz", "port": 9090}},
            },
        ),
    )
    state = runtime._build_state(man, C())  # type: ignore[arg-type]
    assert state.endpoint.endswith(":40000")
