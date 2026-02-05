from ae.config.transport import (
    GatewayJetStreamConfig,
    TransportConfig,
    _parse_nats_endpoint,
    check_nats_connectivity,
)


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


def test_parse_nats_endpoint_defaults_port() -> None:
    host, port = _parse_nats_endpoint("localhost")
    assert host == "localhost"
    assert port == 4222


def test_parse_nats_endpoint_with_port() -> None:
    host, port = _parse_nats_endpoint("nats://user:pass@127.0.0.1:4223")
    assert host == "127.0.0.1"
    assert port == 4223


def test_check_nats_connectivity_invalid_url() -> None:
    ok, detail = check_nats_connectivity("nats://")
    assert not ok
    assert "invalid nats url" in detail
