import json
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

from ae.apishim import server as shim_server
from ae.apishim.ha_store import MultiplexApishimStore
from ae.apishim.store import ObjectStore
from ae.controller.cronjob_authority import CronJobAuthorityController
from ae.controller.state import SQLiteStateStore
from tests.unit.test_apishim_rbac import make_handler
from tests.unit.test_apishim_storage import _handler, _json_body


def _make_store(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    store = MultiplexApishimStore.from_state_and_legacy(state, legacy)
    return state, legacy, store


def _make_store_pair(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy_a = ObjectStore(tmp_path / "apishim-a.db")
    legacy_b = ObjectStore(tmp_path / "apishim-b.db")
    store_a = MultiplexApishimStore.from_state_and_legacy(state, legacy_a)
    store_b = MultiplexApishimStore.from_state_and_legacy(state, legacy_b)
    return state, legacy_a, legacy_b, store_a, store_b


def _ha_handler(store, state, monkeypatch, path: str, *, method: str, body: bytes = b""):
    monkeypatch.setenv("AE_HA_MODE", "1")
    handler, status = _handler(store, monkeypatch, path, method=method, body=body)
    handler.server = SimpleNamespace(store=store, state=state, runtime=None)
    handler.state = state
    return handler, status


def _ha_custom_handler(
    store,
    state,
    monkeypatch,
    path: str,
    *,
    method: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
):
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "a")
    shim_server.ShimHandler.admin_token = "a"  # noqa: S105 - test token
    shim_server.ShimHandler.read_token = None
    monkeypatch.setattr(shim_server.ShimHandler, "handle", lambda _self: None)

    request_headers = dict(headers or {})
    if body and "Content-Length" not in request_headers:
        request_headers["Content-Length"] = str(len(body))
    req = make_handler(path, method=method, headers=request_headers, body=body)
    handler = shim_server.ShimHandler(req, ("127.0.0.1", 0), None)
    handler.path = req.path
    handler.command = req.command
    handler.headers = req.headers
    handler.server = SimpleNamespace(store=store, state=state, runtime=None)
    handler.store = store
    handler.state = state
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{handler.command} {handler.path} HTTP/1.1"
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()

    status: dict[str, int | None] = {"code": None}
    handler.send_response = lambda code, _msg=None: status.__setitem__("code", code)
    handler.send_header = lambda *_a, **_k: None
    handler.end_headers = lambda: None
    return handler, status


def test_apishim_ha_routes_namespace_put_through_shared_authority(monkeypatch, tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "team-a"},
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/api/v1/namespaces/team-a",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "Namespace"
    assert payload["metadata"]["resourceVersion"] == "1"
    assert legacy.get("", "v1", "namespaces", None, "team-a") is None
    entry = state.get_authority_object("", "v1", "namespaces", None, "team-a")
    assert entry is not None


def test_apishim_ha_routes_role_through_shared_authority(monkeypatch, tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "reader", "namespace": "default"},
            "rules": [{"verbs": ["get", "list"], "resources": ["configmaps"]}],
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/rbac.authorization.k8s.io/v1/namespaces/default/roles/reader",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "Role"
    assert payload["rules"][0]["resources"] == ["configmaps"]
    assert legacy.get("rbac.authorization.k8s.io", "v1", "roles", "default", "reader") is None
    entry = state.get_authority_object(
        "rbac.authorization.k8s.io",
        "v1",
        "roles",
        "default",
        "reader",
    )
    assert entry is not None


def test_apishim_ha_routes_clusterrolebinding_through_shared_authority(
    monkeypatch,
    tmp_path,
) -> None:
    state, legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "bind-readers"},
            "subjects": [{"kind": "Group", "name": "readers"}],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "global-reader",
            },
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/bind-readers",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "ClusterRoleBinding"
    assert payload["roleRef"]["name"] == "global-reader"
    assert (
        legacy.get("rbac.authorization.k8s.io", "v1", "clusterrolebindings", None, "bind-readers")
        is None
    )
    entry = state.get_authority_object(
        "rbac.authorization.k8s.io",
        "v1",
        "clusterrolebindings",
        None,
        "bind-readers",
    )
    assert entry is not None


def test_apishim_ha_routes_pdb_through_shared_authority(monkeypatch, tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "policy/v1",
            "kind": "PodDisruptionBudget",
            "metadata": {"name": "web", "namespace": "default"},
            "spec": {
                "minAvailable": 1,
                "selector": {"matchLabels": {"app": "web"}},
            },
        }
    ).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/policy/v1/namespaces/default/poddisruptionbudgets/web",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "PodDisruptionBudget"
    assert payload["spec"]["minAvailable"] == 1
    assert legacy.get("policy", "v1", "poddisruptionbudgets", "default", "web") is None
    entry = state.get_authority_object("policy", "v1", "poddisruptionbudgets", "default", "web")
    assert entry is not None


def test_apishim_ha_clusterrole_conflict_returns_409(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "global-reader"},
            "rules": [{"verbs": ["get"], "resources": ["namespaces"]}],
        }
    ).encode("utf-8")
    handler, _status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/rbac.authorization.k8s.io/v1/clusterroles/global-reader",
        method="PUT",
        body=body,
    )
    handler.do_PUT()

    stale = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": "global-reader", "resourceVersion": "0"},
        "rules": [{"verbs": ["get", "list"], "resources": ["namespaces"]}],
    }
    conflict_handler, conflict_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/rbac.authorization.k8s.io/v1/clusterroles/global-reader",
        method="PUT",
        body=json.dumps(stale).encode("utf-8"),
    )

    conflict_handler.do_PUT()

    assert conflict_status["code"] == 409
    payload = _json_body(conflict_handler)
    assert payload["reason"] == "Conflict"
    assert "resourceVersion conflict" in payload["message"]


def test_apishim_ha_rbac_evaluation_uses_shared_authority_across_replicas(
    monkeypatch,
    tmp_path,
) -> None:
    state, _legacy_a, _legacy_b, store_a, store_b = _make_store_pair(tmp_path)

    role_body = json.dumps(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "cm-reader", "namespace": "default"},
            "rules": [{"verbs": ["list"], "resources": ["configmaps"]}],
        }
    ).encode("utf-8")
    role_handler, role_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/apis/rbac.authorization.k8s.io/v1/namespaces/default/roles/cm-reader",
        method="PUT",
        body=role_body,
    )
    role_handler.do_PUT()
    assert role_status["code"] == 200

    sa_body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "default", "namespace": "default"},
        }
    ).encode("utf-8")
    sa_handler, sa_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/serviceaccounts/default",
        method="PUT",
        body=sa_body,
    )
    sa_handler.do_PUT()
    assert sa_status["code"] == 200
    sa_get_handler, sa_get_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/serviceaccounts/default",
        method="GET",
    )
    sa_get_handler.do_GET()
    assert sa_get_status["code"] == 200
    sa_obj = store_a.get("", "v1", "serviceaccounts", "default", "default")
    assert sa_obj is not None
    token = ((sa_obj.metadata or {}).get("annotations") or {}).get("ae.apishim/token")
    assert token

    binding_body = json.dumps(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "bind-sa", "namespace": "default"},
            "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": "default"}],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "cm-reader",
            },
        }
    ).encode("utf-8")
    binding_handler, binding_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/apis/rbac.authorization.k8s.io/v1/namespaces/default/rolebindings/bind-sa",
        method="PUT",
        body=binding_body,
    )
    binding_handler.do_PUT()
    assert binding_status["code"] == 200

    monkeypatch.setenv("AE_APISHIM_RBAC", "1")
    monkeypatch.setenv("AE_APISHIM_RBAC_EVAL", "1")
    shim_server.ShimHandler.rbac_enabled = True
    shim_server.ShimHandler.rbac_eval_roles = True
    shim_server.ShimHandler.admin_token = "a"  # noqa: S105 - test token
    shim_server.ShimHandler.read_token = None
    shim_server.ShimHandler.allow_anonymous = False

    handler, status = _ha_custom_handler(
        store_b,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps",
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )

    handler.do_GET()

    assert status["code"] == 200


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
