from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ae.controller.etcd_state import EtcdStateStore
from ae.controller.state import EdgeIngressRouteRecord, SQLiteStateStore, ServiceEndpoint
from ae.transport.route_bundle_publisher import RouteBundlePublisher
from ae.transport.subjects import hub_route_bundle_subject


def _edge_local_route_record(site_id: str) -> EdgeIngressRouteRecord:
    now = datetime.now(timezone.utc)
    return EdgeIngressRouteRecord(
        name="app-edge-local",
        namespace="default",
        site_id=site_id,
        policy_name=None,
        policy_namespace=None,
        spec={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-edge-local", "namespace": "default"},
            "spec": {
                "host": "app-edge-local.home.arpa",
                "paths": [
                    {
                        "path": "/",
                        "serviceRef": {"name": "app-svc", "namespace": "default", "port": 8080},
                    }
                ],
                "exposure": {"mode": "edge-local", "placement": {"site": site_id}},
            },
        },
        status=None,
        created_at=now,
        updated_at=now,
    )


def test_sqlite_route_bundle_site_ids_include_lease_and_route_sites(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    store.acquire_lease(
        site_id="lease-site",
        node_id="node-1",
        session_id="sess-1",
        lease_ttl_ms=60000,
        renew_after_ms=20000,
        controller_epoch=1,
    )
    store.upsert_edge_ingress_route(
        name="route-1",
        namespace="default",
        site_id="route-site",
        policy_name=None,
        policy_namespace=None,
        document={
            "host": "app-edge-local.home.arpa",
            "paths": [{"path": "/", "serviceRef": {"name": "app-svc", "namespace": "default", "port": 8080}}],
            "exposure": {"mode": "edge-local", "placement": {"site": "route-site"}},
        },
    )
    store.upsert_edge_ingress_route(
        name="route-empty-site",
        namespace="default",
        site_id="",
        policy_name=None,
        policy_namespace=None,
        document={
            "host": "ignored.home.arpa",
            "paths": [{"path": "/", "serviceRef": {"name": "app-svc", "namespace": "default", "port": 8080}}],
            "exposure": {"mode": "edge-local", "placement": {"site": ""}},
        },
    )

    assert store.list_route_bundle_site_ids() == ["lease-site", "route-site"]


def test_etcd_route_bundle_site_ids_include_routes_and_leases() -> None:
    store = object.__new__(EtcdStateStore)
    store.list_site_ids = lambda: ["lease-site"]  # type: ignore[method-assign]
    store.list_edge_ingress_routes = lambda: [  # type: ignore[method-assign]
        _edge_local_route_record("route-site"),
        _edge_local_route_record(""),
    ]

    assert store.list_route_bundle_site_ids() == ["lease-site", "route-site"]


def test_route_bundle_publisher_uses_route_bundle_site_ids(monkeypatch) -> None:
    monkeypatch.setenv("AE_CONTROLLER_ID", "ctrl-a")
    monkeypatch.setenv("AE_CONTROLLER_EPOCH", "9")

    class FakeNatsClient:
        def __init__(self, *args, **kwargs) -> None:
            self.subscriptions = []
            self.published = []

        def connect(self) -> None:
            return None

        def subscribe(self, subject, callback) -> None:
            self.subscriptions.append((subject, callback))

        def publish_json(self, subject, payload) -> None:
            self.published.append((subject, payload))

        def close(self) -> None:
            return None

    class FakeStore:
        def list_site_ids(self):
            raise AssertionError("publisher should not use lease-only site discovery")

        def list_route_bundle_site_ids(self):
            return ["sea-edge-02"]

        def list_edge_ingress_routes_for_site(self, site_id: str):
            assert site_id == "sea-edge-02"
            return [_edge_local_route_record(site_id)]

        def get_edge_ingress_policy(self, *, name: str, namespace: str):
            return None

        def list_service_endpoints(self, app_name: str):
            assert app_name == "app-svc"
            return [
                ServiceEndpoint(
                    app_name="app-svc",
                    port=18119,
                    ip="10.88.0.11",
                    target_port=8080,
                    ready=True,
                ),
                ServiceEndpoint(
                    app_name="app-svc",
                    port=18119,
                    ip="10.88.0.12",
                    target_port=8080,
                    ready=True,
                ),
            ]

    monkeypatch.setattr("ae.transport.route_bundle_publisher.NatsClient", FakeNatsClient)

    publisher = RouteBundlePublisher(FakeStore(), nats_url="nats://127.0.0.1:4222")
    publisher.run_once()

    assert publisher._client.published  # type: ignore[attr-defined]
    subject, payload = publisher._client.published[0]  # type: ignore[attr-defined]
    assert subject == hub_route_bundle_subject("sea-edge-02")
    assert payload.get("site_id") == "sea-edge-02"
    assert payload.get("controller_id") == "ctrl-a"
    assert payload.get("controller_epoch") == 9
    assert payload.get("operation_id") == "route:sea-edge-02:1:9"
    assert payload.get("routes")
    assert payload.get("service_endpoints") == {
        "default/app-svc": [
            {
                "ip": "10.88.0.11",
                "service_port": 18119,
                "target_port": 8080,
                "ready": True,
            },
            {
                "ip": "10.88.0.12",
                "service_port": 18119,
                "target_port": 8080,
                "ready": True,
            },
        ]
    }


def test_route_bundle_publisher_hash_changes_when_endpoints_change(monkeypatch) -> None:
    monkeypatch.setenv("AE_CONTROLLER_ID", "ctrl-a")
    monkeypatch.setenv("AE_CONTROLLER_EPOCH", "9")

    class FakeNatsClient:
        def __init__(self, *args, **kwargs) -> None:
            self.published = []

        def connect(self) -> None:
            return None

        def subscribe(self, subject, callback) -> None:
            return None

        def publish_json(self, subject, payload) -> None:
            self.published.append((subject, payload))

        def close(self) -> None:
            return None

    class FakeStore:
        endpoint_ip = "10.88.0.11"

        def list_route_bundle_site_ids(self):
            return ["sea-edge-02"]

        def list_edge_ingress_routes_for_site(self, site_id: str):
            assert site_id == "sea-edge-02"
            return [_edge_local_route_record(site_id)]

        def get_edge_ingress_policy(self, *, name: str, namespace: str):
            return None

        def list_service_endpoints(self, app_name: str):
            assert app_name == "app-svc"
            return [
                ServiceEndpoint(
                    app_name="app-svc",
                    port=18119,
                    ip=self.endpoint_ip,
                    target_port=8080,
                    ready=True,
                )
            ]

    monkeypatch.setattr("ae.transport.route_bundle_publisher.NatsClient", FakeNatsClient)

    store = FakeStore()
    publisher = RouteBundlePublisher(store, nats_url="nats://127.0.0.1:4222")
    publisher.run_once()

    state = publisher._state["sea-edge-02"]  # type: ignore[attr-defined]
    state.acked_rev = state.rev
    first_hash = state.hash
    first_rev = state.rev

    store.endpoint_ip = "10.88.0.21"
    publisher.run_once()

    state = publisher._state["sea-edge-02"]  # type: ignore[attr-defined]
    second_hash = state.hash
    second_rev = state.rev

    assert second_hash != first_hash
    assert second_rev == first_rev + 1
    assert len(publisher._client.published) == 2  # type: ignore[attr-defined]


def test_route_bundle_publish_reuses_operation_id_on_retry(monkeypatch) -> None:
    monkeypatch.setenv("AE_CONTROLLER_ID", "ctrl-a")
    monkeypatch.setenv("AE_CONTROLLER_EPOCH", "9")

    class FakeNatsClient:
        def __init__(self, *args, **kwargs) -> None:
            self.published = []

        def connect(self) -> None:
            return None

        def subscribe(self, subject, callback) -> None:
            return None

        def publish_json(self, subject, payload) -> None:
            self.published.append((subject, payload))

        def close(self) -> None:
            return None

    class FakeStore:
        def list_route_bundle_site_ids(self):
            return ["sea-edge-02"]

        def list_edge_ingress_routes_for_site(self, site_id: str):
            return [_edge_local_route_record(site_id)]

        def get_edge_ingress_policy(self, *, name: str, namespace: str):
            return None

        def list_service_endpoints(self, app_name: str):
            return []

    monkeypatch.setattr("ae.transport.route_bundle_publisher.NatsClient", FakeNatsClient)

    publisher = RouteBundlePublisher(FakeStore(), nats_url="nats://127.0.0.1:4222")
    publisher.run_once()
    publisher._state["sea-edge-02"].next_send_at = 0.0  # type: ignore[attr-defined]
    publisher.run_once()

    first = publisher._client.published[0][1]  # type: ignore[attr-defined]
    second = publisher._client.published[1][1]  # type: ignore[attr-defined]
    assert first["operation_id"] == "route:sea-edge-02:1:9"
    assert second["operation_id"] == first["operation_id"]
