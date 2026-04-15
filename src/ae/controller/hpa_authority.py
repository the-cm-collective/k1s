"""Leader-gated HPA control loop over shared HA authority state."""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable

from ae.controller.spec import AppManifest, app_key
from ae.controller.state import (
    AuthorityObjectEntry,
    NodeRecord,
    RegistryConflictError,
    SQLiteStateStore,
)
from ae.observability.http_api import record_hpa_activity
from ae.runtime import WorkloadMetricSample

LOGGER = logging.getLogger(__name__)

_TARGET_RESOURCES: dict[str, tuple[str, str, str]] = {
    "deployment": ("apps", "v1", "deployments"),
    "statefulset": ("apps", "v1", "statefulsets"),
    "daemonset": ("apps", "v1", "daemonsets"),
}


@dataclass(slots=True)
class WorkloadMetricsCollectorConfig:
    interval_s: float = 15.0


@dataclass(slots=True)
class HPAAuthorityControllerConfig:
    interval_s: float = 15.0
    metrics_max_age_s: float = 45.0
    cooldown_s: float = 30.0


class WorkloadMetricsCollector:
    """Poll node agents for workload metrics and persist shared snapshots."""

    def __init__(
        self,
        store: SQLiteStateStore,
        sample_reader: Callable[[NodeRecord], list[WorkloadMetricSample]],
        *,
        config: WorkloadMetricsCollectorConfig | None = None,
        authority=None,
    ) -> None:
        self._store = store
        self._sample_reader = sample_reader
        self._config = config or WorkloadMetricsCollectorConfig()
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
            if self._is_leader():
                self.run_once()
                self._sleep(self._config.interval_s)
            else:
                self._sleep(1.0)

    def _sleep(self, seconds: float) -> None:
        remaining = max(0.1, float(seconds))
        while not self._stop and remaining > 0:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def run_once(self) -> None:
        identity = self._leader_identity()
        if identity is None:
            return
        samples: list[WorkloadMetricSample] = []
        try:
            nodes = self._store.list_nodes()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("hpa metrics collector failed to list nodes: %s", exc)
            return
        for node, _status in nodes:
            if self._stop or not self._is_leader():
                return
            try:
                samples.extend(self._sample_reader(node))
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("hpa metrics poll failed node=%s: %s", node.node_id, exc)
        aggregates: dict[str, dict[str, object]] = {}
        for sample in samples:
            app_name = str(sample.app_name or "").strip()
            if not app_name:
                continue
            aggregate = aggregates.setdefault(
                app_name,
                {
                    "cpu_total": 0.0,
                    "cpu_seen": False,
                    "cpu_missing": False,
                    "memory_bytes": 0,
                    "pod_count": 0,
                    "nodes": set(),
                    "collected_at": sample.collected_at,
                },
            )
            if sample.cpu_cores is None:
                aggregate["cpu_missing"] = True
            else:
                aggregate["cpu_total"] = float(aggregate["cpu_total"]) + float(sample.cpu_cores)
                aggregate["cpu_seen"] = True
            aggregate["memory_bytes"] = int(aggregate["memory_bytes"]) + int(sample.memory_bytes or 0)
            aggregate["pod_count"] = int(aggregate["pod_count"]) + int(sample.pod_count or 0)
            cast_nodes = aggregate["nodes"]
            if isinstance(cast_nodes, set):
                cast_nodes.add(str(sample.node_id or ""))
            prior_ts = aggregate["collected_at"]
            if isinstance(prior_ts, datetime) and sample.collected_at > prior_ts:
                aggregate["collected_at"] = sample.collected_at
        for app_name, aggregate in aggregates.items():
            if self._stop or not self._is_leader():
                return
            entry = self._store.get_registered_entry(app_name)
            cpu_request, memory_request = _requested_resources_per_pod(
                entry.manifest if entry is not None else None
            )
            pod_count = int(aggregate["pod_count"])
            cpu_utilization = None
            if (
                cpu_request is not None
                and cpu_request > 0
                and pod_count > 0
                and bool(aggregate.get("cpu_seen"))
                and not bool(aggregate.get("cpu_missing"))
            ):
                cpu_utilization = (
                    float(aggregate["cpu_total"]) / float(cpu_request * pod_count)
                ) * 100.0
            memory_utilization = None
            if memory_request is not None and memory_request > 0 and pod_count > 0:
                memory_utilization = (
                    float(int(aggregate["memory_bytes"])) / float(memory_request * pod_count)
                ) * 100.0
            nodes = aggregate["nodes"]
            node_count = len(nodes) if isinstance(nodes, set) else 0
            collected_at = aggregate["collected_at"]
            if not isinstance(collected_at, datetime):
                collected_at = datetime.now(timezone.utc)
            self._store.upsert_workload_metrics_snapshot(
                app_name,
                controller_id=identity.controller_id,
                controller_epoch=identity.controller_epoch,
                collected_at=collected_at.astimezone(timezone.utc),
                cpu_utilization=cpu_utilization,
                memory_utilization=memory_utilization,
                memory_bytes=int(aggregate["memory_bytes"]),
                pod_count=pod_count,
                node_count=node_count,
            )
            record_hpa_activity(snapshot_age_seconds=0.0)

    def _leader_identity(self):
        if self._authority is None:
            return SimpleNamespace(
                controller_id="controller",
                controller_epoch=0,
            )
        try:
            snapshot = self._authority.snapshot()
        except Exception:
            return None
        if snapshot is None or not getattr(snapshot, "is_leader", False):
            return None
        return getattr(snapshot, "leader_info", None)

    def _is_leader(self) -> bool:
        return self._leader_identity() is not None


class HPAAuthorityController:
    """Evaluates shared-authority HPA objects and scales converged workloads."""

    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        config: HPAAuthorityControllerConfig | None = None,
        authority=None,
    ) -> None:
        self._store = store
        self._config = config or HPAAuthorityControllerConfig()
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
            if self._is_leader():
                self.run_once()
                self._sleep(self._config.interval_s)
            else:
                self._sleep(1.0)

    def _sleep(self, seconds: float) -> None:
        remaining = max(0.1, float(seconds))
        while not self._stop and remaining > 0:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def run_once(self) -> None:
        if not self._is_leader():
            return
        try:
            entries = self._store.list_authority_objects(
                "autoscaling", "v2", "horizontalpodautoscalers"
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("hpa authority list failed: %s", exc)
            return
        for entry in entries:
            if self._stop or not self._is_leader():
                return
            self._reconcile_hpa(entry)

    def _reconcile_hpa(self, entry: AuthorityObjectEntry) -> None:
        record_hpa_activity(reconcile=True)
        now = self._now()
        leader_info = self._leader_info()
        if leader_info is None:
            return
        spec = dict(entry.spec or {})
        status = dict(entry.status or {})
        target = spec.get("scaleTargetRef") if isinstance(spec.get("scaleTargetRef"), dict) else {}
        target_name = str(target.get("name") or "").strip()
        target_kind = str(target.get("kind") or "").strip().lower()
        current_metrics: list[dict] = []
        able = False
        active = False
        limited = False
        conditions_message = ""
        app_name = app_key(target_name, entry.namespace or "default") if target_name else ""
        resource_map = _TARGET_RESOURCES.get(target_kind)
        registry_entry = self._store.get_registered_entry(app_name) if app_name else None
        current_replicas = (
            int(getattr(getattr(registry_entry, "manifest", None), "spec", None).replicas)
            if registry_entry is not None
            else 0
        )
        desired_replicas = current_replicas
        last_scale_time = status.get("lastScaleTime")
        if resource_map is None or registry_entry is None:
            conditions_message = "target workload not found or unsupported"
        else:
            able = True
            snapshot = self._store.get_workload_metrics_snapshot(app_name)
            if not _snapshot_is_fresh(
                snapshot,
                now=now,
                max_age_s=self._config.metrics_max_age_s,
                controller_epoch=int(getattr(leader_info, "controller_epoch", 0) or 0),
            ):
                conditions_message = "fresh workload metrics unavailable"
                record_hpa_activity(metrics_stale=True)
            else:
                desired_replicas, active, metric_entries, conditions_message = self._desired_replicas(
                    spec,
                    snapshot,
                    current_replicas,
                    registry_entry.manifest,
                )
                current_metrics.extend(metric_entries)
        min_replicas = int(spec.get("minReplicas", max(1, current_replicas)) or max(1, current_replicas))
        max_replicas = int(spec.get("maxReplicas", max(min_replicas, current_replicas)) or max(min_replicas, current_replicas))
        unclamped_desired = desired_replicas
        desired_replicas = min(max(desired_replicas, min_replicas), max_replicas)
        if desired_replicas != unclamped_desired:
            limited = True
        scaled = False
        if able and active and registry_entry is not None and desired_replicas != current_replicas:
            parsed_last_scale = _parse_status_time(last_scale_time)
            if (
                parsed_last_scale is not None
                and now - parsed_last_scale < timedelta(seconds=self._config.cooldown_s)
            ):
                limited = True
            else:
                updated_manifest = registry_entry.manifest.model_copy(
                    update={
                        "spec": registry_entry.manifest.spec.model_copy(
                            update={"replicas": desired_replicas}
                        )
                    }
                )
                try:
                    self._store.register_app(
                        updated_manifest,
                        source=registry_entry.source,
                        labels=registry_entry.labels,
                        expected_resource_version=registry_entry.resource_version,
                    )
                    scaled = True
                    last_scale_time = _iso_utc(now)
                    self._store.record_event(
                        app_name,
                        0,
                        "HPAScale",
                        f"Scaled via {entry.name} from {current_replicas} to {desired_replicas}",
                    )
                    record_hpa_activity(scale=True)
                except RegistryConflictError:
                    conditions_message = "target workload changed during HPA reconcile"
                    able = False
                    record_hpa_activity(metrics_missing=True)
        self._update_status(
            entry,
            current_replicas=current_replicas,
            desired_replicas=desired_replicas,
            current_metrics=current_metrics,
            observed_generation=_observed_generation(entry),
            last_scale_time=last_scale_time,
            able=able,
            active=active,
            limited=limited,
            message=conditions_message,
        )
        if not active and not conditions_message:
            record_hpa_activity(metrics_missing=True)
        snapshot = self._store.get_workload_metrics_snapshot(app_name) if app_name else None
        if snapshot is not None:
            age = max(0.0, (now - snapshot.collected_at).total_seconds())
            record_hpa_activity(snapshot_age_seconds=age)
        if scaled:
            LOGGER.info(
                "hpa authority scaled app=%s hpa=%s namespace=%s to=%s",
                app_name,
                entry.name,
                entry.namespace or "default",
                desired_replicas,
            )

    def _desired_replicas(
        self,
        spec: dict,
        snapshot,
        current_replicas: int,
        manifest: AppManifest,
    ) -> tuple[int, bool, list[dict], str]:
        desired = current_replicas
        active = False
        current_metrics: list[dict] = []
        message = ""
        for metric in spec.get("metrics") or []:
            if not isinstance(metric, dict) or str(metric.get("type") or "") != "Resource":
                continue
            resource_cfg = metric.get("resource")
            if not isinstance(resource_cfg, dict):
                continue
            name = str(resource_cfg.get("name") or "").strip().lower()
            target = resource_cfg.get("target")
            if not isinstance(target, dict):
                continue
            metric_status = {"type": "Resource", "resource": {"name": name, "current": {}, "target": target}}
            metric_desired = current_replicas
            if name == "cpu":
                current = snapshot.cpu_utilization
                target_pct = target.get("averageUtilization")
                if current is None:
                    message = message or "cpu metrics unavailable"
                elif not _manifest_has_cpu_requests(manifest):
                    message = message or "cpu requests required for HPA utilization"
                else:
                    try:
                        metric_desired = max(
                            1, math.ceil(current_replicas * float(current) / float(target_pct))
                        )
                        metric_status["resource"]["current"]["averageUtilization"] = int(current)
                        active = True
                    except Exception:
                        message = message or "invalid cpu target"
                current_metrics.append(metric_status)
            elif name == "memory":
                target_type = str(target.get("type") or "").strip().lower()
                if target_type == "averagevalue":
                    average_bytes = (
                        float(snapshot.memory_bytes) / float(snapshot.pod_count)
                        if int(snapshot.pod_count or 0) > 0
                        else None
                    )
                    target_bytes = _parse_quantity_bytes(str(target.get("averageValue") or target.get("value") or ""))
                    if average_bytes is None or target_bytes is None or target_bytes <= 0:
                        message = message or "memory average value metrics unavailable"
                    else:
                        metric_desired = max(
                            1,
                            math.ceil(
                                current_replicas * float(average_bytes) / float(target_bytes)
                            ),
                        )
                        metric_status["resource"]["current"]["averageValue"] = _fmt_bytes(
                            average_bytes
                        )
                        active = True
                    current_metrics.append(metric_status)
                else:
                    current = snapshot.memory_utilization
                    target_pct = target.get("averageUtilization")
                    if current is None:
                        message = message or "memory metrics unavailable"
                    elif not _manifest_has_memory_requests(manifest):
                        message = message or "memory requests required for HPA utilization"
                    else:
                        try:
                            metric_desired = max(
                                1, math.ceil(current_replicas * float(current) / float(target_pct))
                            )
                            metric_status["resource"]["current"]["averageUtilization"] = int(current)
                            active = True
                        except Exception:
                            message = message or "invalid memory target"
                    current_metrics.append(metric_status)
            desired = max(desired, metric_desired)
        return desired, active, current_metrics, message

    def _update_status(
        self,
        entry: AuthorityObjectEntry,
        *,
        current_replicas: int,
        desired_replicas: int,
        current_metrics: list[dict],
        observed_generation: int,
        last_scale_time: str | None,
        able: bool,
        active: bool,
        limited: bool,
        message: str,
    ) -> None:
        fresh = self._store.get_authority_object(
            entry.group,
            entry.version,
            entry.resource,
            entry.namespace,
            entry.name,
        )
        if fresh is None:
            return
        status = dict(fresh.status or {})
        status["currentReplicas"] = int(current_replicas)
        status["desiredReplicas"] = int(desired_replicas)
        status["currentMetrics"] = current_metrics
        status["observedGeneration"] = int(observed_generation)
        if last_scale_time:
            status["lastScaleTime"] = last_scale_time
        conditions = [
            _condition(
                "AbleToScale",
                able,
                "TargetReady" if able else "TargetNotReady",
                message or ("scale path ready" if able else "target resolution failed"),
            ),
            _condition(
                "ScalingActive",
                active,
                "ValidMetricFound" if active else "MetricsUnavailable",
                message or ("metrics available" if active else "no fresh metrics available"),
            ),
            _condition(
                "ScalingLimited",
                limited,
                "DesiredWithinRange" if not limited else "Limited",
                "desired replicas within range" if not limited else "desired replicas limited by cooldown or bounds",
            ),
        ]
        status["conditions"] = conditions
        try:
            self._store.register_authority_object(
                fresh.group,
                fresh.version,
                fresh.resource,
                fresh.namespace,
                fresh.name,
                kind=fresh.kind or "HorizontalPodAutoscaler",
                metadata=dict(fresh.metadata or {}),
                spec=dict(fresh.spec or {}),
                status=status,
                expected_resource_version=fresh.resource_version,
            )
        except RegistryConflictError:
            return

    def _leader_info(self):
        if self._authority is None:
            return SimpleNamespace(controller_id="controller", controller_epoch=0)
        try:
            snapshot = self._authority.snapshot()
        except Exception:
            return None
        if snapshot is None or not getattr(snapshot, "is_leader", False):
            return None
        return getattr(snapshot, "leader_info", None)

    def _is_leader(self) -> bool:
        return self._leader_info() is not None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


def _manifest_has_cpu_requests(manifest: AppManifest) -> bool:
    cpu, _memory = _requested_resources_per_pod(manifest)
    return cpu is not None and cpu > 0


def _manifest_has_memory_requests(manifest: AppManifest) -> bool:
    _cpu, memory = _requested_resources_per_pod(manifest)
    return memory is not None and memory > 0


def _requested_resources_per_pod(manifest: AppManifest | None) -> tuple[float | None, int | None]:
    if manifest is None:
        return None, None
    cpu_total = 0.0
    cpu_seen = False
    memory_total = 0
    memory_seen = False
    for resources in _iter_request_specs(manifest):
        cpu_val = _spec_value(getattr(resources, "requests", None), "cpu")
        if cpu_val is not None:
            parsed = _parse_cpu_cores(str(cpu_val))
            if parsed is not None:
                cpu_total += parsed
                cpu_seen = True
        mem_val = _spec_value(getattr(resources, "requests", None), "memory")
        if mem_val is not None:
            parsed_mem = _parse_quantity_bytes(str(mem_val))
            if parsed_mem is not None:
                memory_total += parsed_mem
                memory_seen = True
    return (cpu_total if cpu_seen else None, memory_total if memory_seen else None)


def _iter_request_specs(manifest: AppManifest):
    yield getattr(manifest.spec, "resources", None)
    for container in getattr(manifest.spec, "containers", []) or []:
        yield getattr(container, "resources", None)


def _spec_value(spec, field: str):
    if spec is None:
        return None
    if isinstance(spec, dict):
        return spec.get(field)
    return getattr(spec, field, None)


def _parse_cpu_cores(raw: str | None) -> float | None:
    if raw in {None, ""}:
        return None
    text = str(raw).strip()
    try:
        if text.endswith("m"):
            return float(text[:-1]) / 1000.0
        return float(text)
    except Exception:
        return None


def _parse_quantity_bytes(raw: str | None) -> int | None:
    if raw in {None, ""}:
        return None
    suffixes = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "ki": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mi": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gi": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "ti": 1024**4,
    }
    try:
        text = str(raw).strip()
        if text.isdigit():
            return int(text)
        number = ""
        unit = ""
        for ch in text:
            if ch.isdigit() or ch == ".":
                number += ch
            else:
                unit += ch
        factor = suffixes.get(unit.lower())
        if factor is None:
            return None
        return int(float(number) * factor)
    except Exception:
        return None


def _fmt_bytes(value: float) -> str:
    if value >= 1024**2:
        return f"{int(value / 1024**2)}Mi"
    if value >= 1024:
        return f"{int(value / 1024)}Ki"
    return str(int(value))


def _condition(kind: str, status: bool, reason: str, message: str) -> dict[str, str]:
    return {
        "type": kind,
        "status": "True" if status else "False",
        "reason": reason,
        "message": message,
        "lastTransitionTime": _iso_utc(datetime.now(timezone.utc)),
    }


def _snapshot_is_fresh(snapshot, *, now: datetime, max_age_s: float, controller_epoch: int) -> bool:
    if snapshot is None:
        return False
    if int(getattr(snapshot, "controller_epoch", 0) or 0) != int(controller_epoch):
        return False
    age = now - snapshot.collected_at
    return age <= timedelta(seconds=max(0.0, float(max_age_s)))


def _observed_generation(entry: AuthorityObjectEntry) -> int:
    metadata = entry.metadata or {}
    try:
        return int(metadata.get("generation") or 1)
    except Exception:
        return 1


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


__all__ = [
    "HPAAuthorityController",
    "HPAAuthorityControllerConfig",
    "WorkloadMetricsCollector",
    "WorkloadMetricsCollectorConfig",
]
