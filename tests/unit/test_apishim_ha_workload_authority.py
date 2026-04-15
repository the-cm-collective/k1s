import json
from types import SimpleNamespace

from ae.apishim.ha_store import (
    INGRESS_NAME_LABEL,
    SERVICE_NAME_LABEL,
    WORKLOAD_KIND_LABEL,
    MultiplexApishimStore,
)
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


def _deployment_doc(
    name: str,
    *,
    labels: dict[str, str] | None = None,
    replicas: int = 1,
    image: str = "nginx:1.27",
) -> dict:
    workload_labels = dict(labels or {"app": name})
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": "default",
            "labels": workload_labels,
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": workload_labels},
            "template": {
                "metadata": {"labels": workload_labels},
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "ports": [{"name": "http", "containerPort": 8080}],
                        }
                    ]
                },
            },
        },
    }


def _service_doc(name: str, selector: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "selector": selector,
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
    }


def _ingress_doc(name: str, service_name: str) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "rules": [
                {
                    "host": "demo.example.com",
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": service_name,
                                        "port": {"number": 80},
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        },
    }


def test_apishim_ha_routes_workload_put_through_controller_authority(
    monkeypatch, tmp_path
) -> None:
    state, legacy, store = _make_store(tmp_path)
    body = json.dumps(_deployment_doc("demo", replicas=2)).encode("utf-8")
    handler, status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/deployments/demo",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 200
    payload = _json_body(handler)
    assert payload["kind"] == "Deployment"
    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.source == "apishim"
    assert entry.labels[WORKLOAD_KIND_LABEL] == "deployment"
    assert legacy.get("apps", "v1", "deployments", "default", "demo") is None

    read_handler, read_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/deployments/demo",
        method="GET",
    )
    read_handler.do_GET()
    assert read_status["code"] == 200
    read_payload = _json_body(read_handler)
    assert read_payload["metadata"]["resourceVersion"] == "1"
    assert read_payload["spec"]["replicas"] == 2


def test_apishim_ha_deployment_conflict_returns_409(monkeypatch, tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    body = json.dumps(_deployment_doc("demo")).encode("utf-8")
    handler, _status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/deployments/demo",
        method="PUT",
        body=body,
    )
    handler.do_PUT()

    stale_doc = _deployment_doc("demo", replicas=3)
    stale_doc["metadata"]["resourceVersion"] = "0"
    conflict_handler, conflict_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/deployments/demo",
        method="PUT",
        body=json.dumps(stale_doc).encode("utf-8"),
    )

    conflict_handler.do_PUT()

    assert conflict_status["code"] == 409
    payload = _json_body(conflict_handler)
    assert payload["reason"] == "Conflict"
    assert "resourceVersion conflict" in payload["message"]


def test_apishim_ha_deployment_scale_updates_controller_authority(
    monkeypatch, tmp_path
) -> None:
    state, _legacy, store = _make_store(tmp_path)
    create_handler, _create_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/deployments/demo",
        method="PUT",
        body=json.dumps(_deployment_doc("demo")).encode("utf-8"),
    )
    create_handler.do_PUT()

    scale_handler, scale_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/deployments/demo/scale",
        method="PUT",
        body=json.dumps(
            {
                "apiVersion": "autoscaling/v1",
                "kind": "Scale",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {"replicas": 4},
            }
        ).encode("utf-8"),
    )

    scale_handler.do_PUT()

    assert scale_status["code"] == 200
    payload = _json_body(scale_handler)
    assert payload["kind"] == "Scale"
    assert payload["spec"]["replicas"] == 4
    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.manifest.spec.replicas == 4


def test_apishim_ha_service_and_ingress_attach_and_delete_with_workload(
    monkeypatch, tmp_path
) -> None:
    state, _legacy, store = _make_store(tmp_path)
    deploy_handler, _deploy_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/deployments/demo",
        method="PUT",
        body=json.dumps(_deployment_doc("demo")).encode("utf-8"),
    )
    deploy_handler.do_PUT()

    service_handler, service_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/services/demo-svc",
        method="PUT",
        body=json.dumps(_service_doc("demo-svc", {"app": "demo"})).encode("utf-8"),
    )
    service_handler.do_PUT()
    assert service_status["code"] == 200

    ingress_handler, ingress_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/networking.k8s.io/v1/namespaces/default/ingresses/demo-ing",
        method="PUT",
        body=json.dumps(_ingress_doc("demo-ing", "demo-svc")).encode("utf-8"),
    )
    ingress_handler.do_PUT()
    assert ingress_status["code"] == 200

    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.manifest.spec.service is not None
    assert entry.manifest.spec.ingress is not None
    assert entry.labels[SERVICE_NAME_LABEL] == "demo-svc"
    assert entry.labels[INGRESS_NAME_LABEL] == "demo-ing"

    delete_ingress, ingress_delete_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/apis/networking.k8s.io/v1/namespaces/default/ingresses/demo-ing",
        method="DELETE",
    )
    delete_ingress.do_DELETE()
    assert ingress_delete_status["code"] == 200

    delete_service, service_delete_status = _ha_handler(
        store,
        state,
        monkeypatch,
        "/api/v1/namespaces/default/services/demo-svc",
        method="DELETE",
    )
    delete_service.do_DELETE()
    assert service_delete_status["code"] == 200

    updated = state.get_registered_entry("demo")
    assert updated is not None
    assert updated.manifest.spec.service is None
    assert updated.manifest.spec.ingress is None
