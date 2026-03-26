"""Control-plane Envoy renderer for docs/dashboard browser auth."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class ControlPlaneEnvoyConfig:
    listen_address: str = "127.0.0.1"
    listen_port: int = 10081
    admin_address: str = "127.0.0.1"
    admin_port: int = 9902
    dash_host: str = "dash.home.arpa"
    docs_host: str = "docs.home.arpa"
    controller_addr: str = "127.0.0.1"
    controller_port: int = 9108
    docs_addr: str = "127.0.0.1"
    docs_port: int = 9109
    read_token: str | None = None
    auth_enabled: bool = False
    authentik_base_url: str = ""
    authentik_provider_slug: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str | None = None
    oauth_hmac_secret: str | None = None
    auth_scopes: tuple[str, ...] = ("openid", "profile", "email", "offline_access")


def build_control_plane_envoy_config_from_env() -> ControlPlaneEnvoyConfig:
    auth_enabled = _truthy_env("AE_CONTROLPLANE_AUTH_ENABLE")
    client_secret = None
    hmac_secret = None
    if auth_enabled:
        client_secret = _read_secret_file("AE_AUTHENTIK_CLIENT_SECRET_FILE")
        hmac_secret = _read_secret_file("AE_AUTHENTIK_HMAC_SECRET_FILE")
    return ControlPlaneEnvoyConfig(
        listen_address=str(os_env("AE_CONTROLPLANE_PROXY_ADDR", "127.0.0.1")).strip(),
        listen_port=int(os_env("AE_CONTROLPLANE_PROXY_PORT", "10081") or "10081"),
        admin_address=str(os_env("AE_CONTROLPLANE_PROXY_ADMIN_ADDR", "127.0.0.1")).strip(),
        admin_port=int(os_env("AE_CONTROLPLANE_PROXY_ADMIN_PORT", "9902") or "9902"),
        dash_host=str(os_env("AE_CONTROLPLANE_DASH_HOST", "dash.home.arpa")).strip().lower(),
        docs_host=str(os_env("AE_CONTROLPLANE_DOCS_HOST", "docs.home.arpa")).strip().lower(),
        controller_addr=_upstream_host("AE_CONTROLPLANE_CONTROLLER_UPSTREAM", "127.0.0.1"),
        controller_port=_upstream_port("AE_CONTROLPLANE_CONTROLLER_UPSTREAM", 9108),
        docs_addr=_upstream_host("AE_CONTROLPLANE_DOCS_UPSTREAM", "127.0.0.1"),
        docs_port=_upstream_port("AE_CONTROLPLANE_DOCS_UPSTREAM", 9109),
        read_token=str(os_env("AE_API_READ_TOKEN", "") or "").strip() or None,
        auth_enabled=auth_enabled,
        authentik_base_url=str(os_env("AE_AUTHENTIK_BASE_URL", "") or "").strip().rstrip("/"),
        authentik_provider_slug=str(os_env("AE_AUTHENTIK_PROVIDER_SLUG", "") or "").strip(),
        oauth_client_id=str(os_env("AE_AUTHENTIK_CLIENT_ID", "") or "").strip(),
        oauth_client_secret=client_secret,
        oauth_hmac_secret=hmac_secret,
        auth_scopes=tuple(_auth_scopes_from_env()),
    )


def render_control_plane_envoy_config(
    config: ControlPlaneEnvoyConfig,
    *,
    secrets_path: str | None = None,
) -> str:
    if config.auth_enabled:
        _validate_oauth_config(config, secrets_path)

    class _NoAliasDumper(yaml.SafeDumper):
        def ignore_aliases(self, _data):  # type: ignore[override]
            return True

    def _domains_for_host(host: str, port: int) -> list[str]:
        domains = [str(host)]
        alias = f"{host}:{int(port)}"
        if alias not in domains:
            domains.append(alias)
        return domains

    controller_cluster = {
        "name": "controller_http",
        "connect_timeout": "2s",
        "type": "STATIC",
        "lb_policy": "ROUND_ROBIN",
        "load_assignment": {
            "cluster_name": "controller_http",
            "endpoints": [
                {
                    "lb_endpoints": [
                        {
                            "endpoint": {
                                "address": {
                                    "socket_address": {
                                        "address": str(config.controller_addr),
                                        "port_value": int(config.controller_port),
                                    }
                                }
                            }
                        }
                    ]
                }
            ],
        },
    }
    docs_cluster = {
        "name": "docs_http",
        "connect_timeout": "2s",
        "type": "STATIC",
        "lb_policy": "ROUND_ROBIN",
        "load_assignment": {
            "cluster_name": "docs_http",
            "endpoints": [
                {
                    "lb_endpoints": [
                        {
                            "endpoint": {
                                "address": {
                                    "socket_address": {
                                        "address": str(config.docs_addr),
                                        "port_value": int(config.docs_port),
                                    }
                                }
                            }
                        }
                    ]
                }
            ],
        },
    }
    clusters = [controller_cluster, docs_cluster]
    if config.auth_enabled:
        base = urlparse(config.authentik_base_url)
        auth_host = str(base.hostname or "").strip()
        auth_port = base.port or 443
        auth_cluster = {
            "name": "authentik_https",
            "connect_timeout": "5s",
            "type": "STRICT_DNS",
            "lb_policy": "ROUND_ROBIN",
            "load_assignment": {
                "cluster_name": "authentik_https",
                "endpoints": [
                    {
                        "lb_endpoints": [
                            {
                                "endpoint": {
                                    "address": {
                                        "socket_address": {
                                            "address": auth_host,
                                            "port_value": int(auth_port),
                                        }
                                    }
                                }
                            }
                        ]
                    }
                ],
            },
            "transport_socket": {
                "name": "envoy.transport_sockets.tls",
                "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext",
                    "sni": auth_host,
                },
            },
        }
        clusters.append(auth_cluster)

    docs_routes = [
        _route(
            "/labs",
            "controller_http",
            timeout="0s",
            idle_timeout="0s",
        ),
    ]
    for prefix in (
        "/swagger",
        "/redoc",
        "/openapi",
        "/health",
        "/status",
        "/events",
        "/logs",
        "/metrics",
        "/system",
        "/ui/features",
    ):
        docs_routes.append(
            _route(
                prefix,
                "controller_http",
                timeout="0s" if prefix in {"/logs"} else None,
                idle_timeout="0s" if prefix in {"/logs"} else None,
                add_auth=config.read_token,
            )
        )
    docs_routes.extend(
        [
            _route("/callback", "docs_http"),
            _route("/signout", "docs_http"),
            _route("/", "docs_http"),
        ]
    )

    dash_routes = [
        _direct_response("/api/apishim/session", 404),
        _route("/callback", "controller_http", add_auth=config.read_token),
        _route("/signout", "controller_http", add_auth=config.read_token),
        _route("/", "controller_http", timeout="0s", idle_timeout="0s", add_auth=config.read_token),
    ]

    http_filters: list[dict] = []
    if config.auth_enabled:
        http_filters.append(
            {
                "name": "envoy.filters.http.csrf",
                "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.filters.http.csrf.v3.CsrfPolicy",
                    "filter_enabled": {
                        "default_value": {"numerator": 100, "denominator": "HUNDRED"}
                    },
                    "shadow_enabled": {"default_value": {"numerator": 0, "denominator": "HUNDRED"}},
                },
            }
        )
        http_filters.append(
            {
                "name": "envoy.filters.http.oauth2",
                "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.filters.http.oauth2.v3.OAuth2",
                    "config": {
                        "token_endpoint": {
                            "cluster": "authentik_https",
                            "uri": _auth_token_endpoint_uri(config.authentik_base_url),
                            "timeout": "5s",
                        },
                        "authorization_endpoint": _auth_authorization_endpoint(
                            config.authentik_base_url
                        ),
                        "redirect_uri": "%REQ(x-forwarded-proto)%://%REQ(:authority)%/callback",
                        "redirect_path_matcher": {"path": {"exact": "/callback"}},
                        "signout_path": {"path": {"exact": "/signout"}},
                        "credentials": {
                            "client_id": config.oauth_client_id,
                            "token_secret": {
                                "name": "token",
                                "sds_config": {"path": str(secrets_path)},
                            },
                            "hmac_secret": {
                                "name": "hmac",
                                "sds_config": {"path": str(secrets_path)},
                            },
                        },
                        "auth_scopes": list(config.auth_scopes),
                        "use_refresh_token": True,
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

    config_obj = {
        "static_resources": {
            "listeners": [
                {
                    "name": "controlplane_http",
                    "address": {
                        "socket_address": {
                            "address": str(config.listen_address),
                            "port_value": int(config.listen_port),
                        }
                    },
                    "filter_chains": [
                        {
                            "filters": [
                                {
                                    "name": "envoy.filters.network.http_connection_manager",
                                    "typed_config": {
                                        "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                                        "stat_prefix": "controlplane_http",
                                        "route_config": {
                                            "name": "controlplane_routes",
                                            "virtual_hosts": [
                                                {
                                                    "name": "docs_host",
                                                    "domains": _domains_for_host(
                                                        config.docs_host, config.listen_port
                                                    ),
                                                    "routes": docs_routes,
                                                },
                                                {
                                                    "name": "dash_host",
                                                    "domains": _domains_for_host(
                                                        config.dash_host, config.listen_port
                                                    ),
                                                    "routes": dash_routes,
                                                },
                                            ],
                                        },
                                        "http_filters": http_filters,
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
            "access_log_path": "/tmp/controlplane_envoy_admin_access.log",
            "address": {
                "socket_address": {
                    "address": str(config.admin_address),
                    "port_value": int(config.admin_port),
                }
            },
        },
    }
    return yaml.dump(config_obj, Dumper=_NoAliasDumper, sort_keys=False)


def render_control_plane_envoy_secrets(config: ControlPlaneEnvoyConfig) -> str:
    if not config.auth_enabled:
        return ""
    if not config.oauth_client_secret or not config.oauth_hmac_secret:
        raise ValueError("oauth client secret and hmac secret are required when auth is enabled")

    class _NoAliasDumper(yaml.SafeDumper):
        def ignore_aliases(self, _data):  # type: ignore[override]
            return True

    config_obj = {
        "static_resources": {
            "secrets": [
                {"name": "token", "generic_secret": {"secret": str(config.oauth_client_secret)}},
                {"name": "hmac", "generic_secret": {"secret": str(config.oauth_hmac_secret)}},
            ]
        }
    }
    return yaml.dump(config_obj, Dumper=_NoAliasDumper, sort_keys=False)


def write_control_plane_envoy_bundle(
    config_path: Path,
    secrets_path: Path,
    config: ControlPlaneEnvoyConfig,
) -> tuple[str, str]:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    config_text = render_control_plane_envoy_config(config, secrets_path=str(secrets_path))
    secrets_text = render_control_plane_envoy_secrets(config)
    config_path.write_text(config_text, encoding="utf-8")
    secrets_path.write_text(secrets_text, encoding="utf-8")
    return config_text, secrets_text


def _route(
    prefix: str,
    cluster: str,
    *,
    add_auth: str | None = None,
    timeout: str | None = None,
    idle_timeout: str | None = None,
) -> dict:
    route_entry: dict[str, object] = {
        "match": {"prefix": prefix},
        "route": {"cluster": cluster},
    }
    if timeout is not None:
        route_entry["route"]["timeout"] = timeout
    if idle_timeout is not None:
        route_entry["route"]["idle_timeout"] = idle_timeout
    if add_auth:
        route_entry["request_headers_to_add"] = [
            {"header": {"key": "Authorization", "value": f"Bearer {add_auth}"}}
        ]
    return route_entry


def _direct_response(prefix: str, status: int) -> dict:
    return {
        "match": {"prefix": prefix},
        "direct_response": {"status": int(status)},
    }


def _validate_oauth_config(config: ControlPlaneEnvoyConfig, secrets_path: str | None) -> None:
    if not config.authentik_base_url:
        raise ValueError("AE_AUTHENTIK_BASE_URL is required when control-plane auth is enabled")
    if not config.oauth_client_id:
        raise ValueError("AE_AUTHENTIK_CLIENT_ID is required when control-plane auth is enabled")
    if not config.oauth_client_secret:
        raise ValueError(
            "AE_AUTHENTIK_CLIENT_SECRET_FILE is required when control-plane auth is enabled"
        )
    if not config.oauth_hmac_secret:
        raise ValueError(
            "AE_AUTHENTIK_HMAC_SECRET_FILE is required when control-plane auth is enabled"
        )
    if not secrets_path:
        raise ValueError("control-plane envoy secrets path is required when auth is enabled")


def _auth_authorization_endpoint(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/application/o/authorize/"


def _auth_token_endpoint_uri(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = str(parsed.hostname or "").strip()
    if not host:
        raise ValueError("invalid AE_AUTHENTIK_BASE_URL")
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{host}/application/o/token/"


def _auth_scopes_from_env() -> list[str]:
    raw = str(os_env("AE_AUTHENTIK_SCOPES", "") or "").strip()
    if not raw:
        return ["openid", "profile", "email", "offline_access"]
    scopes = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    return scopes or ["openid", "profile", "email", "offline_access"]


def _upstream_host(env_name: str, default_host: str) -> str:
    parsed = _parse_env_upstream(env_name)
    if parsed is None:
        return default_host
    return parsed[0]


def _upstream_port(env_name: str, default_port: int) -> int:
    parsed = _parse_env_upstream(env_name)
    if parsed is None:
        return int(default_port)
    return parsed[1]


def _parse_env_upstream(env_name: str) -> tuple[str, int] | None:
    raw = str(os_env(env_name, "") or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = str(parsed.hostname or "").strip()
    if not host:
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    return host, int(parsed.port or default_port)


def _read_secret_file(env_name: str) -> str:
    raw_path = str(os_env(env_name, "") or "").strip()
    if not raw_path:
        raise ValueError(f"{env_name} is required when control-plane auth is enabled")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"secret file not found: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file is empty: {path}")
    return value


def _truthy_env(name: str) -> bool:
    return str(os_env(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def os_env(name: str, default: str) -> str:
    import os

    return str(os.getenv(name, default) or default)


__all__ = [
    "ControlPlaneEnvoyConfig",
    "build_control_plane_envoy_config_from_env",
    "render_control_plane_envoy_config",
    "render_control_plane_envoy_secrets",
    "write_control_plane_envoy_bundle",
]
