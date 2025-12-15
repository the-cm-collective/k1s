from datetime import datetime, timezone

from ae.controller.state import SQLiteStateStore


def test_upsert_and_get_node(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_node(
        "node-1",
        name="worker1",
        labels={"role": "worker"},
        taints=[{"key": "dedicated", "effect": "NoSchedule"}],
        backend="podman",
        endpoint="127.0.0.1:9000",
        pod_cidr="10.42.0.0/24",
        wg_pubkey="pubkey",
    )
    store.record_heartbeat("node-1", "Ready")

    res = store.get_node("node-1")
    assert res is not None
    node, status = res
    assert node.node_id == "node-1"
    assert node.name == "worker1"
    assert node.labels["role"] == "worker"
    assert node.backend == "podman"
    assert node.endpoint == "127.0.0.1:9000"
    assert node.pod_cidr == "10.42.0.0/24"
    assert node.wg_pubkey == "pubkey"
    assert isinstance(node.created_at, datetime)
    assert status is not None
    assert status.status == "Ready"
    assert status.seen_at.tzinfo == timezone.utc


def test_list_nodes_returns_status(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_node("n1", name=None, labels=None, taints=None, backend=None, endpoint=None, pod_cidr=None, wg_pubkey=None)
    store.record_heartbeat("n1", "NotReady")

    items = store.list_nodes()
    assert len(items) == 1
    node, status = items[0]
    assert node.node_id == "n1"
    assert status is not None
    assert status.status == "NotReady"


def test_storage_bindings_roundtrip(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_storage_binding("app", "data", "node-a", retention="Retain")
    bindings = store.list_storage_bindings("app")
    assert len(bindings) == 1
    b = bindings[0]
    assert b.app_name == "app"
    assert b.volume_name == "data"
    assert b.node_id == "node-a"
    assert b.retention == "Retain"
    store.delete_storage_bindings("app")
    assert store.list_storage_bindings("app") == []
