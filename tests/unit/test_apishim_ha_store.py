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
