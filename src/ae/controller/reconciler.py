"""Reconcile loop skeleton for the application engine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ae.controller.health import HealthManager, HealthReport
from ae.controller.spec import AppManifest, load_manifest
from ae.runtime import RuntimeAdapter, RuntimeResult

from .state import SQLiteStateStore
from ae.ingress.service import IngressService
from ae.secrets import SecretManager
from ae.config.manager import ConfigManager


@dataclass(slots=True)
class ReconcileReport:
    """Summary of a reconcile run."""

    app_name: str
    created: int
    updated: int
    removed: int
    ready_replicas: int
    live_replicas: int
    revision: int
    revision_status: str


class Reconciler:
    """Coordinates manifest application across runtime, health, and state store."""

    def __init__(
        self,
        runtime: RuntimeAdapter,
        state_store: SQLiteStateStore,
        health_manager: HealthManager | None = None,
        ingress_service: IngressService | None = None,
        secret_manager: SecretManager | None = None,
        config_manager: ConfigManager | None = None,
    ) -> None:
        self._runtime = runtime
        self._state_store = state_store
        self._health_manager = health_manager or HealthManager()
        self._ingress_service = ingress_service
        self._secret_manager = secret_manager
        self._config_manager = config_manager or ConfigManager()

    def reconcile_manifest_path(self, path: Path) -> ReconcileReport:
        """Load a manifest from disk and reconcile it."""

        manifest = load_manifest(path)
        return self.reconcile(manifest)

    def reconcile(self, manifest: AppManifest) -> ReconcileReport:
        """Reconcile the runtime to match the manifest."""

        spec_hash = self._compute_spec_hash(manifest)
        revision, _ = self._state_store.prepare_revision(manifest, spec_hash)

        self._state_store.record_event(
            manifest.metadata.name,
            revision,
            "ApplyStarted",
            f"Reconciling revision {revision}",
        )

        manifest_with_env = self._apply_configs_and_secrets(manifest)

        result = self._runtime.ensure_app(manifest_with_env, revision)
        health_report = self._health_manager.evaluate(manifest, result)
        if manifest.spec.ingress and self._ingress_service:
            upstreams = self._select_upstreams(manifest, result, health_report)
            if upstreams:
                self._ingress_service.apply(manifest, upstreams)
                self._ingress_service.reload()
                self._state_store.record_event(
                    manifest.metadata.name,
                    revision,
                    "IngressConfigured",
                    f"Ingress upstreams set to {', '.join(upstreams)}",
                )
        elif self._ingress_service and not manifest.spec.ingress:
            self._ingress_service.remove(manifest.metadata.name)
            self._ingress_service.reload()
            self._state_store.record_event(
                manifest.metadata.name,
                revision,
                "IngressRemoved",
                "Ingress configuration removed",
            )
        revision_status = self._calculate_revision_status(manifest, health_report)

        self._state_store.record_snapshot(
            manifest=manifest_with_env,
            runtime_result=result,
            health_report=health_report,
            revision=revision,
            revision_status=revision_status,
        )
        self._state_store.record_event(
            manifest.metadata.name,
            revision,
            "ApplyCompleted",
            f"Revision {revision} status {revision_status}",
        )
        return ReconcileReport(
            app_name=manifest.metadata.name,
            created=result.created,
            updated=result.updated,
            removed=result.removed,
            ready_replicas=health_report.ready_replicas,
            live_replicas=health_report.live_replicas,
            revision=revision,
            revision_status=revision_status,
        )

    def _select_upstreams(
        self,
        manifest: AppManifest,
        result: RuntimeResult,
        health_report: HealthReport,
    ) -> list[str]:
        states_by_id = {state.replica_id: state for state in result.replica_states}

        ready_eps: list[str] = []
        for replica in health_report.replicas:
            if not replica.ready:
                continue
            state = states_by_id.get(replica.replica_id)
            if state and state.endpoint:
                ready_eps.append(state.endpoint)
        if ready_eps:
            return ready_eps

        # fall back to any endpoints we have
        any_eps = [s.endpoint for s in result.replica_states if s.endpoint]
        if any_eps:
            return any_eps  # type: ignore[return-value]

        # final fallback: first declared port on loopback
        if manifest.spec.ports:
            port = manifest.spec.ports[0].container_port
            return [f"127.0.0.1:{port}"]
        return []

    def _compute_spec_hash(self, manifest: AppManifest) -> str:
        payload = json.dumps(
            manifest.model_dump(by_alias=True, exclude_none=True),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _calculate_revision_status(self, manifest: AppManifest, report: HealthReport) -> str:
        desired = manifest.spec.replicas
        if report.ready_replicas >= desired:
            return "ready"
        if report.live_replicas >= desired:
            return "progressing"
        return "degraded"

    def _apply_configs_and_secrets(self, manifest: AppManifest) -> AppManifest:
        env_map: dict[str, str] = {}

        # Configs first
        if getattr(manifest.spec, "config_refs", None):
            cfg_env = self._config_manager.load_env(manifest.spec.config_refs)
            env_map.update(cfg_env)

        # Secrets override configs
        if manifest.spec.secret_refs and self._secret_manager:
            sec_env = self._secret_manager.load_env(manifest.spec.secret_refs)
            env_map.update(sec_env)

        # Manifest env wins last
        for item in manifest.spec.env:
            if "name" in item and "value" in item:
                env_map[item["name"]] = item["value"]

        if not env_map:
            return manifest

        merged_env = [{"name": k, "value": v} for k, v in sorted(env_map.items())]
        updated_spec = manifest.spec.model_copy(update={"env": merged_env})
        return manifest.model_copy(update={"spec": updated_spec})
