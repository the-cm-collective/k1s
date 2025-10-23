"""Tests for reconcile skeleton."""

from pathlib import Path

from ae.controller.reconciler import ReconcileReport, Reconciler
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import SQLiteStateStore
from ae.runtime.base import ReplicaState, RuntimeAdapter, RuntimeResult


class StubRuntime(RuntimeAdapter):
    def ensure_app(self, manifest: AppManifest) -> RuntimeResult:
        return RuntimeResult(
            created=1,
            updated=0,
            removed=0,
            replica_states=[
                ReplicaState(
                    replica_id=f"{manifest.metadata.name}-0",
                    ready=True,
                    status="running",
                )
            ],
        )


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
    assert status.image == "alpine:3.20"
    assert status.created == 1
    replicas = state.list_replicas("demo")
    assert len(replicas) == 1
    assert replicas[0].live is True
