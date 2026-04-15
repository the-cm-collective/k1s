from __future__ import annotations

from ae.ha.dashboard import HaDashboardProbeCache
from ae.ha.ops import NatsEdgeMonitorRecord, NatsHubMonitorRecord


def test_ha_dashboard_probe_cache_collects_etcd_and_nats(monkeypatch) -> None:
    env = {
        "AE_HA_DASHBOARD_PROBES": "1",
        "AE_HA_DASHBOARD_HUB_MONITORS": "hub-a=http://hub-a:8222",
        "AE_HA_DASHBOARD_EDGE_MONITORS": "sea=http://edge-sea:8223",
        "AE_HA_DASHBOARD_PROBE_TIMEOUT_S": "1.5",
        "AE_ETCD_ENDPOINTS": "http://etcd-a:2379,http://etcd-b:2379",
        "AE_JS_DOMAIN": "K1S",
        "AE_JS_STREAM_NAME": "K1S_WORK",
        "AE_HA_MODE": "1",
    }

    monkeypatch.setattr(
        "ae.ha.dashboard.etcd_endpoint_healthy",
        lambda endpoint, timeout_s=3.0: (
            endpoint.endswith("a:2379"),
            "ok" if endpoint.endswith("a:2379") else "down",
        ),
    )
    monkeypatch.setattr(
        "ae.ha.dashboard.fetch_nats_hub_monitor_record",
        lambda target, timeout_s=3.0, include_leafz=False: NatsHubMonitorRecord(
            name=target.name,
            monitor_url=target.monitor_url,
            server_name=target.name,
            server_id=f"{target.name}-id",
            version="2.10.0",
            git_commit="nats-sha",
            cluster_name="k1s-hub",
            jetstream_domain="K1S",
            meta_leader="hub-a",
            route_count=0,
            route_peers=(),
            leaf_count=1,
            stream_leaders={"K1S_WORK": "hub-a"},
            stream_replicas={"K1S_WORK": 3},
            stream_offline={"K1S_WORK": ()},
            consumer_leaders={"WORK_SITE_sea": "hub-a"},
            consumer_replicas={"WORK_SITE_sea": 3},
            consumer_offline={"WORK_SITE_sea": ()},
        ),
    )
    monkeypatch.setattr(
        "ae.ha.dashboard.fetch_nats_edge_monitor_record",
        lambda target, timeout_s=3.0, include_leafz=True: NatsEdgeMonitorRecord(
            site_id=target.site_id,
            monitor_url=target.monitor_url,
            server_name="edge-sea",
            server_id="edge-sea-id",
            version="2.10.0",
            git_commit="edge-sha",
            leaf_count=1,
        ),
    )

    cache = HaDashboardProbeCache.from_env(env)
    assert cache is not None

    snapshot = cache.run_once()

    assert snapshot["enabled"] is True
    assert snapshot["etcd"]["healthy_endpoints"] == 1
    assert snapshot["etcd"]["unhealthy_endpoints"] == 1
    assert snapshot["hubs"]["healthy"] is True
    assert snapshot["hubs"]["expected_stream"] == "K1S_WORK"
    assert snapshot["hubs"]["expected_replicas"] == 3
    assert snapshot["hubs"]["nodes"][0]["name"] == "hub-a"
    assert snapshot["edges"]["healthy"] is True
    assert snapshot["edges"]["sites"][0]["site_id"] == "sea"


def test_ha_dashboard_probe_cache_records_probe_failures(monkeypatch) -> None:
    env = {
        "AE_HA_DASHBOARD_PROBES": "1",
        "AE_HA_DASHBOARD_HUB_MONITORS": "hub-a=http://hub-a:8222,hub-b=http://hub-b:8222",
        "AE_HA_DASHBOARD_EDGE_MONITORS": "sea=http://edge-sea:8223",
        "AE_ETCD_ENDPOINTS": "http://etcd-a:2379",
        "AE_JS_DOMAIN": "K1S",
    }

    monkeypatch.setattr(
        "ae.ha.dashboard.etcd_endpoint_healthy",
        lambda endpoint, timeout_s=3.0: (True, "ok"),
    )

    def _hub_record(target, timeout_s=3.0, include_leafz=False):
        if target.name == "hub-b":
            raise RuntimeError("timeout")
        return NatsHubMonitorRecord(
            name=target.name,
            monitor_url=target.monitor_url,
            server_name=target.name,
            server_id=f"{target.name}-id",
            version="2.10.0",
            git_commit="nats-sha",
            cluster_name="k1s-hub",
            jetstream_domain="K1S",
            meta_leader="hub-a",
            route_count=0,
            route_peers=(),
            leaf_count=1,
            stream_leaders={"K1S_WORK": "hub-a"},
            stream_replicas={"K1S_WORK": 3},
            stream_offline={"K1S_WORK": ()},
            consumer_leaders={"WORK_SITE_sea": "hub-a"},
            consumer_replicas={"WORK_SITE_sea": 3},
            consumer_offline={"WORK_SITE_sea": ()},
        )

    monkeypatch.setattr("ae.ha.dashboard.fetch_nats_hub_monitor_record", _hub_record)
    monkeypatch.setattr(
        "ae.ha.dashboard.fetch_nats_edge_monitor_record",
        lambda target, timeout_s=3.0, include_leafz=True: (_ for _ in ()).throw(
            RuntimeError("edge down")
        ),
    )

    cache = HaDashboardProbeCache.from_env(env)
    assert cache is not None

    snapshot = cache.run_once()

    assert snapshot["hubs"]["healthy"] is False
    assert snapshot["hubs"]["errors"][0]["name"] == "hub-b"
    assert snapshot["edges"]["healthy"] is False
    assert snapshot["edges"]["errors"][0]["site_id"] == "sea"
