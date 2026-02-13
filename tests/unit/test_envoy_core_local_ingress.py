from pathlib import Path

from ae.controller.spec import app_key
from ae.controller.state import ServiceEndpoint, SQLiteStateStore
from ae.ingress.edge_core_proxy import (
    EdgeCoreProxyConfig,
    EdgeCoreProxyRenderer,
    build_core_proxy_config,
)


def test_envoy_core_local_ingress_renders_tls(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)

    app_name = app_key("demo", "default")
    store.upsert_service_endpoints(
        app_name,
        [
            ServiceEndpoint(
                app_name=app_name,
                port=8080,
                ip="127.0.0.1",
                target_port=8080,
                ready=True,
            )
        ],
    )

    tls_root = tmp_path / "tls"
    tls_root.mkdir(parents=True, exist_ok=True)
    cert_path = tls_root / "demo-cert.crt"
    key_path = tls_root / "demo-cert.key"
    cert_path.write_text("dummy cert", encoding="utf-8")
    key_path.write_text("dummy key", encoding="utf-8")

    store.upsert_edge_ingress_route(
        name="demo-ingress",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "host": "demo.local",
            "paths": [
                {
                    "path": "/",
                    "serviceRef": {"name": "demo", "namespace": "default", "port": 8080},
                }
            ],
            "exposure": {
                "mode": "core-local",
                "tls": {
                    "mode": "terminate-core",
                    "terminateCore": {
                        "secretName": "demo-cert",
                        "redirectHttpToHttps": True,
                    },
                },
            },
        },
    )

    config_dir = tmp_path / "edge-ingress"
    envoy_path = config_dir / "envoy.yaml"
    rathole_path = config_dir / "rathole-server.toml"
    cfg = EdgeCoreProxyConfig(
        config_dir=config_dir,
        envoy_config_path=envoy_path,
        rathole_server_path=rathole_path,
        rathole_client_dir=None,
        site_domain_suffix="edge.local",
        http_listen_port=10080,
        tls_listen_port=10443,
        tls_root=tls_root,
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )

    renderer = EdgeCoreProxyRenderer(store, cfg)
    renderer.render()

    text = envoy_path.read_text(encoding="utf-8")
    assert "demo.local" in text
    assert "core_default_demo_8080" in text
    assert "edge_listener_tls" in text
    assert str(cert_path) in text


def test_build_core_proxy_config_normalizes_relative_tls_root(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "edge-ingress"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AE_EDGE_INGRESS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AE_TLS_DIR", "state/tls")
    monkeypatch.delenv("AE_EDGE_INGRESS_ENVOY_CONFIG", raising=False)
    monkeypatch.delenv("AE_RATHOLE_SERVER_CONFIG", raising=False)

    config = build_core_proxy_config()

    assert config is not None
    assert config.tls_root == (tmp_path / "state" / "tls").resolve()
