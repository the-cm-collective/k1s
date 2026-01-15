# ruff: noqa: E501,S110,S112,SIM105
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ae.controller.reconciler import Reconciler
from ae.controller.spec import (
    AppManifest,
    AppSpec,
    IngressSpec,
    Metadata,
    PortSpec,
    ServiceSpec,
)
from ae.controller.state import SQLiteStateStore
from ae.runtime import DockerRuntime, PodmanRuntime, RuntimeAdapter, StubRuntime

from .store import K8sObject, ObjectStore


def _app_name(ns: str | None, name: str) -> str:
    return f"{ns}--{name}" if ns else name


def _manifest_from_deployment(
    dep: K8sObject,
    *,
    service_spec: ServiceSpec | None = None,
    ingress_spec: IngressSpec | None = None,
) -> AppManifest:
    spec: dict[str, Any] = dep.spec or {}
    tpl = ((spec.get("template") or {}).get("spec") or {})
    containers = tpl.get("containers") or []
    if not containers:
        # Minimal placeholder to satisfy schema; image required
        image = "busybox:latest"
        ports: list[PortSpec] = []
    else:
        c0 = containers[0]
        image = c0.get("image") or "busybox:latest"
        ports = []
        for p in c0.get("ports") or []:
            try:
                port_num = int(p.get("containerPort"))
            except Exception:
                continue
            name = p.get("name") or f"p{port_num}"
            ports.append(PortSpec(name=name, containerPort=port_num))

    replicas = int(spec.get("replicas", 1) or 1)
    # Ensure >=1 for manifest schema; scale-to-0 handled by adapter
    m_replicas = max(1, replicas)

    app_spec = AppSpec(image=image, replicas=m_replicas, ports=ports)
    if service_spec is not None:
        app_spec = app_spec.model_copy(update={"service": service_spec})
    if ingress_spec is not None:
        app_spec = app_spec.model_copy(update={"ingress": ingress_spec})
    meta = Metadata(name=_app_name(dep.namespace, dep.name))
    return AppManifest(apiVersion="ae.dev/v1alpha1", kind="App", metadata=meta, spec=app_spec)


def _runtime_from_env() -> RuntimeAdapter:
    backend = (os.getenv("AE_APISHIM_RUNTIME") or os.getenv("AE_RUNTIME_BACKEND") or "stub").lower()
    if backend in {"stub", "test"}:
        return StubRuntime()
    if backend in {"podman", "oci"}:
        try:
            return PodmanRuntime()
        except Exception:
            return DockerRuntime()
    return DockerRuntime()


class AdapterWorker(threading.Thread):
    """Watches apps/v1 Deployments and reconciles into k1s via Reconciler.

    For replicas=0, removes app instead of reconciling (k1s schema requires replicas>=1).
    """

    daemon = True

    def __init__(self, store: ObjectStore, state_store: SQLiteStateStore, reconciler: Reconciler) -> None:
        super().__init__(name="apishim-adapter")
        self._store = store
        self._state = state_store
        self._reconciler = reconciler
        self._stop = threading.Event()
        self._service_specs: dict[tuple[str | None, str], ServiceSpec] = {}
        self._ingress_specs: dict[tuple[str | None, str], IngressSpec] = {}
        self._service_name_map: dict[tuple[str | None, str], tuple[str | None, str]] = {}
        self._ingress_owner_map: dict[tuple[str | None, str], tuple[str | None, str]] = {}
        # CronJob bookkeeping: key -> {"job": name, "last_run": timestamp}
        self._cronjob_jobs: dict[tuple[str | None, str], dict] = {}
        self._lock = threading.RLock()
        self._service_thread: threading.Thread | None = None
        self._ingress_thread: threading.Thread | None = None
        self._statefulset_thread: threading.Thread | None = None
        self._daemonset_thread: threading.Thread | None = None
        self._job_thread: threading.Thread | None = None
        self._cronjob_thread: threading.Thread | None = None
        self._port_file = Path(
            os.getenv("AE_APISHIM_PORT_STATE", "state/apishim_service_ports.json")
        )
        self._port_file.parent.mkdir(parents=True, exist_ok=True)
        self._port_assignments: dict[str, dict[str, int]] = {}
        self._used_ports: set[int] = set()
        self._port_low = int(os.getenv("AE_APISHIM_NODEPORT_MIN", "31000"))
        self._port_high = int(os.getenv("AE_APISHIM_NODEPORT_MAX", "32767"))
        self._load_port_assignments()
        self._hpa_thread: threading.Thread | None = None
        self._hpa_last_scale: dict[str, float] = {}
        self._hpa_cooldown_seconds = max(0, int(os.getenv("AE_HPA_COOLDOWN_SECONDS", "30")))

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._service_thread and self._service_thread.is_alive():
                self._service_thread.join(timeout=0.2)
        except Exception:
            pass
        try:
            if self._ingress_thread and self._ingress_thread.is_alive():
                self._ingress_thread.join(timeout=0.2)
        except Exception:
            pass
        try:
            if self._statefulset_thread and self._statefulset_thread.is_alive():
                self._statefulset_thread.join(timeout=0.2)
        except Exception:
            pass
        try:
            if self._daemonset_thread and self._daemonset_thread.is_alive():
                self._daemonset_thread.join(timeout=0.2)
        except Exception:
            pass
        try:
            if self._job_thread and self._job_thread.is_alive():
                self._job_thread.join(timeout=0.2)
        except Exception:
            pass
        try:
            if self._cronjob_thread and self._cronjob_thread.is_alive():
                self._cronjob_thread.join(timeout=0.2)
        except Exception:
            pass
        try:
            if self._hpa_thread and self._hpa_thread.is_alive():
                self._hpa_thread.join(timeout=0.2)
        except Exception:
            pass

    def run(self) -> None:
        self._service_thread = threading.Thread(target=self._watch_services, daemon=True)
        self._ingress_thread = threading.Thread(target=self._watch_ingresses, daemon=True)
        self._statefulset_thread = threading.Thread(target=self._watch_statefulsets, daemon=True)
        self._daemonset_thread = threading.Thread(target=self._watch_daemonsets, daemon=True)
        self._job_thread = threading.Thread(target=self._watch_jobs, daemon=True)
        self._cronjob_thread = threading.Thread(target=self._watch_cronjobs, daemon=True)
        self._hpa_thread = threading.Thread(target=self._watch_hpa, daemon=True)
        self._service_thread.start()
        self._ingress_thread.start()
        self._statefulset_thread.start()
        self._daemonset_thread.start()
        self._job_thread.start()
        self._cronjob_thread.start()
        self._hpa_thread.start()
        gen = self._store.watch(
            "apps", "v1", "deployments", None, heartbeat_seconds=5, allow_bookmarks=True
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev in {"ADDED", "MODIFIED"}:
                    self._apply_deployment(obj)
        finally:
            self._stop.set()
            try:
                gen.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    def _apply_deployment(self, dep: K8sObject) -> None:
        spec = dep.spec or {}
        desired = int(spec.get("replicas", 1) or 1)
        app_name = _app_name(dep.namespace, dep.name)
        if desired <= 0:
            # Scale-to-0: call runtime remove and update status to zero
            try:
                # Reconciler's runtime may have remove_app
                self._reconciler._runtime.remove_app(app_name)  # type: ignore[attr-defined]
            except Exception:
                pass
            # Update synthesized status to zeros
            st = {"replicas": 0, "updatedReplicas": 0, "readyReplicas": 0, "availableReplicas": 0,
                  "conditions": [{"type": "Available", "status": "False", "reason": "ScaledDown"},
                                  {"type": "Progressing", "status": "False", "reason": "ScaledDown"}]}
            self._store.upsert("apps", "v1", "deployments", dep.namespace, dep.name, dep.metadata, dep.spec, status=st)
            return

        dep_key = (dep.namespace, dep.name)
        svc_spec = self._service_specs.get(dep_key)
        ing_spec = self._ingress_specs.get(dep_key)
        m = _manifest_from_deployment(dep, service_spec=svc_spec, ingress_spec=ing_spec)
        self._reconciler.reconcile(m)
        # Reflect status from state store
        st_row = self._state.get_status(m.metadata.name)
        if st_row is not None:
            st = {
                "replicas": st_row.desired_replicas,
                "updatedReplicas": st_row.live_replicas,
                "readyReplicas": st_row.ready_replicas,
                "availableReplicas": st_row.ready_replicas,
                "unavailableReplicas": max(0, st_row.desired_replicas - st_row.ready_replicas),
                "conditions": [
                    {
                        "type": "Available",
                        "status": "True" if st_row.ready_replicas >= st_row.desired_replicas else "False",
                        "reason": "MinimumReplicasAvailable",
                    },
                    {
                        "type": "Progressing",
                        "status": "True",
                        "reason": "NewReplicaSetAvailable" if st_row.revision_status == "live" else st_row.revision_status or "Progressing",
                    },
                ],
                "observedGeneration": st_row.revision,
            }
            self._store.upsert(
                "apps", "v1", "deployments", dep.namespace, dep.name, dep.metadata, dep.spec, status=st
            )

    def _apply_statefulset(self, sts: K8sObject) -> None:
        spec = sts.spec or {}
        desired = int(spec.get("replicas", 1) or 1)
        if desired <= 0:
            self._remove_app_for(sts)
            st = {
                "replicas": 0,
                "readyReplicas": 0,
                "currentReplicas": 0,
                "updatedReplicas": 0,
                "currentRevision": sts.metadata.get("generation", 1),
                "updateRevision": sts.metadata.get("generation", 1),
            }
            self._store.upsert("apps", "v1", "statefulsets", sts.namespace, sts.name, sts.metadata, sts.spec, status=st)
            return
        m = _manifest_from_deployment(sts)
        self._reconciler.reconcile(m)
        st_row = self._state.get_status(m.metadata.name)
        if st_row is not None:
            st = {
                "replicas": st_row.desired_replicas,
                "readyReplicas": st_row.ready_replicas,
                "currentReplicas": st_row.live_replicas,
                "updatedReplicas": st_row.live_replicas,
                "currentRevision": sts.metadata.get("generation", 1),
                "updateRevision": sts.metadata.get("generation", 1),
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True" if st_row.ready_replicas >= st_row.desired_replicas else "False",
                    }
                ],
            }
            self._store.upsert("apps", "v1", "statefulsets", sts.namespace, sts.name, sts.metadata, sts.spec, status=st)

    def _apply_daemonset(self, ds: K8sObject) -> None:
        spec = ds.spec or {}
        # Desired replicas approximate to number of nodes; fallback to 1
        desired = 1
        try:
            desired = max(1, len(self._state.list_nodes()))
        except Exception:
            desired = max(1, int(spec.get("replicas", 1) or 1))
        if desired <= 0:
            self._remove_app_for(ds)
            st = {
                "desiredNumberScheduled": 0,
                "currentNumberScheduled": 0,
                "numberReady": 0,
                "numberAvailable": 0,
            }
            self._store.upsert("apps", "v1", "daemonsets", ds.namespace, ds.name, ds.metadata, ds.spec, status=st)
            return
        spec_mod = dict(spec)
        spec_mod["replicas"] = desired
        ds_mod = K8sObject(ds.group, ds.version, ds.resource, ds.namespace, ds.name, ds.metadata, spec_mod, ds.status, ds.resource_version)
        m = _manifest_from_deployment(ds_mod)
        self._reconciler.reconcile(m)
        st_row = self._state.get_status(m.metadata.name)
        if st_row is not None:
            st = {
                "desiredNumberScheduled": desired,
                "currentNumberScheduled": st_row.live_replicas,
                "numberReady": st_row.ready_replicas,
                "numberAvailable": st_row.ready_replicas,
                "updatedNumberScheduled": st_row.live_replicas,
            }
            self._store.upsert("apps", "v1", "daemonsets", ds.namespace, ds.name, ds.metadata, spec_mod, status=st)

    def _apply_job(self, job: K8sObject) -> None:
        spec = job.spec or {}
        parallelism = int(spec.get("parallelism", 1) or 1)
        completions = int(spec.get("completions", parallelism) or parallelism)
        if parallelism <= 0:
            self._remove_app_for(job)
            st = {"active": 0, "succeeded": 0, "failed": 0, "conditions": []}
            self._store.upsert("batch", "v1", "jobs", job.namespace, job.name, job.metadata, job.spec, status=st)
            return
        # Treat Job as short-lived deployment with desired replicas=parallelism
        spec_mod = dict(spec)
        spec_mod["replicas"] = parallelism
        job_mod = K8sObject(job.group, job.version, job.resource, job.namespace, job.name, job.metadata, spec_mod, job.status, job.resource_version)
        m = _manifest_from_deployment(job_mod)
        self._reconciler.reconcile(m)
        st_row = self._state.get_status(m.metadata.name)
        succeeded = 0
        ready = 0
        if st_row is not None:
            ready = st_row.ready_replicas
            succeeded = min(ready, completions)
        conditions = []
        if succeeded >= completions:
            conditions.append({"type": "Complete", "status": "True"})
            try:
                self._state.record_event(m.metadata.name, st_row.revision if st_row else 0, "Complete", f"Job {job.name} succeeded")  # type: ignore[arg-type]
            except Exception:
                pass
        st = {
            "active": max(0, parallelism - succeeded),
            "succeeded": succeeded,
            "failed": 0,
            "conditions": conditions,
        }
        self._store.upsert("batch", "v1", "jobs", job.namespace, job.name, job.metadata, job.spec, status=st)

    def _apply_cronjob(self, cj: K8sObject) -> None:
        spec = cj.spec or {}
        suspend = bool(spec.get("suspend", False))
        key = (cj.namespace, cj.name)
        last_schedule = None
        last_success = None
        now = time.time()
        with self._lock:
            state = self._cronjob_jobs.get(key) or {}
        should_fire = False
        if not suspend:
            annotations = cj.metadata.get("annotations") or {}
            interval = annotations.get("cronjob.k1s.dev/intervalSeconds")
            schedule_expr = annotations.get("cronjob.k1s.dev/schedule") or spec.get("schedule")
            last_ts = float(state.get("last_run", 0))
            if interval is not None:
                try:
                    interval_i = int(interval)
                except Exception:
                    interval_i = 60
                if now - last_ts >= interval_i:
                    should_fire = True
            elif schedule_expr:
                try:
                    from croniter import croniter  # type: ignore

                    base = state.get("last_run", now - 60) or now - 60
                    it = croniter(schedule_expr, base)
                    next_run = it.get_next(float)
                    if now >= next_run:
                        should_fire = True
                except Exception:
                    # fallback to 60s interval when cron expression invalid or croniter missing
                    if now - last_ts >= 60:
                        should_fire = True
            else:
                if now - last_ts >= 60:
                    should_fire = True
        if should_fire:
            fired_name = f"{cj.name}-run-{int(now)}"
            job_spec = {
                "template": spec.get("jobTemplate", {}).get("spec", {}).get("template"),
                "parallelism": spec.get("jobTemplate", {}).get("spec", {}).get("parallelism", 1),
                "completions": spec.get("jobTemplate", {}).get("spec", {}).get("completions", 1),
            }
            job_md = {
                "name": fired_name,
                "namespace": cj.namespace or "default",
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "CronJob",
                        "name": cj.name,
                        "uid": cj.metadata.get("uid", cj.name),
                        "controller": True,
                        "blockOwnerDeletion": True,
                    }
                ],
            }
            job_obj = K8sObject("batch", "v1", "jobs", cj.namespace, fired_name, job_md, job_spec, {}, 0)
            self._apply_job(job_obj)
            last_schedule = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            last_success = last_schedule
            with self._lock:
                self._cronjob_jobs[key] = {"job": fired_name, "last_run": now}
        status = {
            "active": [],
            "lastScheduleTime": last_schedule or state.get("last_schedule"),
            "lastSuccessfulTime": last_success or state.get("last_success"),
        }
        self._store.upsert("batch", "v1", "cronjobs", cj.namespace, cj.name, cj.metadata, cj.spec, status=status)

    def _remove_app_for(self, obj: K8sObject) -> None:
        app_name = _app_name(obj.namespace, obj.name)
        try:
            self._reconciler._runtime.remove_app(app_name)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _trigger_reconcile(self, namespace: str | None, deploy_name: str) -> None:
        if namespace is None:
            return
        dep = self._store.get("apps", "v1", "deployments", namespace, deploy_name)
        if dep is None:
            return
        self._apply_deployment(dep)

    def _watch_services(self) -> None:
        gen = self._store.watch(
            "", "v1", "services", None, heartbeat_seconds=5, allow_bookmarks=True
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                self._handle_service_event(ev, obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _watch_ingresses(self) -> None:
        gen = self._store.watch(
            "networking.k8s.io",
            "v1",
            "ingresses",
            None,
            heartbeat_seconds=5,
            allow_bookmarks=True,
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                self._handle_ingress_event(ev, obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _watch_statefulsets(self) -> None:
        gen = self._store.watch(
            "apps",
            "v1",
            "statefulsets",
            None,
            heartbeat_seconds=5,
            allow_bookmarks=True,
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev in {"ADDED", "MODIFIED"}:
                    self._apply_statefulset(obj)
                elif ev == "DELETED":
                    self._remove_app_for(obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _watch_daemonsets(self) -> None:
        gen = self._store.watch(
            "apps",
            "v1",
            "daemonsets",
            None,
            heartbeat_seconds=5,
            allow_bookmarks=True,
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev in {"ADDED", "MODIFIED"}:
                    self._apply_daemonset(obj)
                elif ev == "DELETED":
                    self._remove_app_for(obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _watch_jobs(self) -> None:
        gen = self._store.watch(
            "batch",
            "v1",
            "jobs",
            None,
            heartbeat_seconds=5,
            allow_bookmarks=True,
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev in {"ADDED", "MODIFIED"}:
                    self._apply_job(obj)
                elif ev == "DELETED":
                    self._remove_app_for(obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _watch_cronjobs(self) -> None:
        gen = self._store.watch(
            "batch",
            "v1",
            "cronjobs",
            None,
            heartbeat_seconds=5,
            allow_bookmarks=True,
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev in {"ADDED", "MODIFIED"}:
                    self._apply_cronjob(obj)
                elif ev == "DELETED":
                    key = (obj.namespace, obj.name)
                    with self._lock:
                        self._cronjob_jobs.pop(key, None)
                    self._remove_app_for(obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _watch_hpa(self) -> None:
        gen = self._store.watch(
            "autoscaling",
            "v2",
            "horizontalpodautoscalers",
            None,
            heartbeat_seconds=5,
            allow_bookmarks=True,
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev in {"ADDED", "MODIFIED", "BOOKMARK"}:
                    self._apply_hpa(obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _resolve_target_gvr(self, api_version: str, kind: str) -> tuple[str, str, str] | None:
        """Map scaleTargetRef to (group, version, resource)."""
        kind_l = kind.lower()
        resource_map = {
            "deployment": ("apps", "v1", "deployments"),
            "statefulset": ("apps", "v1", "statefulsets"),
            "daemonset": ("apps", "v1", "daemonsets"),
            "replicaset": ("apps", "v1", "replicasets"),
            "job": ("batch", "v1", "jobs"),
        }
        if kind_l in resource_map:
            return resource_map[kind_l]
        if "/" in api_version:
            grp, ver = api_version.split("/", 1)
        else:
            grp, ver = "", api_version
        if not kind_l:
            return None
        return (grp, ver, f"{kind_l}s")

    def _parse_quantity_bytes(self, raw: str | None) -> int | None:
        if raw is None:
            return None
        try:
            s = str(raw).strip()
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
            }
            if s.isdigit():
                return int(s)
            num = ""
            unit = ""
            for ch in s:
                if ch.isdigit() or ch == ".":
                    num += ch
                else:
                    unit += ch
            factor = suffixes.get(unit.lower())
            if factor is None:
                return None
            return int(float(num) * factor)
        except Exception:
            return None

    def _fmt_bytes(self, val: float) -> str:
        try:
            if val >= 1024**2:
                return f"{int(val / 1024**2)}Mi"
            if val >= 1024:
                return f"{int(val / 1024)}Ki"
            return f"{int(val)}"
        except Exception:
            return str(val)

    def _cpu_percent(self, stats: dict) -> float | None:
        try:
            cpu_stats = stats.get("cpu_stats", {}) or {}
            precpu = stats.get("precpu_stats", {}) or {}
            cpu_delta = float(cpu_stats.get("cpu_usage", {}).get("total_usage", 0)) - float(
                precpu.get("cpu_usage", {}).get("total_usage", 0)
            )
            system_delta = float(cpu_stats.get("system_cpu_usage", 0)) - float(
                precpu.get("system_cpu_usage", 0)
            )
            if system_delta <= 0:
                return None
            online = cpu_stats.get("online_cpus") or len(
                cpu_stats.get("cpu_usage", {}).get("percpu_usage", []) or []
            ) or 1
            return max(0.0, (cpu_delta / system_delta) * float(online) * 100.0)
        except Exception:
            return None

    def _docker_metrics(self, app_name: str) -> dict[str, float | None]:
        out: dict[str, float | None] = {"cpu_util": None, "mem_util": None, "mem_bytes": None}
        try:
            rt = getattr(self._reconciler, "_runtime", None)
            if not isinstance(rt, DockerRuntime):
                return out
            client = getattr(rt, "_client", None)
            if client is None:
                return out
            containers = client.containers.list(filters={"label": f"ae.app={app_name}"})
        except Exception:
            return out
        cpu_vals: list[float] = []
        mem_utils: list[float] = []
        mem_bytes: list[float] = []
        for c in containers:
            try:
                stats = c.stats(stream=False)
                cpu_pct = self._cpu_percent(stats)
                if cpu_pct is not None:
                    cpu_vals.append(cpu_pct)
                mem = stats.get("memory_stats", {}) or {}
                usage = mem.get("usage")
                limit = mem.get("limit") or None
                if isinstance(usage, int | float):
                    mem_bytes.append(float(usage))
                    if isinstance(limit, int | float) and limit > 0:
                        mem_utils.append((float(usage) / float(limit)) * 100.0)
            except Exception:
                continue
        if cpu_vals:
            out["cpu_util"] = sum(cpu_vals) / len(cpu_vals)
        if mem_utils:
            out["mem_util"] = sum(mem_utils) / len(mem_utils)
        if mem_bytes:
            out["mem_bytes"] = sum(mem_bytes) / len(mem_bytes)
        return out

    def _podman_metrics(self, app_name: str) -> dict[str, float | None]:
        """Best-effort Podman metrics via `podman stats --no-stream --format json`."""
        out: dict[str, float | None] = {"cpu_util": None, "mem_util": None, "mem_bytes": None}
        try:
            rt = getattr(self._reconciler, "_runtime", None)
            bin_path = getattr(rt, "_bin", None)
            if not isinstance(rt, PodmanRuntime) or not bin_path:
                return out
            # Guard binary path to a basename or absolute path without whitespace
            bin_str = str(bin_path)
            if any(ch.isspace() for ch in bin_str):
                return out
            if os.path.sep in bin_str and not os.path.isabs(bin_str):
                return out

            proc = subprocess.run(  # noqa: S603,S607 - podman CLI; shell disabled; path vetted
                [bin_str, "stats", "--no-stream", "--format", "json"],  # noqa: S603,S607
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            if proc.returncode != 0 or not proc.stdout:
                return out
            data = json.loads(proc.stdout)
        except Exception:
            return out
        cpu_vals: list[float] = []
        mem_utils: list[float] = []
        mem_bytes: list[float] = []
        for item in data if isinstance(data, list) else []:
            try:
                labels = item.get("Labels") or item.get("labels") or {}
                if labels.get("ae.app") not in {app_name, f"{app_name}"}:
                    continue
                cpu_raw = item.get("CPU %") or item.get("CPUPerc") or item.get("cpu_percent")
                if isinstance(cpu_raw, str) and cpu_raw.endswith("%"):
                    cpu_raw = cpu_raw.rstrip("%")
                if cpu_raw is not None:
                    cpu_vals.append(float(cpu_raw))
                mem_raw = item.get("MemUsage") or item.get("MemUsage") or item.get("mem_usage")
                # MemUsage may be "10MiB / 100MiB"
                if isinstance(mem_raw, str) and "/" in mem_raw:
                    usage_s = mem_raw.split("/", 1)[0].strip()
                    usage = self._parse_quantity_bytes(usage_s)
                else:
                    usage = mem_raw if isinstance(mem_raw, int | float) else None
                if usage is not None:
                    mem_bytes.append(float(usage))
                mem_pct = item.get("MemPerc") or item.get("Mem %") or item.get("mem_percent")
                if isinstance(mem_pct, str) and mem_pct.endswith("%"):
                    mem_pct = mem_pct.rstrip("%")
                if mem_pct is not None:
                    mem_utils.append(float(mem_pct))
            except Exception:
                continue
        if cpu_vals:
            out["cpu_util"] = sum(cpu_vals) / len(cpu_vals)
        if mem_utils:
            out["mem_util"] = sum(mem_utils) / len(mem_utils)
        if mem_bytes:
            out["mem_bytes"] = sum(mem_bytes) / len(mem_bytes)
        return out

    def _collect_metrics_for_app(self, app_name: str) -> dict[str, float | None]:
        metrics = {"cpu_util": None, "mem_util": None, "mem_bytes": None}
        try:
            rt = getattr(self._reconciler, "_runtime", None)
            if isinstance(rt, DockerRuntime):
                metrics.update(self._docker_metrics(app_name))
            elif isinstance(rt, PodmanRuntime):
                metrics.update(self._podman_metrics(app_name))
        except Exception:
            pass
        return metrics

    def _apply_hpa(self, hpa: K8sObject) -> None:
        """Evaluate an HPA object and adjust target replicas + status."""
        spec = hpa.spec or {}
        target = spec.get("scaleTargetRef") or {}
        target_name = target.get("name")
        target_kind = (target.get("kind") or "").lower()
        target_api = target.get("apiVersion", "apps/v1")
        if not target_name:
            return
        gvr = self._resolve_target_gvr(target_api, target_kind)
        if gvr is None:
            return
        group, version, resource = gvr
        target_obj = self._store.get(group, version, resource, hpa.namespace, target_name)
        if target_obj is None:
            return
        app_name = _app_name(hpa.namespace, target_name)
        current_replicas = int(target_obj.spec.get("replicas", 1) or 1)
        min_rep = int(spec.get("minReplicas", current_replicas) or current_replicas or 1)
        max_rep = int(spec.get("maxReplicas", max(min_rep, current_replicas)) or max(min_rep, current_replicas))
        metrics_spec = spec.get("metrics") or []
        metrics = self._collect_metrics_for_app(app_name)
        desired = current_replicas
        current_metrics_status: list[dict] = []
        scale_reason = None

        for m in metrics_spec:
            if m.get("type") != "Resource":
                continue
            res = m.get("resource") or {}
            rname = (res.get("name") or "").lower()
            target_cfg = res.get("target") or {}
            target_type = (target_cfg.get("type") or "Utilization").lower()
            desired_metric = desired

            if rname == "cpu":
                cur_val = metrics.get("cpu_util")
                target_val = target_cfg.get("averageUtilization") or target_cfg.get("value") or target_cfg.get("averageValue")
                if cur_val is not None and target_val:
                    try:
                        desired_metric = max(1, math.ceil(current_replicas * float(cur_val) / float(target_val)))
                        scale_reason = scale_reason or f"cpu {cur_val:.1f}%/{target_val}"
                    except Exception:
                        desired_metric = desired
                cm_entry = {"type": "Resource", "resource": {"name": "cpu", "current": {}, "target": target_cfg}}
                if cur_val is not None:
                    cm_entry["resource"]["current"]["averageUtilization"] = int(cur_val)
                current_metrics_status.append(cm_entry)
            elif rname == "memory":
                cur_val = None
                target_val = None
                if target_type in {"value", "averagevalue"}:
                    target_val = target_cfg.get("averageValue") or target_cfg.get("value")
                    cur_bytes = metrics.get("mem_bytes")
                    if target_val is not None and cur_bytes is not None:
                        tgt_bytes = self._parse_quantity_bytes(str(target_val))
                        if tgt_bytes:
                            desired_metric = max(1, math.ceil(current_replicas * float(cur_bytes) / float(tgt_bytes)))
                            scale_reason = scale_reason or f"memory {self._fmt_bytes(cur_bytes)}/{target_val}"
                        cur_val = cur_bytes
                else:
                    target_val = target_cfg.get("averageUtilization")
                    cur_val = metrics.get("mem_util")
                    if target_val and cur_val is not None:
                        try:
                            desired_metric = max(1, math.ceil(current_replicas * float(cur_val) / float(target_val)))
                            scale_reason = scale_reason or f"memory {cur_val:.1f}%/{target_val}"
                        except Exception:
                            desired_metric = desired
                cm_entry = {"type": "Resource", "resource": {"name": "memory", "current": {}, "target": target_cfg}}
                if cur_val is not None:
                    if target_type in {"value", "averagevalue"}:
                        cm_entry["resource"]["current"]["averageValue"] = self._fmt_bytes(float(cur_val))
                    else:
                        cm_entry["resource"]["current"]["averageUtilization"] = int(cur_val)
                current_metrics_status.append(cm_entry)
            else:
                continue
            desired = max(desired, desired_metric)

        desired = min(max(desired, min_rep), max_rep)
        now = time.time()
        last_scale = self._hpa_last_scale.get(app_name, 0)
        limited = False
        last_scale_time = None

        if desired != current_replicas:
            if now - last_scale < self._hpa_cooldown_seconds:
                limited = True
            else:
                self._hpa_last_scale[app_name] = now
                last_scale_time = datetime.now(UTC).isoformat()
                new_spec = dict(target_obj.spec or {})
                new_spec["replicas"] = desired
                updated = self._store.upsert(
                    group, version, resource, hpa.namespace, target_name, target_obj.metadata, new_spec, status=target_obj.status
                )
                if target_kind == "deployment":
                    self._apply_deployment(updated)
                elif target_kind == "statefulset":
                    self._apply_statefulset(updated)
                elif target_kind == "daemonset":
                    self._apply_daemonset(updated)
                try:
                    st = self._state.get_status(app_name)
                    rev = st.revision if st else 0  # type: ignore[arg-type]
                    self._state.record_event(app_name, int(rev or 0), "HPA", f"scaled to {desired} replicas ({scale_reason or 'autoscale'})")
                except Exception:
                    pass

        conditions = [
            {"type": "AbleToScale", "status": "True"},
            {"type": "ScalingActive", "status": "True" if metrics_spec else "False"},
            {"type": "ScalingLimited", "status": "True" if limited or desired in {min_rep, max_rep} else "False"},
        ]
        # Build status snapshot
        status: dict[str, Any] = {
            "currentReplicas": current_replicas,
            "desiredReplicas": desired,
            "conditions": conditions,
            "currentMetrics": current_metrics_status,
            "observedGeneration": hpa.metadata.get("generation", 1) if isinstance(hpa.metadata, dict) else 1,
        }
        if last_scale_time:
            status["lastScaleTime"] = last_scale_time
        elif hpa.status and hpa.status.get("lastScaleTime"):
            status["lastScaleTime"] = hpa.status.get("lastScaleTime")

        self._store.upsert(
            "autoscaling",
            "v2",
            "horizontalpodautoscalers",
            hpa.namespace,
            hpa.name,
            hpa.metadata,
            hpa.spec,
            status=status,
        )

    def _handle_service_event(self, ev: str, obj: K8sObject) -> None:
        ns = obj.namespace
        svc_name = obj.name
        if ev == "DELETED":
            dep_key = None
            with self._lock:
                dep_key = self._service_name_map.pop((ns, svc_name), None)
                if dep_key:
                    self._service_specs.pop(dep_key, None)
            self._release_service_ports(f"{ns or ''}/{svc_name}")
            if dep_key:
                self._trigger_reconcile(dep_key[0], dep_key[1])
            return

        result = self._service_spec_for(obj)
        if not result:
            dep_key = None
            with self._lock:
                dep_key = self._service_name_map.pop((ns, svc_name), None)
                if dep_key:
                    self._service_specs.pop(dep_key, None)
            self._release_service_ports(f"{ns or ''}/{svc_name}")
            if dep_key:
                self._trigger_reconcile(dep_key[0], dep_key[1])
            return
        dep_key, svc_spec = result
        with self._lock:
            self._service_specs[dep_key] = svc_spec
            self._service_name_map[(ns, svc_name)] = dep_key
        self._trigger_reconcile(dep_key[0], dep_key[1])

    def _service_spec_for(
        self, svc: K8sObject
    ) -> tuple[tuple[str | None, str], ServiceSpec] | None:
        spec = svc.spec or {}
        selector = spec.get("selector") or {}
        if not selector:
            selector = (spec.get("selector") or {}).get("matchLabels") or {}
        target = (
            selector.get("app")
            or selector.get("app.kubernetes.io/name")
            or svc.metadata.get("labels", {}).get("app")
            or svc.metadata.get("annotations", {}).get("apishim.k1s.dev/app")
            or svc.metadata.get("name")
        )
        if not target:
            return None
        svc_type = spec.get("type", "ClusterIP")
        expose_host = svc_type in {"NodePort", "LoadBalancer"}
        ports_in = self._prepare_service_ports(svc, spec, expose_host)
        if not ports_in:
            return None
        svc_ports: list[ServiceSpec.ServicePort] = []
        for idx, entry in enumerate(ports_in):
            try:
                svc_port = int(entry.get("port"))
            except Exception:
                continue
            node_port_raw = entry.get("nodePort")
            try:
                node_port = int(node_port_raw) if node_port_raw is not None else None
            except Exception:
                node_port = None
            tgt_raw = entry.get("targetPort", svc_port)
            tgt_val: int | None
            if isinstance(tgt_raw, int):
                tgt_val = tgt_raw
            else:
                try:
                    tgt_val = int(tgt_raw)
                except Exception:
                    # Fallback: when targetPort is a named port (e.g., "http"), just reuse service port
                    tgt_val = svc_port
            host_port = node_port if node_port is not None and expose_host else None
            svc_ports.append(
                ServiceSpec.ServicePort(
                    name=entry.get("name") or f"port-{idx}",
                    port=int(host_port or svc_port),
                    target_port=tgt_val,
                    protocol=entry.get("protocol", "TCP"),
                    node_port=node_port if expose_host else None,
                )
            )
        if not svc_ports:
            return None
        svc_spec = ServiceSpec(
            type=svc_type,
            ports=svc_ports,
            port=svc_ports[0].port if expose_host else None,
            target_port=svc_ports[0].target_port,
            external_ips=spec.get("externalIPs", []),
            session_affinity=spec.get("sessionAffinity"),
        )
        dep_key = (svc.namespace, target)
        return dep_key, svc_spec

    def _prepare_service_ports(
        self, svc: K8sObject, spec: dict[str, Any], expose_host: bool
    ) -> list[dict[str, Any]]:
        desired = spec.get("ports") or []
        svc_key = f"{svc.namespace or ''}/{svc.name}"
        if not desired:
            self._release_service_ports(svc_key)
            return []
        seen_ids: set[str] = set()
        prepared: list[dict[str, Any]] = []
        for idx, entry in enumerate(desired):
            port_entry = dict(entry)
            port_id = str(port_entry.get("name") or f"idx-{idx}")
            seen_ids.add(port_id)
            node_port = port_entry.get("nodePort")
            if expose_host:
                if node_port is None:
                    node_port = self._allocate_node_port(svc_key, port_id)
                    port_entry["nodePort"] = node_port
                else:
                    try:
                        node_port = int(node_port)
                    except Exception:
                        node_port = self._allocate_node_port(svc_key, port_id)
                    self._reserve_node_port(svc_key, port_id, node_port)
                port_entry["port"] = node_port
            else:
                self._release_port_assignment(svc_key, port_id)
            prepared.append(port_entry)
        self._cleanup_unused_ports(svc_key, seen_ids)
        return prepared

    def _cleanup_unused_ports(self, svc_key: str, keep: set[str]) -> None:
        with self._lock:
            existing = self._port_assignments.get(svc_key)
            if not existing:
                return
            stale = [pid for pid in existing if pid not in keep]
            for pid in stale:
                port = existing.pop(pid, None)
                if port is not None:
                    self._used_ports.discard(port)
            if not existing:
                self._port_assignments.pop(svc_key, None)
            self._save_port_assignments()

    def _allocate_node_port(self, svc_key: str, port_id: str) -> int:
        with self._lock:
            existing = self._port_assignments.setdefault(svc_key, {})
            if port_id in existing:
                return existing[port_id]
            for candidate in range(self._port_low, self._port_high + 1):
                if candidate not in self._used_ports:
                    existing[port_id] = candidate
                    self._used_ports.add(candidate)
                    self._save_port_assignments()
                    return candidate
        raise RuntimeError("No available nodePort in configured range")

    def _reserve_node_port(self, svc_key: str, port_id: str, port: int) -> None:
        with self._lock:
            self._port_assignments.setdefault(svc_key, {})[port_id] = port
            self._used_ports.add(port)
            self._save_port_assignments()

    def _release_service_ports(self, svc_key: str) -> None:
        with self._lock:
            ports = self._port_assignments.pop(svc_key, None)
            if ports:
                for port in ports.values():
                    self._used_ports.discard(port)
                self._save_port_assignments()

    def _release_port_assignment(self, svc_key: str, port_id: str) -> None:
        with self._lock:
            ports = self._port_assignments.get(svc_key)
            if not ports:
                return
            port = ports.pop(port_id, None)
            if port is not None:
                self._used_ports.discard(port)
            if not ports:
                self._port_assignments.pop(svc_key, None)
            self._save_port_assignments()

    def _load_port_assignments(self) -> None:
        if not self._port_file.exists():
            return
        try:
            data = json.loads(self._port_file.read_text())
        except Exception:
            return
        if not isinstance(data, dict):
            return
        for svc_key, ports in data.items():
            if not isinstance(ports, dict):
                continue
            normalized: dict[str, int] = {}
            for pid, port in ports.items():
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    continue
                normalized[str(pid)] = port
                self._used_ports.add(port)
            if normalized:
                self._port_assignments[svc_key] = normalized

    def _save_port_assignments(self) -> None:
        try:
            tmp = self._port_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._port_assignments))
            tmp.replace(self._port_file)
        except Exception:
            pass

    def _handle_ingress_event(self, ev: str, obj: K8sObject) -> None:
        if ev == "DELETED":
            dep_key = None
            with self._lock:
                dep_key = self._ingress_owner_map.pop((obj.namespace, obj.name), None)
                if dep_key:
                    self._ingress_specs.pop(dep_key, None)
            if dep_key:
                self._trigger_reconcile(dep_key[0], dep_key[1])
            return
        result = self._ingress_spec_for(obj)
        if not result:
            dep_key = None
            with self._lock:
                dep_key = self._ingress_owner_map.pop((obj.namespace, obj.name), None)
                if dep_key:
                    self._ingress_specs.pop(dep_key, None)
            if dep_key:
                self._trigger_reconcile(dep_key[0], dep_key[1])
            return
        dep_key, ingress_spec = result
        with self._lock:
            self._ingress_specs[dep_key] = ingress_spec
            self._ingress_owner_map[(obj.namespace, obj.name)] = dep_key
        self._trigger_reconcile(dep_key[0], dep_key[1])

    def _ingress_spec_for(
        self, ing: K8sObject
    ) -> tuple[tuple[str | None, str], IngressSpec] | None:
        spec = ing.spec or {}
        rules = spec.get("rules") or []
        tls_entries = spec.get("tls") or []
        target_key = None
        host = None
        path = "/"
        for rule in rules:
            rule_host = rule.get("host")
            http = rule.get("http") or {}
            for path_entry in http.get("paths", []):
                backend = path_entry.get("backend", {}).get("service", {})
                svc_name = backend.get("name")
                if not svc_name:
                    continue
                key = self._service_name_map.get((ing.namespace, svc_name))
                if key:
                    target_key = key
                    host = rule_host
                    path = path_entry.get("path") or "/"
                    break
            if target_key:
                break
        if not target_key:
            return None
        tls_secret = None
        tls_enabled = False
        if host:
            for entry in tls_entries:
                hosts = entry.get("hosts", []) or []
                if host in hosts:
                    tls_enabled = True
                    tls_secret = entry.get("secretName")
                    break
        ingress_spec = IngressSpec(
            host=host or "",
            path=path,
            tls=tls_enabled,
            tlsSecretName=tls_secret,
        )
        return target_key, ingress_spec


def build_adapter(store: ObjectStore, runtime: RuntimeAdapter | None = None) -> AdapterWorker:
    db_path = os.getenv("AE_STATE_DB", "state/controller.db")
    state = SQLiteStateStore(Path(db_path))  # type: ignore[name-defined]
    runtime = runtime or _runtime_from_env()
    # Minimal reconciler wiring; skip ingress/secrets/config extras for MVP
    from ae.controller.health import HealthManager

    reconciler = Reconciler(runtime=runtime, state_store=state, health_manager=HealthManager())
    return AdapterWorker(store, state, reconciler)
