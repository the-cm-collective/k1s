"""Core-proxy config renderer for Envoy + Rathole."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ae.controller.state import SiteIngressListItem, SQLiteStateStore
from ae.ingress.envoy_core_proxy import (
    CoreProxyCluster,
    CoreProxyRoute,
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

LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True)
class EdgeCoreProxyConfig:
    config_dir: Path
    envoy_config_path: Path
    rathole_server_path: Path
    rathole_client_dir: Path | None
    site_domain_suffix: str
    rathole_bind_addr: str
    rathole_default_token: str
    rathole_server_addr: str
    edge_local_addr: str
    reload_cmd: str | None


class EdgeCoreProxyRenderer:
    def __init__(self, store: SQLiteStateStore, config: EdgeCoreProxyConfig) -> None:
        self._store = store
        self._config = config
        self._last_hash: str | None = None
        self._lock = threading.Lock()

    def render(self) -> None:
        with self._lock:
            self._render_locked()

    def _render_locked(self) -> None:
        endpoints = self._store.list_site_ingress_endpoints()
        routes, clusters, ext_authz_config, enable_local_ratelimit = _build_routes_and_clusters(
            self._store, endpoints, self._config
        )
        envoy_cfg = EnvoyRenderConfig(domain_suffix=self._config.site_domain_suffix)
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
        payload = (envoy_text + "\n" + rathole_text).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        if self._last_hash == digest:
            return
        self._last_hash = digest
        if self._config.reload_cmd:
            _run_reload(self._config.reload_cmd)


def build_core_proxy_config() -> EdgeCoreProxyConfig | None:
    raw_dir = os.getenv("AE_EDGE_INGRESS_CONFIG_DIR")
    if not raw_dir:
        return None
    config_dir = Path(raw_dir)
    envoy_path = Path(os.getenv("AE_EDGE_INGRESS_ENVOY_CONFIG", config_dir / "envoy.yaml"))
    rathole_server = Path(
        os.getenv("AE_RATHOLE_SERVER_CONFIG", config_dir / "rathole-server.toml")
    )
    client_dir_raw = os.getenv("AE_RATHOLE_CLIENT_DIR")
    client_dir = Path(client_dir_raw) if client_dir_raw else None
    site_suffix = os.getenv("AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX", "edge.local")
    rathole_bind = os.getenv("AE_RATHOLE_BIND_ADDR", "0.0.0.0:2333")
    rathole_token = os.getenv("AE_RATHOLE_DEFAULT_TOKEN", "dev")
    rathole_server_addr = os.getenv("AE_RATHOLE_SERVER_ADDR", "127.0.0.1:2333")
    edge_local_addr = os.getenv("AE_EDGE_INGRESS_LOCAL_ADDR", "127.0.0.1:18081")
    reload_cmd = os.getenv("AE_EDGE_INGRESS_RELOAD_CMD")
    return EdgeCoreProxyConfig(
        config_dir=config_dir,
        envoy_config_path=envoy_path if isinstance(envoy_path, Path) else Path(envoy_path),
        rathole_server_path=rathole_server,
        rathole_client_dir=client_dir,
        site_domain_suffix=site_suffix,
        rathole_bind_addr=rathole_bind,
        rathole_default_token=rathole_token,
        rathole_server_addr=rathole_server_addr,
        edge_local_addr=edge_local_addr,
        reload_cmd=reload_cmd,
    )


def _core_proxy_services(
    endpoints: list[SiteIngressListItem],
) -> list[SiteIngressListItem]:
    return [ep for ep in endpoints if ep.core_proxy_port is not None]


def _build_routes_and_clusters(
    store: SQLiteStateStore,
    endpoints: list[SiteIngressListItem],
    config: EdgeCoreProxyConfig,
) -> tuple[list[CoreProxyRoute], list[CoreProxyCluster], dict | None, bool]:
    routes: list[CoreProxyRoute] = []
    clusters: dict[str, CoreProxyCluster] = {}
    endpoint_map = {ep.site_id: ep for ep in endpoints}
    domain_suffix = config.site_domain_suffix.strip(".") or "edge.local"
    policy_cache: dict[tuple[str, str], dict] = {}
    forward_auth_url = _select_forward_auth_url(store, policy_cache)
    enable_local_ratelimit = False

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
    for record in store.list_edge_ingress_routes():
        spec = record.spec if isinstance(record.spec, dict) else {}
        host = str(spec.get("host") or "").strip()
        if not host:
            continue
        exposure = spec.get("exposure") if isinstance(spec.get("exposure"), dict) else {}
        mode = str(exposure.get("mode") or "").strip().lower()
        placement = (
            exposure.get("placement") if isinstance(exposure.get("placement"), dict) else {}
        )
        site_id = str(placement.get("site") or record.site_id or "").strip()
        if not site_id:
            continue
        policy_spec = _policy_for_route(record, store, policy_cache)
        route_opts = _policy_route_options(policy_spec) if policy_spec else {}
        local_rate_limit = route_opts.get("local_rate_limit")
        if local_rate_limit:
            enable_local_ratelimit = True
        ext_authz_enabled = False
        if forward_auth_url and policy_spec:
            route_forward_auth = _policy_forward_auth_url(policy_spec)
            ext_authz_enabled = bool(
                route_forward_auth and route_forward_auth == forward_auth_url
            )
        if mode == "core-proxy":
            ep = endpoint_map.get(site_id)
            if ep is None or ep.core_proxy_port is None:
                continue
            cluster_name = f"site_{site_id}"
            clusters.setdefault(
                cluster_name,
                CoreProxyCluster(
                    name=cluster_name,
                    endpoints=[("127.0.0.1", int(ep.core_proxy_port))],
                ),
            )
            for path in _route_paths(spec):
                routes.append(
                    CoreProxyRoute(
                        host=host,
                        path_prefix=path,
                        cluster=cluster_name,
                        request_headers_add=route_opts.get("request_headers_add", []),
                        request_headers_remove=route_opts.get("request_headers_remove", []),
                        response_headers_add=route_opts.get("response_headers_add", []),
                        response_headers_remove=route_opts.get("response_headers_remove", []),
                        timeout_ms=route_opts.get("timeout_ms"),
                        idle_timeout_ms=route_opts.get("idle_timeout_ms"),
                        ext_authz_enabled=ext_authz_enabled,
                        local_rate_limit=local_rate_limit,
                    )
                )
        elif mode == "core-to-edge-public":
            ep = endpoint_map.get(site_id)
            public = _public_endpoint(ep.public_urls if ep else [])
            if public is None:
                continue
            cluster_name = f"public_{site_id}"
            clusters.setdefault(
                cluster_name,
                CoreProxyCluster(
                    name=cluster_name,
                    endpoints=[(public["host"], public["port"])],
                    use_tls=public["use_tls"],
                    sni=public["sni"],
                    ca_cert_path=public.get("ca_bundle_path"),
                    expected_sans=public.get("expected_sans") or [],
                ),
            )
            for path in _route_paths(spec):
                routes.append(
                    CoreProxyRoute(
                        host=host,
                        path_prefix=path,
                        cluster=cluster_name,
                        request_headers_add=route_opts.get("request_headers_add", []),
                        request_headers_remove=route_opts.get("request_headers_remove", []),
                        response_headers_add=route_opts.get("response_headers_add", []),
                        response_headers_remove=route_opts.get("response_headers_remove", []),
                        timeout_ms=route_opts.get("timeout_ms"),
                        idle_timeout_ms=route_opts.get("idle_timeout_ms"),
                        ext_authz_enabled=ext_authz_enabled,
                        local_rate_limit=local_rate_limit,
                    )
                )

    ext_authz_config = None
    if forward_auth_url:
        auth_cluster = _forward_auth_cluster(forward_auth_url)
        if auth_cluster is not None:
            clusters.setdefault(auth_cluster.name, auth_cluster)
            ext_authz_config = _forward_auth_ext_authz_config(forward_auth_url)

    return routes, list(clusters.values()), ext_authz_config, enable_local_ratelimit


def _route_paths(spec: dict) -> list[str]:
    paths: list[str] = []
    raw_paths = spec.get("paths") if isinstance(spec.get("paths"), list) else []
    for entry in raw_paths:
        if not isinstance(entry, dict):
            continue
        raw_path = str(entry.get("path") or "").strip() or "/"
        if not raw_path.startswith("/"):
            raw_path = f"/{raw_path}"
        paths.append(raw_path)
    if not paths:
        paths.append("/")
    return paths


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
    if policy and isinstance(policy.spec, dict):
        cache[key] = policy.spec
        return policy.spec
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
    local_rate_limit = _policy_rate_limit(policy)
    if local_rate_limit:
        opts["local_rate_limit"] = local_rate_limit
    return opts


def _select_forward_auth_url(
    store: SQLiteStateStore, cache: dict[tuple[str, str], dict]
) -> str | None:
    urls: set[str] = set()
    for record in store.list_edge_ingress_routes():
        spec = record.spec if isinstance(record.spec, dict) else {}
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
            "authorization_request": {
                "allowed_headers": {"patterns": [{"regex": ".*"}]}
            },
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


def _run_reload(cmd: str) -> None:
    try:
        subprocess.run(cmd, shell=True, check=False)  # noqa: S602
    except Exception:
        return


__all__ = ["EdgeCoreProxyRenderer", "EdgeCoreProxyConfig", "build_core_proxy_config"]
