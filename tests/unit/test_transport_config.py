import pytest

from ae.config.transport import (
    GatewayJetStreamConfig,
    TransportConfig,
    _parse_nats_endpoint,
    check_nats_connectivity,
    desired_js_replicas,
    ha_mode_enabled,
    parse_nats_explicit_port,
)
from ae.transport.nats_client import _consumer_config_drift, _stream_config_drift


def test_transport_config_defaults() -> None:
    cfg = TransportConfig.from_env({})
    assert cfg.backend == "http"
    assert cfg.nats_url is None
    assert cfg.nats_creds is None
    assert cfg.site_id is None


def test_gateway_js_defaults() -> None:
    cfg = GatewayJetStreamConfig.from_env({})
    assert cfg.ack_wait == "30s"
    assert cfg.progress_interval == "10s"
    assert cfg.progress_jitter_pct == 15
    assert cfg.max_ack_pending == 32
    assert cfg.max_deliver == 20
    assert cfg.max_waiting == 512
    assert str(cfg.spool_path) == "/var/lib/ae/gateway/spool.db"


def test_ha_mode_enabled_default_false() -> None:
    assert ha_mode_enabled({}) is False


def test_desired_js_replicas_defaults_to_three_in_ha() -> None:
    assert desired_js_replicas({"AE_HA_MODE": "1"}) == 3


def test_desired_js_replicas_honors_override() -> None:
    assert desired_js_replicas({"AE_HA_MODE": "1", "AE_JS_REPLICAS": "5"}) == 5


def test_parse_nats_endpoint_defaults_port() -> None:
    host, port = _parse_nats_endpoint("localhost")
    assert host == "localhost"
    assert port == 4222


def test_parse_nats_endpoint_with_port() -> None:
    host, port = _parse_nats_endpoint("nats://user:pass@127.0.0.1:4223")
    assert host == "127.0.0.1"
    assert port == 4223


def test_parse_nats_explicit_port_with_port() -> None:
    assert parse_nats_explicit_port("nats://gateway:dev@127.0.0.1:4224") == 4224


def test_parse_nats_explicit_port_without_port() -> None:
    assert parse_nats_explicit_port("nats://gateway:dev@127.0.0.1") is None


def test_parse_nats_explicit_port_invalid_url() -> None:
    with pytest.raises(ValueError, match="missing host"):
        parse_nats_explicit_port("nats://")


def test_check_nats_connectivity_invalid_url() -> None:
    ok, detail = check_nats_connectivity("nats://")
    assert not ok
    assert "invalid nats url" in detail


def test_stream_config_drift_detects_replica_mismatch() -> None:
    info = {
        "config": {
            "subjects": ["k1s.v1.work.site.>"],
            "storage": type("Storage", (), {"name": "FILE"})(),
            "retention": type("Retention", (), {"name": "WORK_QUEUE"})(),
            "num_replicas": 1,
        }
    }
    drift = _stream_config_drift(
        info,
        subjects=["k1s.v1.work.site.>"],
        storage="file",
        retention="workqueue",
        replicas=3,
    )
    assert drift == ["replicas"]


def test_consumer_config_drift_detects_ack_wait_mismatch() -> None:
    info = {
        "config": {
            "filter_subject": "k1s.v1.work.site.sea",
            "ack_wait": 10_000_000_000,
            "ack_policy": type("Ack", (), {"name": "EXPLICIT"})(),
            "deliver_policy": type("Deliver", (), {"name": "ALL"})(),
            "max_ack_pending": 32,
            "max_deliver": 20,
            "max_waiting": 512,
            "num_replicas": 3,
        }
    }
    drift = _consumer_config_drift(
        info,
        filter_subject="k1s.v1.work.site.sea",
        ack_wait_s=30.0,
        max_ack_pending=32,
        max_deliver=20,
        max_waiting=512,
        replicas=3,
    )
    assert drift == ["ack_wait"]
