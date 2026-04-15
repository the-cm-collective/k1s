from __future__ import annotations

import yaml

from ae.ingress.envoy_control_plane import (
    ControlPlaneEnvoyConfig,
    build_control_plane_envoy_config_from_env,
    render_control_plane_envoy_config,
    render_control_plane_envoy_secrets,
)


def test_controlplane_envoy_renders_readonly_routes_without_oauth() -> None:
    text = render_control_plane_envoy_config(
        ControlPlaneEnvoyConfig(
            read_token="read-token",
            auth_enabled=False,
        )
    )

    payload = yaml.safe_load(text)
    listeners = payload["static_resources"]["listeners"]
    vhosts = listeners[0]["filter_chains"][0]["filters"][0]["typed_config"]["route_config"][
        "virtual_hosts"
    ]
    docs_vhost = next(v for v in vhosts if "docs.home.arpa" in v["domains"])
    dash_vhost = next(v for v in vhosts if "dash.home.arpa" in v["domains"])
    http_filters = listeners[0]["filter_chains"][0]["filters"][0]["typed_config"]["http_filters"]
    clusters = payload["static_resources"]["clusters"]

    assert [flt["name"] for flt in http_filters] == ["envoy.filters.http.router"]
    assert {cluster["name"] for cluster in clusters} == {"controller_http", "docs_http"}
    assert {"docs.home.arpa", "docs.home.arpa:10081"} <= set(docs_vhost["domains"])
    assert {"dash.home.arpa", "dash.home.arpa:10081"} <= set(dash_vhost["domains"])
    assert docs_vhost["routes"][1]["request_headers_to_add"][0]["header"] == {
        "key": "Authorization",
        "value": "Bearer read-token",
    }
    assert dash_vhost["routes"][0]["direct_response"] == {"status": 404}
    assert dash_vhost["routes"][-1]["request_headers_to_add"][0]["header"]["value"] == (
        "Bearer read-token"
    )


def test_controlplane_envoy_renders_public_tls_authority_aliases() -> None:
    text = render_control_plane_envoy_config(
        ControlPlaneEnvoyConfig(
            read_token="read-token",
            auth_enabled=False,
            public_tls_authority_port=10443,
        )
    )

    payload = yaml.safe_load(text)
    vhosts = payload["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
        "typed_config"
    ]["route_config"]["virtual_hosts"]
    docs_vhost = next(v for v in vhosts if "docs.home.arpa" in v["domains"])
    dash_vhost = next(v for v in vhosts if "dash.home.arpa" in v["domains"])

    assert {
        "docs.home.arpa",
        "docs.home.arpa:10081",
        "docs.home.arpa:10443",
    } <= set(docs_vhost["domains"])
    assert {
        "dash.home.arpa",
        "dash.home.arpa:10081",
        "dash.home.arpa:10443",
    } <= set(dash_vhost["domains"])


def test_build_controlplane_envoy_config_reads_public_tls_authority_port(monkeypatch) -> None:
    monkeypatch.setenv("AE_CONTROLPLANE_PUBLIC_ENABLE", "1")
    monkeypatch.setenv("AE_EDGE_INGRESS_TLS_PORT", "10443")
    monkeypatch.setenv("AE_CONTROLPLANE_PROXY_PORT", "10081")

    cfg = build_control_plane_envoy_config_from_env()

    assert cfg.listen_port == 10081
    assert cfg.public_tls_authority_port == 10443


def test_controlplane_envoy_renders_oauth_and_sds_secrets() -> None:
    cfg = ControlPlaneEnvoyConfig(
        auth_enabled=True,
        read_token="read-token",
        authentik_base_url="https://auth.home.arpa",
        oauth_client_id="k1s-dashboard",
        oauth_client_secret="client-secret",
        oauth_hmac_secret="hmac-secret",
    )

    text = render_control_plane_envoy_config(cfg, secrets_path="/tmp/controlplane-secrets.yaml")
    secrets_text = render_control_plane_envoy_secrets(cfg)

    payload = yaml.safe_load(text)
    secrets_payload = yaml.safe_load(secrets_text)
    listener = payload["static_resources"]["listeners"][0]
    http_filters = listener["filter_chains"][0]["filters"][0]["typed_config"]["http_filters"]
    oauth_filter = next(f for f in http_filters if f["name"] == "envoy.filters.http.oauth2")
    clusters = payload["static_resources"]["clusters"]

    assert [flt["name"] for flt in http_filters[:2]] == [
        "envoy.filters.http.csrf",
        "envoy.filters.http.oauth2",
    ]
    assert "authentik_https" in {cluster["name"] for cluster in clusters}
    assert oauth_filter["typed_config"]["config"]["authorization_endpoint"] == (
        "https://auth.home.arpa/application/o/authorize/"
    )
    assert oauth_filter["typed_config"]["config"]["token_endpoint"]["uri"] == (
        "auth.home.arpa/application/o/token/"
    )
    assert oauth_filter["typed_config"]["config"]["credentials"]["token_secret"]["sds_config"] == {
        "path": "/tmp/controlplane-secrets.yaml"
    }
    assert secrets_payload["static_resources"]["secrets"] == [
        {"name": "token", "generic_secret": {"secret": "client-secret"}},
        {"name": "hmac", "generic_secret": {"secret": "hmac-secret"}},
    ]
