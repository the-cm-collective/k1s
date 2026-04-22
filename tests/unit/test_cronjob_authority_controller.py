from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from ae.apishim.ha_store import MultiplexApishimStore
from ae.apishim.store import ObjectStore
from ae.controller.cronjob_authority import CronJobAuthorityController
from ae.controller.state import SQLiteStateStore


class _FakeAuthority:
    def __init__(self, *, is_leader: bool) -> None:
        self.is_leader = is_leader

    def snapshot(self):
        return SimpleNamespace(is_leader=self.is_leader)


def _make_state(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    return state, legacy, MultiplexApishimStore.from_state_and_legacy(state, legacy)


def _cronjob_body(*, interval_seconds: int = 60) -> tuple[dict, dict]:
    metadata = {
        "name": "demo-cron",
        "namespace": "default",
        "uid": "cron-uid-1",
        "annotations": {"cronjob.k1s.dev/intervalSeconds": str(interval_seconds)},
    }
    spec = {
        "jobTemplate": {
            "spec": {
                "parallelism": 1,
                "completions": 1,
                "template": {
                    "metadata": {"labels": {"app": "demo-cron"}},
                    "spec": {
                        "containers": [{"name": "main", "image": "busybox"}],
                        "restartPolicy": "Never",
                    },
                },
            }
        }
    }
    return metadata, spec


def test_cronjob_authority_controller_schedules_only_on_leader(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_state(tmp_path)
    metadata, spec = _cronjob_body()
    store.upsert(
        "batch",
        "v1",
        "cronjobs",
        "default",
        "demo-cron",
        metadata=metadata,
        spec=spec,
        status={},
    )
    authority = _FakeAuthority(is_leader=False)
    controller = CronJobAuthorityController(state, authority=authority)
    fixed_now = datetime(2026, 3, 17, 12, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(controller, "_now", lambda: fixed_now)

    controller.run_once()
    assert state.list_registered_apps() == []

    authority.is_leader = True
    controller.run_once()

    jobs = store.list("batch", "v1", "jobs", "default")
    assert len(jobs) == 1
    owner_refs = jobs[0].metadata.get("ownerReferences", [])
    assert owner_refs and owner_refs[0]["kind"] == "CronJob"
    cron = store.get("batch", "v1", "cronjobs", "default", "demo-cron")
    assert cron is not None
    assert cron.status["lastScheduleTime"] == "2026-03-17T12:00:00Z"


def test_cronjob_authority_controller_uses_deterministic_job_identity(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_state(tmp_path)
    metadata, spec = _cronjob_body()
    store.upsert(
        "batch",
        "v1",
        "cronjobs",
        "default",
        "demo-cron",
        metadata=metadata,
        spec=spec,
        status={},
    )
    controller = CronJobAuthorityController(state, authority=_FakeAuthority(is_leader=True))
    fixed_now = datetime(2026, 3, 17, 12, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(controller, "_now", lambda: fixed_now)

    controller.run_once()

    cron_entry = state.get_authority_object("batch", "v1", "cronjobs", "default", "demo-cron")
    assert cron_entry is not None
    state.register_authority_object(
        "batch",
        "v1",
        "cronjobs",
        "default",
        "demo-cron",
        kind="CronJob",
        metadata=dict(cron_entry.metadata or {}),
        spec=dict(cron_entry.spec or {}),
        status={},
        expected_resource_version=cron_entry.resource_version,
    )

    controller.run_once()

    jobs = store.list("batch", "v1", "jobs", "default")
    assert len(jobs) == 1
    assert jobs[0].name == "demo-cron-run-20260317120000"
    cron = store.get("batch", "v1", "cronjobs", "default", "demo-cron")
    assert cron is not None
    assert cron.status["lastScheduleTime"] == "2026-03-17T12:00:00Z"


def test_cronjob_authority_controller_resumes_after_failover_without_duplicate_slot(
    monkeypatch,
    tmp_path,
) -> None:
    state, _legacy, store = _make_state(tmp_path)
    metadata, spec = _cronjob_body()
    store.upsert(
        "batch",
        "v1",
        "cronjobs",
        "default",
        "demo-cron",
        metadata=metadata,
        spec=spec,
        status={},
    )
    authority_a = _FakeAuthority(is_leader=True)
    authority_b = _FakeAuthority(is_leader=False)
    controller_a = CronJobAuthorityController(state, authority=authority_a)
    controller_b = CronJobAuthorityController(state, authority=authority_b)
    fixed_now = datetime(2026, 3, 17, 12, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(controller_a, "_now", lambda: fixed_now)
    monkeypatch.setattr(controller_b, "_now", lambda: fixed_now)

    controller_a.run_once()
    authority_a.is_leader = False
    authority_b.is_leader = True
    controller_b.run_once()

    jobs = store.list("batch", "v1", "jobs", "default")
    assert len(jobs) == 1
    assert jobs[0].name == "demo-cron-run-20260317120000"
