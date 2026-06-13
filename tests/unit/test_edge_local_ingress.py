from pathlib import Path

from ae.ingress.edge_local import EdgeLocalIngressConfig, render_edge_local_caddy


def _route_doc() -> list[dict]:
    return [
        {
            "apiVersion": "k1s.io/v1",
            "kind": "EdgeIngressRoute",
            "metadata": {"name": "app", "namespace": "default"},
            "spec": {
                "host": "app.example.com",
                "paths": [
                    {
                        "path": "/",
                        "serviceRef": {"name": "app-svc", "port": 8080},
                    }
                ],
                "exposure": {"mode": "edge-local", "placement": {"site": "sfo"}},
            },
        }
    ]


def test_render_edge_local_caddy_basic(tmp_path: Path) -> None:
    cfg = EdgeLocalIngressConfig(
        config_dir=tmp_path,
        config_file=tmp_path / "edge-local.caddy",
        reload_cmd=None,
        service_domain=None,
        service_port_fallback=8080,
    )
    routes = _route_doc()
    content = render_edge_local_caddy(routes, [], cfg)
    assert "https://app.example.com" in content
    assert "reverse_proxy app-svc.default:8080" in content


def test_render_edge_local_caddy_can_bind_http_port(tmp_path: Path) -> None:
    cfg = EdgeLocalIngressConfig(
        config_dir=tmp_path,
        config_file=tmp_path / "edge-local.caddy",
        reload_cmd=None,
        service_domain=None,
        service_port_fallback=8080,
        listen_scheme="http",
        listen_port=18081,
    )
    content = render_edge_local_caddy(_route_doc(), [], cfg)
    assert "http://app.example.com:18081" in content
    assert "tls internal" not in content


def test_render_edge_local_caddy_uses_bundle_endpoints_auto_mode(tmp_path: Path) -> None:
    cfg = EdgeLocalIngressConfig(
        config_dir=tmp_path,
        config_file=tmp_path / "edge-local.caddy",
        reload_cmd=None,
        service_domain=None,
        service_port_fallback=8080,
        upstream_mode="auto",
    )
    service_endpoints = {
        "default/app-svc": [
            {
                "ip": "10.88.0.11",
                "service_port": 18119,
                "target_port": 8080,
                "ready": True,
            },
            {
                "ip": "10.88.0.12",
                "service_port": 18119,
                "target_port": 8080,
                "ready": True,
            },
        ]
    }
    content = render_edge_local_caddy(_route_doc(), [], cfg, service_endpoints=service_endpoints)
    assert "reverse_proxy 10.88.0.11:8080 10.88.0.12:8080 {" in content
    assert "reverse_proxy app-svc.default:8080 {" not in content


def test_render_edge_local_caddy_auto_mode_falls_back_to_dns(tmp_path: Path) -> None:
    cfg = EdgeLocalIngressConfig(
        config_dir=tmp_path,
        config_file=tmp_path / "edge-local.caddy",
        reload_cmd=None,
        service_domain=None,
        service_port_fallback=8080,
        upstream_mode="auto",
    )
    content = render_edge_local_caddy(_route_doc(), [], cfg, service_endpoints={})
    assert "reverse_proxy app-svc.default:8080 {" in content


def test_render_edge_local_caddy_bundle_mode_skips_missing_endpoints(tmp_path: Path) -> None:
    cfg = EdgeLocalIngressConfig(
        config_dir=tmp_path,
        config_file=tmp_path / "edge-local.caddy",
        reload_cmd=None,
        service_domain=None,
        service_port_fallback=8080,
        upstream_mode="bundle-endpoints",
    )
    content = render_edge_local_caddy(_route_doc(), [], cfg, service_endpoints={})
    assert "respond 503" in content
    assert "https://edge-local-unconfigured.invalid" in content
    assert "https://app.example.com" not in content
