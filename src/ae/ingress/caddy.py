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

    def __init__(self, config_root: Path, caddy_binary: str = "caddy") -> None:
        self._config_root = config_root
        self._caddy_binary = caddy_binary
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
        try:
            subprocess.run(
                [self._caddy_binary, "reload", "--config", str(self._config_root)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Caddy binary {self._caddy_binary} not found") from exc
        except subprocess.CalledProcessError as exc:
            LOGGER.error("Caddy reload failed: %s", exc.stderr.decode("utf-8", "ignore"))
            raise RuntimeError("Caddy reload failed") from exc

    def _render_site(self, ingress: IngressSpec, upstream: str) -> str:
        host = ingress.host
        upstream_target = upstream
        if ingress.path and ingress.path != "/":
            upstream_target = f"{upstream} {ingress.path}"
        return SITE_TEMPLATE.substitute(host=host, upstream=upstream_target)

    def _site_path(self, app_name: str) -> Path:
        return self._config_root / f"{app_name}.caddy"
