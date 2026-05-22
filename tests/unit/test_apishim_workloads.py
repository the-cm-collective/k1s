from types import SimpleNamespace

from ae.apishim.adapter import AdapterWorker
from ae.apishim.store import K8sObject, ObjectStore
from ae.controller.health import HealthManager
from ae.controller.reconciler import Reconciler
from ae.controller.spec import app_key
from ae.controller.state import SQLiteStateStore
from ae.runtime import StubRuntime


def _make_adapter(tmp_path):
    store = ObjectStore(tmp_path / "apishim.db")
    state = SQLiteStateStore(tmp_path / "state.db")
    reconciler = Reconciler(
        runtime=StubRuntime(), state_store=state, health_manager=HealthManager()
    )
    adapter = AdapterWorker(store, state, reconciler)
    return store, state, adapter


def _status_row(
    *,
    desired: int = 1,
    ready: int = 1,
    live: int = 1,
    revision: int = 1,
    revision_status: str = "ready",
):
    return SimpleNamespace(
        desired_replicas=desired,
        ready_replicas=ready,
        live_replicas=live,
        revision=revision,
        revision_status=revision_status,
    )


def test_statefulset_status_populated(tmp_path):
    store, _state, adapter = _make_adapter(tmp_path)
    md = {"name": "db", "namespace": "default", "generation": 2}
    spec = {
        "replicas": 3,
        "selector": {"matchLabels": {"app": "db"}},
        "template": {
            "metadata": {"labels": {"app": "db"}},
            "spec": {"containers": [{"name": "db", "image": "busybox"}]},
        },
    }
    sts = K8sObject("apps", "v1", "statefulsets", "default", "db", md, spec, {}, 1)
    store.upsert("apps", "v1", "statefulsets", "default", "db", md, spec, status={})

    adapter._apply_statefulset(sts)

    stored = store.get("apps", "v1", "statefulsets", "default", "db")
    assert stored is not None
    st = stored.status
    assert st.get("replicas") == 3
    assert st.get("readyReplicas") == 3
    assert st.get("currentRevision") == 2


def test_daemonset_status_reflects_nodes(tmp_path):
    store, state, adapter = _make_adapter(tmp_path)
    # Register two nodes so desiredNumberScheduled matches
    state.upsert_node("n1", name="node1", labels={"zone": "a"})
    state.upsert_node("n2", name="node2", labels={"zone": "b"})
    state.record_heartbeat("n1", "Ready")
    state.record_heartbeat("n2", "Ready")

    md = {"name": "agent", "namespace": "default"}
    spec = {
        "selector": {"matchLabels": {"app": "agent"}},
        "template": {
            "metadata": {"labels": {"app": "agent"}},
            "spec": {"containers": [{"name": "agent", "image": "busybox"}]},
        },
    }
    ds = K8sObject("apps", "v1", "daemonsets", "default", "agent", md, spec, {}, 1)
    store.upsert("apps", "v1", "daemonsets", "default", "agent", md, spec, status={})

    adapter._apply_daemonset(ds)

    stored = store.get("apps", "v1", "daemonsets", "default", "agent")
    st = stored.status
    assert st.get("desiredNumberScheduled") == 2
    assert st.get("numberReady") == 2


def test_job_completes_and_records_event(tmp_path):
    store, state, adapter = _make_adapter(tmp_path)
    md = {"name": "batcher", "namespace": "default"}
    spec = {
        "parallelism": 2,
        "completions": 2,
        "template": {
            "metadata": {"labels": {"job": "batcher"}},
            "spec": {"containers": [{"name": "job", "image": "busybox"}]},
        },
    }
    job = K8sObject("batch", "v1", "jobs", "default", "batcher", md, spec, {}, 1)
    store.upsert("batch", "v1", "jobs", "default", "batcher", md, spec, status={})

    adapter._apply_job(job)

    stored = store.get("batch", "v1", "jobs", "default", "batcher")
    st = stored.status
    assert st.get("succeeded") == 2
    conds = {c["type"]: c["status"] for c in st.get("conditions", [])}
    assert conds.get("Complete") == "True"
    events = state.list_events(app_key("batcher", "default"))
    assert any(e.event_type == "Complete" for e in events)


def test_cronjob_fires_job_with_owner_reference(tmp_path):
    store, _state, adapter = _make_adapter(tmp_path)
    md = {
        "name": "cron",
        "namespace": "default",
        "annotations": {"cronjob.k1s.dev/intervalSeconds": "0"},
    }
    spec = {
        "jobTemplate": {
            "spec": {
                "template": {
                    "metadata": {"labels": {"job": "cron"}},
                    "spec": {"containers": [{"name": "job", "image": "busybox"}]},
                },
                "parallelism": 1,
                "completions": 1,
            }
        }
    }
    cj = K8sObject("batch", "v1", "cronjobs", "default", "cron", md, spec, {}, 1)

    adapter._apply_cronjob(cj)

    jobs = store.list("batch", "v1", "jobs", "default")
    assert jobs, "cronjob should have spawned a job"
    job = jobs[0]
    owner_refs = job.metadata.get("ownerReferences", [])
    assert owner_refs and owner_refs[0].get("kind") == "CronJob"
    cj_status = store.get("batch", "v1", "cronjobs", "default", "cron").status
    assert cj_status.get("lastScheduleTime") is not None


def test_cronjob_invalid_schedule_does_not_fallback_to_interval(tmp_path):
    store, _state, adapter = _make_adapter(tmp_path)
    md = {"name": "cron", "namespace": "default"}
    spec = {
        "schedule": "not a cron",
        "jobTemplate": {
            "spec": {
                "template": {
                    "metadata": {"labels": {"job": "cron"}},
                    "spec": {"containers": [{"name": "job", "image": "busybox"}]},
                },
            }
        },
    }
    cj = K8sObject("batch", "v1", "cronjobs", "default", "cron", md, spec, {}, 1)

    adapter._apply_cronjob(cj)

    assert store.list("batch", "v1", "jobs", "default") == []
    cj_status = store.get("batch", "v1", "cronjobs", "default", "cron").status
    conditions = {item.get("type"): item for item in cj_status.get("conditions", [])}
    assert conditions["ScheduleValid"]["status"] == "False"
    assert conditions["ScheduleValid"]["reason"] == "InvalidSchedule"


def test_cronjob_invalid_schedule_fallback_requires_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("AE_APISHIM_ALLOW_INVALID_CRON_FALLBACK", "1")
    store, _state, adapter = _make_adapter(tmp_path)
    adapter._cronjob_jobs[("default", "cron")] = {"last_run": 0}
    md = {"name": "cron", "namespace": "default"}
    spec = {
        "schedule": "not a cron",
        "jobTemplate": {
            "spec": {
                "template": {
                    "metadata": {"labels": {"job": "cron"}},
                    "spec": {"containers": [{"name": "job", "image": "busybox"}]},
                },
            }
        },
    }
    cj = K8sObject("batch", "v1", "cronjobs", "default", "cron", md, spec, {}, 1)

    adapter._apply_cronjob(cj)

    assert store.list("batch", "v1", "jobs", "default")
    cj_status = store.get("batch", "v1", "cronjobs", "default", "cron").status
    conditions = {item.get("type"): item for item in cj_status.get("conditions", [])}
    assert conditions["ScheduleValid"]["reason"] == "InvalidScheduleFallback"


def test_service_named_target_port_must_resolve(tmp_path):
    store, _state, adapter = _make_adapter(tmp_path)
    dep_spec = {
        "selector": {"matchLabels": {"app": "demo"}},
        "template": {
            "metadata": {"labels": {"app": "demo"}},
            "spec": {
                "containers": [
                    {
                        "name": "demo",
                        "image": "busybox",
                        "ports": [{"name": "http", "containerPort": 8080}],
                    }
                ]
            },
        },
    }
    store.upsert(
        "apps",
        "v1",
        "deployments",
        "default",
        "demo",
        {"name": "demo", "namespace": "default"},
        dep_spec,
    )
    svc = K8sObject(
        "",
        "v1",
        "services",
        "default",
        "demo",
        {"name": "demo", "namespace": "default"},
        {"selector": {"app": "demo"}, "ports": [{"name": "web", "port": 80, "targetPort": "missing"}]},
        {},
        1,
    )

    assert adapter._service_spec_for(svc) is None
    stored = store.get("", "v1", "services", "default", "demo")
    assert stored is not None
    conditions = {item.get("type"): item for item in stored.status.get("conditions", [])}
    assert conditions["PortResolution"]["reason"] == "UnresolvedTargetPort"


def test_service_unresolved_named_target_port_fallback_requires_opt_in(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AE_APISHIM_ALLOW_UNRESOLVED_TARGETPORT_FALLBACK", "1")
    store, _state, adapter = _make_adapter(tmp_path)
    dep_spec = {
        "selector": {"matchLabels": {"app": "demo"}},
        "template": {
            "metadata": {"labels": {"app": "demo"}},
            "spec": {"containers": [{"name": "demo", "image": "busybox"}]},
        },
    }
    store.upsert(
        "apps",
        "v1",
        "deployments",
        "default",
        "demo",
        {"name": "demo", "namespace": "default"},
        dep_spec,
    )
    svc = K8sObject(
        "",
        "v1",
        "services",
        "default",
        "demo",
        {"name": "demo", "namespace": "default"},
        {"selector": {"app": "demo"}, "ports": [{"name": "web", "port": 80, "targetPort": "missing"}]},
        {},
        1,
    )

    result = adapter._service_spec_for(svc)

    assert result is not None
    _dep_key, svc_spec = result
    assert svc_spec.ports[0].target_port == 80


def test_statefulset_deleted_during_reconcile_is_not_recreated(tmp_path, monkeypatch):
    monkeypatch.setenv("AE_APISHIM_TOMBSTONE_TTL", "0")
    store, _state, adapter = _make_adapter(tmp_path)
    md = {"name": "db", "namespace": "default", "generation": 2, "uid": "sts-old"}
    spec = {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "db"}},
        "template": {
            "metadata": {"labels": {"app": "db"}},
            "spec": {"containers": [{"name": "db", "image": "busybox"}]},
        },
    }
    store.upsert("apps", "v1", "statefulsets", "default", "db", md, spec, status={})
    sts = K8sObject("apps", "v1", "statefulsets", "default", "db", md, spec, {}, 1)
    adapter._state.get_status = lambda _app: _status_row()  # type: ignore[method-assign]

    def _delete_during_reconcile(_manifest):
        assert store.delete("apps", "v1", "statefulsets", "default", "db")

    adapter._reconciler.reconcile = _delete_during_reconcile  # type: ignore[method-assign]

    adapter._apply_statefulset(sts)

    assert store.get("apps", "v1", "statefulsets", "default", "db") is None


def test_daemonset_replaced_during_reconcile_does_not_overwrite_new_uid(tmp_path, monkeypatch):
    monkeypatch.setenv("AE_APISHIM_TOMBSTONE_TTL", "0")
    store, _state, adapter = _make_adapter(tmp_path)
    old_md = {"name": "agent", "namespace": "default", "uid": "ds-old"}
    old_spec = {
        "selector": {"matchLabels": {"app": "agent"}},
        "template": {
            "metadata": {"labels": {"app": "agent"}},
            "spec": {"containers": [{"name": "agent", "image": "busybox"}]},
        },
    }
    new_md = {
        "name": "agent",
        "namespace": "default",
        "uid": "ds-new",
        "labels": {"version": "replacement"},
    }
    new_spec = {
        "selector": {"matchLabels": {"app": "agent-v2"}},
        "template": {
            "metadata": {"labels": {"app": "agent-v2"}},
            "spec": {"containers": [{"name": "agent", "image": "busybox:1.37"}]},
        },
    }
    replacement_status = {"phase": "replacement"}
    store.upsert("apps", "v1", "daemonsets", "default", "agent", old_md, old_spec, status={})
    ds = K8sObject("apps", "v1", "daemonsets", "default", "agent", old_md, old_spec, {}, 1)
    adapter._state.get_status = lambda _app: _status_row(desired=1, ready=1, live=1)  # type: ignore[method-assign]

    def _replace_during_reconcile(_manifest):
        assert store.delete("apps", "v1", "daemonsets", "default", "agent")
        store.upsert(
            "apps",
            "v1",
            "daemonsets",
            "default",
            "agent",
            new_md,
            new_spec,
            status=replacement_status,
        )

    adapter._reconciler.reconcile = _replace_during_reconcile  # type: ignore[method-assign]

    adapter._apply_daemonset(ds)

    current = store.get("apps", "v1", "daemonsets", "default", "agent")
    assert current is not None
    assert current.metadata.get("uid") == "ds-new"
    assert current.spec == new_spec
    assert current.status == replacement_status


def test_pending_status_write_skips_replaced_workload(tmp_path, monkeypatch):
    monkeypatch.setenv("AE_APISHIM_TOMBSTONE_TTL", "0")
    store, _state, adapter = _make_adapter(tmp_path)
    old_md = {"name": "db", "namespace": "default", "generation": 2, "uid": "sts-old"}
    old_spec = {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "db"}},
        "template": {
            "metadata": {"labels": {"app": "db"}},
            "spec": {"containers": [{"name": "db", "image": "busybox"}]},
        },
    }
    new_md = {"name": "db", "namespace": "default", "generation": 3, "uid": "sts-new"}
    new_spec = {
        "replicas": 2,
        "selector": {"matchLabels": {"app": "db-v2"}},
        "template": {
            "metadata": {"labels": {"app": "db-v2"}},
            "spec": {"containers": [{"name": "db", "image": "busybox:1.37"}]},
        },
    }
    replacement_status = {"message": "replacement"}
    store.upsert("apps", "v1", "statefulsets", "default", "db", old_md, old_spec, status={})
    assert store.delete("apps", "v1", "statefulsets", "default", "db")
    store.upsert(
        "apps",
        "v1",
        "statefulsets",
        "default",
        "db",
        new_md,
        new_spec,
        status=replacement_status,
    )
    stale = K8sObject("apps", "v1", "statefulsets", "default", "db", old_md, old_spec, {}, 1)

    adapter._update_workload_pending_status(
        stale,
        desired=1,
        pending=["data-db-0"],
        kind="statefulset",
    )

    current = store.get("apps", "v1", "statefulsets", "default", "db")
    assert current is not None
    assert current.metadata.get("uid") == "sts-new"
    assert current.spec == new_spec
    assert current.status == replacement_status


# ruff: noqa: E501
