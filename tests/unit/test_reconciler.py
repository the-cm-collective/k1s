"""Tests for reconcile skeleton."""

from pathlib import Path

from ae.controller.health import HealthReport, PodHealth
from ae.controller.reconciler import Reconciler, ReconcileReport
from ae.controller.spec import (
    AppManifest,
    AppSpec,
    IngressSpec,
    Metadata,
    SecretEnvMapping,
    SecretRef,
    ServiceSpec,
)
from ae.controller.state import ServiceEndpoint, SQLiteStateStore
from ae.runtime.base import PodState, RuntimeAdapter, RuntimeResult


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
