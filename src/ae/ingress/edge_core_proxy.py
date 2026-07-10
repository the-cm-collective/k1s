"""Core-proxy config renderer for Envoy + Rathole."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ae.controller.spec import app_key
from ae.controller.state import SiteIngressListItem, SQLiteStateStore
from ae.ingress.edge_docs import normalize_route_doc
from ae.ingress.envoy_core_proxy import (
    CoreProxyCluster,
    CoreProxyRoute,
    DownstreamTlsCert,
    EnvoyRenderConfig,
    write_envoy_config,
)
from ae.ingress.rathole import (
    RatholeClientConfig,
    RatholeClientService,
    RatholeServerConfig,
    RatholeServerService,
    write_rathole_client,
    write_rathole_server,
)
from ae.ingress.tls_sync import TlsSecretResolver

LOGGER = logging.getLogger(__name__)
_ALLOWED_CLUSTER_LB_POLICIES = {"ROUND_ROBIN", "LEAST_REQUEST", "RING_HASH"}


@dataclass(frozen=True)
class EdgeCoreProxyConfig:
    config_dir: Path
    envoy_config_path: Path
    rathole_server_path: Path
    rathole_client_dir: Path | None
    site_domain_suffix: str
    http_listen_port: int
    tls_listen_port: int | None
    tls_root: Path
    tls_default_secret: str | None
    tls_fallback: bool
    tls_fallback_cn: str
    tls_fallback_days: int
    rathole_bind_addr: str
    rathole_default_token: str
    rathole_server_addr: str
    edge_local_addr: str
    reload_cmd: str | None
    tls_fallback_sans: tuple[str, ...] = ()
    rathole_reload_cmd: str | None = None
    rathole_reload_enabled: bool = False
    controlplane_public_enable: bool = False
    controlplane_proxy_addr: str = "127.0.0.1"
    controlplane_proxy_port: int = 10081
    controlplane_dash_host: str = "dash.home.arpa"
    controlplane_docs_host: str = "docs.home.arpa"
    controlplane_api_host: str = "api.home.arpa"
    controlplane_api_controller_addr: str = "127.0.0.1"
    controlplane_api_controller_port: int = 9108
    controlplane_api_apishim_addr: str = "127.0.0.1"
    controlplane_api_apishim_port: int = 8445
    controlplane_api_apishim_use_tls: bool = True


class EdgeCoreProxyRenderer:
    def __init__(self, store: SQLiteStateStore, config: EdgeCoreProxyConfig) -> None:
        self._store = store
        self._config = config
        self._last_envoy_hash: str | None = None
        self._last_rathole_hash: str | None = None
        self._lock = threading.Lock()

    def render(self) -> None:
        with self._lock:
            self._render_locked()

    def _render_locked(self) -> None:
        endpoints = self._store.list_site_ingress_endpoints()
        (
            routes,
            clusters,
            ext_authz_config,
            enable_local_ratelimit,
            downstream_tls,
            fallback_tls,
        ) = _build_routes_and_clusters(self._store, endpoints, self._config)
        tls_port = self._config.tls_listen_port if (downstream_tls or fallback_tls) else None
        envoy_cfg = EnvoyRenderConfig(
            domain_suffix=self._config.site_domain_suffix,
            listen_port=self._config.http_listen_port,
            tls_listen_port=tls_port,
            downstream_tls=downstream_tls,
            tls_fallback_cert=fallback_tls,
        )
        envoy_text = write_envoy_config(
            self._config.envoy_config_path,
            routes,
            clusters,
            envoy_cfg,
            ext_authz_config=ext_authz_config,
            enable_local_ratelimit=enable_local_ratelimit,
        )
        rathole_text = write_rathole_server(
            self._config.rathole_server_path,
            RatholeServerConfig(
                bind_addr=self._config.rathole_bind_addr,
                default_token=self._config.rathole_default_token,
                services=[
                    RatholeServerService(
                        name=route.site_id,
                        bind_addr=f"0.0.0.0:{route.core_proxy_port}",
                    )
                    for route in _core_proxy_services(endpoints)
                ],
            ),
        )
        if self._config.rathole_client_dir:
            for route in _core_proxy_services(endpoints):
                client_cfg = RatholeClientConfig(
                    remote_addr=self._config.rathole_server_addr,
                    default_token=self._config.rathole_default_token,
                    services=[
                        RatholeClientService(
                            name=route.site_id, local_addr=self._config.edge_local_addr
                        )
                    ],
                )
                out = self._config.rathole_client_dir / f"rathole-client-{route.site_id}.toml"
                write_rathole_client(out, client_cfg)
        envoy_digest = hashlib.sha256(envoy_text.encode("utf-8")).hexdigest()
        rathole_digest = hashlib.sha256(rathole_text.encode("utf-8")).hexdigest()
        envoy_changed = self._last_envoy_hash != envoy_digest
        rathole_changed = self._last_rathole_hash != rathole_digest
        if not envoy_changed and not rathole_changed:
            return
        self._last_envoy_hash = envoy_digest
        self._last_rathole_hash = rathole_digest
        if envoy_changed and self._config.reload_cmd:
            _run_reload(self._config.reload_cmd)
        if (
            rathole_changed
            and self._config.rathole_reload_enabled
            and self._config.rathole_reload_cmd
        ):
            _run_reload(self._config.rathole_reload_cmd)


def build_core_proxy_config() -> EdgeCoreProxyConfig | None:
    raw_dir = os.getenv("AE_EDGE_INGRESS_CONFIG_DIR")
    if not raw_dir:
        return None
    config_dir = Path(raw_dir)
    envoy_path = Path(os.getenv("AE_EDGE_INGRESS_ENVOY_CONFIG", config_dir / "envoy.yaml"))
    rathole_server = Path(os.getenv("AE_RATHOLE_SERVER_CONFIG", config_dir / "rathole-server.toml"))
    client_dir_raw = os.getenv("AE_RATHOLE_CLIENT_DIR")
    client_dir = Path(client_dir_raw) if client_dir_raw else None
    site_suffix = os.getenv("AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX", "edge.local")
    rathole_bind = os.getenv("AE_RATHOLE_BIND_ADDR", "0.0.0.0:2333")
    rathole_token = os.getenv("AE_RATHOLE_DEFAULT_TOKEN", "dev")
    rathole_server_addr = os.getenv("AE_RATHOLE_SERVER_ADDR", "127.0.0.1:2333")
    edge_local_addr = os.getenv("AE_EDGE_INGRESS_LOCAL_ADDR", "127.0.0.1:18081")
    reload_cmd = os.getenv("AE_EDGE_INGRESS_RELOAD_CMD")
    rathole_reload_cmd = os.getenv("AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD")
    rathole_reload_enabled = str(
        os.getenv("AE_EDGE_INGRESS_RATHOLE_RELOAD", "1") or "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    tls_root = Path(os.getenv("AE_TLS_DIR", "state/tls")).expanduser()
    if not tls_root.is_absolute():
        tls_root = (Path.cwd() / tls_root).resolve()
    else:
        tls_root = tls_root.resolve()
    tls_default_secret = os.getenv("AE_EDGE_INGRESS_TLS_DEFAULT_SECRET") or None
    tls_fallback = str(os.getenv("AE_EDGE_INGRESS_TLS_FALLBACK", "1") or "1").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    tls_fallback_cn = os.getenv("AE_EDGE_INGRESS_TLS_FALLBACK_CN", "edge.local")
    try:
        tls_fallback_days = int(os.getenv("AE_EDGE_INGRESS_TLS_FALLBACK_DAYS", "7") or 7)
    except Exception:
        tls_fallback_days = 7
    controlplane_public_enable = str(
        os.getenv("AE_CONTROLPLANE_PUBLIC_ENABLE", "0") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    controlplane_dash_host = (
        str(os.getenv("AE_CONTROLPLANE_DASH_HOST", "dash.home.arpa") or "").strip().lower()
    )
    controlplane_docs_host = (
        str(os.getenv("AE_CONTROLPLANE_DOCS_HOST", "docs.home.arpa") or "").strip().lower()
    )
    controlplane_api_host = (
        str(os.getenv("AE_CONTROLPLANE_API_HOST", "api.home.arpa") or "").strip().lower()
    )
    controlplane_proxy_addr = str(
        os.getenv("AE_CONTROLPLANE_PROXY_ADDR", "127.0.0.1") or "127.0.0.1"
    ).strip()
    try:
        controlplane_proxy_port = int(os.getenv("AE_CONTROLPLANE_PROXY_PORT", "10081") or "10081")
    except Exception:
        controlplane_proxy_port = 10081
    controller_upstream = _parse_upstream(
        os.getenv("AE_CONTROLPLANE_API_CONTROLLER_UPSTREAM")
        or os.getenv("AE_CONTROLPLANE_CONTROLLER_UPSTREAM"),
        default_host="127.0.0.1",
        default_port=9108,
    )
    apishim_upstream = _parse_upstream(
        os.getenv("AE_CONTROLPLANE_API_APISHIM_UPSTREAM")
        or os.getenv("AE_CONTROLPLANE_APISHIM_UPSTREAM"),
        default_host="127.0.0.1",
        default_port=8445,
    )
    controlplane_apishim_use_tls = str(
        os.getenv("AE_CONTROLPLANE_API_APISHIM_TLS")
        or os.getenv("AE_CONTROLPLANE_APISHIM_TLS", "1")
        or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    fallback_sans = tuple(
        host
        for host in (
            controlplane_dash_host,
            controlplane_docs_host,
            controlplane_api_host,
        )
        if host
    )
    try:
        http_listen = int(os.getenv("AE_EDGE_INGRESS_HTTP_PORT", "10080") or 10080)
    except Exception:
        http_listen = 10080
    tls_port_raw = os.getenv("AE_EDGE_INGRESS_TLS_PORT", "").strip()
    try:
        tls_listen = int(tls_port_raw) if tls_port_raw else None
    except Exception:
        tls_listen = None
    return EdgeCoreProxyConfig(
        config_dir=config_dir,
        envoy_config_path=envoy_path if isinstance(envoy_path, Path) else Path(envoy_path),
        rathole_server_path=rathole_server,
        rathole_client_dir=client_dir,
        site_domain_suffix=site_suffix,
        http_listen_port=http_listen,
        tls_listen_port=tls_listen,
        tls_root=tls_root,
        tls_default_secret=tls_default_secret,
        tls_fallback=tls_fallback,
        tls_fallback_cn=tls_fallback_cn,
        tls_fallback_days=tls_fallback_days,
        tls_fallback_sans=fallback_sans,
        rathole_bind_addr=rathole_bind,
        rathole_default_token=rathole_token,
        rathole_server_addr=rathole_server_addr,
        edge_local_addr=edge_local_addr,
        reload_cmd=reload_cmd,
        rathole_reload_cmd=rathole_reload_cmd,
        rathole_reload_enabled=rathole_reload_enabled,
        controlplane_public_enable=controlplane_public_enable,
        controlplane_proxy_addr=controlplane_proxy_addr,
        controlplane_proxy_port=controlplane_proxy_port,
        controlplane_dash_host=controlplane_dash_host,
        controlplane_docs_host=controlplane_docs_host,
        controlplane_api_host=controlplane_api_host,
        controlplane_api_controller_addr=controller_upstream[0],
        controlplane_api_controller_port=controller_upstream[1],
        controlplane_api_apishim_addr=apishim_upstream[0],
        controlplane_api_apishim_port=apishim_upstream[1],
        controlplane_api_apishim_use_tls=controlplane_apishim_use_tls,
    )


def render_core_proxy_bootstrap_from_env(*, state_db_path: Path | None = None) -> bool:
    config = build_core_proxy_config()
    if config is None:
        return False
    bootstrap_db = Path(state_db_path) if state_db_path is not None else (
        config.config_dir / "bootstrap-state.db"
    )
    bootstrap_db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStateStore(db_path=bootstrap_db)
    EdgeCoreProxyRenderer(store, config).render()
    return True


def _parse_upstream(
    raw_value: str | None,
    *,
    default_host: str,
    default_port: int,
) -> tuple[str, int]:
    raw = str(raw_value or "").strip()
    if not raw:
        return default_host, int(default_port)
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = str(parsed.hostname or "").strip() or default_host
    port = parsed.port or int(default_port)
    return host, int(port)


def _core_proxy_services(
    endpoints: list[SiteIngressListItem],
) -> list[SiteIngressListItem]:
    return [ep for ep in endpoints if ep.core_proxy_port is not None]


def _reserved_controlplane_hosts(config: EdgeCoreProxyConfig) -> set[str]:
    if not config.controlplane_public_enable:
        return set()
    return {
        host
        for host in {
            str(config.controlplane_dash_host or "").strip().lower(),
            str(config.controlplane_docs_host or "").strip().lower(),
            str(config.controlplane_api_host or "").strip().lower(),
        }
        if host
    }


def _append_controlplane_public_routes(
    routes: list[CoreProxyRoute],
    clusters: dict[str, CoreProxyCluster],
    config: EdgeCoreProxyConfig,
    *,
    tls_enabled: bool,
) -> None:
    if not config.controlplane_public_enable:
        return

    redirect_https = bool(tls_enabled)
    forward_proto = "https" if tls_enabled else "http"
    proxy_cluster_name = "controlplane_proxy"
    clusters.setdefault(
        proxy_cluster_name,
        CoreProxyCluster(
            name=proxy_cluster_name,
            endpoints=[(config.controlplane_proxy_addr, int(config.controlplane_proxy_port))],
        ),
    )
    proxy_headers = [("x-forwarded-proto", forward_proto)]
    for host in (config.controlplane_dash_host, config.controlplane_docs_host):
        clean_host = str(host or "").strip().lower()
        if not clean_host:
            continue
        routes.append(
            CoreProxyRoute(
                host=clean_host,
                path_prefix="/",
                cluster=proxy_cluster_name,
                redirect_to_https=redirect_https,
                request_headers_add=proxy_headers,
            )
        )

    api_host = str(config.controlplane_api_host or "").strip().lower()
    if not api_host:
        return

    controller_cluster_name = "controlplane_api_controller"
    apishim_cluster_name = "controlplane_api_apishim"
    clusters.setdefault(
        controller_cluster_name,
        CoreProxyCluster(
            name=controller_cluster_name,
            endpoints=[
                (
                    str(config.controlplane_api_controller_addr or "127.0.0.1"),
                    int(config.controlplane_api_controller_port),
                )
            ],
        ),
    )
    clusters.setdefault(
        apishim_cluster_name,
        CoreProxyCluster(
            name=apishim_cluster_name,
            endpoints=[
                (
                    str(config.controlplane_api_apishim_addr or "127.0.0.1"),
                    int(config.controlplane_api_apishim_port),
                )
            ],
            use_tls=bool(config.controlplane_api_apishim_use_tls),
        ),
    )
    routes.extend(
        [
            CoreProxyRoute(
                host=api_host,
                path_prefix="/dashboard",
                cluster=controller_cluster_name,
                redirect_to_https=redirect_https,
                direct_response_status=404,
            ),
            CoreProxyRoute(
                host=api_host,
                path_prefix="/playground",
                cluster=controller_cluster_name,
                redirect_to_https=redirect_https,
                direct_response_status=404,
            ),
        ]
    )
    for prefix in (
        "/swagger",
        "/redoc",
        "/openapi",
        "/openapi.json",
        "/swagger.json",
        "/system",
        "/health",
        "/status",
        "/events",
        "/logs",
        "/metrics",
        "/ui/features",
    ):
        routes.append(
            CoreProxyRoute(
                host=api_host,
                path_prefix=prefix,
                cluster=controller_cluster_name,
                redirect_to_https=redirect_https,
            )
        )
    routes.extend(
        [
            CoreProxyRoute(
                host=api_host,
                path_prefix="/api/v1",
                cluster=apishim_cluster_name,
                redirect_to_https=redirect_https,
            ),
            CoreProxyRoute(
                host=api_host,
                path_prefix="/apis",
                cluster=apishim_cluster_name,
                redirect_to_https=redirect_https,
            ),
            CoreProxyRoute(
                host=api_host,
                path_prefix="/",
                cluster=controller_cluster_name,
                redirect_to_https=redirect_https,
            ),
        ]
    )


def _build_routes_and_clusters(
    store: SQLiteStateStore,
    endpoints: list[SiteIngressListItem],
    config: EdgeCoreProxyConfig,
) -> tuple[
    list[CoreProxyRoute],
    list[CoreProxyCluster],
    dict | None,
    bool,
    list[DownstreamTlsCert],
    DownstreamTlsCert | None,
]:
    routes: list[CoreProxyRoute] = []
    clusters: dict[str, CoreProxyCluster] = {}
    endpoint_map = {ep.site_id: ep for ep in endpoints}
    domain_suffix = config.site_domain_suffix.strip(".") or "edge.local"
    policy_cache: dict[tuple[str, str], dict] = {}
    forward_auth_url = _select_forward_auth_url(store, policy_cache)
    enable_local_ratelimit = False
    route_records = store.list_edge_ingress_routes()
    reserved_hosts = _reserved_controlplane_hosts(config)
    downstream_tls, fallback_tls = _collect_downstream_tls(
        route_records,
        config,
        force_tls=bool(config.controlplane_public_enable),
    )
    tls_enabled = bool(downstream_tls or fallback_tls)

    # Base per-site host for core-proxy mode.
    for ep in endpoints:
        if ep.core_proxy_port is None:
            continue
        cluster_name = f"site_{ep.site_id}"
        clusters.setdefault(
            cluster_name,
            CoreProxyCluster(
                name=cluster_name,
                endpoints=[("127.0.0.1", int(ep.core_proxy_port))],
            ),
        )
        routes.append(
            CoreProxyRoute(
                host=f"{ep.site_id}.{domain_suffix}",
                path_prefix="/",
                cluster=cluster_name,
                ext_authz_enabled=False,
                local_rate_limit=None,
            )
        )

    # Explicit EdgeIngressRoute resources.
    for record in route_records:
        spec = _edge_route_spec(record)
        host = str(spec.get("host") or "").strip().lower()
        if not host:
            continue
        if host in reserved_hosts:
            continue
        exposure = spec.get("exposure") if isinstance(spec.get("exposure"), dict) else {}
        mode = str(exposure.get("mode") or "").strip().lower()
        placement = exposure.get("placement") if isinstance(exposure.get("placement"), dict) else {}
        site_id = str(placement.get("site") or record.site_id or "").strip()
        policy_spec = _policy_for_route(record, store, policy_cache)
        route_opts = _policy_route_options(policy_spec) if policy_spec else {}
        local_rate_limit = route_opts.get("local_rate_limit")
        cluster_lb_policy = _route_lb_policy(route_opts)
        sticky_cookie_name, sticky_cookie_ttl_seconds = _route_sticky_cookie(route_opts)
        websocket_enabled = route_opts.get("websocket_enabled")
        websocket_idle_timeout_ms = route_opts.get("websocket_idle_timeout_ms")
        websocket_max_connection_duration_ms = route_opts.get(
            "websocket_max_connection_duration_ms"
        )
        if local_rate_limit:
            enable_local_ratelimit = True
        ext_authz_enabled = False
        if forward_auth_url and policy_spec:
            route_forward_auth = _policy_forward_auth_url(policy_spec)
            ext_authz_enabled = bool(route_forward_auth and route_forward_auth == forward_auth_url)
        redirect_https = False
        if tls_enabled:
            redirect_https = _route_redirect_https(exposure)
        if mode == "core-proxy":
            if not site_id:
                continue
            ep = endpoint_map.get(site_id)
            if ep is None or ep.core_proxy_port is None:
                continue
            cluster_name = _cluster_name_with_lb_policy(f"site_{site_id}", route_opts)
            clusters.setdefault(
                cluster_name,
                CoreProxyCluster(
                    name=cluster_name,
                    endpoints=[("127.0.0.1", int(ep.core_proxy_port))],
                    lb_policy=cluster_lb_policy,
                ),
            )
            for entry in _route_path_entries(spec):
                routes.append(
                    CoreProxyRoute(
                        host=host,
                        path_prefix=entry["path"],
                        cluster=cluster_name,
                        redirect_to_https=redirect_https,
                        request_headers_add=route_opts.get("request_headers_add", []),
                        request_headers_remove=route_opts.get("request_headers_remove", []),
                        response_headers_add=route_opts.get("response_headers_add", []),
                        response_headers_remove=route_opts.get("response_headers_remove", []),
                        timeout_ms=route_opts.get("timeout_ms"),
                        idle_timeout_ms=route_opts.get("idle_timeout_ms"),
                        ext_authz_enabled=ext_authz_enabled,
                        local_rate_limit=local_rate_limit,
                        sticky_cookie_name=sticky_cookie_name,
                        sticky_cookie_ttl_seconds=sticky_cookie_ttl_seconds,
                        websocket_enabled=websocket_enabled,
                        websocket_idle_timeout_ms=websocket_idle_timeout_ms,
                        websocket_max_connection_duration_ms=websocket_max_connection_duration_ms,
                    )
                )
        elif mode == "core-to-edge-public":
            if not site_id:
                continue
            ep = endpoint_map.get(site_id)
            public = _public_endpoint(ep.public_urls if ep else [])
            if public is None:
                continue
            cluster_name = _cluster_name_with_lb_policy(f"public_{site_id}", route_opts)
            clusters.setdefault(
                cluster_name,
                CoreProxyCluster(
                    name=cluster_name,
                    endpoints=[(public["host"], public["port"])],
                    cluster_type="STRICT_DNS",
                    lb_policy=cluster_lb_policy,
                    use_tls=public["use_tls"],
                    sni=public["sni"],
                    ca_cert_path=public.get("ca_bundle_path"),
                    expected_sans=public.get("expected_sans") or [],
                ),
            )
            for entry in _route_path_entries(spec):
                routes.append(
                    CoreProxyRoute(
                        host=host,
                        path_prefix=entry["path"],
                        cluster=cluster_name,
                        redirect_to_https=redirect_https,
                        request_headers_add=route_opts.get("request_headers_add", []),
                        request_headers_remove=route_opts.get("request_headers_remove", []),
                        response_headers_add=route_opts.get("response_headers_add", []),
                        response_headers_remove=route_opts.get("response_headers_remove", []),
                        timeout_ms=route_opts.get("timeout_ms"),
                        idle_timeout_ms=route_opts.get("idle_timeout_ms"),
                        ext_authz_enabled=ext_authz_enabled,
                        local_rate_limit=local_rate_limit,
                        sticky_cookie_name=sticky_cookie_name,
                        sticky_cookie_ttl_seconds=sticky_cookie_ttl_seconds,
                        websocket_enabled=websocket_enabled,
                        websocket_idle_timeout_ms=websocket_idle_timeout_ms,
                        websocket_max_connection_duration_ms=websocket_max_connection_duration_ms,
                    )
                )
        elif mode in {"core-local", "core"}:
            for entry in _route_path_entries(spec):
                svc_ref = entry.get("service_ref") or {}
                endpoints = _resolve_core_local_endpoints(
                    store, svc_ref, record.namespace, entry.get("port")
                )
                if not endpoints:
                    continue
                cluster_name = _cluster_name_with_lb_policy(
                    _core_local_cluster_name(svc_ref, record.namespace, entry.get("port")),
                    route_opts,
                )
                clusters.setdefault(
                    cluster_name,
                    CoreProxyCluster(
                        name=cluster_name,
                        endpoints=endpoints,
                        lb_policy=cluster_lb_policy,
                    ),
                )
                routes.append(
                    CoreProxyRoute(
                        host=host,
                        path_prefix=entry["path"],
                        cluster=cluster_name,
                        redirect_to_https=redirect_https,
                        request_headers_add=route_opts.get("request_headers_add", []),
                        request_headers_remove=route_opts.get("request_headers_remove", []),
                        response_headers_add=route_opts.get("response_headers_add", []),
                        response_headers_remove=route_opts.get("response_headers_remove", []),
                        timeout_ms=route_opts.get("timeout_ms"),
                        idle_timeout_ms=route_opts.get("idle_timeout_ms"),
                        ext_authz_enabled=ext_authz_enabled,
                        local_rate_limit=local_rate_limit,
                        sticky_cookie_name=sticky_cookie_name,
                        sticky_cookie_ttl_seconds=sticky_cookie_ttl_seconds,
                        websocket_enabled=websocket_enabled,
                        websocket_idle_timeout_ms=websocket_idle_timeout_ms,
                        websocket_max_connection_duration_ms=websocket_max_connection_duration_ms,
                    )
                )

    ext_authz_config = None
    if forward_auth_url:
        auth_cluster = _forward_auth_cluster(forward_auth_url)
        if auth_cluster is not None:
            clusters.setdefault(auth_cluster.name, auth_cluster)
            ext_authz_config = _forward_auth_ext_authz_config(forward_auth_url)

    _append_controlplane_public_routes(
        routes,
        clusters,
        config,
        tls_enabled=tls_enabled,
    )

    return (
        routes,
        list(clusters.values()),
        ext_authz_config,
        enable_local_ratelimit,
        downstream_tls,
        fallback_tls,
    )


def _route_path_entries(spec: dict) -> list[dict]:
    entries: list[dict] = []
    raw_paths = spec.get("paths") if isinstance(spec.get("paths"), list) else []
    for entry in raw_paths:
        if not isinstance(entry, dict):
            continue
        raw_path = str(entry.get("path") or "").strip() or "/"
        if not raw_path.startswith("/"):
            raw_path = f"/{raw_path}"
        service_ref = entry.get("serviceRef") if isinstance(entry.get("serviceRef"), dict) else {}
        port = _coerce_int(entry.get("port")) or _coerce_int(service_ref.get("port"))
        entries.append({"path": raw_path, "service_ref": service_ref, "port": port})
    if not entries:
        service_ref = spec.get("serviceRef") if isinstance(spec.get("serviceRef"), dict) else {}
        port = _coerce_int(service_ref.get("port"))
        entries.append({"path": "/", "service_ref": service_ref, "port": port})
    return entries


def _edge_route_spec(record) -> dict:
    doc = normalize_route_doc(record)
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    return spec if isinstance(spec, dict) else {}


def _edge_policy_spec(record) -> dict | None:
    if record is None:
        return None
    doc = record.spec if isinstance(record.spec, dict) else {}
    if "spec" in doc and isinstance(doc.get("spec"), dict):
        return doc.get("spec")
    return doc if isinstance(doc, dict) else None


def _route_redirect_https(exposure: dict) -> bool:
    tls = exposure.get("tls") if isinstance(exposure.get("tls"), dict) else {}
    term = tls.get("terminateCore") if isinstance(tls.get("terminateCore"), dict) else {}
    if not term:
        term = tls.get("terminate_core") if isinstance(tls.get("terminate_core"), dict) else {}
    raw = term.get("redirectHttpToHttps")
    if raw is None:
        raw = term.get("redirect_http_to_https")
    if raw is None:
        raw = tls.get("redirectHttpToHttps")
    if raw is None:
        return False
    return str(raw).lower() in {"1", "true", "yes", "on"}


def _core_local_cluster_name(service_ref: dict, namespace: str, port: int | None) -> str:
    name = str(service_ref.get("name") or "").strip()
    ns = str(service_ref.get("namespace") or namespace or "default").strip() or "default"
    port_label = str(port) if port is not None else "any"
    safe = f"{ns}_{name}_{port_label}".replace("/", "-")
    return f"core_{safe}"


def _resolve_core_local_endpoints(
    store: SQLiteStateStore,
    service_ref: dict,
    namespace: str,
    port_hint: int | None,
) -> list[tuple[str, int]]:
    name = str(service_ref.get("name") or "").strip()
    if not name:
        return []
    svc_ns = str(service_ref.get("namespace") or namespace or "default").strip() or "default"
    app_name = app_key(name, svc_ns)
    svc_port = port_hint or _service_port_from_store(store, app_name)
    target_port = _service_target_port_from_store(store, app_name, svc_port)
    endpoints: list[tuple[str, int]] = []
    try:
        eps = store.list_service_endpoints(app_name)
    except Exception:
        eps = []
    for ep in eps:
        if svc_port is not None and int(ep.port) != int(svc_port):
            continue
        endpoints.append((str(ep.ip), int(ep.target_port)))
    if endpoints:
        return endpoints
    try:
        pods = store.list_pods(app_name)
    except Exception:
        pods = []
    for pod in pods:
        if not pod.ready:
            continue
        endpoint = getattr(pod, "endpoint", None)
        if not endpoint:
            continue
        host, port = _split_host_port(str(endpoint))
        if host is None or port is None:
            continue
        expected_ports = {
            int(item)
            for item in (target_port, svc_port)
            if item is not None
        }
        if expected_ports and int(port) not in expected_ports:
            continue
        endpoints.append((host, int(port)))
    return endpoints


def _service_port_from_store(store: SQLiteStateStore, app_name: str) -> int | None:
    try:
        svc = store.get_service(app_name)
    except Exception:
        svc = None
    if not svc or not isinstance(svc.ports, dict):
        return None
    ports = svc.ports.get("ports") if isinstance(svc.ports.get("ports"), list) else []
    if ports:
        try:
            return int(ports[0].get("port"))
        except Exception:
            return None
    raw = svc.ports.get("port")
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def _service_target_port_from_store(
    store: SQLiteStateStore,
    app_name: str,
    service_port: int | None = None,
) -> int | None:
    try:
        svc = store.get_service(app_name)
    except Exception:
        svc = None
    if not svc or not isinstance(svc.ports, dict):
        return None
    ports = svc.ports.get("ports") if isinstance(svc.ports.get("ports"), list) else []
    for item in ports:
        try:
            port = int(item.get("port"))
        except Exception:
            port = None
        if service_port is not None and port is not None and port != int(service_port):
            continue
        raw_target = item.get("targetPort")
        if raw_target is None:
            raw_target = item.get("target_port")
        try:
            return int(raw_target) if raw_target is not None else port
        except Exception:
            return port
    raw_target = svc.ports.get("targetPort")
    if raw_target is None:
        raw_target = svc.ports.get("target_port")
    try:
        return int(raw_target) if raw_target is not None else None
    except Exception:
        return None


def _split_host_port(endpoint: str) -> tuple[str | None, int | None]:
    try:
        if endpoint.startswith("["):
            host, port = endpoint.rsplit("]:", 1)
            return host.lstrip("["), int(port)
        host, port = endpoint.rsplit(":", 1)
        return host, int(port)
    except Exception:
        return None, None


def _collect_downstream_tls(
    routes: list,
    config: EdgeCoreProxyConfig,
    *,
    force_tls: bool = False,
) -> tuple[list[DownstreamTlsCert], DownstreamTlsCert | None]:
    if config.tls_listen_port is None:
        return [], None
    resolver = TlsSecretResolver(config.tls_root)
    cert_map: dict[tuple[str, str], set[str]] = {}
    want_tls = bool(force_tls)
    for record in routes:
        spec = _edge_route_spec(record)
        exposure = spec.get("exposure") if isinstance(spec.get("exposure"), dict) else {}
        tls = exposure.get("tls") if isinstance(exposure.get("tls"), dict) else {}
        if _tls_mode(tls) != "terminate-core":
            continue
        want_tls = True
        host = str(spec.get("host") or "").strip()
        if not host:
            continue
        secret = _tls_secret_name(tls)
        if not secret:
            continue
        resolved = resolver.resolve(secret)
        if not resolved:
            continue
        crt_path, key_path = resolved
        cert_map.setdefault((str(crt_path), str(key_path)), set()).add(host)

    fallback_cert: DownstreamTlsCert | None = None
    if config.tls_default_secret:
        want_tls = True
        resolved = resolver.resolve(config.tls_default_secret)
        if resolved:
            crt_path, key_path = resolved
            fallback_cert = DownstreamTlsCert(cert_chain=str(crt_path), private_key=str(key_path))

    if want_tls and fallback_cert is None and config.tls_fallback:
        fallback = _ensure_fallback_tls(
            config.tls_root,
            config.tls_fallback_cn,
            config.tls_fallback_days,
            sans=config.tls_fallback_sans,
        )
        if fallback:
            crt_path, key_path = fallback
            fallback_cert = DownstreamTlsCert(cert_chain=str(crt_path), private_key=str(key_path))

    if not want_tls:
        return [], None

    certs = [
        DownstreamTlsCert(
            cert_chain=crt,
            private_key=key,
            server_names=sorted(hosts),
        )
        for (crt, key), hosts in sorted(cert_map.items())
    ]
    return certs, fallback_cert


def _tls_secret_name(tls: dict) -> str | None:
    if not isinstance(tls, dict):
        return None
    secret = tls.get("secretName") or tls.get("tlsSecretName") or tls.get("secret_name")
    term = tls.get("terminateCore") if isinstance(tls.get("terminateCore"), dict) else {}
    if not term:
        term = tls.get("terminate_core") if isinstance(tls.get("terminate_core"), dict) else {}
    if not secret:
        secret = term.get("secretName") or term.get("tlsSecretName") or term.get("secret_name")
    return str(secret).strip() if secret else None


def _tls_mode(tls: dict) -> str:
    if not isinstance(tls, dict):
        return ""
    return str(tls.get("mode") or "").strip().lower()


def _ensure_fallback_tls(
    root: Path,
    cn: str,
    days: int,
    *,
    sans: tuple[str, ...] = (),
) -> tuple[Path, Path] | None:
    root.mkdir(parents=True, exist_ok=True)
    crt = root / "envoy-fallback.crt"
    key = root / "envoy-fallback.key"
    if crt.exists() and key.exists():
        return crt, key
    if shutil.which("openssl") is None:
        return None
    subj = f"/CN={cn}"
    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-days",
        str(max(1, int(days))),
        "-nodes",
        "-subj",
        subj,
        "-keyout",
        str(key),
        "-out",
        str(crt),
    ]
    san_names = [str(cn).strip()] + [str(name).strip() for name in sans if str(name).strip()]
    seen_names: set[str] = set()
    san_entries = []
    for name in san_names:
        lowered = name.lower()
        if lowered in seen_names:
            continue
        seen_names.add(lowered)
        san_entries.append(f"DNS:{name}")
    addext = "subjectAltName=" + ",".join(san_entries)
    cmd_with_san = cmd + ["-addext", addext]
    try:
        subprocess.run(cmd_with_san, check=False, capture_output=True)  # noqa: S603
    except Exception:
        return None
    if not (crt.exists() and key.exists()):
        try:
            subprocess.run(cmd, check=False, capture_output=True)  # noqa: S603
        except Exception:
            return None
    if crt.exists() and key.exists():
        try:
            crt.chmod(0o644)
            key.chmod(0o600)
        except Exception:
            pass
        return crt, key
    return None


def _public_endpoint(public_urls: list[str | dict]) -> dict | None:
    if not public_urls:
        return None
    entry = public_urls[0]
    if isinstance(entry, dict):
        url = str(entry.get("url") or entry.get("address") or "").strip()
        ca_bundle_path = entry.get("caBundlePath") or entry.get("ca_bundle_path")
        expected_sans = entry.get("expectedSANs") or entry.get("expected_sans") or []
    else:
        url = str(entry or "").strip()
        ca_bundle_path = None
        expected_sans = []
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return None
    scheme = (parsed.scheme or "https").lower()
    use_tls = scheme == "https"
    port = parsed.port or (443 if use_tls else 80)
    sni = host if use_tls else None
    if not isinstance(expected_sans, list):
        expected_sans = []
    return {
        "host": host,
        "port": port,
        "use_tls": use_tls,
        "sni": sni,
        "ca_bundle_path": ca_bundle_path,
        "expected_sans": expected_sans,
    }


def _policy_for_route(
    record, store: SQLiteStateStore, cache: dict[tuple[str, str], dict]
) -> dict | None:
    if not record.policy_name:
        return None
    ns = record.policy_namespace or record.namespace
    key = (record.policy_name, ns)
    if key in cache:
        return cache[key]
    policy = store.get_edge_ingress_policy(name=record.policy_name, namespace=ns)
    spec = _edge_policy_spec(policy) if policy else None
    if spec:
        cache[key] = spec
        return spec
    return None


def _policy_route_options(policy: dict) -> dict:
    opts: dict[str, object] = {}
    headers = policy.get("headers") if isinstance(policy.get("headers"), dict) else {}
    req = headers.get("request") if isinstance(headers.get("request"), dict) else {}
    resp = headers.get("response") if isinstance(headers.get("response"), dict) else {}
    opts["request_headers_add"] = _header_add(req)
    opts["request_headers_remove"] = _header_remove(req)
    opts["response_headers_add"] = _header_add(resp)
    opts["response_headers_remove"] = _header_remove(resp)
    timeouts = policy.get("timeouts") if isinstance(policy.get("timeouts"), dict) else {}
    timeout_ms = _coerce_int(timeouts.get("requestBodyMs")) or _coerce_int(
        timeouts.get("requestHeadersMs")
    )
    idle_timeout_ms = _coerce_int(timeouts.get("idleMs"))
    if timeout_ms:
        opts["timeout_ms"] = timeout_ms
    if idle_timeout_ms:
        opts["idle_timeout_ms"] = idle_timeout_ms
    websockets = policy.get("websockets") if isinstance(policy.get("websockets"), dict) else {}
    ws_enabled = _coerce_bool(websockets.get("enabled"))
    if ws_enabled is not None:
        opts["websocket_enabled"] = ws_enabled
    ws_idle_timeout_ms = _coerce_int(websockets.get("idleMs"))
    if ws_idle_timeout_ms:
        opts["websocket_idle_timeout_ms"] = ws_idle_timeout_ms
    ws_max_connection_duration_ms = _coerce_int(websockets.get("maxConnectionDurationMs"))
    if ws_max_connection_duration_ms:
        opts["websocket_max_connection_duration_ms"] = ws_max_connection_duration_ms

    load_balancing = (
        policy.get("loadBalancing") if isinstance(policy.get("loadBalancing"), dict) else {}
    )
    lb_policy = _normalize_lb_strategy(load_balancing.get("strategy"))
    if lb_policy:
        opts["lb_policy"] = lb_policy

    stickiness = policy.get("stickiness") if isinstance(policy.get("stickiness"), dict) else {}
    sticky_mode = str(stickiness.get("mode") or "").strip().lower()
    cookie = stickiness.get("cookie") if isinstance(stickiness.get("cookie"), dict) else {}
    if not sticky_mode and cookie:
        sticky_mode = "cookie"
    if sticky_mode == "cookie":
        cookie_name = str(cookie.get("name") or "").strip()
        if cookie_name:
            opts["sticky_cookie_name"] = cookie_name
            cookie_ttl = _coerce_int(cookie.get("ttlSeconds"))
            if cookie_ttl is not None and cookie_ttl > 0:
                opts["sticky_cookie_ttl_seconds"] = cookie_ttl
            # Envoy cookie-based session hashing requires ring-hash balancing.
            opts["lb_policy"] = "RING_HASH"

    local_rate_limit = _policy_rate_limit(policy)
    if local_rate_limit:
        opts["local_rate_limit"] = local_rate_limit
    return opts


def _normalize_lb_strategy(raw_value) -> str | None:
    token = str(raw_value or "").strip().lower().replace("-", "_")
    if token in {"", "round_robin", "roundrobin", "rr"}:
        return "ROUND_ROBIN"
    if token in {"least_request", "leastrequest", "least_req", "leastreq"}:
        return "LEAST_REQUEST"
    return None


def _route_lb_policy(route_opts: dict) -> str:
    token = str(route_opts.get("lb_policy") or "").strip().upper()
    if token in _ALLOWED_CLUSTER_LB_POLICIES:
        return token
    return "ROUND_ROBIN"


def _route_sticky_cookie(route_opts: dict) -> tuple[str | None, int | None]:
    cookie_name = str(route_opts.get("sticky_cookie_name") or "").strip()
    if not cookie_name:
        return None, None
    cookie_ttl = _coerce_int(route_opts.get("sticky_cookie_ttl_seconds"))
    if cookie_ttl is not None and cookie_ttl <= 0:
        cookie_ttl = None
    return cookie_name, cookie_ttl


def _cluster_name_with_lb_policy(base_name: str, route_opts: dict) -> str:
    lb_policy = _route_lb_policy(route_opts)
    cookie_name, cookie_ttl = _route_sticky_cookie(route_opts)
    if lb_policy == "ROUND_ROBIN" and not cookie_name:
        return base_name
    digest_source = f"{lb_policy}|{cookie_name or ''}|{cookie_ttl or ''}"
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:10]
    return f"{base_name}_{digest}"


def _select_forward_auth_url(
    store: SQLiteStateStore, cache: dict[tuple[str, str], dict]
) -> str | None:
    urls: set[str] = set()
    for record in store.list_edge_ingress_routes():
        spec = _edge_route_spec(record)
        exposure = spec.get("exposure") if isinstance(spec.get("exposure"), dict) else {}
        mode = str(exposure.get("mode") or "").strip().lower()
        if mode not in {"core-proxy", "core-to-edge-public"}:
            continue
        policy_spec = _policy_for_route(record, store, cache)
        if not policy_spec:
            continue
        url = _policy_forward_auth_url(policy_spec)
        if url:
            urls.add(url)
    if not urls:
        return None
    return sorted(urls)[0]


def _policy_forward_auth_url(policy: dict) -> str | None:
    auth = policy.get("auth") if isinstance(policy.get("auth"), dict) else {}
    mode = str(auth.get("mode") or "").strip().lower()
    if not mode or mode == "none":
        return None
    if mode != "forward-auth":
        return None
    forward = auth.get("forwardAuth") if isinstance(auth.get("forwardAuth"), dict) else {}
    raw_url = str(forward.get("url") or "").strip()
    return _normalize_forward_auth_url(raw_url)


def _policy_rate_limit(policy: dict) -> dict | None:
    waf = policy.get("waf") if isinstance(policy.get("waf"), dict) else {}
    mode = str(waf.get("mode") or "").strip().lower()
    if mode and mode != "basic":
        return None
    basic = waf.get("basic") if isinstance(waf.get("basic"), dict) else {}
    rate = basic.get("rateLimit") if isinstance(basic.get("rateLimit"), dict) else {}
    rps = _coerce_int(rate.get("rps"))
    burst = _coerce_int(rate.get("burst"))
    if rps is None and burst is None:
        return None
    if rps is None:
        rps = burst
    if burst is None:
        burst = rps
    if rps is None or burst is None:
        return None
    if rps <= 0 or burst <= 0:
        return None
    return {
        "max_tokens": burst,
        "tokens_per_fill": rps,
        "fill_interval": "1s",
    }


def _normalize_forward_auth_url(raw_url: str) -> str | None:
    if not raw_url:
        return None
    candidate = raw_url
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https"}:
        return None
    host = parsed.hostname or ""
    if not host:
        return None
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or ""
    return f"{scheme}://{host}:{port}{path}"


def _forward_auth_cluster(url: str) -> CoreProxyCluster | None:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    if not host:
        return None
    port = parsed.port or (443 if scheme == "https" else 80)
    use_tls = scheme == "https"
    return CoreProxyCluster(
        name="auth_forward",
        endpoints=[(host, port)],
        use_tls=use_tls,
        sni=host if use_tls else None,
    )


def _forward_auth_ext_authz_config(url: str) -> dict | None:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    if not host:
        return None
    port = parsed.port or (443 if scheme == "https" else 80)
    path_prefix = parsed.path or ""
    if path_prefix == "/":
        path_prefix = ""
    config: dict = {
        "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz",
        "transport_api_version": "V3",
        "failure_mode_allow": False,
        "http_service": {
            "server_uri": {
                "uri": f"{scheme}://{host}:{port}",
                "cluster": "auth_forward",
                "timeout": "2s",
            },
            "authorization_request": {"allowed_headers": {"patterns": [{"regex": ".*"}]}},
            "authorization_response": {
                "allowed_upstream_headers": {"patterns": [{"regex": ".*"}]},
                "allowed_client_headers": {"patterns": [{"regex": ".*"}]},
            },
        },
    }
    if path_prefix:
        config["http_service"]["path_prefix"] = path_prefix
    return config


def _header_add(section: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    add = section.get("add") if isinstance(section.get("add"), dict) else {}
    for key, value in add.items():
        if key:
            out.append((str(key), str(value)))
    return out


def _header_remove(section: dict) -> list[str]:
    out: list[str] = []
    remove = section.get("remove") if isinstance(section.get("remove"), list) else []
    for key in remove:
        if key:
            out.append(str(key))
    return out


def _coerce_int(value) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _coerce_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if int(value) == 1:
            return True
        if int(value) == 0:
            return False
        return None
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return None


def _run_reload(cmd: str) -> None:
    lock_raw = os.getenv(
        "AE_EDGE_INGRESS_RELOAD_LOCK", "state/profiles/k1s-core/edge-ingress/.reload.lock"
    )
    lock_path = Path(lock_raw).expanduser()
    if not lock_path.is_absolute():
        lock_path = (Path.cwd() / lock_path).resolve()
    else:
        lock_path = lock_path.resolve()

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            LOGGER.info("edge ingress reload start: cmd=%s", cmd)
            proc = subprocess.run(  # noqa: S602
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                LOGGER.warning(
                    "edge ingress reload failed: rc=%s cmd=%s stdout=%s stderr=%s",
                    proc.returncode,
                    cmd,
                    out[-400:],
                    err[-400:],
                )
            else:
                LOGGER.info(
                    "edge ingress reload done: rc=%s cmd=%s stdout=%s stderr=%s",
                    proc.returncode,
                    cmd,
                    out[-200:],
                    err[-200:],
                )
    except Exception as exc:
        LOGGER.warning("edge ingress reload exception: cmd=%s error=%s", cmd, exc)
        return


__all__ = ["EdgeCoreProxyRenderer", "EdgeCoreProxyConfig", "build_core_proxy_config"]
