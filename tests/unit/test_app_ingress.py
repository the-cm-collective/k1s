from __future__ import annotations

import copy

from ae.controller.app_ingress import sync_translated_app_ingress
from ae.controller.etcd_state import EtcdStateStore
from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata, ServiceSpec
from ae.controller.state import SQLiteStateStore


def _manifest(
    name: str,
    *,
    host: str | None = "demo.home.arpa",
    path: str = "/",
    port: int = 8080,
    namespace: str = "default",
    node_selector: dict[str, str] | None = None,
) -> AppManifest:
    spec = AppSpec(
        image="docker.io/library/demo-shell:latest",
        service=ServiceSpec(targetPort=port),
        nodeSelector=node_selector or {},
    )
    if host is not None:
        spec = spec.model_copy(update={"ingress": IngressSpec(host=host, path=path)})
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=name, namespace=namespace),
        spec=spec,
    )


def test_sync_translated_app_ingress_creates_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sea-edge-01")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})

    sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "sea-edge-01"
    assert route.spec["spec"]["host"] == "demo.home.arpa"
    assert route.spec["spec"]["paths"][0]["path"] == "/"
    assert route.spec["spec"]["paths"][0]["serviceRef"]["port"] == 8080
    assert route.spec["metadata"]["annotations"]["k1s.io/translated-from"] == "AppManifest"


def test_sync_translated_app_ingress_updates_existing_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sea-edge-02")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo", host="v1.home.arpa", path="/"), source="test", labels={})
    sync_translated_app_ingress(store, enabled=True)

    current = store.get_registered_entry("echo")
    assert current is not None
    store.register_app(
        _manifest("echo", host="v2.home.arpa", path="/healthz", port=9090),
        source="test",
        labels={},
        expected_resource_version=current.resource_version,
    )

    sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert route is not None
    assert route.spec["spec"]["host"] == "v2.home.arpa"
    assert route.spec["spec"]["paths"][0]["path"] == "/healthz"
    assert route.spec["spec"]["paths"][0]["serviceRef"]["port"] == 9090


def test_sync_translated_app_ingress_uses_service_port_not_target_port(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sea-edge-01")
    store = SQLiteStateStore(tmp_path / "state.db")
    manifest = _manifest("anchor", port=5678)
    manifest.spec.service = ServiceSpec(port=18086, targetPort=5678)
    store.register_app(manifest, source="test", labels={})

    sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="anchor-ingress", namespace="default")
    assert route is not None
    assert route.spec["spec"]["paths"][0]["serviceRef"]["port"] == 18086


def test_sync_translated_app_ingress_allows_annotation_mode_override(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sea-edge-01")
    store = SQLiteStateStore(tmp_path / "state.db")
    manifest = _manifest("anchor", port=5678)
    manifest.spec.service = ServiceSpec(port=18086, targetPort=5678)
    manifest.spec.ingress.annotations = {"k1s.io/edge-ingress-mode": "core-local"}
    store.register_app(manifest, source="test", labels={})

    sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="anchor-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "core"
    assert route.spec["spec"]["exposure"]["mode"] == "core-local"
    assert route.spec["spec"]["paths"][0]["serviceRef"]["port"] == 18086


def test_sync_translated_app_ingress_uses_node_selector_site_when_env_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.delenv("AE_EDGE_INGRESS_APP_SITE", raising=False)
    monkeypatch.delenv("AE_SITE_ID", raising=False)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(
        _manifest("echo", node_selector={"role": "worker", "site": "sea"}),
        source="test",
        labels={},
    )

    sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "sea"


def test_sync_translated_app_ingress_defaults_hub_selector_to_core_local(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.delenv("AE_EDGE_INGRESS_TRANSLATE_MODE", raising=False)
    monkeypatch.delenv("AE_EDGE_INGRESS_APP_SITE", raising=False)
    monkeypatch.delenv("AE_SITE_ID", raising=False)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(
        _manifest("echo", node_selector={"role": "hub", "site": "hub"}),
        source="test",
        labels={},
    )

    sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "core"
    assert route.spec["spec"]["exposure"]["mode"] == "core-local"
    assert "placement" not in route.spec["spec"]["exposure"]


def test_sync_translated_app_ingress_defaults_core_site_selector_to_core_local(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.delenv("AE_EDGE_INGRESS_TRANSLATE_MODE", raising=False)
    monkeypatch.delenv("AE_EDGE_INGRESS_APP_SITE", raising=False)
    monkeypatch.delenv("AE_SITE_ID", raising=False)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(
        _manifest("echo", node_selector={"role": "worker", "site": "core"}),
        source="test",
        labels={},
    )

    sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "core"
    assert route.spec["spec"]["exposure"]["mode"] == "core-local"
    assert "placement" not in route.spec["spec"]["exposure"]


def test_sync_translated_app_ingress_explicit_translate_mode_overrides_hub_default(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_TRANSLATE_MODE", "core-proxy")
    monkeypatch.delenv("AE_EDGE_INGRESS_APP_SITE", raising=False)
    monkeypatch.delenv("AE_SITE_ID", raising=False)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(
        _manifest("echo", node_selector={"role": "hub", "site": "hub"}),
        source="test",
        labels={},
    )

    sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "hub"
    assert route.spec["spec"]["exposure"]["mode"] == "core-proxy"
    assert route.spec["spec"]["exposure"]["placement"] == {"site": "hub"}


def test_sync_translated_app_ingress_deletes_route_when_ingress_removed(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})
    sync_translated_app_ingress(store, enabled=True)
    current = store.get_registered_entry("echo")
    assert current is not None

    store.register_app(
        _manifest("echo", host=None),
        source="test",
        labels={},
        expected_resource_version=current.resource_version,
    )
    sync_translated_app_ingress(store, enabled=True)

    assert store.get_edge_ingress_route(name="echo-ingress", namespace="default") is None


def test_sync_translated_app_ingress_deletes_route_when_app_removed(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})
    sync_translated_app_ingress(store, enabled=True)

    assert store.delete_registered_app("echo") is True
    sync_translated_app_ingress(store, enabled=True)

    assert store.get_edge_ingress_route(name="echo-ingress", namespace="default") is None


def test_sync_translated_app_ingress_preserves_explicit_route(tmp_path, caplog) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})
    explicit = {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressRoute",
        "metadata": {"name": "echo-ingress", "namespace": "default"},
        "spec": {
            "host": "explicit.home.arpa",
            "paths": [{"path": "/", "serviceRef": {"name": "explicit", "namespace": "default"}}],
            "exposure": {"mode": "core-local"},
        },
    }
    store.upsert_edge_ingress_route(
        name="echo-ingress",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document=explicit,
    )

    with caplog.at_level("WARNING"):
        sync_translated_app_ingress(store, enabled=True)

    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert route is not None
    assert route.spec["spec"]["host"] == "explicit.home.arpa"
    assert route.spec.get("metadata", {}).get("annotations") is None
    assert "explicit EdgeIngressRoute already exists" in caplog.text


def test_sync_translated_app_ingress_noops_when_disabled(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})

    sync_translated_app_ingress(store, enabled=False)

    assert store.get_edge_ingress_route(name="echo-ingress", namespace="default") is None


def test_etcd_delete_edge_ingress_route_round_trip() -> None:
    store = object.__new__(EtcdStateStore)
    store._prefix = "k1s/test"  # type: ignore[attr-defined]
    records: dict[str, dict] = {}

    def _get_json(key: str):
        rec = records.get(key)
        return (copy.deepcopy(rec) if rec is not None else None, 1 if rec is not None else 0)

    def _put_json(key: str, payload: dict, *, lease_id: int | None = None) -> None:  # noqa: ARG001
        records[key] = copy.deepcopy(payload)

    def _delete(key: str) -> None:
        records.pop(key, None)

    def _list_prefix(prefix: str):
        return [
            (key, copy.deepcopy(value), 1)
            for key, value in records.items()
            if key.startswith(prefix)
        ]

    store._get_json = _get_json  # type: ignore[method-assign]
    store._put_json = _put_json  # type: ignore[method-assign]
    store._delete = _delete  # type: ignore[method-assign]
    store._list_prefix = _list_prefix  # type: ignore[method-assign]

    store.upsert_edge_ingress_route(
        name="echo-ingress",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "echo-ingress", "namespace": "default"},
            "spec": {"host": "demo.home.arpa", "paths": [{"path": "/"}], "exposure": {"mode": "core-local"}},
        },
    )

    assert store.get_edge_ingress_route(name="echo-ingress", namespace="default") is not None
    assert store.delete_edge_ingress_route(name="echo-ingress", namespace="default") is True
    assert store.get_edge_ingress_route(name="echo-ingress", namespace="default") is None
    assert store.delete_edge_ingress_route(name="echo-ingress", namespace="default") is False
