from datetime import UTC, datetime

from ae.controller.state import SQLiteStateStore


def test_upsert_and_get_node(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_node(
        "node-1",
        name="worker1",
        labels={"role": "worker"},
        capabilities={
            "accelerators": [
                {
                    "id": "gpu-0",
                    "kind": "discrete_gpu",
                    "vendor": "nvidia",
                    "family": "RTX 8000",
                    "device_count": 1,
                    "memory_model": "dedicated",
                    "memory_bytes_per_device": 49152 * 1024 * 1024,
                    "runtime_handlers": ["nvidia"],
                    "partitioning_mode": "none",
                    "backing_device_id": None,
                    "execution_role": "execution",
                }
            ]
        },
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
    assert node.capabilities["accelerators"][0]["family"] == "RTX 8000"
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
        capabilities=None,
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


def test_node_capability_helpers_return_f1_facts(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_node(
        "node-a",
        labels={"site": "site-a"},
        capabilities={
            "networkInterfaces": [
                {
                    "name": "enp1s0",
                    "linkMetrics": [
                        {
                            "fromSite": "site-a",
                            "toSite": "site-b",
                            "rttP95Ms": 6.0,
                        }
                    ],
                }
            ]
        },
    )

    capabilities = store.get_node_capabilities("node-a")
    assert capabilities["network_interfaces"][0]["name"] == "enp1s0"
    assert store.list_node_capabilities()["node-a"]["network_interfaces"][0]["name"] == "enp1s0"
    assert store.list_fabric_link_metrics()[0]["from_site"] == "site-a"
    assert store.list_fabric_link_metrics()[0]["rtt_p95_ms"] == 6.0


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
