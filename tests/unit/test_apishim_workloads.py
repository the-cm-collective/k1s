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

    md = {"name": "agent", "namespace": "default"}
    spec = {
        "selector": {"matchLabels": {"app": "agent"}},
        "template": {
            "metadata": {"labels": {"app": "agent"}},
            "spec": {"containers": [{"name": "agent", "image": "busybox"}]},
        },
    }
    ds = K8sObject("apps", "v1", "daemonsets", "default", "agent", md, spec, {}, 1)

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


# ruff: noqa: E501
