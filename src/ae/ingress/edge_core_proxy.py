"""Core-proxy config renderer for Envoy + Rathole."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ae.controller.state import SiteIngressListItem, SQLiteStateStore
from ae.ingress.envoy_core_proxy import CoreProxyRoute, EnvoyRenderConfig, write_envoy_config
from ae.ingress.rathole import (
    RatholeClientConfig,
    RatholeClientService,
    RatholeServerConfig,
    RatholeServerService,
    write_rathole_client,
    write_rathole_server,
)


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

    def render(self) -> None:
        endpoints = self._store.list_site_ingress_endpoints()
        routes = _routes_from_endpoints(endpoints)
        envoy_cfg = EnvoyRenderConfig(domain_suffix=self._config.site_domain_suffix)
        envoy_text = write_envoy_config(self._config.envoy_config_path, routes, envoy_cfg)
        rathole_text = write_rathole_server(
            self._config.rathole_server_path,
            RatholeServerConfig(
                bind_addr=self._config.rathole_bind_addr,
                default_token=self._config.rathole_default_token,
                services=[
                    RatholeServerService(
                        name=route.site_id,
                        bind_addr=f"0.0.0.0:{route.upstream_port}",
                    )
                    for route in routes
                ],
            ),
        )
        if self._config.rathole_client_dir:
            for route in routes:
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


def _routes_from_endpoints(endpoints: list[SiteIngressListItem]) -> list[CoreProxyRoute]:
    routes: list[CoreProxyRoute] = []
    for ep in endpoints:
        if ep.core_proxy_port is None:
            continue
        routes.append(
            CoreProxyRoute(
                site_id=ep.site_id,
                upstream_host="127.0.0.1",
                upstream_port=int(ep.core_proxy_port),
            )
        )
    return routes


def _run_reload(cmd: str) -> None:
    try:
        subprocess.run(cmd, shell=True, check=False)  # noqa: S602
    except Exception:
        return


__all__ = ["EdgeCoreProxyRenderer", "EdgeCoreProxyConfig", "build_core_proxy_config"]
