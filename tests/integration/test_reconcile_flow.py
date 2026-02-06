"""Integration-style tests covering reconciler, metrics, and events."""

from __future__ import annotations

from pathlib import Path

from ae.controller.reconciler import Reconciler
from ae.controller.spec import (
    AppManifest,
    AppSpec,
    IngressSpec,
    Metadata,
    SecretEnvMapping,
    SecretRef,
)
from ae.controller.state import SQLiteStateStore
from ae.ingress.service import IngressResult, IngressService
from ae.observability import MetricsService
from ae.runtime.base import PodState, RuntimeAdapter, RuntimeResult
from ae.secrets import SecretManager


class StubRuntime(RuntimeAdapter):
    def __init__(self) -> None:
        self.manifests: list[AppManifest] = []

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
        self.manifests.append(manifest)
        names = (
            pod_names
            if pod_names is not None
            else [f"{manifest.metadata.name}-rev{revision}-{idx}" for idx in range(manifest.spec.replicas)]
        )
        return RuntimeResult(
            revision=revision,
            created=len(names),
            updated=0,
            removed=0,
            pod_states=[
                PodState(
                    pod_name=pod_name,
                    ready=True,
                    status="running",
                    endpoint=f"127.0.0.1:{9000 + idx}",
                )
                for idx, pod_name in enumerate(names)
            ],
        )


class StubIngressManager:
    def __init__(self) -> None:
        self.applied: list[IngressResult] = []
        self.removed: list[str] = []
        self.reloads = 0

    def apply(self, manifest: AppManifest, _upstream: str) -> IngressResult:
        result = IngressResult(
            app_name=manifest.metadata.name,
            host=manifest.spec.ingress.host if manifest.spec.ingress else None,
            config_path=f"/tmp/{manifest.metadata.name}.caddy",
        )
        self.applied.append(result)
        return result

    def remove(self, app_name: str) -> None:
        self.removed.append(app_name)

    def reload(self) -> None:
        self.reloads += 1


class StubSecretManager(SecretManager):
    def __init__(self) -> None:  # type: ignore[override]
        super().__init__(allow_plaintext=True)

    def load_env(self, _refs):  # noqa: ANN001
        return {"SECRET_VALUE": "hunter2"}


def build_manifest(image: str) -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image=image,
            replicas=2,
            ingress=IngressSpec(host="demo.local", path="/"),
            secret_refs=[
                SecretRef(
                    name="demo",
                    path="ignored",
                    env=[SecretEnvMapping(name="SECRET_VALUE", key="SECRET_VALUE")],
                )
            ],
        ),
    )


def test_reconcile_emits_events_and_metrics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AE_PROJECTION_ROOT", str(tmp_path / "projections"))
    state = SQLiteStateStore(tmp_path / "state.db")
    runtime = StubRuntime()
    ingress = IngressService(StubIngressManager())
    secrets = StubSecretManager()

    reconciler = Reconciler(
        runtime=runtime,
        state_store=state,
        secret_manager=secrets,
        ingress_service=ingress,
    )

    manifest_v1 = build_manifest("alpine:3.20")
    report_v1 = reconciler.reconcile(manifest_v1)
    assert report_v1.revision == 1
    assert report_v1.revision_status == "ready"

    manifest_v2 = build_manifest("alpine:3.21")
    report_v2 = reconciler.reconcile(manifest_v2)
    assert report_v2.revision == 2

    status = state.get_status("demo")
    assert status is not None
    assert status.revision == 2
    assert status.ready_replicas == 2

    events = state.list_events("demo", limit=10)
    kinds = {event.event_type for event in events}
    assert {"ApplyStarted", "ApplyCompleted", "IngressConfigured"}.issubset(kinds)

    metrics = MetricsService(state).snapshot()
    assert metrics.total_apps == 1
    assert metrics.ready_apps == 1
    assert metrics.total_replicas == 2
    assert metrics.ready_replicas == 2

    revisions = state.list_revisions("demo", limit=5)
    assert len(revisions) >= 2


# ruff: noqa: S108
