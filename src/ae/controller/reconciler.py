"""Reconcile loop skeleton for the application engine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import os

from ae.controller.health import HealthManager, HealthReport, ReplicaHealth
from ae.controller.spec import AppManifest, VolumeSpec, load_manifest
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
        # Inject exec callback for exec probes
        try:
            self._health_manager.set_exec_callback(
                lambda rid, cmd, t: self._runtime.exec(rid, cmd, timeout=t)
            )
        except Exception:
            pass

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

        # Apply config/secret projections. Be resilient to secret errors so a
        # single bad SOPS file does not crash the controller under load.
        try:
            manifest_with_env = self._apply_configs_and_secrets(manifest)
        except Exception as exc:  # noqa: BLE001
            try:
                self._state_store.record_event(
                    manifest.metadata.name,
                    revision,
                    "SecretError",
                    f"secrets/config application failed: {exc}",
                )
            except Exception:
                pass
            # Fallback: continue without injecting secret/config env
            manifest_with_env = manifest
        # Prepare file projections and add a read-only volume mount if any files were written
        projection_root = self._prepare_file_projections(manifest, revision)
        manifest_for_runtime = manifest_with_env
        if projection_root is not None:
            vols = list(manifest_for_runtime.spec.volumes)
            mount_root = f"/var/run/ae/config/{manifest.metadata.name}"
            # Append a typed VolumeSpec so downstream code sees attributes, not dicts
            vols.append(
                VolumeSpec(host_path=str(projection_root), mount_path=mount_root, read_only=True)
            )
            updated_spec = manifest_for_runtime.spec.model_copy(update={"volumes": vols})
            manifest_for_runtime = manifest_for_runtime.model_copy(update={"spec": updated_spec})

        # Rollout policy
        rollout = getattr(manifest.spec, "rollout", {}) or {}
        strategy = str(rollout.get("strategy", "parallel")).lower()
        max_surge = int(rollout.get("maxSurge", 1))
        max_unavail = int(rollout.get("maxUnavailable", 0))

        # Pause: record snapshot with current status and skip runtime/ingress changes
        if bool(rollout.get("pause", False)):
            # Build a health report from last known status (if any)
            prev = self._state_store.get_status(manifest.metadata.name)
            replicas = self._state_store.list_replicas(manifest.metadata.name)
            hr = HealthReport(
                ready_replicas=prev.ready_replicas if prev else 0,
                live_replicas=prev.live_replicas if prev else 0,
                replicas=[
                    ReplicaHealth(
                        replica_id=r.replica_id,
                        ready=r.ready,
                        live=r.live,
                        readiness_message=r.readiness_message,
                        liveness_message=r.liveness_message,
                    )
                    for r in replicas
                ],
            )
            result = RuntimeResult(revision=revision, created=0, updated=0, removed=0, replica_states=[])
            revision_status = "paused"
            self._state_store.record_snapshot(
                manifest=manifest_for_runtime,
                runtime_result=result,
                health_report=hr,
                revision=revision,
                revision_status=revision_status,
            )
            self._state_store.record_event(
                manifest.metadata.name,
                revision,
                "RolloutPaused",
                "Rollout paused by manifest; runtime unchanged",
            )
            return ReconcileReport(
                app_name=manifest.metadata.name,
                created=0,
                updated=0,
                removed=0,
                ready_replicas=hr.ready_replicas,
                live_replicas=hr.live_replicas,
                revision=revision,
                revision_status=revision_status,
            )

        limit_create = 1 if strategy == "ordered" else None
        # Keep old replicas during rollout to respect surge/unavailable; we'll remove them after readiness check
        try:
            result = self._runtime.ensure_app(  # type: ignore[arg-type]
                manifest_for_runtime, revision, keep_old=True, limit_create=limit_create
            )
        except TypeError:
            # Compatibility with runtimes/tests that don't accept new kwargs
            result = self._runtime.ensure_app(manifest_for_runtime, revision)  # type: ignore[arg-type]
        health_report = self._health_manager.evaluate(manifest, result)
        if manifest.spec.ingress and self._ingress_service:
            upstreams = self._select_upstreams(manifest, result, health_report)
            if upstreams:
                upstream_param = upstreams[0] if len(upstreams) == 1 else upstreams
                self._ingress_service.apply(manifest, upstream_param)
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
        revision_status = self._calculate_revision_status(manifest, health_report, result)

        # Remove old revisions if availability is satisfied
        desired = manifest.spec.replicas
        if health_report.ready_replicas >= max(0, desired - max_unavail):
            try:
                removed_old = self._runtime.remove_old_revisions(manifest.metadata.name, revision)
                if removed_old > 0:
                    self._state_store.record_event(
                        manifest.metadata.name,
                        revision,
                        "RolloutOldRemoved",
                        f"Removed {removed_old} old revision container(s)",
                    )
            except Exception:
                pass

        self._state_store.record_snapshot(
            manifest=manifest_for_runtime,
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

        # Prefer only ready endpoints; defer ingress changes until at least one
        # replica is ready to avoid transient 502s during warm-up.
        ready_eps: list[str] = []
        for replica in health_report.replicas:
            if not replica.ready:
                continue
            state = states_by_id.get(replica.replica_id)
            if state and state.endpoint:
                ready_eps.append(state.endpoint)
        if ready_eps:
            return ready_eps

        # No ready endpoints yet: return empty to keep previous ingress
        # configuration intact until readiness is achieved.
        return []

    def _compute_spec_hash(self, manifest: AppManifest) -> str:
        payload = json.dumps(
            manifest.model_dump(by_alias=True, exclude_none=True),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _calculate_revision_status(
        self, manifest: AppManifest, report: HealthReport, runtime_result: RuntimeResult
    ) -> str:
        """Classify status with stable, non-flappy semantics.

        - ready:       ready_replicas >= desired
        - progressing: replicas for this revision exist (even if liveness fails yet),
                       or runtime reported creates/updates but replicas not observed yet (race)
        - degraded:    no replicas exist and no recent create/update for this revision

        Rationale: right after scheduling/creation, HTTP liveness probes can fail
        briefly until the container publishes ports and starts serving. Treating
        that period as "progressing" avoids misleading degraded states during
        normal warm-up while still surfacing real outages (no replicas present).
        """
        desired = max(1, int(manifest.spec.replicas))
        if report.ready_replicas >= desired:
            return "ready"
        # Consider any recorded replica (regardless of liveness) as progressing.
        if len(report.replicas) > 0:
            return "progressing"
        # Race: runtime created/updated containers but list didn't return them yet
        if (runtime_result.created + runtime_result.updated) > 0:
            return "progressing"
        return "degraded"

    def _apply_configs_and_secrets(self, manifest: AppManifest) -> AppManifest:
        env_map: dict[str, str] = {}

        # Configs first
        if getattr(manifest.spec, "config_refs", None):
            cfg_env = self._config_manager.load_env(manifest.spec.config_refs)
            env_map.update(cfg_env)

        # Secrets override configs
        if manifest.spec.secret_refs:
            if self._secret_manager:
                sec_env = self._secret_manager.load_env(manifest.spec.secret_refs)
                env_map.update(sec_env)
            else:
                import json, yaml
                from pathlib import Path as _P

                for ref in manifest.spec.secret_refs:
                    try:
                        content = _P(ref.path).read_text(encoding="utf-8")
                        try:
                            data = json.loads(content)
                        except json.JSONDecodeError:
                            data = yaml.safe_load(content)
                        if isinstance(data, dict):
                            for m in ref.env:
                                if m.key in data:
                                    env_map[m.name] = str(data[m.key])
                    except Exception:
                        pass

        # Manifest env wins last
        for item in manifest.spec.env:
            if "name" in item and "value" in item:
                env_map[item["name"]] = item["value"]

        if not env_map:
            return manifest

        merged_env = [{"name": k, "value": v} for k, v in sorted(env_map.items())]
        updated_spec = manifest.spec.model_copy(update={"env": merged_env})
        return manifest.model_copy(update={"spec": updated_spec})

    def _prepare_file_projections(self, manifest: AppManifest, revision: int) -> Path | None:
        """Write config and secret key/value pairs into files for the app.

        Writes two folders under state/projections/<app>-rev<rev>/{config,secret} with one
        file per key containing the string value. Returns the projection root if any files
        were written, else None.
        """
        app = manifest.metadata.name
        root = Path("state/projections") / f"{app}-rev{revision}"
        wrote = False

        # Config files
        if getattr(manifest.spec, "config_refs", None):
            cfg_dir = root / "config"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            for ref in manifest.spec.config_refs:
                try:
                    data = self._config_manager._load(Path(ref.path))  # type: ignore[arg-type]
                except Exception:
                    continue
                if getattr(ref, "files", None):
                    for mapping in ref.files:
                        k = str(mapping.get("key", ""))
                        fn = str(mapping.get("file", ""))
                        if not k or not fn:
                            continue
                        if k not in data:
                            continue
                        path = (root / fn) if fn.startswith("/") else (cfg_dir / fn)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            path.write_text(str(data[k]), encoding="utf-8")
                            wrote = True
                        except Exception:
                            pass
                else:
                    for k, v in data.items():
                        try:
                            (cfg_dir / str(k)).write_text(str(v), encoding="utf-8")
                            wrote = True
                        except Exception:
                            pass

        # Secret files
        if manifest.spec.secret_refs:
            sec_dir = root / "secret"
            sec_dir.mkdir(parents=True, exist_ok=True)
            for ref in manifest.spec.secret_refs:
                # load via manager if available, else plaintext YAML/JSON
                data = None
                if self._secret_manager:
                    try:
                        data = self._secret_manager._decrypt(Path(ref.path))  # type: ignore[arg-type]
                    except Exception:
                        data = None
                if data is None:
                    try:
                        content = Path(ref.path).read_text(encoding="utf-8")
                        import json, yaml

                        try:
                            data = json.loads(content)
                        except json.JSONDecodeError:
                            data = yaml.safe_load(content)
                    except Exception:
                        data = None
                if not isinstance(data, dict):
                    continue
                if getattr(ref, "files", None):
                    for mapping in ref.files:
                        k = str(mapping.get("key", ""))
                        fn = str(mapping.get("file", ""))
                        if not k or not fn:
                            continue
                        if k not in data:
                            continue
                        path = (root / fn) if fn.startswith("/") else (sec_dir / fn)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            path.write_text(str(data[k]), encoding="utf-8")
                            wrote = True
                        except Exception:
                            pass
                else:
                    for k, v in data.items():
                        try:
                            (sec_dir / str(k)).write_text(str(v), encoding="utf-8")
                            wrote = True
                        except Exception:
                            pass

        return root if wrote else None
