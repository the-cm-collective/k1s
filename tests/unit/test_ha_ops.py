from __future__ import annotations

from pathlib import Path

from ae.ha.ops import (
    collect_prometheus_metric_values,
    collect_site_gateway_status,
    EdgeGatewayStatusRecord,
    EtcdRestoreMemberSpec,
    NatsEdgeSiteTarget,
    NatsHubNodeTarget,
    build_nats_edge_monitor_record,
    build_nats_hub_monitor_record,
    build_container_etcdctl_command,
    build_local_etcdctl_command,
    build_local_etcdctl_recovery_command,
    build_quorum_restore_plan,
    derive_client_url,
    evaluate_nats_edge_site,
    evaluate_nats_hub_cluster,
    format_quorum_restore_plan,
    ha_core_missing_env,
    leader_key,
    parse_etcd_leader_response,
    parse_etcd_member_add_output,
    parse_ha_core_node_target,
    parse_nats_edge_site_target,
    parse_nats_hub_node_target,
    parse_nats_url,
    parse_prometheus_metric_value,
    split_csv,
)


def test_ha_core_missing_env_reports_required_keys() -> None:
    missing = ha_core_missing_env(
        {
            "AE_CONTROLLER_ID": "core-a",
            "AE_ETCD_ENDPOINTS": "http://127.0.0.1:2379",
        }
    )

    assert missing == [
        "AE_CONTROLLER_ADVERTISE_ADDR",
        "AE_ETCD_PREFIX",
        "AE_NATS_URL",
    ]


def test_split_csv_ignores_empty_entries() -> None:
    assert split_csv(" a, ,b ,, c ") == ["a", "b", "c"]


def test_parse_nats_url_defaults_port() -> None:
    assert parse_nats_url("nats://gateway:dev@127.0.0.1") == ("127.0.0.1", 4222)


def test_leader_key_appends_controlplane_suffix() -> None:
    assert leader_key("k1s/profiles/k1s-ha-core/") == "k1s/profiles/k1s-ha-core/controlplane/leader"


def test_parse_etcd_leader_response_uses_mod_revision_for_epoch() -> None:
    payload = {
        "kvs": [
            {
                "value": "eyJhZHZlcnRpc2VfYWRkciI6Imh0dHA6Ly9jb3JlLWE6OTAwMCIsImNvbnRyb2xsZXJfaWQiOiJjb3JlLWEiLCJsZWFzZV9pZCI6NTAxfQ==",
                "mod_revision": 42,
            }
        ]
    }

    record = parse_etcd_leader_response(payload)

    assert record is not None
    assert record.controller_id == "core-a"
    assert record.controller_epoch == 42
    assert record.advertise_addr == "http://core-a:9000"
    assert record.lease_id == 501


def test_parse_prometheus_metric_value_matches_labels() -> None:
    text = """
# HELP sample help
ae_gateway_result_replay_backlog{site="sea"} 3
ae_gateway_result_replay_backlog{site="sfo"} 7
"""

    value = parse_prometheus_metric_value(
        text,
        "ae_gateway_result_replay_backlog",
        labels={"site": "sfo"},
    )

    assert value == 7.0


def test_collect_prometheus_metric_values_returns_all_samples() -> None:
    text = """
# HELP sample help
ae_site_stale{site="sea"} 0
ae_site_stale{site="sfo"} 1
"""

    values = collect_prometheus_metric_values(text, "ae_site_stale")

    assert values == [
        ({"site": "sea"}, 0.0),
        ({"site": "sfo"}, 1.0),
    ]


def test_parse_ha_core_node_target_requires_controller_and_apishim_urls() -> None:
    node = parse_ha_core_node_target("core-a=http://core-a:9108,https://core-a:8445")

    assert node.name == "core-a"
    assert node.controller_url == "http://core-a:9108"
    assert node.apishim_url == "https://core-a:8445"


def test_parse_nats_hub_node_target_requires_monitor_url() -> None:
    node = parse_nats_hub_node_target("hub-a=http://hub-a:8222")

    assert node.name == "hub-a"
    assert node.monitor_url == "http://hub-a:8222"


def test_parse_nats_edge_site_target_requires_monitor_url() -> None:
    site = parse_nats_edge_site_target("sea=http://edge-sea:8224")

    assert site.site_id == "sea"
    assert site.monitor_url == "http://edge-sea:8224"


def test_build_nats_hub_monitor_record_extracts_cluster_and_replica_state() -> None:
    record = build_nats_hub_monitor_record(
        NatsHubNodeTarget(name="hub-a", monitor_url="http://hub-a:8222"),
        varz={
            "server_name": "hub-a",
            "server_id": "srv-a",
            "version": "2.10.18",
            "git_commit": "sha-a",
            "cluster": {"name": "k1s-hub"},
            "jetstream": {"config": {"domain": "K1S"}},
        },
        routez={
            "num_routes": 2,
            "routes": [
                {"remote_name": "hub-b"},
                {"remote_name": "hub-c"},
            ],
        },
        jsz={
            "meta_cluster": {"leader": "hub-b"},
            "streams": [
                {
                    "config": {"name": "K1S_WORK"},
                    "cluster": {
                        "leader": "hub-a",
                        "replicas": [{"name": "hub-b"}, {"name": "hub-c"}],
                    },
                    "consumer_detail": [
                        {
                            "config": {"durable_name": "WORK_SITE_sea"},
                            "cluster": {
                                "leader": "hub-a",
                                "replicas": [{"name": "hub-b"}, {"name": "hub-c"}],
                            },
                        }
                    ],
                }
            ],
        },
        leafz={"num_leafs": 4},
    )

    assert record.cluster_name == "k1s-hub"
    assert record.jetstream_domain == "K1S"
    assert record.meta_leader == "hub-b"
    assert record.route_count == 2
    assert record.route_peers == ("hub-b", "hub-c")
    assert record.leaf_count == 4
    assert record.stream_replicas["K1S_WORK"] == 3
    assert record.consumer_replicas["WORK_SITE_sea"] == 3


def test_build_nats_edge_monitor_record_extracts_leaf_connectivity() -> None:
    record = build_nats_edge_monitor_record(
        NatsEdgeSiteTarget(site_id="sea", monitor_url="http://edge-sea:8224"),
        varz={
            "server_name": "edge-sea",
            "server_id": "srv-edge-sea",
            "version": "2.10.18",
            "git_commit": "sha-edge",
        },
        leafz={"num_leafs": 1},
    )

    assert record.site_id == "sea"
    assert record.server_name == "edge-sea"
    assert record.server_id == "srv-edge-sea"
    assert record.version == "2.10.18"
    assert record.git_commit == "sha-edge"
    assert record.leaf_count == 1


def test_evaluate_nats_hub_cluster_reports_route_and_replica_drift() -> None:
    records = [
        build_nats_hub_monitor_record(
            NatsHubNodeTarget(name="hub-a", monitor_url="http://hub-a:8222"),
            varz={
                "server_name": "hub-a",
                "version": "2.10.18",
                "cluster": {"name": "k1s-hub"},
                "jetstream": {"config": {"domain": "K1S"}},
            },
            routez={"num_routes": 1, "routes": [{"remote_name": "hub-b"}]},
            jsz={
                "meta_cluster": {"leader": "hub-a"},
                "streams": [
                    {
                        "config": {"name": "K1S_WORK"},
                        "cluster": {"leader": "hub-a", "replicas": [{"name": "hub-b"}]},
                        "consumer_detail": [
                            {
                                "config": {"durable_name": "WORK_SITE_sea"},
                                "cluster": {"leader": "hub-a", "replicas": [{"name": "hub-b"}]},
                            }
                        ],
                    }
                ],
            },
        ),
        build_nats_hub_monitor_record(
            NatsHubNodeTarget(name="hub-b", monitor_url="http://hub-b:8222"),
            varz={
                "server_name": "hub-b",
                "version": "2.10.18",
                "cluster": {"name": "k1s-hub"},
                "jetstream": {"config": {"domain": "K1S"}},
            },
            routez={"num_routes": 1, "routes": [{"remote_name": "hub-a"}]},
            jsz={
                "meta_cluster": {"leader": "hub-a"},
                "streams": [
                    {
                        "config": {"name": "K1S_WORK"},
                        "cluster": {"leader": "hub-a", "replicas": [{"name": "hub-b"}]},
                        "consumer_detail": [
                            {
                                "config": {"durable_name": "WORK_SITE_sea"},
                                "cluster": {"leader": "hub-a", "replicas": [{"name": "hub-b"}]},
                            }
                        ],
                    }
                ],
            },
        ),
        build_nats_hub_monitor_record(
            NatsHubNodeTarget(name="hub-c", monitor_url="http://hub-c:8222"),
            varz={
                "server_name": "hub-c",
                "version": "2.10.18",
                "cluster": {"name": "k1s-hub"},
                "jetstream": {"config": {"domain": "K1S"}},
            },
            routez={"num_routes": 0, "routes": []},
            jsz={
                "meta_cluster": {"leader": "hub-a"},
                "streams": [
                    {
                        "config": {"name": "K1S_WORK"},
                        "cluster": {"leader": "hub-a", "replicas": [{"name": "hub-b"}]},
                        "consumer_detail": [
                            {
                                "config": {"durable_name": "WORK_SITE_sea"},
                                "cluster": {"leader": "hub-a", "replicas": [{"name": "hub-b"}]},
                            }
                        ],
                    }
                ],
            },
        ),
    ]

    issues = evaluate_nats_hub_cluster(
        records,
        expected_domain="K1S",
        expected_stream="K1S_WORK",
        expected_replicas=3,
        expected_consumers=["WORK_SITE_sea"],
    )

    assert "route_mesh:hub-a:1/2" in issues
    assert "route_mesh:hub-b:1/2" in issues
    assert "route_mesh:hub-c:0/2" in issues
    assert "stream_replicas:hub-a:K1S_WORK:2/3" in issues
    assert "consumer_replicas:hub-c:WORK_SITE_sea:2/3" in issues


def test_evaluate_nats_edge_site_reports_missing_leaf_connectivity() -> None:
    record = build_nats_edge_monitor_record(
        NatsEdgeSiteTarget(site_id="sea", monitor_url="http://edge-sea:8224"),
        varz={"server_name": "edge-sea", "version": "2.10.18"},
        leafz={"num_leafs": 0},
    )

    issues = evaluate_nats_edge_site(record, expected_leaf_min=1)

    assert issues == ["leaf_count:sea:0/1"]


def test_collect_site_gateway_status_merges_last_seen_and_build_info() -> None:
    text = """
ae_site_gateway_last_seen_seconds{site="sea",node="edge-1"} 5
ae_site_gateway_last_seen_seconds{site="sea",node="edge-2"} 7
ae_site_gateway_build_info{site="sea",node="edge-1",version="0.1.3.dev0",sha="sha-a",date="2026-03-18"} 1
ae_site_gateway_build_info{site="sea",node="edge-2",version="0.1.3.dev1",sha="sha-b",date="2026-03-19"} 1
"""

    records = collect_site_gateway_status(text, "sea")

    assert records == {
        "edge-1": EdgeGatewayStatusRecord(
            site_id="sea",
            node_id="edge-1",
            last_seen_seconds=5.0,
            version="0.1.3.dev0",
            sha="sha-a",
            date="2026-03-18",
        ),
        "edge-2": EdgeGatewayStatusRecord(
            site_id="sea",
            node_id="edge-2",
            last_seen_seconds=7.0,
            version="0.1.3.dev1",
            sha="sha-b",
            date="2026-03-19",
        ),
    }


def test_build_local_etcdctl_restore_command_includes_cluster_flags(tmp_path: Path) -> None:
    snap = tmp_path / "snapshot.db"
    data_dir = tmp_path / "restore"
    cmd, env = build_local_etcdctl_command(
        "restore",
        snapshot_path=snap,
        data_dir=data_dir,
        name="etcd1",
        initial_cluster="etcd1=http://10.0.0.2:2380",
        initial_advertise_peer_urls="http://10.0.0.2:2380",
        initial_cluster_token="k1s-ha",
    )

    assert cmd == [
        "etcdctl",
        "snapshot",
        "restore",
        str(snap),
        f"--data-dir={data_dir}",
        "--name=etcd1",
        "--initial-cluster=etcd1=http://10.0.0.2:2380",
        "--initial-advertise-peer-urls=http://10.0.0.2:2380",
        "--initial-cluster-token=k1s-ha",
    ]
    assert env == {"ETCDCTL_API": "3"}


def test_build_container_etcdctl_command_mounts_unique_paths(tmp_path: Path) -> None:
    mount_path = tmp_path / "snapshots"
    mount_path.mkdir()

    cmd = build_container_etcdctl_command(
        "podman",
        "quay.io/coreos/etcd:v3.5.13",
        ["etcdctl", "snapshot", "status", str(mount_path / "snap.db")],
        mounts=[mount_path, mount_path],
        extra_env={"ETCDCTL_API": "3"},
    )

    assert cmd == [
        "podman",
        "run",
        "--rm",
        "-v",
        f"{mount_path}:{mount_path}",
        "-e",
        "ETCDCTL_API=3",
        "quay.io/coreos/etcd:v3.5.13",
        "etcdctl",
        "snapshot",
        "status",
        str(mount_path / "snap.db"),
    ]


def test_build_local_etcdctl_recovery_command_for_member_add_uses_learner() -> None:
    cmd, env = build_local_etcdctl_recovery_command(
        "member-add",
        endpoints=["http://10.0.0.11:2379", "http://10.0.0.12:2379"],
        member_name="etcd-c",
        peer_urls="http://10.0.0.13:2380",
    )

    assert cmd == [
        "etcdctl",
        "--endpoints=http://10.0.0.11:2379,http://10.0.0.12:2379",
        "member",
        "add",
        "etcd-c",
        "--peer-urls=http://10.0.0.13:2380",
        "--learner",
    ]
    assert env == {"ETCDCTL_API": "3"}


def test_parse_etcd_member_add_output_extracts_join_settings() -> None:
    raw = """
Member 278c654c9a6dfd3b added to cluster 8e9e05c52164694d

ETCD_NAME="etcd-c"
ETCD_INITIAL_CLUSTER="etcd-a=http://10.0.0.11:2380,etcd-b=http://10.0.0.12:2380,etcd-c=http://10.0.0.13:2380"
ETCD_INITIAL_ADVERTISE_PEER_URLS="http://10.0.0.13:2380"
ETCD_INITIAL_CLUSTER_STATE="existing"
"""

    result = parse_etcd_member_add_output(raw)

    assert result.member_id == "278c654c9a6dfd3b"
    assert result.cluster_id == "8e9e05c52164694d"
    assert result.member_name == "etcd-c"
    assert result.initial_cluster_state == "existing"
    assert result.initial_advertise_peer_urls == "http://10.0.0.13:2380"


def test_derive_client_url_switches_peer_port_to_client_port() -> None:
    assert derive_client_url("http://10.0.0.13:2380") == "http://10.0.0.13:2379"


def test_build_quorum_restore_plan_renders_three_member_restore_commands(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.db"
    plan = build_quorum_restore_plan(
        snapshot_path=snapshot,
        cluster_token="k1s-ha",
        members=[
            EtcdRestoreMemberSpec(
                name="etcd-a",
                peer_url="http://10.0.0.11:2380",
                client_url="http://10.0.0.11:2379",
                data_dir="/var/lib/etcd",
            ),
            EtcdRestoreMemberSpec(
                name="etcd-b",
                peer_url="http://10.0.0.12:2380",
                client_url="http://10.0.0.12:2379",
                data_dir="/var/lib/etcd",
            ),
            EtcdRestoreMemberSpec(
                name="etcd-c",
                peer_url="http://10.0.0.13:2380",
                client_url="http://10.0.0.13:2379",
                data_dir="/var/lib/etcd",
            ),
        ],
    )

    assert plan.initial_cluster == (
        "etcd-a=http://10.0.0.11:2380,"
        "etcd-b=http://10.0.0.12:2380,"
        "etcd-c=http://10.0.0.13:2380"
    )
    assert len(plan.members) == 3
    assert plan.members[0].restore_command[0:3] == ["etcdctl", "snapshot", "restore"]
    assert "--initial-cluster-token=k1s-ha" in plan.members[0].restore_command
    assert "--initial-cluster-state=new" in plan.members[0].start_command

    rendered = format_quorum_restore_plan(plan)
    assert "Quorum restore plan from snapshot:" in rendered
    assert "[etcd-b]" in rendered
    assert "Initial cluster token: k1s-ha" in rendered
