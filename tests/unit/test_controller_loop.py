"""Controller daemon one-shot reconcile smoke test."""

from pathlib import Path
from types import SimpleNamespace

from ae.controller.__main__ import (
    _reconcile_manifest_after_api_apply,
    _reconcile_registry_apps_then_translated_ingress,
    _should_run_etcd_maintenance,
    _sync_translated_ingress_after_api_apply,
    main,
)
from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata, ServiceSpec
from ae.controller.state import SQLiteStateStore


def write_manifest(path: Path) -> None:
    write_named_manifest(path, "echo")


def write_named_manifest(path: Path, name: str) -> None:
    path.write_text(
        f"""
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: {name}
spec:
  image: alpine:3.20
  replicas: 1
        """.strip()
    )


def write_ingress_manifest(path: Path, name: str, host: str) -> None:
    path.write_text(
        f"""
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: {name}
spec:
  image: alpine:3.20
  replicas: 1
  ingress:
    host: {host}
    path: /
  service:
    targetPort: 8080
        """.strip()
    )


def build_manifest(name: str) -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=name),
        spec=AppSpec(image="alpine:3.20", replicas=1),
    )


class _FakeAuthority:
    def __init__(self, *, is_leader: bool) -> None:
        self._is_leader = is_leader
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def wait_until_ready(self, timeout=None) -> bool:
        return True

    def snapshot(self):
        return SimpleNamespace(is_leader=self._is_leader)

    def stop(self) -> None:
        self.stopped = True


class _FakeReport:
    def __init__(self, *, revision_status: str) -> None:
        self.app_name = "k1s-dev-anchor"
        self.revision = 1
        self.revision_status = revision_status
        self.created = 1
        self.updated = 0
        self.removed = 0


def test_should_run_etcd_maintenance_requires_leader_and_interval():
    assert (
        _should_run_etcd_maintenance(
            enabled=True,
            is_leader=True,
            now=1200.0,
            last_run=0.0,
            interval=900.0,
        )
        is True
    )
    assert (
        _should_run_etcd_maintenance(
            enabled=True,
            is_leader=False,
            now=1200.0,
            last_run=0.0,
            interval=900.0,
        )
        is False
    )
    assert (
        _should_run_etcd_maintenance(
            enabled=False,
            is_leader=True,
            now=1200.0,
            last_run=0.0,
            interval=900.0,
        )
        is False
    )
    assert (
        _should_run_etcd_maintenance(
            enabled=True,
            is_leader=True,
            now=1000.0,
            last_run=200.0,
            interval=900.0,
        )
        is False
    )


def test_registry_reconcile_runs_before_translated_ingress(monkeypatch):
    calls = []

    class Store:
        def list_registered_apps(self):
            calls.append("list")
            return ["entry"]

    def materialize(_store, entries):
        calls.append(("materialize", tuple(entries)))
        return ["manifest"]

    def reconcile_all(_reconciler, manifests, *, should_continue=None):
        calls.append(("reconcile", tuple(manifests), should_continue))

    monkeypatch.setattr("ae.controller.__main__.materialize_registry_manifests", materialize)
    monkeypatch.setattr("ae.controller.__main__._reconcile_all", reconcile_all)
    monkeypatch.setattr(
        "ae.controller.__main__.sync_translated_app_ingress",
        lambda _store: calls.append("sync"),
    )
    monkeypatch.setattr(
        "ae.controller.__main__._reconcile_edge_ingress",
        lambda _store, edge_renderer=None: calls.append(("edge", edge_renderer)),
    )

    entries = _reconcile_registry_apps_then_translated_ingress(
        Store(),
        object(),
        edge_renderer="renderer",
        should_continue="leader-check",
    )

    assert entries == ["entry"]
    assert calls == [
        "list",
        ("materialize", ("entry",)),
        ("reconcile", ("manifest",), "leader-check"),
        "sync",
        ("edge", "renderer"),
    ]


def test_api_apply_reconcile_helper_runs_until_ready(monkeypatch) -> None:
    reports = [_FakeReport(revision_status="progressing"), _FakeReport(revision_status="ready")]
    calls = []

    class Reconciler:
        def reconcile(self, manifest):
            calls.append(manifest.metadata.name)
            return reports.pop(0)

    monkeypatch.setenv("AE_APPLY_RECONCILE_BURST", "2")
    monkeypatch.setenv("AE_APPLY_RECONCILE_DELAY_MS", "1")
    manifest = build_manifest("k1s-dev-anchor")

    report = _reconcile_manifest_after_api_apply(Reconciler(), manifest)

    assert report.revision_status == "ready"
    assert calls == ["k1s-dev-anchor", "k1s-dev-anchor"]


def test_controller_once_reconciles(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_manifest(specs_dir / "echo.yaml")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    # Run once
    assert main(["--once", "--specs", str(specs_dir)]) == 0

    # Verify status persisted
    store = SQLiteStateStore(db_path)
    statuses = store.list_status()
    assert any(s.app_name == "echo" and s.ready_replicas == 1 for s in statuses)


def test_controller_once_translates_ingress_routes(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_ingress_manifest(specs_dir / "echo.yaml", "echo", "echo.home.arpa")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS", "1")
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sea-edge-01")

    assert main(["--once", "--specs", str(specs_dir)]) == 0

    store = SQLiteStateStore(db_path)
    route = store.get_edge_ingress_route(name="echo-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "sea-edge-01"
    assert route.spec["spec"]["host"] == "echo.home.arpa"


def test_controller_once_ha_standby_skips_specs_import(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_manifest(specs_dir / "echo.yaml")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    authority = _FakeAuthority(is_leader=False)
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: authority,
    )

    assert main(["--once", "--specs", str(specs_dir)]) == 0

    store = SQLiteStateStore(db_path)
    assert store.list_registered_apps() == []
    assert store.list_status() == []
    assert authority.started is True
    assert authority.stopped is True


def test_controller_once_ha_leader_uses_shared_registry_only(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_named_manifest(specs_dir / "local.yaml", "local-only")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    authority = _FakeAuthority(is_leader=True)
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: authority,
    )

    store = SQLiteStateStore(db_path)
    store.register_app(build_manifest("persisted"), source="test", labels={})

    assert main(["--once", "--specs", str(specs_dir)]) == 0

    store = SQLiteStateStore(db_path)
    statuses = store.list_status()
    assert any(s.app_name == "persisted" and s.ready_replicas == 1 for s in statuses)
    assert not any(s.app_name == "local-only" for s in statuses)
    assert store.list_registered_app_names() == ["persisted"]
    assert authority.started is True
    assert authority.stopped is True


def test_controller_once_ha_leader_translates_shared_registry_ingress(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_named_manifest(specs_dir / "local.yaml", "local-only")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS", "1")
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.setenv("AE_EDGE_INGRESS_APP_SITE", "sea-edge-01")
    authority = _FakeAuthority(is_leader=True)
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: authority,
    )

    store = SQLiteStateStore(db_path)
    store.register_app(
        AppManifest(
            apiVersion="ae.dev/v1alpha1",
            kind="Deployment",
            metadata=Metadata(name="persisted"),
            spec=AppSpec(
                image="alpine:3.20",
                replicas=1,
                ingress=IngressSpec(host="persisted.home.arpa", path="/"),
                service=ServiceSpec(targetPort=8080),
            ),
        ),
        source="test",
        labels={},
    )

    assert main(["--once", "--specs", str(specs_dir)]) == 0

    store = SQLiteStateStore(db_path)
    route = store.get_edge_ingress_route(name="persisted-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "sea-edge-01"
    assert route.spec["spec"]["host"] == "persisted.home.arpa"


def test_ha_api_apply_syncs_translated_app_ingress(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS", "1")

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="k1s-dev-anchor", namespace="default"),
        spec=AppSpec(
            image="hashicorp/http-echo:1.0",
            replicas=1,
            ingress=IngressSpec(
                host="demo.apps.k1s-dev-a.core.home.arpa",
                path="/",
                annotations={"k1s.io/edge-ingress-mode": "core-local"},
            ),
            service=ServiceSpec(port=18087, targetPort=5678),
        ),
    )
    store = SQLiteStateStore(db_path)
    store.register_app(manifest, source="api", labels={})

    warnings = _sync_translated_ingress_after_api_apply(store, manifest)

    route = store.get_edge_ingress_route(name="k1s-dev-anchor-ingress", namespace="default")
    assert warnings == []
    assert route is not None
    assert route.site_id == "core"
    assert route.spec["metadata"]["annotations"]["k1s.io/translated-from"] == "AppManifest"
    assert route.spec["spec"]["host"] == "demo.apps.k1s-dev-a.core.home.arpa"
    assert route.spec["spec"]["exposure"]["mode"] == "core-local"
    assert route.spec["spec"]["paths"][0]["serviceRef"]["port"] == 18087


def test_controller_once_ha_leader_translates_shared_registry_ingress_from_node_selector_site(
    tmp_path, monkeypatch
):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS", "1")
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.delenv("AE_EDGE_INGRESS_APP_SITE", raising=False)
    monkeypatch.delenv("AE_SITE_ID", raising=False)
    authority = _FakeAuthority(is_leader=True)
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: authority,
    )

    store = SQLiteStateStore(db_path)
    store.register_app(
        AppManifest(
            apiVersion="ae.dev/v1alpha1",
            kind="Deployment",
            metadata=Metadata(name="persisted"),
            spec=AppSpec(
                image="alpine:3.20",
                replicas=1,
                ingress=IngressSpec(host="persisted.home.arpa", path="/"),
                service=ServiceSpec(targetPort=8080),
                nodeSelector={"role": "worker", "site": "sea"},
            ),
        ),
        source="test",
        labels={},
    )

    assert main(["--once", "--specs", str(specs_dir)]) == 0

    store = SQLiteStateStore(db_path)
    route = store.get_edge_ingress_route(name="persisted-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "sea"


def test_controller_once_ha_leader_defaults_hub_ingress_to_core_local(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS", "1")
    monkeypatch.setenv("AE_EDGE_INGRESS_MODE", "core-proxy")
    monkeypatch.delenv("AE_EDGE_INGRESS_TRANSLATE_MODE", raising=False)
    monkeypatch.delenv("AE_EDGE_INGRESS_APP_SITE", raising=False)
    monkeypatch.delenv("AE_SITE_ID", raising=False)
    authority = _FakeAuthority(is_leader=True)
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: authority,
    )

    store = SQLiteStateStore(db_path)
    store.register_app(
        AppManifest(
            apiVersion="ae.dev/v1alpha1",
            kind="Deployment",
            metadata=Metadata(name="persisted"),
            spec=AppSpec(
                image="alpine:3.20",
                replicas=1,
                ingress=IngressSpec(host="persisted.home.arpa", path="/"),
                service=ServiceSpec(targetPort=8080),
                nodeSelector={"role": "hub", "site": "hub"},
            ),
        ),
        source="test",
        labels={},
    )

    assert main(["--once", "--specs", str(specs_dir)]) == 0

    store = SQLiteStateStore(db_path)
    route = store.get_edge_ingress_route(name="persisted-ingress", namespace="default")
    assert route is not None
    assert route.site_id == "core"
    assert route.spec["spec"]["exposure"]["mode"] == "core-local"
