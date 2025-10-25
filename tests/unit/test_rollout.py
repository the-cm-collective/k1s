from pathlib import Path

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, Metadata, PortSpec
from ae.controller.state import SQLiteStateStore
from ae.controller.health import HealthManager


class DummyRuntime:
    def __init__(self):
        self.calls = []

    def ensure_app(self, manifest, revision, *, keep_old=False, limit_create=None):  # noqa: ANN001
        self.calls.append((keep_old, limit_create))
        from ae.runtime.base import RuntimeResult, ReplicaState

        states = []
        if limit_create is None or limit_create > 0:
            states = [
                ReplicaState(
                    replica_id=f"{manifest.metadata.name}-rev{revision}-0",
                    ready=True,
                    status="running",
                    endpoint="127.0.0.1:9000",
                    started_at=None,
                )
            ]
        return RuntimeResult(
            revision=revision, created=1, updated=0, removed=0, replica_states=states
        )

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        self.calls.append(("remove_old", app_name, keep_revision))
        return 1


def _manifest(strategy: str, replicas: int = 1) -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="roll"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=replicas,
            ports=[PortSpec(name="http", containerPort=8080)],
            rollout={"strategy": strategy, "maxSurge": 1, "maxUnavailable": 0},
        ),
    )


def test_ordered_rollout_calls_keep_old_and_limit(tmp_path: Path):
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = DummyRuntime()
    rec = Reconciler(rt, store, health_manager=HealthManager(), ingress_service=None)
    m = _manifest("ordered", replicas=2)
    rec.reconcile(m)
    assert rt.calls, "ensure_app was not called"
    keep_old, limit = rt.calls[0]
    assert keep_old is True
    assert limit == 1


def test_old_removal_gated_by_readiness(tmp_path: Path):
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = DummyRuntime()
    rec = Reconciler(rt, store, health_manager=HealthManager(), ingress_service=None)
    m = _manifest("parallel", replicas=1)
    rec.reconcile(m)
    assert any(c[0] == "remove_old" for c in rt.calls if isinstance(c, tuple) and len(c) > 0)
