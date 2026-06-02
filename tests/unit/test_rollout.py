from pathlib import Path

from ae.controller.health import HealthManager
from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, Metadata, PortSpec, ServiceSpec
from ae.controller.state import SQLiteStateStore


class DummyRuntime:
    def __init__(self):
        self.calls = []

    def ensure_app(
        self,
        manifest,
        revision,
        *,
        keep_old=False,
        limit_create=None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ):  # noqa: ANN001
        _ = node_id
        self.calls.append((keep_old, limit_create))
        from ae.runtime.base import PodState, RuntimeResult

        states: list[PodState] = []
        if limit_create is None or limit_create > 0:
            names = (
                pod_names
                if pod_names is not None
                else [f"{manifest.metadata.name}-rev{revision}-0"]
            )
            states = [
                PodState(
                    pod_name=name,
                    ready=True,
                    status="running",
                    endpoint="127.0.0.1:9000",
                    started_at=None,
                )
                for name in names
            ]
        return RuntimeResult(
            revision=revision, created=1, updated=0, removed=0, pod_states=states
        )

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        self.calls.append(("remove_old", app_name, keep_revision))
        return 1

    def remove_replicas(self, app_name: str, replica_ids: list[str]) -> int:
        self.calls.append(("remove_replicas", app_name, list(replica_ids)))
        return len(replica_ids)

    def list_containers_info(self) -> list[dict]:
        return []


class BudgetRuntime:
    def __init__(self, *, old_revision: int = 0, old_replicas: int = 0):
        self.ensure_calls: list[dict[str, object]] = []
        self.remove_calls: list[list[str]] = []
        self.current_revision = 0
        self.current_ids: set[str] = set()
        self.old_ids: set[str] = {f"roll-rev{old_revision}-{idx}" for idx in range(old_replicas)}

    def ensure_app(
        self,
        manifest,
        revision,
        *,
        keep_old=False,
        limit_create=None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ):  # noqa: ANN001
        _ = node_id
        desired_ids = (
            list(pod_names)
            if pod_names is not None
            else [f"{manifest.metadata.name}-rev{revision}-{idx}" for idx in range(manifest.spec.replicas)]
        )
        self.current_revision = revision
        self.ensure_calls.append(
            {
                "keep_old": keep_old,
                "limit_create": limit_create,
                "pod_names": list(desired_ids),
            }
        )
        created = 0
        for replica_id in desired_ids:
            if replica_id in self.current_ids:
                continue
            if limit_create is not None and created >= int(limit_create):
                continue
            self.current_ids.add(replica_id)
            created += 1
        removed = 0
        if not keep_old:
            removed = len(self.old_ids)
            self.old_ids.clear()
        from ae.runtime.base import PodState, RuntimeResult

        pod_states = [
            PodState(
                pod_name=replica_id,
                ready=True,
                status="running",
                endpoint="127.0.0.1:9000",
                revision=revision,
            )
            for replica_id in sorted(self.current_ids)
        ]
        return RuntimeResult(
            revision=revision,
            created=created,
            updated=0,
            removed=removed,
            pod_states=pod_states,
        )

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        _ = (app_name, keep_revision)
        removed = len(self.old_ids)
        self.old_ids.clear()
        return removed

    def remove_replicas(self, app_name: str, replica_ids: list[str]) -> int:
        _ = app_name
        self.remove_calls.append(list(replica_ids))
        removed = 0
        for replica_id in replica_ids:
            if replica_id in self.old_ids:
                self.old_ids.remove(replica_id)
                removed += 1
        return removed

    def list_containers_info(self) -> list[dict]:
        items = []
        for replica_id in sorted(self.old_ids):
            items.append(
                {
                    "name": replica_id,
                    "running": True,
                    "labels": {
                        "ae.app": "roll",
                        "ae.namespace": "default",
                        "ae.pod_name": replica_id,
                        "ae.replica_id": replica_id,
                        "ae.revision": replica_id.split("-rev", 1)[1].split("-", 1)[0],
                    },
                }
            )
        for replica_id in sorted(self.current_ids):
            items.append(
                {
                    "name": replica_id,
                    "running": True,
                    "labels": {
                        "ae.app": "roll",
                        "ae.namespace": "default",
                        "ae.pod_name": replica_id,
                        "ae.replica_id": replica_id,
                        "ae.revision": str(self.current_revision),
                    },
                }
            )
        return items


def _manifest(
    strategy: str,
    replicas: int = 1,
    *,
    max_surge: int = 1,
    max_unavailable: int = 0,
) -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="roll"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=replicas,
            ports=[PortSpec(name="http", containerPort=8080)],
            rollout={
                "strategy": strategy,
                "maxSurge": max_surge,
                "maxUnavailable": max_unavailable,
            },
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


def test_serial_service_rollout_disables_keep_old_for_fixed_port_single_replica(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("AE_SERIAL_SERVICE_ROLLOUT", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = DummyRuntime()
    rec = Reconciler(rt, store, health_manager=HealthManager(), ingress_service=None)
    m = _manifest("parallel", replicas=1).model_copy(
        update={
            "spec": _manifest("parallel", replicas=1).spec.model_copy(
                update={"service": ServiceSpec(port=18080, targetPort=8080)}
            )
        }
    )
    rec.reconcile(m)
    assert rt.calls, "ensure_app was not called"
    keep_old, limit = rt.calls[0]
    assert keep_old is False
    assert limit == 1


def test_restart_rollout_disables_keep_old_for_fixed_port_single_replica(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("AE_SERIAL_SERVICE_ROLLOUT", raising=False)
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = DummyRuntime()
    rec = Reconciler(rt, store, health_manager=HealthManager(), ingress_service=None)
    base = _manifest("parallel", replicas=1)
    m = base.model_copy(
        update={
            "spec": base.spec.model_copy(
                update={
                    "service": ServiceSpec(port=18080, targetPort=8080),
                    "rollout": {
                        "strategy": "parallel",
                        "restartAt": "2026-06-02T00:00:00+00:00",
                    },
                }
            )
        }
    )
    rec.reconcile(m)
    assert rt.calls, "ensure_app was not called"
    keep_old, limit = rt.calls[0]
    assert keep_old is False
    assert limit == 1


def test_parallel_rollout_respects_surge_zero_and_unavailable_one(tmp_path: Path):
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = BudgetRuntime(old_replicas=5)
    rec = Reconciler(rt, store, health_manager=HealthManager(), ingress_service=None)
    manifest = _manifest("parallel", replicas=5, max_surge=0, max_unavailable=1)

    rec.reconcile(manifest)
    assert rt.remove_calls == [["roll-rev0-0"]]
    assert rt.ensure_calls[0]["limit_create"] == 1
    assert rt.ensure_calls[0]["pod_names"] == ["roll-rev1-0"]
    assert len(rt.current_ids) == 1
    assert len(rt.old_ids) == 4
    assert len(rt.current_ids) + len(rt.old_ids) == 5

    rec.reconcile(manifest)
    assert rt.remove_calls[1] == ["roll-rev0-1"]
    assert rt.ensure_calls[1]["limit_create"] == 1
    assert rt.ensure_calls[1]["pod_names"] == ["roll-rev1-0", "roll-rev1-1"]
    assert len(rt.current_ids) == 2
    assert len(rt.old_ids) == 3
    assert len(rt.current_ids) + len(rt.old_ids) == 5


def test_parallel_rollout_uses_surge_budget_before_old_removal(tmp_path: Path):
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = BudgetRuntime(old_replicas=5)
    rec = Reconciler(rt, store, health_manager=HealthManager(), ingress_service=None)
    manifest = _manifest("parallel", replicas=5, max_surge=1, max_unavailable=0)

    rec.reconcile(manifest)
    assert rt.remove_calls == []
    assert rt.ensure_calls[0]["limit_create"] == 1
    assert rt.ensure_calls[0]["pod_names"] == ["roll-rev1-0"]
    assert len(rt.current_ids) + len(rt.old_ids) == 6

    rec.reconcile(manifest)
    assert rt.remove_calls[0] == ["roll-rev0-0"]
    assert rt.ensure_calls[1]["limit_create"] == 1
    assert rt.ensure_calls[1]["pod_names"] == ["roll-rev1-0", "roll-rev1-1"]
    assert len(rt.current_ids) + len(rt.old_ids) == 6


def test_ordered_rollout_caps_create_budget_per_reconcile(tmp_path: Path):
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = BudgetRuntime(old_replicas=5)
    rec = Reconciler(rt, store, health_manager=HealthManager(), ingress_service=None)
    manifest = _manifest("ordered", replicas=5, max_surge=3, max_unavailable=1)

    rec.reconcile(manifest)
    assert rt.remove_calls == [["roll-rev0-0"]]
    assert rt.ensure_calls[0]["limit_create"] == 1
    assert rt.ensure_calls[0]["pod_names"] == [
        "roll-rev1-0",
        "roll-rev1-1",
        "roll-rev1-2",
        "roll-rev1-3",
    ]
    assert len(rt.current_ids) == 1
    assert len(rt.old_ids) == 4
