from datetime import UTC, datetime

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
        rp_pubkey="rppubkey",
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
    assert node.rp_pubkey == "rppubkey"
    assert isinstance(node.created_at, datetime)
    assert status is not None
    assert status.status == "Ready"
    assert status.seen_at.tzinfo == UTC


def test_list_nodes_returns_status(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_node(
        "n1",
        name=None,
        labels=None,
        taints=None,
        backend=None,
        endpoint=None,
        pod_cidr=None,
        wg_pubkey=None,
    )
    store.record_heartbeat("n1", "NotReady")

    items = store.list_nodes()
    assert len(items) == 1
    node, status = items[0]
    assert node.node_id == "n1"
    assert status is not None
    assert status.status == "NotReady"


def test_volume_attachments_roundtrip(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_volume_attachment("app", "data", "node-a", retention="Retain")
    attachments = store.list_volume_attachments("app")
    assert len(attachments) == 1
    att = attachments[0]
    assert att.app_name == "app"
    assert att.volume_name == "data"
    assert att.node_id == "node-a"
    assert att.retention == "Retain"
    store.delete_volume_attachments("app")
    assert store.list_volume_attachments("app") == []
