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


def test_apishim_ha_service_rejects_unresolved_named_target_port(tmp_path) -> None:
    _state, _legacy, store = _make_store(tmp_path)
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

    try:
        store.upsert(
            "",
            "v1",
            "services",
            "default",
            "demo-svc",
            metadata={"name": "demo-svc", "namespace": "default"},
            spec={
                "selector": {"app": "demo"},
                "ports": [{"name": "web", "port": 80, "targetPort": "missing"}],
            },
            status={},
        )
    except AuthorityMutationError as exc:
        assert "does not match a named container port" in exc.message
    else:
        raise AssertionError("expected unresolved targetPort to be rejected")


def test_apishim_ha_service_unresolved_named_target_port_fallback_opt_in(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AE_APISHIM_ALLOW_UNRESOLVED_TARGETPORT_FALLBACK", "1")
    state, _legacy, store = _make_store(tmp_path)
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

    store.upsert(
        "",
        "v1",
        "services",
        "default",
        "demo-svc",
        metadata={"name": "demo-svc", "namespace": "default"},
        spec={
            "selector": {"app": "demo"},
            "ports": [{"name": "web", "port": 80, "targetPort": "missing"}],
        },
        status={},
    )

    entry = state.get_registered_entry("demo")
    assert entry is not None
    assert entry.manifest.spec.service is not None
    assert entry.manifest.spec.service.ports[0].target_port == 80


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


def test_generic_authority_store_round_trips_core_storage_resources(tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)

    storage_class = store.upsert(
        "storage.k8s.io",
        "v1",
        "storageclasses",
        None,
        "k1s-local",
        metadata={"name": "k1s-local"},
        spec={
            "provisioner": "k1s.io/local-path",
            "volumeBindingMode": "WaitForFirstConsumer",
        },
        status={},
    )
    assert storage_class.spec["provisioner"] == "k1s.io/local-path"
    assert legacy.get("storage.k8s.io", "v1", "storageclasses", None, "k1s-local") is None

    pvc = store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "demo-pvc",
        metadata={"name": "demo-pvc", "namespace": "default"},
        spec={
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "k1s-local",
            "resources": {"requests": {"storage": "1Gi"}},
        },
        status={"phase": "Pending"},
    )
    assert pvc.spec["storageClassName"] == "k1s-local"
    assert legacy.get("", "v1", "persistentvolumeclaims", "default", "demo-pvc") is None

    pv = store.upsert(
        "",
        "v1",
        "persistentvolumes",
        None,
        "demo-pv",
        metadata={"name": "demo-pv"},
        spec={
            "capacity": {"storage": "1Gi"},
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "k1s-local",
        },
        status={"phase": "Available"},
    )
    assert pv.spec["storageClassName"] == "k1s-local"
    assert legacy.get("", "v1", "persistentvolumes", None, "demo-pv") is None

    assert (
        state.get_authority_object("storage.k8s.io", "v1", "storageclasses", None, "k1s-local")
        is not None
    )
    assert (
        state.get_authority_object("", "v1", "persistentvolumeclaims", "default", "demo-pvc")
        is not None
    )
    assert state.get_authority_object("", "v1", "persistentvolumes", None, "demo-pv") is not None


def test_generic_authority_store_round_trips_snapshot_and_csi_resources(tmp_path) -> None:
    state, legacy, store = _make_store(tmp_path)

    driver = store.upsert(
        "storage.k8s.io",
        "v1",
        "csidrivers",
        None,
        "csi.example.com",
        metadata={"name": "csi.example.com"},
        spec={"attachRequired": True, "podInfoOnMount": False},
        status={},
    )
    assert driver.spec["attachRequired"] is True
    assert legacy.get("storage.k8s.io", "v1", "csidrivers", None, "csi.example.com") is None

    node = store.upsert(
        "storage.k8s.io",
        "v1",
        "csinodes",
        None,
        "node-a",
        metadata={"name": "node-a"},
        spec={"drivers": [{"name": "csi.example.com", "nodeID": "node-a"}]},
        status={},
    )
    assert node.spec["drivers"][0]["nodeID"] == "node-a"
    assert legacy.get("storage.k8s.io", "v1", "csinodes", None, "node-a") is None

    snapshot_class = store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshotclasses",
        None,
        "csi-snap",
        metadata={"name": "csi-snap"},
        spec={"driver": "csi.example.com", "deletionPolicy": "Retain"},
        status={},
    )
    assert snapshot_class.spec["driver"] == "csi.example.com"
    assert (
        legacy.get("snapshot.storage.k8s.io", "v1", "volumesnapshotclasses", None, "csi-snap")
        is None
    )

    snapshot = store.upsert(
        "snapshot.storage.k8s.io",
        "v1",
        "volumesnapshots",
        "default",
        "snap-a",
        metadata={"name": "snap-a", "namespace": "default"},
        spec={
            "source": {"persistentVolumeClaimName": "demo-pvc"},
            "volumeSnapshotClassName": "csi-snap",
        },
        status={"readyToUse": False},
    )
    assert snapshot.spec["volumeSnapshotClassName"] == "csi-snap"
    assert (
        legacy.get("snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "snap-a")
        is None
    )

    assert state.get_authority_object("storage.k8s.io", "v1", "csidrivers", None, "csi.example.com")
    assert state.get_authority_object("storage.k8s.io", "v1", "csinodes", None, "node-a")
    assert (
        state.get_authority_object(
            "snapshot.storage.k8s.io", "v1", "volumesnapshotclasses", None, "csi-snap"
        )
        is not None
    )
    assert (
        state.get_authority_object(
            "snapshot.storage.k8s.io", "v1", "volumesnapshots", "default", "snap-a"
        )
        is not None
    )


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


def test_workload_authority_watch_defaults_since_rv_to_zero(tmp_path) -> None:
    _state, _legacy, store = _make_store(tmp_path)
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

    gen = store.watch("apps", "v1", "deployments", "default")
    try:
        event_type, obj = next(gen)
    finally:
        gen.close()

    assert event_type == "ADDED"
    assert obj.name == "demo"


def test_generic_authority_watch_defaults_since_rv_to_zero(tmp_path) -> None:
    _state, _legacy, store = _make_store(tmp_path)
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "demo-pvc",
        metadata={"name": "demo-pvc", "namespace": "default"},
        spec={
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
        status={"phase": "Pending"},
    )

    gen = store.watch("", "v1", "persistentvolumeclaims", "default")
    try:
        event_type, obj = next(gen)
    finally:
        gen.close()

    assert event_type == "ADDED"
    assert obj.name == "demo-pvc"


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
