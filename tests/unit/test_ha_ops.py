from __future__ import annotations

from pathlib import Path

from ae.ha.ops import (
    build_container_etcdctl_command,
    build_local_etcdctl_command,
    ha_core_missing_env,
    leader_key,
    parse_etcd_leader_response,
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
