from pathlib import Path

import yaml

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
    assert "codec_type: AUTO" in text
    assert "alpn_protocols" in text
    assert "- h2" in text
    assert "- http/1.1" in text
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


def test_core_proxy_policy_least_request_sets_cluster_lb_policy(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=18081,
    )
    store.upsert_edge_ingress_policy(
        name="lb-policy",
        namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressPolicy",
            "metadata": {"name": "lb-policy", "namespace": "default"},
            "spec": {"loadBalancing": {"strategy": "least_request"}},
        },
    )
    store.upsert_edge_ingress_route(
        name="app-core-proxy",
        namespace="default",
        site_id="sea-edge-02",
        policy_name="lb-policy",
        policy_namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-core-proxy", "namespace": "default"},
            "spec": {
                "host": "app-core-proxy.home.arpa",
                "paths": [
                    {
                        "path": "/",
                        "serviceRef": {"name": "app", "namespace": "default", "port": 8080},
                    }
                ],
                "exposure": {
                    "mode": "core-proxy",
                    "placement": {"site": "sea-edge-02"},
                },
                "policyRef": {"name": "lb-policy", "namespace": "default"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
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
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    clusters = payload["static_resources"]["clusters"]
    least_request = [c for c in clusters if c.get("lb_policy") == "LEAST_REQUEST"]
    assert least_request


def test_core_proxy_policy_stickiness_sets_ring_hash_and_cookie(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=18081,
    )
    store.upsert_edge_ingress_policy(
        name="sticky-policy",
        namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressPolicy",
            "metadata": {"name": "sticky-policy", "namespace": "default"},
            "spec": {
                "stickiness": {
                    "mode": "cookie",
                    "cookie": {"name": "k1s_route", "ttlSeconds": 900},
                }
            },
        },
    )
    store.upsert_edge_ingress_route(
        name="app-sticky",
        namespace="default",
        site_id="sea-edge-02",
        policy_name="sticky-policy",
        policy_namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-sticky", "namespace": "default"},
            "spec": {
                "host": "sticky-core-proxy.home.arpa",
                "paths": [{"path": "/"}],
                "exposure": {
                    "mode": "core-proxy",
                    "placement": {"site": "sea-edge-02"},
                },
                "policyRef": {"name": "sticky-policy", "namespace": "default"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
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
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    vhosts = (
        payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]["route_config"]["virtual_hosts"]
    )
    sticky_vhost = next(v for v in vhosts if v["domains"] == ["sticky-core-proxy.home.arpa"])
    route_action = sticky_vhost["routes"][0]["route"]
    cookie_policy = route_action["hash_policy"][0]["cookie"]
    assert cookie_policy["name"] == "k1s_route"
    assert cookie_policy["ttl"] == "900s"

    cluster_name = route_action["cluster"]
    clusters = payload["static_resources"]["clusters"]
    cluster = next(c for c in clusters if c["name"] == cluster_name)
    assert cluster["lb_policy"] == "RING_HASH"


def test_core_proxy_policy_websocket_enabled_renders_upgrade_settings(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=18081,
    )
    store.upsert_edge_ingress_policy(
        name="ws-policy",
        namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressPolicy",
            "metadata": {"name": "ws-policy", "namespace": "default"},
            "spec": {
                "websockets": {
                    "enabled": True,
                    "idleMs": 120000,
                    "maxConnectionDurationMs": 300000,
                }
            },
        },
    )
    store.upsert_edge_ingress_route(
        name="app-ws",
        namespace="default",
        site_id="sea-edge-02",
        policy_name="ws-policy",
        policy_namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-ws", "namespace": "default"},
            "spec": {
                "host": "ws-core-proxy.home.arpa",
                "paths": [{"path": "/ws"}],
                "exposure": {
                    "mode": "core-proxy",
                    "placement": {"site": "sea-edge-02"},
                },
                "policyRef": {"name": "ws-policy", "namespace": "default"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=10443,
        tls_root=tmp_path / "tls",
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
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    hcm_http = (
        payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
            "typed_config"
        ]
    )
    assert hcm_http["upgrade_configs"] == [{"upgrade_type": "websocket"}]

    vhosts = hcm_http["route_config"]["virtual_hosts"]
    ws_vhost = next(v for v in vhosts if v["domains"] == ["ws-core-proxy.home.arpa"])
    ws_route = ws_vhost["routes"][0]["route"]
    assert ws_route["idle_timeout"] == "120.000s"
    assert ws_route["max_stream_duration"]["max_stream_duration"] == "300.000s"


def test_core_proxy_policy_websocket_disabled_disables_upgrade_for_route(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-proxy",
        core_proxy_port=18081,
    )
    store.upsert_edge_ingress_policy(
        name="ws-policy-off",
        namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressPolicy",
            "metadata": {"name": "ws-policy-off", "namespace": "default"},
            "spec": {"websockets": {"enabled": False}},
        },
    )
    store.upsert_edge_ingress_route(
        name="app-ws-off",
        namespace="default",
        site_id="sea-edge-02",
        policy_name="ws-policy-off",
        policy_namespace="default",
        document={
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app-ws-off", "namespace": "default"},
            "spec": {
                "host": "ws-off-core-proxy.home.arpa",
                "paths": [{"path": "/ws"}],
                "exposure": {
                    "mode": "core-proxy",
                    "placement": {"site": "sea-edge-02"},
                },
                "policyRef": {"name": "ws-policy-off", "namespace": "default"},
            },
        },
    )

    cfg = EdgeCoreProxyConfig(
        config_dir=tmp_path / "edge-ingress",
        envoy_config_path=tmp_path / "edge-ingress" / "envoy.yaml",
        rathole_server_path=tmp_path / "edge-ingress" / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="home.arpa",
        http_listen_port=10080,
        tls_listen_port=None,
        tls_root=tmp_path / "tls",
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
    EdgeCoreProxyRenderer(store, cfg).render()

    payload = yaml.safe_load(cfg.envoy_config_path.read_text(encoding="utf-8"))
    hcm_http = (
        payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
            "typed_config"
        ]
    )
    vhosts = hcm_http["route_config"]["virtual_hosts"]
    ws_vhost = next(v for v in vhosts if v["domains"] == ["ws-off-core-proxy.home.arpa"])
    ws_route = ws_vhost["routes"][0]["route"]
    assert ws_route["upgrade_configs"] == [
        {"upgrade_type": "websocket", "enabled": False}
    ]
