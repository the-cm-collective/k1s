"""Registry authentication helpers."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


def _default_config_path() -> Path:
    override = os.getenv("AE_REGISTRY_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "ae" / "registries.yaml"


class RegistryAuthProvider:
    """Loads registry credentials and logs into docker clients as needed."""

    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path or _default_config_path()
        self._credentials = self._load_config()

    def ensure_login(self, client, image: str) -> None:
        """Login to a registry if credentials are configured.

        Notes:
        - Images without an explicit registry are treated as Docker Hub (docker.io).
        - Accept common Docker Hub aliases in registries.yaml:
          docker.io, index.docker.io, registry-1.docker.io, https://index.docker.io/v1/
        """
        registry = self._extract_registry(image)
        # Build candidate hostnames (handle Docker Hub aliases)
        candidates = [registry] if registry else ["docker.io"]
        if candidates[0] == "docker.io":
            candidates.extend(
                [
                    "index.docker.io",
                    "registry-1.docker.io",
                    "https://index.docker.io/v1/",
                ]
            )

        creds = None
        chosen_host = None
        for host in candidates:
            c = self._credentials.get(host)
            if c:
                creds = c
                chosen_host = host
                break
        if not creds:
            return

        client.login(
            registry=chosen_host,
            username=creds.get("username"),
            password=creds.get("password"),
        )

    def list_registries(self) -> dict[str, dict[str, str]]:
        return self._credentials

    def _load_config(self) -> dict[str, dict[str, str]]:
        if not self._config_path.exists():
            return {}
        data = yaml.safe_load(self._config_path.read_text())
        if not data:
            return {}
        if not isinstance(data, dict):
            raise ValueError("registry config must be a mapping")
        creds: dict[str, dict[str, str]] = {}
        for host, values in data.items():
            if not isinstance(values, dict):
                continue
            username = values.get("username")
            password = values.get("password")
            if username and password:
                creds[str(host)] = {"username": str(username), "password": str(password)}
        return creds

    def _extract_registry(self, image: str) -> str | None:
        """Return registry hostname if present in the image reference.

        Examples:
        - "ghcr.io/org/app:tag" → "ghcr.io"
        - "redis:7" → None (treated as docker.io elsewhere)
        """
        if "/" not in image:
            return None
        host = image.split("/", 1)[0]
        if "." not in host and ":" not in host:
            return None
        return host
