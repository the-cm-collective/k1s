"""Ingress orchestration service to manage Caddy configs per manifest."""

from __future__ import annotations

from dataclasses import dataclass

from ae.controller.spec import AppManifest

from .caddy import CaddyIngressManager


@dataclass(slots=True)
class IngressResult:
    app_name: str
    host: str | None
    config_path: str | None


class IngressService:
    """Coordinates ingress manager operations based on manifest state."""

    def __init__(self, manager: CaddyIngressManager) -> None:
        self._manager = manager

    def apply(self, manifest: AppManifest, upstream) -> IngressResult:
        if manifest.spec.ingress is None:
            return IngressResult(
                app_name=manifest.metadata.name,
                host=None,
                config_path=None,
            )
        readiness_path = None
        if manifest.spec.health and manifest.spec.health.readiness and manifest.spec.health.readiness.http_get:
            readiness_path = manifest.spec.health.readiness.http_get.path or "/"
        # Prefer-first policy biases towards listed upstream order (new revision first)
        site_path = self._manager.apply(manifest, upstream, readiness_path, prefer_first=True)
        return IngressResult(
            app_name=manifest.metadata.name,
            host=manifest.spec.ingress.host,
            config_path=str(site_path),
        )

    def remove(self, app_name: str) -> None:
        self._manager.remove(app_name)

    def reload(self) -> None:
        try:
            self._manager.reload()
        except Exception as exc:  # pragma: no cover - defensive path
            # Do not crash the controller if Caddy reload is unavailable in dev.
            import logging
            logging.getLogger(__name__).warning("ingress reload skipped: %s", exc)
