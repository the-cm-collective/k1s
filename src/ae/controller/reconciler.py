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
        # Track last seen restart counts per container/app to detect crash loops
        self._last_restart_counts: dict[str, int] = {}
        self._last_restart_ts: dict[str, float] = {}
        # Create cooldowns: app -> unix timestamp until which we avoid creating new replicas
        self._create_cooldown_until: dict[str, float] = {}

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
            result = RuntimeResult(
                revision=revision, created=0, updated=0, removed=0, replica_states=[]
            )
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

        import time as _t

        now_ts = float(_t.time())
        limit_create = 1 if strategy == "ordered" else None
        # Apply create cooldown when active
        until = float(self._create_cooldown_until.get(manifest.metadata.name, 0.0) or 0.0)
        if until > now_ts:
            limit_create = 0
        # Keep old replicas during rollout to respect surge/unavailable; we'll remove them after readiness check
        try:
            result = self._runtime.ensure_app(  # type: ignore[arg-type]
                manifest_for_runtime, revision, keep_old=True, limit_create=limit_create
            )
        except TypeError:
            # Compatibility with runtimes/tests that don't accept new kwargs
            result = self._runtime.ensure_app(manifest_for_runtime, revision)  # type: ignore[arg-type]
        health_report = self._health_manager.evaluate(manifest, result)
        # Record probe backoff metrics from messages
        try:
            from ae.observability.http_api import record_probe_backoff  # type: ignore

            def _parse_bo(msg: str | None) -> int:
                try:
                    import re

                    m = re.search(r"backoff \((\d+)s\)", str(msg or ""))
                    return int(m.group(1)) if m else 0
                except Exception:
                    return 0

            for rep in getattr(health_report, "replicas", []) or []:
                app_name = manifest.metadata.name
                record_probe_backoff(
                    app_name,
                    rep.replica_id,
                    "readiness",
                    _parse_bo(getattr(rep, "readiness_message", "")),
                )
                record_probe_backoff(
                    app_name,
                    rep.replica_id,
                    "liveness",
                    _parse_bo(getattr(rep, "liveness_message", "")),
                )
        except Exception:
            pass
        # Pre-switch rollout hook (optional)
        hook_ok = True
        hook_msg = None
        try:
            ro = getattr(manifest.spec, "rollout", {}) or {}
            hooks = ro.get("hooks") if isinstance(ro, dict) else None
            pre = (hooks or {}).get("preSwitch") or (hooks or {}).get("pre_switch")
            if pre:
                hook_ok, hook_msg = self._run_rollout_hook(manifest, result, pre)
                if not hook_ok:
                    try:
                        self._state_store.record_event(
                            manifest.metadata.name,
                            revision,
                            "HookFailed",
                            f"preSwitch: {hook_msg or 'failed'}",
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        if manifest.spec.ingress and self._ingress_service and hook_ok:
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
        # Post-switch rollout hook (optional, best-effort)
        try:
            if hook_ok:
                ro = getattr(manifest.spec, "rollout", {}) or {}
                hooks = ro.get("hooks") if isinstance(ro, dict) else None
                post = (hooks or {}).get("postSwitch") or (hooks or {}).get("post_switch")
                if post:
                    ok, msg = self._run_rollout_hook(manifest, result, post)
                    ev = "HookPassed" if ok else "HookFailed"
                    self._state_store.record_event(
                        manifest.metadata.name,
                        revision,
                        ev,
                        f"postSwitch: {msg or ('ok' if ok else 'failed')}",
                    )
        except Exception:
            pass
        revision_status = self._calculate_revision_status(manifest, health_report, result)

        # Crashloop detection: emit events when container restart counts surge in a window
        try:
            self._detect_crashloops(manifest)
        except Exception:
            pass

        # Remove old revisions if availability is satisfied, except while canary is active
        desired = manifest.spec.replicas
        ro_now = getattr(manifest.spec, "rollout", {}) or {}
        strat_now = str(ro_now.get("strategy", "parallel")).lower()
        w_now = None
        try:
            w_now = int(ro_now.get("weight")) if ro_now.get("weight") is not None else None
        except Exception:
            w_now = None
        canary_active = strat_now == "canary" and (w_now or 0) > 0 and (w_now or 0) < 100
        if (not canary_active) and health_report.ready_replicas >= max(0, desired - max_unavail):
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

        # Emit rollout change events (e.g., canary enabled/updated/disabled)
        try:
            prev = self._state_store.get_status(manifest.metadata.name)
            prev_ro = None
            if prev is not None:
                try:
                    prev_man = self._state_store.get_revision_manifest(prev.app_name, prev.revision)
                    prev_ro = getattr(prev_man.spec, "rollout", {}) or {}
                except Exception:
                    prev_ro = None
            new_ro = getattr(manifest.spec, "rollout", {}) or {}

            def _ro_view(ro: dict | None) -> tuple[str, int | None, bool]:
                try:
                    strat = str((ro or {}).get("strategy", "parallel")).lower()
                    weight = (ro or {}).get("weight")
                    w = int(weight) if weight is not None else None
                    paused = bool((ro or {}).get("pause", False))
                    return strat, w, paused
                except Exception:
                    return "parallel", None, False

            p_strat, p_weight, _p_paused = _ro_view(prev_ro if isinstance(prev_ro, dict) else {})
            n_strat, n_weight, _n_paused = _ro_view(new_ro if isinstance(new_ro, dict) else {})
            ev_type = None
            msg = None
            if p_strat != "canary" and n_strat == "canary" and (n_weight or 0) > 0:
                ev_type = "CanaryEnabled"
                msg = (
                    f"canary enabled: weight {int(n_weight)}%"
                    if n_weight is not None
                    else "canary enabled"
                )
            elif p_strat == "canary" and (n_strat != "canary" or (n_weight or 0) == 0):
                ev_type = "CanaryDisabled"
                msg = "canary disabled"
            elif (
                p_strat == "canary"
                and n_strat == "canary"
                and (p_weight != n_weight)
                and (n_weight is not None)
            ):
                ev_type = "CanaryUpdated"
                msg = f"canary weight {int(p_weight or 0)}% -> {int(n_weight)}%"
            if ev_type and msg:
                self._state_store.record_event(manifest.metadata.name, revision, ev_type, msg)
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

    def _run_rollout_hook(self, manifest, runtime_result, hook) -> tuple[bool, str | None]:  # noqa: ANN001
        """Execute a rollout hook against the new revision.

        Supports:
          - exec: list[str] executed in the first ready replica (fallback to first replica)
          - tcp: { port } TCP connect to replica endpoint host:port
          - timeoutSeconds: optional per-hook timeout (default 5)
        """
        timeout = 5
        try:
            timeout = int(hook.get("timeoutSeconds", 5))
        except Exception:
            timeout = 5
        replicas = list(runtime_result.replica_states or [])
        target = None
        # prefer ready
        for r in replicas:
            if getattr(r, "ready", False):
                target = r
                break
        if target is None and replicas:
            target = replicas[0]
        if target is None:
            return False, "no replicas available for hook"
        # exec hook
        if "exec" in hook:
            cmd = hook.get("exec") or []
            if not isinstance(cmd, (list, tuple)) or not cmd:
                return False, "exec hook missing/invalid command"
            try:
                code = self._runtime.exec(target.replica_id, [str(x) for x in cmd], timeout=timeout)
                return (code == 0), (f"exec rc={code}")
            except Exception as exc:  # noqa: BLE001
                return False, f"exec error: {exc}"
        # tcp hook
        if "tcp" in hook:
            port = None
            try:
                port = int((hook.get("tcp") or {}).get("port", 0))
            except Exception:
                port = 0
            if port <= 0:
                return False, "tcp.port must be set"
            # Build target host: use resolved endpoint host or localhost
            host = "127.0.0.1"
            try:
                ep = str(getattr(target, "endpoint", "") or "")
                if ":" in ep:
                    host = ep.split(":", 1)[0]
            except Exception:
                pass
            import socket as _s

            try:
                with _s.create_connection((host, int(port)), timeout=max(1, int(timeout))):
                    return True, "tcp ok"
            except OSError as exc:
                return False, f"tcp error: {exc}"
        return False, "unsupported hook"

    def _detect_crashloops(self, manifest: AppManifest) -> None:
        import time as _t

        app = manifest.metadata.name
        try:
            infos = self._runtime.list_containers_info()  # type: ignore[attr-defined]
        except Exception:
            infos = []
        # thresholds
        try:
            wnd = float(os.getenv("AE_RESTART_WINDOW_SEC", "60"))
            thr = int(os.getenv("AE_RESTART_THRESHOLD", "3"))
        except Exception:
            wnd, thr = 60.0, 3
        now = float(_t.time())
        surges = 0
        for c in infos or []:
            labels = c.get("labels") or {}
            if (labels.get("ae.app") or "") != app:
                continue
            name = str(c.get("name", ""))
            rc = int(c.get("restart_count", 0) or 0)
            key = f"{app}|{name}"
            last_rc = self._last_restart_counts.get(key, rc)
            last_ts = self._last_restart_ts.get(key, now)
            if rc > last_rc:
                # increased since last observe; if within window, count surge
                if (now - last_ts) <= wnd and (rc - last_rc) >= 1:
                    surges += rc - last_rc
                # update trackers
                self._last_restart_counts[key] = rc
                self._last_restart_ts[key] = now
        if surges >= thr:
            try:
                # record event and mark crashloop in API for a short TTL
                self._state_store.record_event(
                    app,
                    0,
                    "CrashLoopDetected",
                    f"container restarts surged: {surges} in {int(wnd)}s (>= {thr})",
                )
            except Exception:
                pass
            try:
                from ae.observability.http_api import set_app_crashloop  # type: ignore

                set_app_crashloop(app, ttl_seconds=float(os.getenv("AE_CRASHLOOP_TTL", "300")))
            except Exception:
                pass
            # Apply create cooldown
            try:
                cd = float(os.getenv("AE_RECREATE_COOLDOWN_SEC", "30"))
            except Exception:
                cd = 30.0
            try:
                self._create_cooldown_until[app] = float(_t.time()) + float(cd)
                self._state_store.record_event(
                    app, 0, "RecreateCooldown", f"suppressing new replica creation for {int(cd)}s"
                )
            except Exception:
                pass

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
        # When canary is enabled, include previous revision endpoints to split traffic
        try:
            ro = getattr(manifest.spec, "rollout", {}) or {}
            strat = str(ro.get("strategy", "parallel")).lower()
            w = int(ro.get("weight")) if ro.get("weight") is not None else 0
        except Exception:
            strat, w = "parallel", 0
        if ready_eps and strat == "canary" and w > 0:
            try:
                items = self._runtime.list_containers_info()  # type: ignore[attr-defined]
            except Exception:
                items = []
            prev_eps: list[str] = []
            cur_rev = str(result.revision)
            app = manifest.metadata.name
            for it in items or []:
                labs = it.get("labels") or {}
                if (labs.get("ae.app") or "") != app:
                    continue
                if (labs.get("ae.revision") or "") == cur_rev:
                    continue
                ports = list(it.get("host_ports") or [])
                if not ports:
                    continue
                try:
                    ep = f"127.0.0.1:{int(ports[0])}"
                    prev_eps.append(ep)
                except Exception:
                    continue
            # Merge, de-duplicating, keeping new first to honor prefer-first policy
            seen = set()
            merged: list[str] = []
            for ep in ready_eps + prev_eps:
                if ep in seen:
                    continue
                seen.add(ep)
                merged.append(ep)
            if merged:
                return merged
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

        # Prefer absolute path for runtime volume mounts to avoid Docker/Podman
        # treating relative paths as named volumes (which can fail validation).
        try:
            abs_root = root.resolve()
        except Exception:
            abs_root = root
        return abs_root if wrote else None
