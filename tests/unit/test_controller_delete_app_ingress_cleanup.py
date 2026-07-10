from __future__ import annotations

from ae.controller.__main__ import _delete_app_and_cleanup_translated_ingress, _reconcile_all
from ae.controller.app_ingress import sync_translated_app_ingress
from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata, ServiceSpec
from ae.controller.state import SQLiteStateStore


class _Runtime:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def remove_app(self, app_name: str) -> int:
        self.removed.append(app_name)
        return 1


class _Ingress:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.reloads = 0

    def remove(self, app_name: str) -> bool:
        self.removed.append(app_name)
        return True

    def reload(self) -> None:
        self.reloads += 1


class _Reconciler:
    def __init__(self) -> None:
        self._runtime = _Runtime()
        self._ingress_service = _Ingress()

    def remove_app_across_runtimes(self, app_name: str) -> int:
        return self._runtime.remove_app(app_name)


class _MultiRuntimeReconciler(_Reconciler):
    def __init__(self) -> None:
        super().__init__()
        self.remote_runtime = _Runtime()

    def remove_app_across_runtimes(self, app_name: str) -> int:
        return self._runtime.remove_app(app_name) + self.remote_runtime.remove_app(app_name)


class _Renderer:
    def __init__(self) -> None:
        self.calls = 0

    def render(self) -> None:
        self.calls += 1


class _SweepReconciler:
    def __init__(self, store: SQLiteStateStore) -> None:
        self._state_store = store
        self.reconciled: list[str] = []

    def reconcile(self, manifest: AppManifest):  # noqa: ANN201
        self.reconciled.append(manifest.metadata.name)
        raise AssertionError("stale registry manifest should not be reconciled")


def _manifest(name: str = "echo") -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=name, namespace="default"),
        spec=AppSpec(
            image="docker.io/library/demo-shell:latest",
            service=ServiceSpec(targetPort=8080),
            ingress=IngressSpec(host=f"{name}.apps.home.arpa", path="/"),
        ),
    )


def test_delete_app_removes_registered_manifest_and_translated_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sfo-edge-01")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})
    sync_translated_app_ingress(store, enabled=True)
    assert store.get_registered_manifest("echo") is not None
    assert store.get_edge_ingress_route(name="echo-ingress", namespace="default") is not None
    reconciler = _Reconciler()
    renderer = _Renderer()

    result = _delete_app_and_cleanup_translated_ingress(
        store,
        reconciler,
        "echo",
        True,
        edge_renderer=renderer,
    )

    assert result["translated_route_removed"] is True
    assert result["purged"] is True
    assert result["removed"] == 1
    assert store.get_registered_manifest("echo") is None
    assert store.list_revisions("echo") == []
    assert store.get_edge_ingress_route(name="echo-ingress", namespace="default") is None
    assert reconciler._runtime.removed == ["echo"]
    assert reconciler._ingress_service.removed == ["echo"]
    assert reconciler._ingress_service.reloads == 1
    assert renderer.calls == 1


def test_delete_app_preserves_explicit_edge_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sfo-edge-01")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})
    explicit = {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressRoute",
        "metadata": {"name": "echo-ingress", "namespace": "default"},
        "spec": {
            "host": "explicit.apps.home.arpa",
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
    reconciler = _Reconciler()
    renderer = _Renderer()

    result = _delete_app_and_cleanup_translated_ingress(
        store,
        reconciler,
        "echo",
        True,
        edge_renderer=renderer,
    )

    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert result["translated_route_removed"] is False
    assert store.get_registered_manifest("echo") is None
    assert route is not None
    assert route.spec["spec"]["host"] == "explicit.apps.home.arpa"
    assert renderer.calls == 0


def test_delete_app_removes_remote_node_runtime_replicas(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sfo-edge-01")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})
    sync_translated_app_ingress(store, enabled=True)
    reconciler = _MultiRuntimeReconciler()
    renderer = _Renderer()

    result = _delete_app_and_cleanup_translated_ingress(
        store,
        reconciler,
        "echo",
        True,
        edge_renderer=renderer,
    )

    assert result["removed"] == 2
    assert reconciler._runtime.removed == ["echo"]
    assert reconciler.remote_runtime.removed == ["echo"]


def test_reconcile_sweep_skips_manifest_deleted_after_snapshot(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    manifest = _manifest("echo")
    store.register_app(manifest, source="test", labels={})
    stale_snapshot = [manifest]
    assert store.delete_registered_app("echo") is True
    store.delete_app_state("echo", purge_history=True)

    reconciler = _SweepReconciler(store)

    _reconcile_all(reconciler, stale_snapshot)

    assert reconciler.reconciled == []


def test_reconcile_sweep_skips_manifest_changed_after_snapshot(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    old_manifest = _manifest("echo")
    store.register_app(old_manifest, source="test", labels={})
    current = store.get_registered_entry("echo")
    assert current is not None
    new_manifest = _manifest("echo")
    new_manifest = new_manifest.model_copy(
        update={"spec": new_manifest.spec.model_copy(update={"image": "docker.io/library/other:latest"})}
    )
    store.register_app(
        new_manifest,
        source="test",
        labels={},
        expected_resource_version=current.resource_version,
    )

    reconciler = _SweepReconciler(store)

    _reconcile_all(reconciler, [old_manifest])

    assert reconciler.reconciled == []
