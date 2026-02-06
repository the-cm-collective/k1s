from pathlib import Path

from ae.ingress.edge_local import EdgeLocalIngressConfig, render_edge_local_caddy


def test_render_edge_local_caddy_basic(tmp_path: Path) -> None:
    cfg = EdgeLocalIngressConfig(
        config_dir=tmp_path,
        config_file=tmp_path / "edge-local.caddy",
        reload_cmd=None,
        service_domain=None,
        service_port_fallback=8080,
    )
    routes = [
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
    content = render_edge_local_caddy(routes, [], cfg)
    assert "https://app.example.com" in content
    assert "reverse_proxy app-svc.default:8080" in content
