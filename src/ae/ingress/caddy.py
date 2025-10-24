"""Caddy ingress templating and reload helpers."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from string import Template
from typing import Iterable, List, Optional

from ae.controller.spec import AppManifest, IngressSpec

LOGGER = logging.getLogger(__name__)


SITE_TEMPLATE = Template(
    """$host {
    reverse_proxy $upstream
}"""
)


class CaddyIngressManager:
    """Renders Caddy configuration blocks per application and triggers reloads."""

    def __init__(
        self,
        config_root: Path,
        caddy_binary: str = "caddy",
        config_file: Path | None = None,
        container: str | None = None,
    ) -> None:
        self._config_root = config_root
        self._caddy_binary = caddy_binary
        self._config_file = config_file
        self._container = container
        self._config_root.mkdir(parents=True, exist_ok=True)

    def apply(self, manifest: AppManifest, upstream: str) -> Path:
        ingress = manifest.spec.ingress
        if ingress is None:
            raise ValueError("Manifest lacks ingress configuration")

        site_config = self._render_site(ingress, upstream)
        site_path = self._site_path(manifest.metadata.name)
        site_path.write_text(site_config)
        LOGGER.debug("Wrote Caddy site config to %s", site_path)
        return site_path

    def remove(self, app_name: str) -> None:
        site_path = self._site_path(app_name)
        if site_path.exists():
            site_path.unlink()
            LOGGER.debug("Removed Caddy site config %s", site_path)

    def reload(self) -> None:
        config_path = str(self._config_file or self._config_root)
        cmd: List[str]
        if self._container:
            cmd = [
                "docker",
                "exec",
                self._container,
                self._caddy_binary,
                "reload",
                "--config",
                config_path,
            ]
        else:
            cmd = [self._caddy_binary, "reload", "--config", config_path]

        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            missing = self._caddy_binary if not self._container else "docker"
            raise RuntimeError(f"Caddy reload dependency not found: {missing}") from exc
        except subprocess.CalledProcessError as exc:
            LOGGER.error("Caddy reload failed: %s", exc.stderr.decode("utf-8", "ignore"))
            raise RuntimeError("Caddy reload failed") from exc

    def _render_site(self, ingress: IngressSpec, upstream: str) -> str:
        host = ingress.host
        upstream_target = upstream
        # If we're reloading Caddy inside a container, route via the host
        # gateway so the container can reach Docker-published ports.
        if self._container:
            try:
                host_part, port_part = upstream.split(":", 1)
                if host_part in {"127.0.0.1", "0.0.0.0"}:
                    upstream_target = f"host.docker.internal:{port_part}"
            except ValueError:
                pass
        if ingress.path and ingress.path != "/":
            upstream_target = f"{upstream} {ingress.path}"
        return SITE_TEMPLATE.substitute(host=host, upstream=upstream_target)

    def _site_path(self, app_name: str) -> Path:
        return self._config_root / f"{app_name}.caddy"
