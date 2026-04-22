import json
from types import SimpleNamespace

from ae.apishim import server as shim_server
from ae.apishim.ha_store import MultiplexApishimStore
from ae.apishim.store import ObjectStore
from ae.controller.state import SQLiteStateStore
from tests.unit.test_apishim_storage import _handler, _json_body


def _make_store_pair(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy_a = ObjectStore(tmp_path / "apishim-a.db")
    legacy_b = ObjectStore(tmp_path / "apishim-b.db")
    store_a = MultiplexApishimStore.from_state_and_legacy(state, legacy_a)
    store_b = MultiplexApishimStore.from_state_and_legacy(state, legacy_b)
    shim_server.ShimHandler.state = state
    shim_server.ShimHandler.crd_registry = {}
    shim_server.ShimHandler.crd_index = {}
    shim_server.ShimHandler._crd_refresh_monotonic = 0.0
    return state, legacy_a, legacy_b, store_a, store_b


def _ha_handler(store, state, monkeypatch, path: str, *, method: str, body: bytes = b""):
    monkeypatch.setenv("AE_HA_MODE", "1")
    handler, status = _handler(store, monkeypatch, path, method=method, body=body)
    handler.server = SimpleNamespace(store=store, state=state, runtime=None)
    handler.state = state
    return handler, status


def _crd_body() -> bytes:
    return json.dumps(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "apps.ae.dev"},
            "spec": {
                "group": "ae.dev",
                "scope": "Namespaced",
                "names": {"plural": "apps", "singular": "app", "kind": "Deployment"},
                "versions": [{"name": "v1alpha1", "served": True, "storage": True}],
            },
        }
    ).encode("utf-8")


def test_apishim_ha_crd_discovery_refreshes_across_replicas(monkeypatch, tmp_path) -> None:
    state, _legacy_a, _legacy_b, store_a, store_b = _make_store_pair(tmp_path)

    create_handler, create_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/apps.ae.dev",
        method="PUT",
        body=_crd_body(),
    )
    create_handler.do_PUT()
    assert create_status["code"] == 200

    group_handler, group_status = _ha_handler(store_b, state, monkeypatch, "/apis/ae.dev", method="GET")
    group_handler.do_GET()
    assert group_status["code"] == 200
    group_payload = _json_body(group_handler)
    versions = {item.get("version") for item in (group_payload.get("versions") or [])}
    assert "v1alpha1" in versions

    version_handler, version_status = _ha_handler(
        store_b,
        state,
        monkeypatch,
        "/apis/ae.dev/v1alpha1",
        method="GET",
    )
    version_handler.do_GET()
    assert version_status["code"] == 200
    version_payload = _json_body(version_handler)
    resources = {item.get("name") for item in (version_payload.get("resources") or [])}
    assert "apps" in resources


def test_apishim_ha_custom_resource_reads_across_replicas(monkeypatch, tmp_path) -> None:
    state, _legacy_a, legacy_b, store_a, store_b = _make_store_pair(tmp_path)

    create_crd_handler, create_crd_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/apps.ae.dev",
        method="PUT",
        body=_crd_body(),
    )
    create_crd_handler.do_PUT()
    assert create_crd_status["code"] == 200

    create_handler, create_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/apis/ae.dev/v1alpha1/namespaces/default/apps",
        method="POST",
        body=json.dumps(
            {
                "apiVersion": "ae.dev/v1alpha1",
                "kind": "Deployment",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {"image": "busybox"},
            }
        ).encode("utf-8"),
    )
    create_handler.do_POST()
    assert create_status["code"] == 201

    get_handler, get_status = _ha_handler(
        store_b,
        state,
        monkeypatch,
        "/apis/ae.dev/v1alpha1/namespaces/default/apps/demo",
        method="GET",
    )
    get_handler.do_GET()
    assert get_status["code"] == 200
    payload = _json_body(get_handler)
    assert payload["kind"] == "Deployment"
    assert payload["metadata"]["name"] == "demo"
    assert legacy_b.list_all("ae.dev", "v1alpha1", "apps") == []


def test_apishim_ha_custom_resource_conflict_returns_409(monkeypatch, tmp_path) -> None:
    state, _legacy_a, _legacy_b, store_a, store_b = _make_store_pair(tmp_path)

    crd_handler, crd_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/apps.ae.dev",
        method="PUT",
        body=_crd_body(),
    )
    crd_handler.do_PUT()
    assert crd_status["code"] == 200

    create_handler, create_status = _ha_handler(
        store_a,
        state,
        monkeypatch,
        "/apis/ae.dev/v1alpha1/namespaces/default/apps",
        method="POST",
        body=json.dumps(
            {
                "apiVersion": "ae.dev/v1alpha1",
                "kind": "Deployment",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {"image": "busybox"},
            }
        ).encode("utf-8"),
    )
    create_handler.do_POST()
    assert create_status["code"] == 201

    stale_handler, stale_status = _ha_handler(
        store_b,
        state,
        monkeypatch,
        "/apis/ae.dev/v1alpha1/namespaces/default/apps/demo",
        method="PUT",
        body=json.dumps(
            {
                "apiVersion": "ae.dev/v1alpha1",
                "kind": "Deployment",
                "metadata": {"name": "demo", "namespace": "default", "resourceVersion": "0"},
                "spec": {"image": "busybox:latest"},
            }
        ).encode("utf-8"),
    )
    stale_handler.do_PUT()

    assert stale_status["code"] == 409
    payload = _json_body(stale_handler)
    assert payload["reason"] == "Conflict"
    assert "resourceVersion conflict" in payload["message"]
