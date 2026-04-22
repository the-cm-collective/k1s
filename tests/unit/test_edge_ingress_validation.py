from __future__ import annotations

from ae.controller.__main__ import _reconcile_edge_ingress
from ae.controller.state import SQLiteStateStore


def test_edge_ingress_reserved_controlplane_host_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AE_CONTROLPLANE_PUBLIC_ENABLE", "1")
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_edge_ingress_route(
        name="reserved-host",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "reserved-host", "namespace": "default"},
            "spec": {
                "host": "dash.home.arpa",
                "paths": [{"path": "/"}],
                "exposure": {"mode": "core-local"},
            },
        },
    )

    _reconcile_edge_ingress(store)

    route = store.get_edge_ingress_route(name="reserved-host", namespace="default")
    assert route is not None
    assert route.status is not None
    assert route.status["valid"] is False
    assert "reserved_control_plane_host" in route.status["errors"]
