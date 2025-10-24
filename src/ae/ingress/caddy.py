"""Caddy ingress templating and reload helpers."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from string import Template
import os
from typing import Iterable, List, Optional, Sequence, Union

from ae.controller.spec import AppManifest, IngressSpec

LOGGER = logging.getLogger(__name__)


SITE_TEMPLATE = Template(
    """https://$host {
    log {
        output stdout
        format console
    }
    # Ensure upstream HSTS does not stick during dev
    header -Strict-Transport-Security
    reverse_proxy $upstreams {
        $health_block
        $policy_block
    }
}
"""
)


class CaddyIngressManager:
    """Renders Caddy configuration blocks per application and triggers reloads."""

    def __init__(
        self,
        config_root: Path,
        caddy_binary: str = "caddy",
        config_file: Path | None = None,
        container: str | None = None,
        reload_timeout: float | None = None,
    ) -> None:
        self._config_root = config_root
        self._caddy_binary = caddy_binary
        self._config_file = config_file
        self._container = container
        self._reload_timeout = reload_timeout
        self._config_root.mkdir(parents=True, exist_ok=True)

    def apply(self, manifest: AppManifest, upstream: Union[str, Sequence[str]], readiness_path: Optional[str] = None, prefer_first: bool = True) -> Path:
        ingress = manifest.spec.ingress
        if ingress is None:
            raise ValueError("Manifest lacks ingress configuration")

        site_config = self._render_site(ingress, upstream, readiness_path, prefer_first)
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
        # Validate Caddyfile via 'caddy adapt' before reloading to avoid crashing/restarting container
        adapt_cmd: List[str]
        if self._container:
            adapt_cmd = [
                "docker", "exec", self._container, self._caddy_binary, "adapt", "--config", config_path
            ]
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
            adapt_cmd = [self._caddy_binary, "adapt", "--config", config_path]
            cmd = [self._caddy_binary, "reload", "--config", config_path]

        try:
            # Run adapt first
            subprocess.run(
                adapt_cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._reload_timeout,
            )
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._reload_timeout,
            )
        except FileNotFoundError as exc:
            missing = self._caddy_binary if not self._container else "docker"
            raise RuntimeError(f"Caddy reload dependency not found: {missing}") from exc
        except subprocess.TimeoutExpired as exc:
            LOGGER.error("Caddy reload timed out after %.1fs", (self._reload_timeout or 0))
            raise RuntimeError("Caddy reload timed out") from exc
        except subprocess.CalledProcessError as exc:
            LOGGER.error("Caddy reload failed: %s", exc.stderr.decode("utf-8", "ignore"))
            raise RuntimeError("Caddy reload failed") from exc

    def _render_site(self, ingress: IngressSpec, upstreams: Union[str, Sequence[str]], readiness_path: Optional[str], prefer_first: bool) -> str:
        host = ingress.host
        if isinstance(upstreams, str):
            ups_list = [upstreams]
        else:
            ups_list = list(upstreams)

        targets: list[str] = []
        for up in ups_list:
            target = up
            # If Caddy is running in a container and target refers to host loopback,
            # route via the host gateway name so the container can reach it.
            if self._container:
                try:
                    host_part, port_part = up.split(":", 1)
                except ValueError:
                    host_part, port_part = up, ""
                if host_part in {"127.0.0.1", "0.0.0.0"} and port_part:
                    target = f"host.docker.internal:{port_part}"
            if ingress.path and ingress.path != "/":
                target = f"{target} {ingress.path}"
            targets.append(target)

        # Optional weighting: duplicate the first upstream N times to bias selection
        if prefer_first:
            try:
                first_weight = int(os.getenv("AE_ROLLOUT_FIRST_WEIGHT", "1"))
            except ValueError:
                first_weight = 1
            if first_weight > 1 and targets:
                targets = [targets[0]] * int(first_weight) + targets[1:]
        upstreams_str = " ".join(targets)
        health_block = ""
        if readiness_path and os.getenv("AE_CADDY_ACTIVE_HEALTH") == "1":
            # Opt-in active health checks
            health_block = (
                "health_checks {\n"
                f"            path {readiness_path}\n"
                "            interval 10s\n"
                "            timeout 2s\n"
                "        }"
            )
        policy_block = "lb_policy first" if prefer_first else ""
        return SITE_TEMPLATE.substitute(host=host, upstreams=upstreams_str, health_block=health_block, policy_block=policy_block)

    def _site_path(self, app_name: str) -> Path:
        return self._config_root / f"{app_name}.caddy"
