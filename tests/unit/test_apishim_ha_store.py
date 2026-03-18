from types import SimpleNamespace

from ae.apishim.ha_store import (
    AuthorityMutationError,
    MultiplexApishimStore,
    materialize_registry_manifests,
)
from ae.apishim.store import ObjectStore
from ae.controller.state import SQLiteStateStore


def _make_store(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    legacy = ObjectStore(tmp_path / "apishim.db")
    store = MultiplexApishimStore.from_state_and_legacy(state, legacy)
    return state, legacy, store


def _deployment_spec(name: str, *, labels: dict[str, str] | None = None) -> dict:
    workload_labels = dict(labels or {"app": name})
    return {
        "replicas": 1,
        "selector": {"matchLabels": workload_labels},
        "template": {
            "metadata": {"labels": workload_labels},
            "spec": {
                "containers": [
                    {
                        "name": name,
                        "image": "nginx:1.27",
                        "ports": [{"name": "http", "containerPort": 8080}],
                    }
                ]
            },
        },
    }


def test_workload_authority_store_round_trips_workload_and_attached_resources(tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)
    store.upsert(
        "apps",
        "v1",
        "deployments",
        "default",
        "demo",
        metadata={"name": "demo", "namespace": "default"},
        spec=_deployment_spec("demo"),
        status={},
    )

    workload = store.get("apps", "v1", "deployments", "default", "demo")
    assert workload is not None
    assert workload.spec["replicas"] == 1
    assert legacy.get("apps", "v1", "deployments", "default", "demo") is None

    service = store.upsert(
        "",
        "v1",
        "services",
        "default",
        "demo-svc",
        metadata={"name": "demo-svc", "namespace": "default"},
        spec={
            "selector": {"app": "demo"},
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
        status={},
    )
    assert service.name == "demo-svc"
    assert store.get("", "v1", "services", "default", "demo-svc") is not None

    ingress = store.upsert(
        "networking.k8s.io",
        "v1",
        "ingresses",
        "default",
        "demo-ing",
        metadata={"name": "demo-ing", "namespace": "default"},
        spec={
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
                                        "name": "demo-svc",
                                        "port": {"number": 80},
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        },
        status={},
    )
    assert ingress.name == "demo-ing"
    assert store.get("networking.k8s.io", "v1", "ingresses", "default", "demo-ing") is not None

    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.manifest.spec.service is not None
    assert entry.manifest.spec.ingress is not None


def test_apishim_ha_service_selector_must_be_unambiguous(tmp_path) -> None:
    _state, _legacy, store = _make_store(tmp_path)
    shared_labels = {"app": "shared"}
    store.upsert(
        "apps",
        "v1",
        "deployments",
        "default",
        "api-a",
        metadata={"name": "api-a", "namespace": "default", "labels": shared_labels},
        spec=_deployment_spec("api-a", labels=shared_labels),
        status={},
    )
    store.upsert(
        "apps",
        "v1",
        "deployments",
        "default",
        "api-b",
        metadata={"name": "api-b", "namespace": "default", "labels": shared_labels},
        spec=_deployment_spec("api-b", labels=shared_labels),
        status={},
    )

    try:
        store.upsert(
            "",
            "v1",
            "services",
            "default",
            "shared-svc",
            metadata={"name": "shared-svc", "namespace": "default"},
            spec={
                "selector": shared_labels,
                "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
            },
            status={},
        )
    except AuthorityMutationError as exc:
        assert "unambiguously" in exc.message
    else:
        raise AssertionError("expected ambiguous service selector to be rejected")


def test_materialize_registry_manifests_expands_daemonset_replicas(tmp_path) -> None:
    state, _legacy, store = _make_store(tmp_path)
    store.upsert(
        "apps",
        "v1",
        "daemonsets",
        "default",
        "agent",
        metadata={"name": "agent", "namespace": "default"},
        spec={
            "selector": {"matchLabels": {"app": "agent"}},
            "template": {
                "metadata": {"labels": {"app": "agent"}},
                "spec": {"containers": [{"name": "agent", "image": "busybox"}]},
            },
        },
        status={},
    )
    entry = state.get_registered_entry("agent")
    assert entry is not None

    class FakeNodeState:
        def list_nodes(self):
            return [
                (SimpleNamespace(node_id="n1"), None),
                (SimpleNamespace(node_id="n2"), None),
                (SimpleNamespace(node_id="n3"), None),
            ]

    manifests = materialize_registry_manifests(FakeNodeState(), [entry])
    assert len(manifests) == 1
    assert manifests[0].spec.replicas == 3


def test_generic_authority_store_round_trips_passive_resources(tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)

    configmap = store.upsert(
        "",
        "v1",
        "configmaps",
        "default",
        "demo-config",
        metadata={"name": "demo-config", "namespace": "default"},
        spec={"app.yaml": "hello: world"},
        status={},
    )
    assert configmap.spec["app.yaml"] == "hello: world"
    assert legacy.get("", "v1", "configmaps", "default", "demo-config") is None

    service_account = store.upsert(
        "",
        "v1",
        "serviceaccounts",
        "default",
        "demo-sa",
        metadata={"name": "demo-sa", "namespace": "default"},
        spec={"imagePullSecrets": [{"name": "regcred"}]},
        status={},
    )
    assert service_account.spec["imagePullSecrets"][0]["name"] == "regcred"
    assert legacy.get("", "v1", "serviceaccounts", "default", "demo-sa") is None

    cronjob = store.upsert(
        "batch",
        "v1",
        "cronjobs",
        "default",
        "demo-cron",
        metadata={"name": "demo-cron", "namespace": "default"},
        spec={
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
        status={"lastScheduleTime": "2026-03-17T00:00:00Z"},
    )
    assert cronjob.status["lastScheduleTime"] == "2026-03-17T00:00:00Z"
    assert legacy.get("batch", "v1", "cronjobs", "default", "demo-cron") is None

    config_entry = state.get_authority_object("", "v1", "configmaps", "default", "demo-config")
    assert config_entry is not None
    assert config_entry.spec["app.yaml"] == "hello: world"

    sa_entry = state.get_authority_object("", "v1", "serviceaccounts", "default", "demo-sa")
    assert sa_entry is not None
    assert sa_entry.spec["imagePullSecrets"][0]["name"] == "regcred"

    cron_entry = state.get_authority_object("batch", "v1", "cronjobs", "default", "demo-cron")
    assert cron_entry is not None
    assert cron_entry.status["lastScheduleTime"] == "2026-03-17T00:00:00Z"


def test_generic_authority_store_round_trips_built_in_passive_resources(tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)

    namespace = store.upsert(
        "",
        "v1",
        "namespaces",
        None,
        "team-a",
        metadata={"name": "team-a"},
        spec={},
        status={},
    )
    assert namespace.name == "team-a"
    assert legacy.get("", "v1", "namespaces", None, "team-a") is None

    role = store.upsert(
        "rbac.authorization.k8s.io",
        "v1",
        "roles",
        "team-a",
        "reader",
        metadata={"name": "reader", "namespace": "team-a"},
        spec={"rules": [{"verbs": ["get", "list"], "resources": ["configmaps"]}]},
        status={},
    )
    assert role.name == "reader"
    assert legacy.get("rbac.authorization.k8s.io", "v1", "roles", "team-a", "reader") is None

    cluster_role = store.upsert(
        "rbac.authorization.k8s.io",
        "v1",
        "clusterroles",
        None,
        "global-reader",
        metadata={"name": "global-reader"},
        spec={"rules": [{"verbs": ["get"], "resources": ["namespaces"]}]},
        status={},
    )
    assert cluster_role.name == "global-reader"
    assert (
        legacy.get("rbac.authorization.k8s.io", "v1", "clusterroles", None, "global-reader")
        is None
    )

    pdb = store.upsert(
        "policy",
        "v1",
        "poddisruptionbudgets",
        "team-a",
        "web",
        metadata={"name": "web", "namespace": "team-a"},
        spec={
            "minAvailable": 1,
            "selector": {"matchLabels": {"app": "web"}},
        },
        status={},
    )
    assert pdb.name == "web"
    assert legacy.get("policy", "v1", "poddisruptionbudgets", "team-a", "web") is None

    ns_entry = state.get_authority_object("", "v1", "namespaces", None, "team-a")
    assert ns_entry is not None
    role_entry = state.get_authority_object(
        "rbac.authorization.k8s.io",
        "v1",
        "roles",
        "team-a",
        "reader",
    )
    assert role_entry is not None
    cluster_role_entry = state.get_authority_object(
        "rbac.authorization.k8s.io",
        "v1",
        "clusterroles",
        None,
        "global-reader",
    )
    assert cluster_role_entry is not None
    pdb_entry = state.get_authority_object("policy", "v1", "poddisruptionbudgets", "team-a", "web")
    assert pdb_entry is not None


def test_generic_authority_store_routes_crds_and_dynamic_custom_resources(tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)

    crd = store.upsert(
        "apiextensions.k8s.io",
        "v1",
        "customresourcedefinitions",
        None,
        "widgets.example.io",
        metadata={"name": "widgets.example.io"},
        spec={
            "group": "example.io",
            "scope": "Namespaced",
            "names": {
                "plural": "widgets",
                "singular": "widget",
                "kind": "Widget",
                "shortNames": ["wdg"],
            },
            "versions": [{"name": "v1", "served": True, "storage": True}],
        },
        status={},
    )
    assert crd.name == "widgets.example.io"
    assert (
        legacy.get(
            "apiextensions.k8s.io",
            "v1",
            "customresourcedefinitions",
            None,
            "widgets.example.io",
        )
        is None
    )

    custom = store.upsert(
        "example.io",
        "v1",
        "widgets",
        "default",
        "blue",
        metadata={"name": "blue", "namespace": "default"},
        spec={"size": "large"},
        status={"phase": "Ready"},
    )
    assert custom.name == "blue"
    assert custom.resource == "widgets"
    assert custom.status["phase"] == "Ready"
    assert legacy.get("example.io", "v1", "widgets", "default", "blue") is None

    crd_entry = state.get_authority_object(
        "apiextensions.k8s.io",
        "v1",
        "customresourcedefinitions",
        None,
        "widgets.example.io",
    )
    assert crd_entry is not None
    custom_entry = state.get_authority_object("example.io", "v1", "widgets", "default", "blue")
    assert custom_entry is not None
    assert custom_entry.kind == "Widget"
    assert custom_entry.spec["size"] == "large"
