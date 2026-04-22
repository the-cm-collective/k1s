"""Leader-gated CronJob scheduling over shared HA authority state."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ae.apishim.ha_store import (
    CRONJOB_SCHEDULED_AT_LABEL,
    OWNER_API_VERSION_LABEL,
    OWNER_KIND_LABEL,
    OWNER_NAME_LABEL,
    OWNER_UID_LABEL,
    WORKLOAD_KIND_LABEL,
)
from ae.apishim.store import K8sObject
from ae.controller.state import AuthorityObjectEntry, RegistryConflictError, SQLiteStateStore
from ae.controller.spec import app_key
from ae.k8s.convert import manifest_from_k8s_workload

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CronJobAuthorityControllerConfig:
    interval_s: float = 5.0


class CronJobAuthorityController:
    """Schedules CronJob-backed Job manifests from shared authority state."""

    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        config: CronJobAuthorityControllerConfig | None = None,
        authority=None,
    ) -> None:
        self._store = store
        self._config = config or CronJobAuthorityControllerConfig()
        self._authority = authority
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False
        self._stop = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("cronjob authority loop error: %s", exc)
            time.sleep(self._config.interval_s)

    def run_once(self) -> None:
        if not self._is_leader():
            return
        now = self._now()
        try:
            entries = self._store.list_authority_objects("batch", "v1", "cronjobs")
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("cronjob authority list failed: %s", exc)
            return
        for entry in entries:
            if self._stop or not self._is_leader():
                return
            self._reconcile_cronjob(entry, now)

    def _reconcile_cronjob(self, entry: AuthorityObjectEntry, now: datetime) -> None:
        spec = dict(entry.spec or {})
        if bool(spec.get("suspend", False)):
            return
        scheduled_at = _next_due_schedule(entry, now)
        if scheduled_at is None:
            return
        slot_iso = _iso_utc(scheduled_at)
        job_name = _scheduled_job_name(entry.name, scheduled_at)
        self._ensure_job(entry, job_name, slot_iso)
        if not self._is_leader():
            return
        self._update_cronjob_status(entry, slot_iso)

    def _ensure_job(self, entry: AuthorityObjectEntry, job_name: str, slot_iso: str) -> None:
        namespace = entry.namespace or "default"
        job_app_name = app_key(job_name, namespace)
        try:
            if self._store.get_registered_entry(job_app_name) is not None:
                return
        except Exception:
            pass
        manifest = _job_manifest_from_cronjob(entry, job_name)
        labels = {
            WORKLOAD_KIND_LABEL: "job",
            OWNER_API_VERSION_LABEL: "batch/v1",
            OWNER_KIND_LABEL: "CronJob",
            OWNER_NAME_LABEL: entry.name,
            OWNER_UID_LABEL: str((entry.metadata or {}).get("uid") or entry.name),
            CRONJOB_SCHEDULED_AT_LABEL: slot_iso,
        }
        try:
            self._store.register_app(
                manifest,
                source="apishim-cronjob",
                labels=labels,
                expected_resource_version=0,
            )
        except RegistryConflictError:
            return

    def _update_cronjob_status(self, entry: AuthorityObjectEntry, slot_iso: str) -> None:
        try:
            fresh = self._store.get_authority_object(
                entry.group,
                entry.version,
                entry.resource,
                entry.namespace,
                entry.name,
            )
        except Exception:
            fresh = None
        if fresh is None:
            return
        status = dict(fresh.status or {})
        if status.get("lastScheduleTime") == slot_iso and status.get("lastSuccessfulTime") == slot_iso:
            return
        status.setdefault("active", [])
        status["lastScheduleTime"] = slot_iso
        status["lastSuccessfulTime"] = slot_iso
        try:
            self._store.register_authority_object(
                fresh.group,
                fresh.version,
                fresh.resource,
                fresh.namespace,
                fresh.name,
                kind=fresh.kind or "CronJob",
                metadata=dict(fresh.metadata or {}),
                spec=dict(fresh.spec or {}),
                status=status,
                expected_resource_version=fresh.resource_version,
            )
        except RegistryConflictError:
            return

    def _is_leader(self) -> bool:
        if self._authority is None:
            return True
        try:
            return bool(self._authority.snapshot().is_leader)
        except Exception:
            return False

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


def _job_manifest_from_cronjob(entry: AuthorityObjectEntry, job_name: str):
    spec = dict(entry.spec or {})
    namespace = entry.namespace or "default"
    job_spec = dict((spec.get("jobTemplate") or {}).get("spec") or {})
    job_metadata = {
        "name": job_name,
        "namespace": namespace,
        "labels": dict((((job_spec.get("template") or {}).get("metadata") or {}).get("labels") or {})),
    }
    job_metadata["ownerReferences"] = [
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "name": entry.name,
            "uid": str((entry.metadata or {}).get("uid") or entry.name),
            "controller": True,
            "blockOwnerDeletion": True,
        }
    ]
    job = K8sObject(
        "batch",
        "v1",
        "jobs",
        namespace,
        job_name,
        job_metadata,
        job_spec,
        {},
        0,
    )
    manifest = manifest_from_k8s_workload(job)
    labels = dict(getattr(manifest.metadata, "labels", None) or {})
    labels.setdefault("ae.workload", "job")
    updates: dict[str, object] = {
        "workload": "job",
        "replicas": int(job_spec.get("parallelism", 1) or 1),
    }
    if job_spec.get("backoffLimit") is not None:
        updates["jobBackoffLimit"] = int(job_spec.get("backoffLimit"))
    if job_spec.get("ttlSecondsAfterFinished") is not None:
        updates["jobTtlSecondsAfterFinished"] = int(job_spec.get("ttlSecondsAfterFinished"))
    return manifest.model_copy(
        update={
            "metadata": manifest.metadata.model_copy(update={"labels": labels}),
            "spec": manifest.spec.model_copy(update=updates),
        }
    )


def _next_due_schedule(entry: AuthorityObjectEntry, now: datetime) -> datetime | None:
    spec = dict(entry.spec or {})
    annotations = (entry.metadata or {}).get("annotations") or {}
    interval_raw = annotations.get("cronjob.k1s.dev/intervalSeconds")
    schedule_expr = annotations.get("cronjob.k1s.dev/schedule") or spec.get("schedule")
    last_schedule = _parse_status_time((entry.status or {}).get("lastScheduleTime"))
    if interval_raw is not None:
        return _next_interval_schedule(now, last_schedule, interval_raw)
    if schedule_expr:
        next_run = _next_cron_schedule(str(schedule_expr), now, last_schedule)
        if next_run is not None:
            return next_run
    return _next_interval_schedule(now, last_schedule, 60)


def _next_interval_schedule(
    now: datetime,
    last_schedule: datetime | None,
    interval_raw: object,
) -> datetime | None:
    try:
        interval_s = int(interval_raw)
    except Exception:
        interval_s = 60
    if interval_s <= 0:
        slot = datetime.fromtimestamp(int(now.timestamp()), tz=timezone.utc)
    elif last_schedule is None:
        slot = datetime.fromtimestamp(
            int(now.timestamp() // interval_s) * interval_s,
            tz=timezone.utc,
        )
    else:
        slot = last_schedule + timedelta(seconds=interval_s)
    if last_schedule is not None and slot <= last_schedule:
        return None
    if slot > now:
        return None
    return slot


def _next_cron_schedule(
    schedule_expr: str,
    now: datetime,
    last_schedule: datetime | None,
) -> datetime | None:
    try:
        from croniter import croniter  # type: ignore
    except Exception:
        return None
    base = last_schedule if last_schedule is not None else (now - timedelta(seconds=60))
    try:
        next_run = croniter(schedule_expr, base).get_next(datetime)
    except Exception:
        return None
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    else:
        next_run = next_run.astimezone(timezone.utc)
    if next_run > now:
        return None
    return next_run


def _parse_status_time(raw: object) -> datetime | None:
    if raw in {None, ""}:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _scheduled_job_name(cronjob_name: str, scheduled_at: datetime) -> str:
    suffix = f"run-{scheduled_at.strftime('%Y%m%d%H%M%S')}"
    plain = f"{cronjob_name}-{suffix}"
    if len(plain) <= 63:
        return plain
    digest = hashlib.sha1(cronjob_name.encode("utf-8")).hexdigest()[:8]  # noqa: S324
    prefix_budget = max(1, 63 - len(suffix) - len(digest) - 2)
    return f"{cronjob_name[:prefix_budget]}-{digest}-{suffix}"


__all__ = ["CronJobAuthorityController", "CronJobAuthorityControllerConfig"]
