from datetime import UTC, datetime, timedelta

from ae.controller.scheduler import Scheduler
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import SQLiteStateStore


def _manifest(name: str = "app", replicas: int = 3) -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name=name),
        spec=AppSpec(image="busybox", replicas=replicas),
    )


def _store_with_nodes(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    return store


def test_scheduler_round_robin(tmp_path):
    store = _store_with_nodes(tmp_path)
    store.upsert_node(
        "n1",
        name="n1",
        labels={"zone": "a"},
        taints=[],
        backend="podman",
        endpoint="http://n1:9109",
    )
    store.upsert_node(
        "n2",
        name="n2",
        labels={"zone": "b"},
        taints=[],
        backend="podman",
        endpoint="http://n2:9109",
    )
    store.record_heartbeat("n1", "Ready")
    store.record_heartbeat("n2", "Ready")
    sched = Scheduler(store)
    placements, warnings = sched.plan(_manifest(replicas=3), revision=1)
    assert not warnings
    assert len(placements) == 2
    total = sum(len(p.replica_ids) for p in placements)
    assert total == 3
    # round-robin yields at least one replica per node when replicas >= nodes
    ids_by_node = {p.node.node_id: p.replica_ids for p in placements if p.node}
    assert "n1" in ids_by_node and "n2" in ids_by_node
    assert ids_by_node["n1"][0].startswith("app-rev1-")


def test_scheduler_storage_pins_single_node(tmp_path):
    store = _store_with_nodes(tmp_path)
    store.upsert_node(
        "n1", name="n1", labels={}, taints=[], backend="podman", endpoint="http://n1:9109"
    )
    store.upsert_node(
        "n2", name="n2", labels={}, taints=[], backend="podman", endpoint="http://n2:9109"
    )
    store.record_heartbeat("n1", "Ready")
    store.record_heartbeat("n2", "Ready")
    man = _manifest(replicas=2)
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={"storage": [{"name": "data", "mountPath": "/data"}]}
            )
        }
    )
    sched = Scheduler(store)
    placements, _warnings = sched.plan(man, revision=1)
    assert len(placements) == 1
    assert placements[0].node is not None
    assert len(placements[0].replica_ids) == 2


def test_scheduler_prefers_bound_storage_node(tmp_path):
    store = _store_with_nodes(tmp_path)
    store.upsert_node(
        "n1", name="n1", labels={}, taints=[], backend="podman", endpoint="http://n1:9109"
    )
    store.upsert_node(
        "n2", name="n2", labels={}, taints=[], backend="podman", endpoint="http://n2:9109"
    )
    store.record_heartbeat("n1", "Ready")
    store.record_heartbeat("n2", "Ready")
    store.upsert_volume_attachment("app", "data", "n2", retention="Retain")
    man = _manifest(replicas=1)
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={"storage": [{"name": "data", "mountPath": "/data"}]}
            )
        }
    )
    sched = Scheduler(store)
    placements, warnings = sched.plan(man, revision=1)
    assert not warnings
    assert len(placements) == 1
    assert placements[0].node is not None
    assert placements[0].node.node_id == "n2"


def test_scheduler_skips_when_bound_node_unavailable(tmp_path):
    store = _store_with_nodes(tmp_path)
    store.upsert_node(
        "n1", name="n1", labels={}, taints=[], backend="podman", endpoint="http://n1:9109"
    )
    store.upsert_node(
        "n2", name="n2", labels={}, taints=[], backend="podman", endpoint="http://n2:9109"
    )
    store.record_heartbeat("n1", "Ready")
    store.record_heartbeat("n2", "Ready")
    store.cordon_node("n2", True)
    store.upsert_volume_attachment("app", "data", "n2", retention="Retain")
    man = _manifest(replicas=1)
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={"storage": [{"name": "data", "mountPath": "/data"}]}
            )
        }
    )
    sched = Scheduler(store)
    placements, warnings = sched.plan(man, revision=1)
    assert placements == []
    assert warnings
    assert any("bound to node n2" in w for w in warnings)


def test_scheduler_filters_cordoned_and_stale(tmp_path):
    store = _store_with_nodes(tmp_path)
    store.upsert_node(
        "n1", name="n1", labels={}, taints=[], backend="podman", endpoint="http://n1:9109"
    )
    store.cordon_node("n1", True)
    store.upsert_node(
        "n2", name="n2", labels={}, taints=[], backend="podman", endpoint="http://n2:9109"
    )
    store.record_heartbeat("n2", "Ready")
    # stale node n3
    store.upsert_node(
        "n3", name="n3", labels={}, taints=[], backend="podman", endpoint="http://n3:9109"
    )
    store.record_heartbeat("n3", "Ready")
    # Manually mark stale by manipulating seen_at
    with store._connect() as conn:  # type: ignore[attr-defined]
        ts = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        conn.execute("UPDATE node_heartbeats SET seen_at=? WHERE node_id='n3'", (ts,))
        conn.commit()
    sched = Scheduler(store)
    man = _manifest(replicas=1)
    placements, warnings = sched.plan(man, revision=1)
    assert placements and placements[0].node and placements[0].node.node_id == "n2"
    assert warnings  # stale/cordoned nodes were skipped


def test_scheduler_spreads_by_topology_key(tmp_path):
    store = _store_with_nodes(tmp_path)
    store.upsert_node(
        "n1",
        name="n1",
        labels={"zone": "a"},
        taints=[],
        backend="podman",
        endpoint="http://n1:9109",
    )
    store.upsert_node(
        "n2",
        name="n2",
        labels={"zone": "b"},
        taints=[],
        backend="podman",
        endpoint="http://n2:9109",
    )
    store.record_heartbeat("n1", "Ready")
    store.record_heartbeat("n2", "Ready")
    man = _manifest(replicas=4)
    constraints = [{"topologyKey": "zone"}]
    man = man.model_copy(
        update={"spec": man.spec.model_copy(update={"topology_spread_constraints": constraints})}
    )
    sched = Scheduler(store)
    placements, warnings = sched.plan(man, revision=1)
    assert not warnings
    counts = {p.node.node_id: len(p.replica_ids) for p in placements if p.node}
    assert counts.get("n1") == 2
    assert counts.get("n2") == 2


def test_scheduler_spread_ignored_when_label_missing(tmp_path):
    store = _store_with_nodes(tmp_path)
    store.upsert_node(
        "n1",
        name="n1",
        labels={"zone": "a"},
        taints=[],
        backend="podman",
        endpoint="http://n1:9109",
    )
    store.upsert_node(
        "n2", name="n2", labels={}, taints=[], backend="podman", endpoint="http://n2:9109"
    )
    store.record_heartbeat("n1", "Ready")
    store.record_heartbeat("n2", "Ready")
    man = _manifest(replicas=3)
    constraints = [{"topologyKey": "zone"}]
    man = man.model_copy(
        update={"spec": man.spec.model_copy(update={"topology_spread_constraints": constraints})}
    )
    sched = Scheduler(store)
    placements, warnings = sched.plan(man, revision=1)
    assert len(placements) == 2
    total = sum(len(p.replica_ids) for p in placements)
    assert total == 3


# ruff: noqa: E501
