"""Caddy ingress templating and reload helpers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from string import Template

from ae.controller.spec import AppManifest, IngressSpec, app_key_for_manifest

LOGGER = logging.getLogger(__name__)


SITE_TEMPLATE = Template(
    """https://$host {
    log {
        output stdout
        format console
    }
    # Ensure upstream HSTS does not stick during dev
    header -Strict-Transport-Security
    $tls_block
    $routes
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
        container_cli: str = "docker",
    ) -> None:
        self._config_root = config_root
        self._caddy_binary = shutil.which(caddy_binary) or caddy_binary
        self._config_file = config_file
        self._container = container
        self._reload_timeout = reload_timeout
        # Which container CLI to use when reloading inside a container (docker|podman)
        resolved_cli = container_cli or "docker"
        self._container_cli = shutil.which(resolved_cli) or resolved_cli
        self._config_root.mkdir(parents=True, exist_ok=True)

    def apply(
        self,
        manifest: AppManifest,
        upstream: str | Sequence[str],
        readiness_path: str | None = None,
        prefer_first: bool = True,
        first_weight: int = 1,
    ) -> Path:
        ingress = manifest.spec.ingress
        if ingress is None:
            raise ValueError("Manifest lacks ingress configuration")

        site_config = self._render_site(
            ingress, upstream, readiness_path, prefer_first, first_weight
        )
        site_path = self._site_path(app_key_for_manifest(manifest))
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
        cmd: list[str]
        # Validate Caddyfile via 'caddy adapt' before reloads to avoid crashing/restarting
        # the container
        adapt_cmd: list[str]
        if self._container:
            adapt_cmd = [
                self._container_cli,
                "exec",
                self._container,
                self._caddy_binary,
                "adapt",
                "--config",
                config_path,
            ]
            cmd = [
                self._container_cli,
                "exec",
                self._container,
                self._caddy_binary,
                "reload",
                "--config",
                config_path,
            ]
        else:
            # On host, skip adapt to keep tests and simple setups happy
            cmd = [self._caddy_binary, "reload", "--config", config_path]

        try:
            kwargs = {"check": True, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
            if self._reload_timeout:
                kwargs["timeout"] = self._reload_timeout
            # Run adapt first only when inside container
            if self._container:
                subprocess.run(adapt_cmd, **kwargs)  # noqa: S603,S607 - fixed binaries; shell disabled
            subprocess.run(cmd, **kwargs)  # noqa: S603,S607 - fixed binaries; shell disabled
        except FileNotFoundError as exc:
            missing = self._caddy_binary if not self._container else self._container_cli
            raise RuntimeError(f"Caddy reload dependency not found: {missing}") from exc
        except subprocess.TimeoutExpired as exc:
            LOGGER.error("Caddy reload timed out after %.1fs", (self._reload_timeout or 0))
            raise RuntimeError("Caddy reload timed out") from exc
        except subprocess.CalledProcessError as exc:
            msg = exc.stderr.decode("utf-8", "ignore")
            # During early startup the dev Caddy container may not exist yet; avoid noisy warnings.
            transient = (
                "No such container" in msg
                or ("container" in msg and "not found" in msg)
                or ("state improper" in msg)
            )
            if self._container and transient:
                LOGGER.info(
                    "Caddy container %s not available yet; skipping reload", self._container
                )
                return
            LOGGER.error("Caddy reload failed: %s", msg)
            raise RuntimeError("Caddy reload failed") from exc

    def _render_site(
        self,
        ingress: IngressSpec,
        upstreams: str | Sequence[str],
        readiness_path: str | None,
        prefer_first: bool,
        first_weight: int,
    ) -> str:
        host = ingress.host
        ups_list = [upstreams] if isinstance(upstreams, str) else list(upstreams)

        targets: list[str] = []
        env_host_alias = os.getenv("AE_CADDY_HOST_ALIAS", "").strip()
        for up in ups_list:
            target = up
            # If Caddy runs in a container and target refers to host loopback,
            # route via the host gateway alias so the container can reach it.
            if self._container:
                try:
                    host_part, port_part = up.split(":", 1)
                except ValueError:
                    host_part, port_part = up, ""
                if host_part in {"127.0.0.1", "0.0.0.0"} and port_part:  # noqa: S104 - loopback mapping for container access
                    # Prefer Podman alias when using podman; otherwise use Docker alias.
                    host_alias = env_host_alias or (
                        "host.containers.internal"
                        if self._container_cli == "podman"
                        else "host.docker.internal"
                    )
                    target = f"{host_alias}:{port_part}"
                # Also normalize if previous runs wrote the other runtime's alias
                if (
                    host_part == "host.docker.internal"
                    and self._container_cli == "podman"
                    and port_part
                ):
                    target = f"host.containers.internal:{port_part}"
                if (
                    host_part == "host.containers.internal"
                    and self._container_cli != "podman"
                    and port_part
                ):
                    target = f"host.docker.internal:{port_part}"
            if ingress.path and ingress.path != "/":
                target = f"{target} {ingress.path}"
            targets.append(target)

        # Optional weighting: duplicate the first upstream N times to bias selection
        if prefer_first and first_weight > 1 and targets:
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
        # Build routes: multi-path support when ingress.paths is set
        paths = list(getattr(ingress, "paths", []) or [])
        if not paths:
            # Single path (ingress.path)
            routes = (
                "reverse_proxy "
                + upstreams_str
                + " {\n"
                + (health_block + "\n" if health_block else "")
                + (policy_block + "\n" if policy_block else "")
                + "}"
            )
        else:
            blocks: list[str] = []
            for p in paths:
                if not p or p == "/":
                    blocks.append(
                        "reverse_proxy "
                        + upstreams_str
                        + " {\n"
                        + (health_block + "\n" if health_block else "")
                        + (policy_block + "\n" if policy_block else "")
                        + "}"
                    )
                else:
                    blocks.append(
                        "handle_path "
                        + p
                        + " {\n    reverse_proxy "
                        + upstreams_str
                        + " {\n"
                        + (health_block + "\n" if health_block else "")
                        + (policy_block + "\n" if policy_block else "")
                        + "    }\n}"
                    )
            routes = "\n    ".join(blocks)
        # TLS block: prefer BYO cert/key when provided; else use internal
        cert = getattr(ingress, "tls_cert_path", None)
        key = getattr(ingress, "tls_key_path", None)
        tls_block = f"tls {cert} {key}" if cert and key else "tls internal"
        return SITE_TEMPLATE.substitute(host=host, routes=routes, tls_block=tls_block)

    def _site_path(self, app_name: str) -> Path:
        return self._config_root / f"{app_name}.caddy"
