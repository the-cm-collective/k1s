"""Envoy core ingress config renderer for edge core-proxy mode."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoreProxyRoute:
    site_id: str
    upstream_host: str
    upstream_port: int


@dataclass(frozen=True)
class EnvoyRenderConfig:
    listen_address: str = "0.0.0.0"
    listen_port: int = 10080
    admin_address: str = "127.0.0.1"
    admin_port: int = 9901
    domain_suffix: str = "edge.local"


def render_envoy_config(routes: list[CoreProxyRoute], config: EnvoyRenderConfig) -> str:
    listener_addr = config.listen_address
    listener_port = config.listen_port
    admin_addr = config.admin_address
    admin_port = config.admin_port
    domain_suffix = config.domain_suffix.strip(".") or "edge.local"

    clusters = []
    vhosts = []
    for route in routes:
        cluster_name = f"site_{route.site_id}"
        host = f"{route.site_id}.{domain_suffix}"
        vhosts.append(
            {
                "name": cluster_name,
                "domains": [host],
                "routes": [
                    {
                        "match": {"prefix": "/"},
                        "route": {"cluster": cluster_name},
                    }
                ],
            }
        )
        clusters.append(
            {
                "name": cluster_name,
                "connect_timeout": "1s",
                "type": "STATIC",
                "lb_policy": "ROUND_ROBIN",
                "load_assignment": {
                    "cluster_name": cluster_name,
                    "endpoints": [
                        {
                            "lb_endpoints": [
                                {
                                    "endpoint": {
                                        "address": {
                                            "socket_address": {
                                                "address": route.upstream_host,
                                                "port_value": int(route.upstream_port),
                                            }
                                        }
                                    }
                                }
                            ]
                        }
                    ],
                },
            }
        )

    if not vhosts:
        vhosts.append(
            {
                "name": "default",
                "domains": ["*"],
                "routes": [
                    {
                        "match": {"prefix": "/"},
                        "direct_response": {"status": 503},
                    }
                ],
            }
        )

    def _yaml(obj, indent=0):  # minimal YAML emitter for nested dicts/lists
        pad = " " * indent
        if isinstance(obj, dict):
            lines = []
            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    lines.append(f"{pad}{key}:")
                    lines.append(_yaml(value, indent + 2))
                else:
                    lines.append(f"{pad}{key}: {value}")
            return "\n".join(lines)
        if isinstance(obj, list):
            lines = []
            for item in obj:
                if isinstance(item, (dict, list)):
                    lines.append(f"{pad}-")
                    lines.append(_yaml(item, indent + 2))
                else:
                    lines.append(f"{pad}- {item}")
            return "\n".join(lines)
        return f"{pad}{obj}"

    config_obj = {
        "static_resources": {
            "listeners": [
                {
                    "name": "edge_listener",
                    "address": {
                        "socket_address": {
                            "address": listener_addr,
                            "port_value": int(listener_port),
                        }
                    },
                    "filter_chains": [
                        {
                            "filters": [
                                {
                                    "name": "envoy.filters.network.http_connection_manager",
                                    "typed_config": {
                                        "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                                        "stat_prefix": "edge_ingress",
                                        "route_config": {
                                            "name": "edge_routes",
                                            "virtual_hosts": vhosts,
                                        },
                                        "http_filters": [
                                            {"name": "envoy.filters.http.router"}
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ],
            "clusters": clusters,
        },
        "admin": {
            "access_log_path": "/tmp/envoy_admin_access.log",
            "address": {
                "socket_address": {
                    "address": admin_addr,
                    "port_value": int(admin_port),
                }
            },
        },
    }
    return _yaml(config_obj) + "\n"


def write_envoy_config(path: Path, routes: list[CoreProxyRoute], config: EnvoyRenderConfig) -> str:
    content = render_envoy_config(routes, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


__all__ = ["CoreProxyRoute", "EnvoyRenderConfig", "render_envoy_config", "write_envoy_config"]
