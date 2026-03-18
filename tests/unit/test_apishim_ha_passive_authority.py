import json
from datetime import datetime, timezone
from types import SimpleNamespace

from ae.apishim.ha_store import MultiplexApishimStore
from ae.apishim.store import ObjectStore
from ae.controller.cronjob_authority import CronJobAuthorityController
from ae.controller.state import SQLiteStateStore
from tests.unit.test_apishim_storage import _handler, _json_body


def _make_store(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    store = MultiplexApishimStore.from_state_and_legacy(state, legacy)
    return state, legacy, store


def _ha_handler(store, state, monkeypatch, path: str, *, method: str, body: bytes = b""):
    monkeypatch.setenv("AE_HA_MODE", "1")
    handler, status = _handler(store, monkeypatch, path, method=method, body=body)
    handler.server = SimpleNamespace(store=store, state=state, runtime=None)
    handler.state = state
    return handler, status


def test_apishim_ha_routes_configmap_put_through_shared_authority(monkeypatch, tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo", "namespace": "default"},
            "data": {"hello": "world"},
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps/demo",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "ConfigMap"
    assert payload["data"]["hello"] == "world"
    assert payload["metadata"]["resourceVersion"] == "1"
    assert legacy.get("", "v1", "configmaps", "default", "demo") is None
    entry = state.get_authority_object("", "v1", "configmaps", "default", "demo")
    assert entry is not None
    assert entry.spec["hello"] == "world"


def test_apishim_ha_configmap_conflict_returns_409(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo", "namespace": "default"},
            "data": {"hello": "world"},
        }
    ).encode("utf-8")
    handler, _status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps/demo",
        method="PUT",
        body=body,
    )
    handler.do_PUT()

    stale = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "demo", "namespace": "default", "resourceVersion": "0"},
        "data": {"hello": "again"},
    }
    conflict_handler, conflict_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps/demo",
        method="PUT",
        body=json.dumps(stale).encode("utf-8"),
    )

    conflict_handler.do_PUT()

    assert conflict_status["code"] == 409
    payload = _json_body(conflict_handler)
    assert payload["reason"] == "Conflict"
    assert "resourceVersion conflict" in payload["message"]


def test_apishim_ha_serviceaccount_round_trips_top_level_fields(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "demo-sa", "namespace": "default"},
            "imagePullSecrets": [{"name": "regcred"}],
            "automountServiceAccountToken": False,
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/serviceaccounts/demo-sa",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "ServiceAccount"
    assert payload["imagePullSecrets"] == [{"name": "regcred"}]
    assert payload["automountServiceAccountToken"] is False
    entry = state.get_authority_object("", "v1", "serviceaccounts", "default", "demo-sa")
    assert entry is not None
    assert entry.spec["imagePullSecrets"] == [{"name": "regcred"}]
    assert entry.spec["automountServiceAccountToken"] is False


def test_apishim_ha_routes_cronjob_through_shared_authority(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "demo-cron", "namespace": "default"},
            "spec": {
                "schedule": "*/5 * * * *",
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "metadata": {"labels": {"app": "demo-cron"}},
                            "spec": {
                                "containers": [{"name": "main", "image": "busybox"}],
                                "restartPolicy": "Never",
                            },
                        }
                    }
                },
            },
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/batch/v1/namespaces/default/cronjobs/demo-cron",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "CronJob"
    assert payload["spec"]["schedule"] == "*/5 * * * *"
    entry = state.get_authority_object("batch", "v1", "cronjobs", "default", "demo-cron")
    assert entry is not None
    assert entry.spec["schedule"] == "*/5 * * * *"


def test_apishim_ha_routes_hpa_through_shared_authority(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": "demo-hpa", "namespace": "default"},
            "spec": {
                "minReplicas": 1,
                "maxReplicas": 5,
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "demo",
                },
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {"type": "Utilization", "averageUtilization": 70},
                        },
                    }
                ],
            },
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/autoscaling/v2/namespaces/default/horizontalpodautoscalers/demo-hpa",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "HorizontalPodAutoscaler"
    assert payload["spec"]["maxReplicas"] == 5
    entry = state.get_authority_object(
        "autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa"
    )
    assert entry is not None
    assert entry.spec["scaleTargetRef"]["kind"] == "Deployment"


def test_apishim_ha_rejects_unsupported_hpa_metric_type(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": "demo-hpa", "namespace": "default"},
            "spec": {
                "minReplicas": 1,
                "maxReplicas": 5,
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "demo",
                },
                "metrics": [
                    {
                        "type": "Pods",
                        "pods": {
                            "metric": {"name": "requests"},
                            "target": {"type": "AverageValue", "averageValue": "1"},
                        },
                    }
                ],
            },
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/autoscaling/v2/namespaces/default/horizontalpodautoscalers/demo-hpa",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 422
    payload = _json_body(handler)
    assert payload["reason"] == "Invalid"
    assert "Resource metrics" in payload["message"]
    assert state.get_authority_object(
        "autoscaling", "v2", "horizontalpodautoscalers", "default", "demo-hpa"
    ) is None


def test_apishim_ha_rejects_unsupported_hpa_target_kind(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": "demo-hpa", "namespace": "default"},
            "spec": {
                "minReplicas": 1,
                "maxReplicas": 5,
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "demo",
                },
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {"type": "Utilization", "averageUtilization": 70},
                        },
                    }
                ],
            },
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/autoscaling/v2/namespaces/default/horizontalpodautoscalers/demo-hpa",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 422
    payload = _json_body(handler)
    assert payload["reason"] == "Invalid"
    assert "Deployment, StatefulSet, and DaemonSet" in payload["message"]


def test_apishim_ha_cronjob_write_executes_through_controller_authority(
    monkeypatch,
    tmp_path,
) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {
                "name": "demo-cron",
                "namespace": "default",
                "annotations": {"cronjob.k1s.dev/intervalSeconds": "60"},
            },
            "spec": {
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "metadata": {"labels": {"app": "demo-cron"}},
                            "spec": {
                                "containers": [{"name": "main", "image": "busybox"}],
                                "restartPolicy": "Never",
                            },
                        }
                    }
                },
            },
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/batch/v1/namespaces/default/cronjobs/demo-cron",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    controller = CronJobAuthorityController(
        state,
        authority=SimpleNamespace(snapshot=lambda: SimpleNamespace(is_leader=True)),
    )
    monkeypatch.setattr(
        controller,
        "_now",
        lambda: datetime(2026, 3, 17, 12, 0, 5, tzinfo=timezone.utc),
    )

    controller.run_once()

    jobs = store.list("batch", "v1", "jobs", "default")
    assert len(jobs) == 1
    assert jobs[0].metadata["ownerReferences"][0]["kind"] == "CronJob"
    cronjob = store.get("batch", "v1", "cronjobs", "default", "demo-cron")
    assert cronjob is not None
    assert cronjob.status["lastScheduleTime"] == "2026-03-17T12:00:00Z"
