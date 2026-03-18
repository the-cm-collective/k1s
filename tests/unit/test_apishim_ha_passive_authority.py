import json
from types import SimpleNamespace

from ae.apishim.ha_store import MultiplexApishimStore
from ae.apishim.store import ObjectStore
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
