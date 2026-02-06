"""Envoy core ingress config renderer for edge core-proxy mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CoreProxyRoute:
    host: str
    path_prefix: str
    cluster: str
    request_headers_add: list[tuple[str, str]] = field(default_factory=list)
    request_headers_remove: list[str] = field(default_factory=list)
    response_headers_add: list[tuple[str, str]] = field(default_factory=list)
    response_headers_remove: list[str] = field(default_factory=list)
    timeout_ms: int | None = None
    idle_timeout_ms: int | None = None
    ext_authz_enabled: bool = False
    local_rate_limit: dict | None = None


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
    *,
    ext_authz_config: dict | None = None,
    enable_local_ratelimit: bool = False,
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
        route_entry = {
            "match": {"prefix": route.path_prefix or "/"},
            "route": {"cluster": route.cluster},
        }
        if route.timeout_ms:
            route_entry["route"]["timeout"] = f"{route.timeout_ms/1000:.3f}s"
        if route.idle_timeout_ms:
            route_entry["route"]["idle_timeout"] = f"{route.idle_timeout_ms/1000:.3f}s"
        per_filter: dict[str, dict] = {}
        if ext_authz_config is not None:
            per_filter["envoy.filters.http.ext_authz"] = {
                "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthzPerRoute",
                "disabled": not bool(route.ext_authz_enabled),
            }
        if route.local_rate_limit:
            per_filter["envoy.filters.http.local_ratelimit"] = {
                "@type": "type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit",
                "stat_prefix": "edge_local_ratelimit",
                "token_bucket": route.local_rate_limit,
                "filter_enabled": {
                    "default_value": {"numerator": 100, "denominator": "HUNDRED"}
                },
                "filter_enforced": {
                    "default_value": {"numerator": 100, "denominator": "HUNDRED"}
                },
            }
        if per_filter:
            route_entry["typed_per_filter_config"] = per_filter
        if route.request_headers_add:
            route_entry["request_headers_to_add"] = [
                {"header": {"key": key, "value": value}} for key, value in route.request_headers_add
            ]
        if route.request_headers_remove:
            route_entry["request_headers_to_remove"] = route.request_headers_remove
        if route.response_headers_add:
            route_entry["response_headers_to_add"] = [
                {"header": {"key": key, "value": value}}
                for key, value in route.response_headers_add
            ]
        if route.response_headers_remove:
            route_entry["response_headers_to_remove"] = route.response_headers_remove
        vhost["routes"].append(route_entry)
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

    http_filters = []
    if ext_authz_config is not None:
        http_filters.append(
            {
                "name": "envoy.filters.http.ext_authz",
                "typed_config": ext_authz_config,
            }
        )
    if enable_local_ratelimit:
        http_filters.append(
            {
                "name": "envoy.filters.http.local_ratelimit",
                "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit",
                    "stat_prefix": "edge_local_ratelimit",
                    "token_bucket": {
                        "max_tokens": 1000000,
                        "tokens_per_fill": 1000000,
                        "fill_interval": "1s",
                    },
                    "filter_enabled": {
                        "default_value": {"numerator": 0, "denominator": "HUNDRED"}
                    },
                    "filter_enforced": {
                        "default_value": {"numerator": 0, "denominator": "HUNDRED"}
                    },
                },
            }
        )
    http_filters.append({"name": "envoy.filters.http.router"})

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
                                        "http_filters": http_filters,
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
    *,
    ext_authz_config: dict | None = None,
    enable_local_ratelimit: bool = False,
) -> str:
    content = render_envoy_config(
        routes,
        clusters,
        config,
        ext_authz_config=ext_authz_config,
        enable_local_ratelimit=enable_local_ratelimit,
    )
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
