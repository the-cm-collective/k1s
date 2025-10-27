"""Ingress orchestration service to manage Caddy configs per manifest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ae.controller.spec import AppManifest
from ae.controller.state import SQLiteStateStore
from .tls_sync import TlsSecretResolver

from .caddy import CaddyIngressManager


@dataclass(slots=True)
class IngressResult:
    app_name: str
    host: str | None
    config_path: str | None


class IngressService:
    """Coordinates ingress manager operations based on manifest state."""

    def __init__(self, manager: CaddyIngressManager, store: SQLiteStateStore | None = None) -> None:
        self._manager = manager
        self._store = store
        self._last_sig: dict[str, str] = {}
        self._dirty: bool = False
        # Back-compat in-memory state when no store is available
        self._canary_state: dict[str, dict[str, float]] = {}

    def apply(self, manifest: AppManifest, upstream) -> IngressResult:
        if manifest.spec.ingress is None:
            return IngressResult(
                app_name=manifest.metadata.name,
                host=None,
                config_path=None,
            )
        readiness_path = None
        if (
            manifest.spec.health
            and manifest.spec.health.readiness
            and manifest.spec.health.readiness.http_get
        ):
            readiness_path = manifest.spec.health.readiness.http_get.path or "/"
        # Resolve TLS secret to local cert/key if needed
        ingress_spec = manifest.spec.ingress
        if (
            getattr(ingress_spec, "tls_secret_name", None)
            and not getattr(ingress_spec, "tls_cert_path", None)
            and not getattr(ingress_spec, "tls_key_path", None)
        ):
            import os

            root = os.getenv("AE_TLS_DIR", "state/tls")
            resolver = TlsSecretResolver(Path(root))
            resolved = resolver.resolve(str(ingress_spec.tls_secret_name))
            if resolved:
                cert_path, key_path = resolved
                # Create a temporary copy of the manifest spec with cert/key paths filled
                ingress_spec = ingress_spec.model_copy(update={"tls_cert_path": str(cert_path), "tls_key_path": str(key_path)})
                manifest = manifest.model_copy(update={"spec": manifest.spec.model_copy(update={"ingress": ingress_spec})})
        # Compute a simple signature to avoid redundant reloads
        if isinstance(upstream, (list, tuple)):
            ups_tuple = tuple(upstream)
        else:
            ups_tuple = (upstream,)
        sig = f"{manifest.spec.ingress.host}|{readiness_path}|{ups_tuple}"
        app = manifest.metadata.name

        site_path = None
        if self._last_sig.get(app) != sig:
            # Prefer-first policy biases towards listed upstream order (new revision first)
            rollout = getattr(manifest.spec, "rollout", {}) or {}
            strategy = str(rollout.get("strategy", "parallel")).lower()
            canary_weight = int(rollout.get("weight", 1)) if strategy == "canary" else 1
            # Auto progression: rollout.auto { start, step, intervalSeconds, max }
            if strategy == "canary" and isinstance(rollout.get("auto", None), dict):
                auto = rollout["auto"]
                try:
                    step = float(auto.get("step", 1))
                    interval = float(auto.get("intervalSeconds", 60))
                    maxw = float(auto.get("max", max(canary_weight, 10)))
                    startw = float(auto.get("start", canary_weight))
                    now = datetime.now(timezone.utc)
                    if self._store:
                        row = self._store.get_canary_state(app)
                        if row is None:
                            next_at = (now + timedelta(seconds=interval)).isoformat()
                            self._store.upsert_canary_state(app, weight=startw, next_step_at=next_at, step=step, max_weight=maxw)
                            effective = startw
                        else:
                            # parse next_step_at
                            try:
                                due = datetime.fromisoformat(row.get("next_step_at", ""))
                            except Exception:
                                due = now
                            effective = max(startw, float(row.get("weight", startw)))
                            if interval <= 0 or now >= due:
                                effective = min(maxw, effective + step)
                                next_at = (now + timedelta(seconds=interval)).isoformat()
                                self._store.upsert_canary_state(app, weight=effective, next_step_at=next_at, step=step, max_weight=maxw)
                        canary_weight = int(max(canary_weight, effective))
                    else:
                        # fallback to in-memory when no store is present
                        import time as _t

                        now_ts = float(_t.time())
                        state = self._canary_state.get(app) or {"weight": startw, "ts": now_ts}
                        if interval <= 0 or (now_ts - state.get("ts", now_ts)) >= interval:
                            state["weight"] = min(maxw, state.get("weight", canary_weight) + step)
                            state["ts"] = now_ts
                            self._canary_state[app] = state
                        canary_weight = int(max(canary_weight, state.get("weight", canary_weight)))
                except Exception:
                    pass
            prefer_first = True
            try:
                site_path = self._manager.apply(
                    manifest, upstream, readiness_path, prefer_first=prefer_first, first_weight=canary_weight
                )  # type: ignore[arg-type]
            except TypeError:
                try:
                    site_path = self._manager.apply(manifest, upstream, readiness_path)  # type: ignore[arg-type]
                except TypeError:
                    site_path = self._manager.apply(manifest, upstream)
            self._last_sig[app] = sig
            self._dirty = True
        else:
            # No change; use existing path if available
            try:
                site_path = self._manager._site_path(app)  # type: ignore[attr-defined]
            except Exception:
                site_path = None
        return IngressResult(
            app_name=manifest.metadata.name,
            host=manifest.spec.ingress.host,
            config_path=str(site_path) if site_path else None,
        )

    def remove(self, app_name: str) -> None:
        self._manager.remove(app_name)

    def reload(self) -> None:
        if not self._dirty:
            return
        try:
            self._manager.reload()
        except Exception as exc:  # pragma: no cover - defensive path
            # Do not crash the controller if Caddy reload is unavailable in dev.
            import logging

            logging.getLogger(__name__).warning("ingress reload skipped: %s", exc)
        finally:
            self._dirty = False
