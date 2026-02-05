"""Envoy core ingress config renderer for edge core-proxy mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CoreProxyRoute:
    host: str
    path_prefix: str
    cluster: str


@dataclass(frozen=True)
class CoreProxyCluster:
    name: str
    endpoints: list[tuple[str, int]] = field(default_factory=list)
    use_tls: bool = False
    sni: str | None = None
    ca_cert_path: str | None = None
    expected_sans: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EnvoyRenderConfig:
    listen_address: str = "0.0.0.0"
    listen_port: int = 10080
    admin_address: str = "127.0.0.1"
    admin_port: int = 9901
    domain_suffix: str = "edge.local"


def render_envoy_config(
    routes: list[CoreProxyRoute],
    clusters: list[CoreProxyCluster],
    config: EnvoyRenderConfig,
) -> str:
    listener_addr = config.listen_address
    listener_port = config.listen_port
    admin_addr = config.admin_address
    admin_port = config.admin_port
    vhosts = []
    vhost_map: dict[str, dict] = {}
    for route in sorted(routes, key=lambda r: len(r.path_prefix or ""), reverse=True):
        host = route.host
        if not host:
            continue
        vhost = vhost_map.get(host)
        if vhost is None:
            vhost = {"name": f"vhost_{host}", "domains": [host], "routes": []}
            vhost_map[host] = vhost
        vhost["routes"].append(
            {
                "match": {"prefix": route.path_prefix or "/"},
                "route": {"cluster": route.cluster},
            }
        )
    vhosts.extend(vhost_map.values())

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

    cluster_defs = []
    for cluster in clusters:
        if not cluster.endpoints:
            continue
        endpoints = [
            {
                "endpoint": {
                    "address": {
                        "socket_address": {"address": host, "port_value": int(port)}
                    }
                }
            }
            for host, port in cluster.endpoints
        ]
        entry = {
            "name": cluster.name,
            "connect_timeout": "1s",
            "type": "STATIC",
            "lb_policy": "ROUND_ROBIN",
            "load_assignment": {
                "cluster_name": cluster.name,
                "endpoints": [{"lb_endpoints": endpoints}],
            },
        }
        if cluster.use_tls:
            tls_ctx: dict = {
                "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext",
            }
            if cluster.sni:
                tls_ctx["sni"] = cluster.sni
            validation: dict = {}
            if cluster.ca_cert_path:
                validation["trusted_ca"] = {"filename": cluster.ca_cert_path}
            if cluster.expected_sans:
                validation["match_subject_alt_names"] = [
                    {"exact": name} for name in cluster.expected_sans
                ]
            if validation:
                tls_ctx["common_tls_context"] = {"validation_context": validation}
            entry["transport_socket"] = {
                "name": "envoy.transport_sockets.tls",
                "typed_config": tls_ctx,
            }
        cluster_defs.append(entry)

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
            "clusters": cluster_defs,
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


def write_envoy_config(
    path: Path,
    routes: list[CoreProxyRoute],
    clusters: list[CoreProxyCluster],
    config: EnvoyRenderConfig,
) -> str:
    content = render_envoy_config(routes, clusters, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


__all__ = [
    "CoreProxyRoute",
    "CoreProxyCluster",
    "EnvoyRenderConfig",
    "render_envoy_config",
    "write_envoy_config",
]
