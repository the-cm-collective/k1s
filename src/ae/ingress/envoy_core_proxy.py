"""Envoy core ingress config renderer for edge core-proxy mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CoreProxyRoute:
    host: str
    path_prefix: str
    cluster: str
    redirect_to_https: bool = False
    request_headers_add: list[tuple[str, str]] = field(default_factory=list)
    request_headers_remove: list[str] = field(default_factory=list)
    response_headers_add: list[tuple[str, str]] = field(default_factory=list)
    response_headers_remove: list[str] = field(default_factory=list)
    timeout_ms: int | None = None
    idle_timeout_ms: int | None = None
    ext_authz_enabled: bool = False
    local_rate_limit: dict | None = None
    sticky_cookie_name: str | None = None
    sticky_cookie_ttl_seconds: int | None = None


@dataclass(frozen=True)
class CoreProxyCluster:
    name: str
    endpoints: list[tuple[str, int]] = field(default_factory=list)
    cluster_type: str = "STATIC"
    lb_policy: str = "ROUND_ROBIN"
    use_tls: bool = False
    sni: str | None = None
    ca_cert_path: str | None = None
    expected_sans: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DownstreamTlsCert:
    cert_chain: str
    private_key: str
    server_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EnvoyRenderConfig:
    listen_address: str = "0.0.0.0"
    listen_port: int = 10080
    tls_listen_port: int | None = None
    admin_address: str = "127.0.0.1"
    admin_port: int = 9901
    domain_suffix: str = "edge.local"
    downstream_tls: list[DownstreamTlsCert] = field(default_factory=list)
    tls_fallback_cert: DownstreamTlsCert | None = None


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

    def _build_vhosts(redirect_https: bool) -> list[dict]:
        vhost_list: list[dict] = []
        vhost_map: dict[str, dict] = {}
        for route in sorted(routes, key=lambda r: len(r.path_prefix or ""), reverse=True):
            host = route.host
            if not host:
                continue
            vhost = vhost_map.get(host)
            if vhost is None:
                vhost = {"name": f"vhost_{host}", "domains": [host], "routes": []}
                vhost_map[host] = vhost
            if redirect_https and route.redirect_to_https:
                route_entry = {
                    "match": {"prefix": route.path_prefix or "/"},
                    "redirect": {"https_redirect": True},
                }
                vhost["routes"].append(route_entry)
                continue
            route_entry = {
                "match": {"prefix": route.path_prefix or "/"},
                "route": {"cluster": route.cluster},
            }
            if route.timeout_ms:
                route_entry["route"]["timeout"] = f"{route.timeout_ms/1000:.3f}s"
            if route.idle_timeout_ms:
                route_entry["route"]["idle_timeout"] = f"{route.idle_timeout_ms/1000:.3f}s"
            sticky_cookie_name = str(route.sticky_cookie_name or "").strip()
            if sticky_cookie_name:
                cookie_cfg: dict[str, object] = {
                    "name": sticky_cookie_name,
                    "path": "/",
                }
                if (
                    route.sticky_cookie_ttl_seconds is not None
                    and int(route.sticky_cookie_ttl_seconds) > 0
                ):
                    cookie_cfg["ttl"] = f"{int(route.sticky_cookie_ttl_seconds)}s"
                route_entry["route"]["hash_policy"] = [
                    {"cookie": cookie_cfg, "terminal": True}
                ]
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
                    {"header": {"key": key, "value": value}}
                    for key, value in route.request_headers_add
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
        vhost_list.extend(vhost_map.values())
        if not vhost_list:
            vhost_list.append(
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
        return vhost_list

    vhosts_http = _build_vhosts(redirect_https=True)
    vhosts_https = _build_vhosts(redirect_https=False)

    class _NoAliasDumper(yaml.SafeDumper):
        def ignore_aliases(self, _data):  # type: ignore[override]
            return True

    cluster_defs = []
    for cluster in clusters:
        if not cluster.endpoints:
            continue
        lb_policy = str(cluster.lb_policy or "ROUND_ROBIN").strip().upper()
        if lb_policy not in {"ROUND_ROBIN", "LEAST_REQUEST", "RING_HASH"}:
            lb_policy = "ROUND_ROBIN"
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
            "type": cluster.cluster_type or "STATIC",
            "lb_policy": lb_policy,
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
    http_filters.append(
        {
            "name": "envoy.filters.http.router",
            "typed_config": {
                "@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"
            },
        }
    )

    listeners = [
        {
            "name": "edge_listener_http",
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
                                    "name": "edge_routes_http",
                                    "virtual_hosts": vhosts_http,
                                },
                                "http_filters": http_filters,
                            },
                        }
                    ]
                }
            ],
        }
    ]

    tls_certs = list(config.downstream_tls or [])
    if config.tls_fallback_cert is not None:
        tls_certs.append(config.tls_fallback_cert)

    if config.tls_listen_port and tls_certs:
        tls_filters = [
            {
                "name": "envoy.filters.network.http_connection_manager",
                "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                    "stat_prefix": "edge_ingress_tls",
                    "codec_type": "AUTO",
                    "route_config": {
                        "name": "edge_routes_tls",
                        "virtual_hosts": vhosts_https,
                    },
                    "http_filters": http_filters,
                },
            }
        ]
        tls_chains = []
        for cert in tls_certs:
            chain = {"filters": tls_filters}
            if cert.server_names:
                chain["filter_chain_match"] = {"server_names": list(cert.server_names)}
            chain["transport_socket"] = {
                "name": "envoy.transport_sockets.tls",
                "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext",
                    "common_tls_context": {
                        "alpn_protocols": ["h2", "http/1.1"],
                        "tls_certificates": [
                            {
                                "certificate_chain": {"filename": cert.cert_chain},
                                "private_key": {"filename": cert.private_key},
                            }
                        ]
                    },
                },
            }
            tls_chains.append(chain)
        listeners.append(
            {
                "name": "edge_listener_tls",
                "address": {
                    "socket_address": {
                        "address": listener_addr,
                        "port_value": int(config.tls_listen_port),
                    }
                },
                "filter_chains": tls_chains,
            }
        )

    config_obj = {
        "static_resources": {"listeners": listeners, "clusters": cluster_defs},
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
    return yaml.dump(config_obj, Dumper=_NoAliasDumper, sort_keys=False)


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
    "DownstreamTlsCert",
    "EnvoyRenderConfig",
    "render_envoy_config",
    "write_envoy_config",
]
