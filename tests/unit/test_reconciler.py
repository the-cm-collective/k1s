"""Tests for reconcile skeleton."""

from pathlib import Path

from ae.controller.health import HealthReport, PodHealth
from ae.controller.reconciler import Reconciler, ReconcileReport
from ae.controller.spec import (
    AppManifest,
    AppSpec,
    HostAlias,
    IngressSpec,
    Metadata,
    SecretEnvMapping,
    SecretRef,
    ServiceSpec,
)
from ae.controller.state import ServiceEndpoint, SQLiteStateStore
from ae.runtime.base import PodState, RuntimeAdapter, RuntimeResult
from ae.runtime.containerd_runtime import ContainerdRuntime


class StubRuntime(RuntimeAdapter):
    def __init__(self) -> None:
        self.last_manifest: AppManifest | None = None

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
        _ = (keep_old, limit_create, node_id)
        self.last_manifest = manifest
        name = (
            pod_names[0]
            if pod_names
            else f"{manifest.metadata.name}-rev{revision}-0"
        )
        return RuntimeResult(
            revision=revision,
            created=1,
            updated=0,
            removed=0,
            pod_states=[
                PodState(
                    pod_name=name,
                    ready=True,
                    status="running",
                    endpoint="127.0.0.1:32000",
                )
            ],
        )

    def list_containers_info(self) -> list[dict]:
        return []


class StubDockerIngressRuntime(StubRuntime):
    def __init__(self) -> None:
        super().__init__()
        self._network_name = "dev_default"

    def list_containers_info(self) -> list[dict]:
        return [
            {
                "name": "ae-demo-rev1-0",
                "labels": {
                    "ae.app": "demo",
                    "ae.pod_name": "demo-rev1-0",
                    "ae.revision": "1",
                },
                "host_ports": [32000],
                "port_map": {8080: 32000},
                "pod_ip": None,
                "running": True,
            }
        ]


class CapturingContainerdRuntime(ContainerdRuntime):
    def __init__(self) -> None:
        super().__init__(namespace="ae-test")
        self.last_manifest: AppManifest | None = None
        self.revisions: list[int] = []
        self.apps: list[str] = []

    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:  # type: ignore[override]
        _ = (keep_old, limit_create, node_id)
        self.last_manifest = manifest
        self.revisions.append(revision)
        self.apps.append(f"{manifest.metadata.namespace}--{manifest.metadata.name}")
        pod_name = pod_names[0] if pod_names else f"{manifest.metadata.name}-rev{revision}-0"
        return RuntimeResult(
            revision=revision,
            created=1,
            updated=0,
            removed=0,
            pod_states=[
                PodState(
                    pod_name=pod_name,
                    ready=True,
                    status="running",
                    endpoint="10.210.0.44:8080",
                )
            ],
        )


class FailingLivenessRuntime(RuntimeAdapter):
    """Runtime that creates a replica without a reachable endpoint.

    This simulates the window where the container exists but liveness HTTP
    probes fail because the port isn't yet published/ready.
    """

    def ensure_app(self, _manifest: AppManifest, revision: int, **_kwargs) -> RuntimeResult:  # type: ignore[override]
        return RuntimeResult(
            revision=revision,
            created=1,
            updated=0,
            removed=0,
            pod_states=[
                PodState(
                    pod_name=f"{_manifest.metadata.name}-rev{revision}-0",
                    ready=False,
                    status="created",
                    endpoint=None,
                )
            ],
        )


def test_status_progressing_when_replica_present_but_liveness_failing(tmp_path: Path) -> None:
    # Manifest with an HTTP liveness probe so live=false when endpoint is None
    from ae.controller.spec import AppManifest, AppSpec, HealthSpec, Metadata, ProbeSpec

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            health=HealthSpec(
                liveness=ProbeSpec(httpGet={"path": "/healthz", "port": 8080})  # type: ignore[arg-type]
            ),
        ),
    )

    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=FailingLivenessRuntime(), state_store=state)

    report = reconciler.reconcile(manifest)
    assert isinstance(report, ReconcileReport)
    status = state.get_status("demo")
    assert status is not None
    # With at least one replica recorded, we should be "progressing", not "degraded"
    assert status.revision_status == "progressing"


class CreateButEmptyStatesRuntime(RuntimeAdapter):
    """Runtime that reports a creation but returns no replica states yet.

    Mirrors a transient race window seen with Podman where `run` succeeds but
    an immediate `ps/inspect` does not include the new container.
    """

    def ensure_app(
        self,
        _manifest: AppManifest,
        revision: int,
        *,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
        keep_old: bool = False,
        limit_create: int | None = None,
    ) -> RuntimeResult:  # type: ignore[override]
        _ = (pod_names, node_id, keep_old, limit_create)
        return RuntimeResult(
            revision=revision,
            created=1,
            updated=0,
            removed=0,
            pod_states=[],
        )


def test_status_progressing_when_created_but_states_empty(tmp_path: Path) -> None:
    from ae.controller.spec import AppManifest, AppSpec, Metadata

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(image="alpine:3.20"),
    )

    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=CreateButEmptyStatesRuntime(), state_store=state)

    report = reconciler.reconcile(manifest)
    assert isinstance(report, ReconcileReport)
    status = state.get_status("demo")
    assert status is not None
    # Even with zero observed replicas, a create implies we're progressing
    assert status.revision_status == "progressing"


class StubIngressService:
    def __init__(self) -> None:
        self.applied: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.reload_count = 0

    def apply(self, manifest: AppManifest, upstream: str):  # noqa: ANN001 - match service signature
        self.applied.append((manifest.metadata.name, upstream))

    def remove(self, app_name: str) -> None:
        self.removed.append(app_name)

    def reload(self) -> None:
        self.reload_count += 1


class CallbackCaptureHealthManager:
    def __init__(self) -> None:
        self.exec_cb = None
        self.portforward_cb = None
        self.event_cb = None

    def set_exec_callback(self, fn):  # type: ignore[no-untyped-def]
        self.exec_cb = fn

    def set_portforward_callback(self, fn):  # type: ignore[no-untyped-def]
        self.portforward_cb = fn

    def set_event_callback(self, fn):  # type: ignore[no-untyped-def]
        self.event_cb = fn


class RolloutOverlapRuntime(RuntimeAdapter):
    def ensure_app(self, manifest: AppManifest, revision: int, **_kwargs) -> RuntimeResult:  # type: ignore[override]
        return RuntimeResult(
            revision=revision,
            created=2,
            updated=0,
            removed=0,
            pod_states=[
                PodState(
                    pod_name=f"{manifest.metadata.name}-rev{revision}-0",
                    ready=True,
                    status="running",
                    revision=revision,
                ),
                PodState(
                    pod_name=f"{manifest.metadata.name}-rev{revision}-1",
                    ready=True,
                    status="running",
                    revision=revision,
                ),
                PodState(
                    pod_name=f"{manifest.metadata.name}-rev{max(revision - 1, 0)}-0",
                    ready=True,
                    status="running",
                    revision=max(revision - 1, 0),
                ),
            ],
        )


class StubHealthManager:
    def set_exec_callback(self, _fn) -> None:  # noqa: ANN001
        return None

    def set_portforward_callback(self, _fn) -> None:  # noqa: ANN001
        return None

    def set_event_callback(self, _fn) -> None:  # noqa: ANN001
        return None

    def evaluate(self, manifest: AppManifest, result: RuntimeResult) -> HealthReport:
        _ = manifest
        pods = [
            PodHealth(
                pod_name=state.pod_name,
                ready=True,
                live=True,
                readiness_message="ok",
                liveness_message="ok",
            )
            for state in result.pod_states
        ]
        return HealthReport(
            ready_replicas=len(pods),
            live_replicas=len(pods),
            pods=pods,
        )


def test_reconciler_updates_state(tmp_path: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=runtime, state_store=state)

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(image="alpine:3.20"),
    )

    report = reconciler.reconcile(manifest)

    assert isinstance(report, ReconcileReport)
    assert report.app_name == "demo"
    status = state.get_status("demo")
    assert status is not None
    assert status.ready_replicas == 1
    assert status.live_replicas == 1
    assert status.revision == 1
    assert status.revision_status in {"ready", "progressing"}
    assert status.image == "alpine:3.20"
    assert status.created == 1
    replicas = state.list_pods("demo")
    assert len(replicas) == 1
    assert replicas[0].live is True
    events = state.list_events("demo", limit=5)
    assert any(event.event_type == "ApplyCompleted" for event in events)


def test_reconciler_records_rollout_overlap_counts(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(
        runtime=RolloutOverlapRuntime(),
        state_store=state,
        health_manager=StubHealthManager(),
    )

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(image="alpine:3.20", replicas=2),
    )

    report = reconciler.reconcile(manifest)
    status = state.get_status("demo")

    assert status is not None
    assert report.current_revision_ready_replicas == 2
    assert report.current_revision_live_replicas == 2
    assert report.old_revision_ready_replicas == 1
    assert report.old_revision_live_replicas == 1
    assert report.overlap_ready_replicas == 1
    assert report.overlap_live_replicas == 1
    assert status.current_revision_ready_replicas == 2
    assert status.current_revision_live_replicas == 2
    assert status.old_revision_ready_replicas == 1
    assert status.old_revision_live_replicas == 1
    assert status.overlap_ready_replicas == 1
    assert status.overlap_live_replicas == 1


def test_reconciler_with_ingress(tmp_path: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    ingress_service = StubIngressService()
    reconciler = Reconciler(runtime=runtime, state_store=state, ingress_service=ingress_service)

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            ingress=IngressSpec(host="demo.local", path="/"),
        ),
    )

    reconciler.reconcile(manifest)

    assert ingress_service.applied == [("demo", "127.0.0.1:32000")]
    assert ingress_service.reload_count == 1
    status = state.get_status("demo")
    assert status is not None
    assert status.ingress_host == "demo.local"
    assert status.revision >= 1
    events = state.list_events("demo", limit=5)
    assert any(event.event_type == "IngressConfigured" for event in events)


def test_reconciler_with_docker_network_ingress_prefers_container_dns(tmp_path: Path) -> None:
    runtime = StubDockerIngressRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    ingress_service = StubIngressService()
    reconciler = Reconciler(runtime=runtime, state_store=state, ingress_service=ingress_service)

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            ports=[{"name": "http", "containerPort": 8080}],  # type: ignore[list-item]
            ingress=IngressSpec(host="demo.local", path="/"),
        ),
    )

    reconciler.reconcile(manifest)

    assert ingress_service.applied == [("demo", "ae-demo-rev1-0:8080")]
    assert ingress_service.reload_count == 1


def test_reconciler_workerbee_ingress_prefers_host_port_upstreams(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AE_CADDY_PREFER_HOST_PORT_UPSTREAMS", "1")
    runtime = StubDockerIngressRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    ingress_service = StubIngressService()
    reconciler = Reconciler(runtime=runtime, state_store=state, ingress_service=ingress_service)

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            ports=[{"name": "http", "containerPort": 8080}],  # type: ignore[list-item]
            ingress=IngressSpec(host="demo.local", path="/"),
        ),
    )

    reconciler.reconcile(manifest)

    assert ingress_service.applied == [("demo", "127.0.0.1:32000")]
    assert ingress_service.reload_count == 1


def test_reconciler_registers_portforward_callback(tmp_path: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    health = CallbackCaptureHealthManager()

    Reconciler(runtime=runtime, state_store=state, health_manager=health)

    assert callable(health.exec_cb)
    assert callable(health.portforward_cb)
    assert callable(health.event_cb)


def test_reconciler_portforward_prefers_cached_remote_runtime(tmp_path: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=runtime, state_store=state)
    sentinel = object()
    captured: list[tuple[str | None, str | None, str | None, int]] = []

    class _RemoteRuntime:
        def port_forward_socket(
            self,
            *,
            pod_id: str | None,
            pod_name: str | None,
            namespace: str | None,
            port: int,
        ):
            captured.append((pod_id, pod_name, namespace, port))
            return sentinel

    reconciler._runtime_cache[("http://192.168.155.20:9111", "hub-1")] = _RemoteRuntime()  # type: ignore[assignment]

    sock = reconciler._portforward_across_runtimes("ha-web-smoke-rev1-0", "default", 8080)

    assert sock is sentinel
    assert captured == [(None, "ha-web-smoke-rev1-0", "default", 8080)]


def test_select_upstreams_prefers_service_vip(tmp_path: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=runtime, state_store=state)

    app = "demo"
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=app),
        spec=AppSpec(
            image="alpine:3.20",
            service=ServiceSpec(port=8080),
            ingress=IngressSpec(host="demo.local", path="/"),
        ),
    )

    state.upsert_service(
        app,
        "10.241.0.10",
        {"ports": [{"name": "http", "port": 8080, "targetPort": 8080, "protocol": "TCP"}]},
    )
    state.upsert_service_endpoints(
        app,
        [
            ServiceEndpoint(
                app_name=app,
                port=8080,
                ip="10.42.0.12",
                target_port=8080,
                ready=True,
            )
        ],
    )

    runtime_result = RuntimeResult(
        revision=1,
        created=0,
        updated=0,
        removed=0,
        pod_states=[
            PodState(
                pod_name="demo-rev1-0",
                ready=True,
                status="running",
                endpoint="10.42.0.12:8080",
            )
        ],
    )
    health_report = HealthReport(
        ready_replicas=1,
        live_replicas=1,
        pods=[
            PodHealth(
                pod_name="demo-rev1-0",
                ready=True,
                live=True,
                readiness_message="ok",
                liveness_message="ok",
            )
        ],
    )

    upstreams = reconciler._select_upstreams(manifest, runtime_result, health_report)
    assert upstreams == ["10.241.0.10:8080"]


def test_select_upstreams_prefers_workerbee_host_port_over_service_vip(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AE_CADDY_PREFER_HOST_PORT_UPSTREAMS", "1")
    runtime = StubDockerIngressRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=runtime, state_store=state)

    app = "demo"
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=app),
        spec=AppSpec(
            image="alpine:3.20",
            ports=[{"name": "http", "containerPort": 8080}],  # type: ignore[list-item]
            service=ServiceSpec(port=8080),
            ingress=IngressSpec(host="demo.local", path="/"),
        ),
    )
    state.upsert_service(
        app,
        "10.241.0.10",
        {"ports": [{"name": "http", "port": 8080, "targetPort": 8080, "protocol": "TCP"}]},
    )
    state.upsert_service_endpoints(
        app,
        [
            ServiceEndpoint(
                app_name=app,
                port=8080,
                ip="10.42.0.12",
                target_port=8080,
                ready=True,
            )
        ],
    )

    runtime_result = RuntimeResult(
        revision=1,
        created=0,
        updated=0,
        removed=0,
        pod_states=[
            PodState(
                pod_name="demo-rev1-0",
                ready=True,
                status="running",
                endpoint="10.42.0.12:8080",
            )
        ],
    )
    health_report = HealthReport(
        ready_replicas=1,
        live_replicas=1,
        pods=[
            PodHealth(
                pod_name="demo-rev1-0",
                ready=True,
                live=True,
                readiness_message="ok",
                liveness_message="ok",
            )
        ],
    )

    upstreams = reconciler._select_upstreams(manifest, runtime_result, health_report)
    assert upstreams == ["127.0.0.1:32000"]


def test_direct_containerd_reconcile_injects_ready_service_host_aliases(
    tmp_path: Path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    runtime = CapturingContainerdRuntime()
    reconciler = Reconciler(runtime=runtime, state_store=state)
    namespace = "rawform-poc-dev-ad9ec5062e"
    minio_app = f"{namespace}--minio"
    other_app = "other--redis"

    ports = {"ports": [{"name": "http", "port": 9000, "targetPort": 9000, "protocol": "TCP"}]}
    state.upsert_service(minio_app, "10.241.0.20", ports)
    state.upsert_service_endpoints(
        minio_app,
        [
            ServiceEndpoint(
                app_name=minio_app,
                port=9000,
                ip="10.210.0.12",
                target_port=9000,
                ready=True,
            ),
            ServiceEndpoint(
                app_name=minio_app,
                port=9000,
                ip="10.210.0.11",
                target_port=9000,
                ready=False,
            ),
        ],
    )
    state.upsert_service(other_app, "10.241.0.21", ports)
    state.upsert_service_endpoints(
        other_app,
        [
            ServiceEndpoint(
                app_name=other_app,
                port=6379,
                ip="10.210.0.22",
                target_port=6379,
                ready=True,
            )
        ],
    )

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="api", namespace=namespace),
        spec=AppSpec(
            image="alpine:3.20",
            env=[{"name": "S3_ENDPOINT", "value": "http://minio:9000"}],
            host_aliases=[
                HostAlias(
                    ip="192.0.2.10",
                    hostnames=[f"minio.{namespace}.svc", "explicit.local"],
                )
            ],
        ),
    )

    reconciler.reconcile(manifest)

    assert runtime.last_manifest is not None
    aliases = runtime.last_manifest.spec.host_aliases
    explicit = next(alias for alias in aliases if alias.ip == "192.0.2.10")
    generated = next(alias for alias in aliases if alias.ip == "10.210.0.12")
    assert set(explicit.hostnames) == {f"minio.{namespace}.svc", "explicit.local"}
    assert set(generated.hostnames) == {
        "minio",
        f"minio.{namespace}",
        f"minio.{namespace}.svc.cluster.local",
    }
    all_names = {name for alias in aliases for name in alias.hostnames}
    assert "redis" not in all_names


def test_direct_containerd_service_aliases_are_revision_affecting(
    tmp_path: Path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    runtime = CapturingContainerdRuntime()
    reconciler = Reconciler(runtime=runtime, state_store=state)
    namespace = "rawform-poc-dev-ad9ec5062e"
    minio_app = f"{namespace}--minio"
    ports = {"ports": [{"name": "http", "port": 9000, "targetPort": 9000, "protocol": "TCP"}]}
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="api", namespace=namespace),
        spec=AppSpec(
            image="alpine:3.20",
            env=[{"name": "S3_ENDPOINT", "value": "http://minio:9000"}],
        ),
    )

    first = reconciler.reconcile(manifest)
    assert first.revision == 1
    assert runtime.last_manifest is not None
    assert not runtime.last_manifest.spec.host_aliases

    state.upsert_service(minio_app, "10.241.0.20", ports)
    state.upsert_service_endpoints(
        minio_app,
        [
            ServiceEndpoint(
                app_name=minio_app,
                port=9000,
                ip="10.210.0.12",
                target_port=9000,
                ready=True,
            )
        ],
    )

    second = reconciler.reconcile(manifest)

    assert second.revision == 2
    assert runtime.revisions == [1, 2]
    assert runtime.last_manifest is not None
    aliases = runtime.last_manifest.spec.host_aliases
    assert aliases and aliases[0].ip == "10.210.0.12"
    assert "minio" in aliases[0].hostnames


def test_direct_containerd_service_aliases_fallback_to_ready_service_pods(
    tmp_path: Path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    runtime = CapturingContainerdRuntime()
    reconciler = Reconciler(
        runtime=runtime,
        state_store=state,
        health_manager=StubHealthManager(),
    )
    namespace = "rawform-poc-dev-ad9ec5062e"
    minio_manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="minio", namespace=namespace),
        spec=AppSpec(image="minio/minio:latest", service=ServiceSpec(port=9000)),
    )
    api_manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="api", namespace=namespace),
        spec=AppSpec(
            image="workerbee-api:dev",
            env=[{"name": "S3_ENDPOINT", "value": "http://minio:9000"}],
        ),
    )

    state.register_app(minio_manifest)
    reconciler.reconcile(minio_manifest)
    state.register_app(api_manifest)
    reconciler.reconcile(api_manifest)

    assert state.list_services() == []
    assert runtime.last_manifest is not None
    assert runtime.last_manifest.metadata.name == "api"
    aliases = runtime.last_manifest.spec.host_aliases
    assert aliases and aliases[0].ip == "10.210.0.44"
    assert "minio" in aliases[0].hostnames
    assert f"minio.{namespace}.svc" in aliases[0].hostnames


def test_direct_containerd_service_ready_refreshes_dependents_without_service_proxy(
    tmp_path: Path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    runtime = CapturingContainerdRuntime()
    reconciler = Reconciler(
        runtime=runtime,
        state_store=state,
        health_manager=StubHealthManager(),
    )
    namespace = "rawform-poc-dev-ad9ec5062e"
    api_manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="api", namespace=namespace),
        spec=AppSpec(
            image="workerbee-api:dev",
            env=[{"name": "S3_ENDPOINT", "value": "http://minio:9000"}],
        ),
    )
    minio_manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="minio", namespace=namespace),
        spec=AppSpec(image="minio/minio:latest", service=ServiceSpec(port=9000)),
    )

    state.register_app(api_manifest)
    first = reconciler.reconcile(api_manifest)
    assert first.revision == 1
    assert runtime.last_manifest is not None
    assert runtime.last_manifest.metadata.name == "api"
    assert not runtime.last_manifest.spec.host_aliases

    state.register_app(minio_manifest)
    reconciler.reconcile(minio_manifest)

    assert state.list_services() == []
    assert runtime.apps == [
        f"{namespace}--api",
        f"{namespace}--minio",
        f"{namespace}--api",
    ]
    assert runtime.revisions == [1, 1, 2]
    assert runtime.last_manifest is not None
    assert runtime.last_manifest.metadata.name == "api"
    aliases = runtime.last_manifest.spec.host_aliases
    assert aliases and aliases[0].ip == "10.210.0.44"
    assert "minio" in aliases[0].hostnames


def test_direct_containerd_service_alias_refresh_removes_old_revision_before_create(
    tmp_path: Path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    namespace = "rawform-poc-dev-ad9ec5062e"

    class NoOverlapRuntime(CapturingContainerdRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[tuple[str, str, int | list[str]]] = []
            self.containers: list[dict] = []

        def ensure_app(
            self,
            manifest: AppManifest,
            revision: int,
            *,
            keep_old: bool = False,
            limit_create: int | None = None,
            pod_names: list[str] | None = None,
            node_id: str | None = None,
        ) -> RuntimeResult:  # type: ignore[override]
            app = f"{manifest.metadata.namespace}--{manifest.metadata.name}"
            self.events.append(("ensure", app, revision))
            result = super().ensure_app(
                manifest,
                revision,
                keep_old=keep_old,
                limit_create=limit_create,
                pod_names=pod_names,
                node_id=node_id,
            )
            pod_name = result.pod_states[0].pod_name
            self.containers.append(
                {
                    "name": pod_name,
                    "labels": {
                        self.APP_LABEL: app,
                        self.POD_LABEL: pod_name,
                        self.REVISION_LABEL: str(revision),
                    },
                    "running": True,
                }
            )
            return result

        def list_containers_info(self) -> list[dict]:
            return list(self.containers)

        def remove_replicas(self, app_name: str, replica_ids: list[str]) -> int:
            self.events.append(("remove", app_name, list(replica_ids)))
            targets = set(replica_ids)
            before = len(self.containers)
            self.containers = [
                item
                for item in self.containers
                if item.get("labels", {}).get(self.POD_LABEL) not in targets
            ]
            return before - len(self.containers)

    runtime = NoOverlapRuntime()
    reconciler = Reconciler(
        runtime=runtime,
        state_store=state,
        health_manager=StubHealthManager(),
    )
    api_manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="api", namespace=namespace),
        spec=AppSpec(
            image="workerbee-api:dev",
            env=[{"name": "S3_ENDPOINT", "value": "http://minio:9000"}],
            service=ServiceSpec(port=8000),
        ),
    )
    minio_manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="minio", namespace=namespace),
        spec=AppSpec(image="minio/minio:latest", service=ServiceSpec(port=9000)),
    )

    state.register_app(api_manifest)
    reconciler.reconcile(api_manifest)
    state.register_app(minio_manifest)
    reconciler.reconcile(minio_manifest)

    remove_index = runtime.events.index(
        (
            "remove",
            f"{namespace}--api",
            [f"{namespace}--api-rev1-0"],
        )
    )
    ensure_refresh_index = runtime.events.index(("ensure", f"{namespace}--api", 2))
    assert remove_index < ensure_refresh_index
    api_containers = [
        item
        for item in runtime.containers
        if item.get("labels", {}).get(runtime.APP_LABEL) == f"{namespace}--api"
    ]
    assert [item["labels"][runtime.REVISION_LABEL] for item in api_containers] == ["2"]

    before_events = list(runtime.events)
    reconciler.reconcile(api_manifest)
    new_events = runtime.events[len(before_events) :]
    assert not any(
        event[0] == "ensure" and event[1] == f"{namespace}--minio" for event in new_events
    )


def test_direct_containerd_service_ready_refreshes_registered_dependents(
    tmp_path: Path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    runtime = CapturingContainerdRuntime()
    namespace = "rawform-poc-dev-ad9ec5062e"
    ports = {"ports": [{"name": "http", "port": 9000, "targetPort": 9000, "protocol": "TCP"}]}

    class FakeServiceController:
        def reconcile(
            self,
            manifest: AppManifest,
            _result: RuntimeResult,
            _health_report: HealthReport,
        ) -> str | None:
            if manifest.metadata.name != "minio":
                return None
            app_name = f"{manifest.metadata.namespace}--{manifest.metadata.name}"
            state.upsert_service(app_name, "10.241.0.20", ports)
            state.upsert_service_endpoints(
                app_name,
                [
                    ServiceEndpoint(
                        app_name=app_name,
                        port=9000,
                        ip="10.210.0.12",
                        target_port=9000,
                        ready=True,
                    )
                ],
            )
            return "10.241.0.20"

    reconciler = Reconciler(
        runtime=runtime,
        state_store=state,
        service_controller=FakeServiceController(),
    )
    api_manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="api", namespace=namespace),
        spec=AppSpec(
            image="workerbee-api:dev",
            env=[{"name": "S3_ENDPOINT", "value": "http://minio:9000"}],
        ),
    )
    minio_manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="minio", namespace=namespace),
        spec=AppSpec(image="minio/minio:latest", service=ServiceSpec(port=9000)),
    )

    state.register_app(api_manifest)
    first = reconciler.reconcile(api_manifest)
    assert first.revision == 1
    assert runtime.last_manifest is not None
    assert runtime.last_manifest.metadata.name == "api"
    assert not runtime.last_manifest.spec.host_aliases

    state.register_app(minio_manifest)
    reconciler.reconcile(minio_manifest)

    assert runtime.apps == [
        f"{namespace}--api",
        f"{namespace}--minio",
        f"{namespace}--api",
    ]
    assert runtime.revisions == [1, 1, 2]
    assert runtime.last_manifest is not None
    assert runtime.last_manifest.metadata.name == "api"
    aliases = runtime.last_manifest.spec.host_aliases
    assert aliases and aliases[0].ip == "10.210.0.12"
    assert "minio" in aliases[0].hostnames


def test_reconciler_applies_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AE_PROJECTION_ROOT", str(tmp_path / "projections"))
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")

    class StubSecrets:
        def load_env(self, _refs):  # noqa: ANN001
            return {"SECRET_VALUE": "hunter2"}

    reconciler = Reconciler(
        runtime=runtime,
        state_store=state,
        secret_manager=StubSecrets(),
    )

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            secret_refs=[
                SecretRef(
                    name="demo-secret",
                    path="irrelevant",
                    env=[SecretEnvMapping(name="SECRET_VALUE", key="SECRET_VALUE")],
                )
            ],
        ),
    )

    reconciler.reconcile(manifest)

    assert runtime.last_manifest is not None
    env_map = {item["name"]: item["value"] for item in runtime.last_manifest.spec.env}
    assert env_map["SECRET_VALUE"] == "hunter2"


# ruff: noqa: S105
