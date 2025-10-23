"""Tests for reconcile skeleton."""

from pathlib import Path

from ae.controller.reconciler import ReconcileReport, Reconciler
from ae.controller.spec import (
    AppManifest,
    AppSpec,
    IngressSpec,
    Metadata,
    SecretEnvMapping,
    SecretRef,
)
from ae.controller.state import SQLiteStateStore
from ae.runtime.base import ReplicaState, RuntimeAdapter, RuntimeResult


class StubRuntime(RuntimeAdapter):
    def __init__(self) -> None:
        self.last_manifest: AppManifest | None = None

    def ensure_app(self, manifest: AppManifest, revision: int) -> RuntimeResult:
        self.last_manifest = manifest
        return RuntimeResult(
            revision=revision,
            created=1,
            updated=0,
            removed=0,
            replica_states=[
                ReplicaState(
                    replica_id=f"{manifest.metadata.name}-rev{revision}-0",
                    ready=True,
                    status="running",
                    endpoint="127.0.0.1:32000",
                )
            ],
        )


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


def test_reconciler_updates_state(tmp_path: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(runtime=runtime, state_store=state)

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
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
    replicas = state.list_replicas("demo")
    assert len(replicas) == 1
    assert replicas[0].live is True


def test_reconciler_with_ingress(tmp_path: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")
    ingress_service = StubIngressService()
    reconciler = Reconciler(runtime=runtime, state_store=state, ingress_service=ingress_service)

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
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


def test_reconciler_applies_secrets(tmp_path: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp_path / "state.db")

    class StubSecrets:
        def load_env(self, refs):  # noqa: ANN001
            return {"SECRET_VALUE": "hunter2"}

    reconciler = Reconciler(
        runtime=runtime,
        state_store=state,
        secret_manager=StubSecrets(),
    )

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
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
