"""Metrics helpers derived from state store snapshots."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ae.controller.state import SQLiteStateStore
from ae.storage.config import load_storage_quotas


@dataclass(slots=True)
class MetricsSnapshot:
    total_apps: int
    ready_apps: int
    progressing_apps: int
    degraded_apps: int
    total_replicas: int
    ready_replicas: int
    live_replicas: int
    total_nodes: int = 0
    ready_nodes: int = 0
    stale_nodes: int = 0
    total_services: int = 0
    total_pvs: int = 0
    healthy_pvs: int = 0
    unhealthy_pvs: int = 0
    storage_used_bytes: dict[str, int] = field(default_factory=dict)
    storage_quota_bytes: dict[str, int] = field(default_factory=dict)


class MetricsService:
    """Aggregates metrics from application status records."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self._store = store
        self._shim_store = self._init_shim_store()

    @staticmethod
    def _init_shim_store():
        try:
            from ae.apishim.store import ObjectStore
        except Exception:
            return None
        dsn = os.getenv("AE_APISHIM_DSN")
        db_env = os.getenv("AE_APISHIM_DB")
        db_path = Path(db_env or "state/apishim.db")
        if not dsn and not db_path.exists():
            return None
        try:
            return ObjectStore(dsn=dsn) if dsn else ObjectStore(db_path=db_path)
        except Exception:
            return None

    def snapshot(self) -> MetricsSnapshot:
        statuses = self._store.list_status()
        total_apps = len(statuses)
        ready_apps = sum(1 for status in statuses if status.revision_status == "ready")
        progressing_apps = sum(1 for status in statuses if status.revision_status == "progressing")
        degraded_apps = total_apps - ready_apps - progressing_apps
        total_replicas = sum(status.desired_replicas for status in statuses)
        ready_replicas = sum(status.ready_replicas for status in statuses)
        live_replicas = sum(status.live_replicas for status in statuses)
        # Node and service counts (best-effort, optional for single-node)
        try:
            import os
            from datetime import datetime, timezone

            grace = int(os.getenv("AE_NODE_NOTREADY_AFTER", "40") or 40)
            now = datetime.now(timezone.utc)
            nodes = self._store.list_nodes()
            total_nodes = len(nodes)
            ready_nodes = 0
            stale_nodes = 0
            for node, status in nodes:
                if status is None:
                    stale_nodes += 1
                    continue
                try:
                    age = (now - status.seen_at).total_seconds()
                except Exception:
                    age = grace + 1
                stale = age > grace
                st = str(status.status or "").lower()
                if st == "ready" and not stale:
                    ready_nodes += 1
                if stale or st != "ready":
                    stale_nodes += 1
        except Exception:
            total_nodes = ready_nodes = stale_nodes = 0

        try:
            total_services = len(self._store.list_services())
        except Exception:
            total_services = 0

        total_pvs = healthy_pvs = unhealthy_pvs = 0
        storage_used: dict[str, int] = {}
        storage_quotas: dict[str, int] = {}
        if self._shim_store is not None:
            try:
                pvs = self._shim_store.list_all("", "v1", "persistentvolumes")
            except Exception:
                pvs = []
            for pv in pvs:
                backing = _pv_host_backing(pv)
                if backing is None:
                    continue
                _root, path = backing
                total_pvs += 1
                if path.exists():
                    healthy_pvs += 1
                else:
                    unhealthy_pvs += 1
            try:
                pvcs = self._shim_store.list_all("", "v1", "persistentvolumeclaims")
            except Exception:
                pvcs = []
            for pvc in pvcs:
                ns = pvc.namespace or "default"
                requested = _pvc_requested_storage(pvc)
                if not requested:
                    continue
                req_bytes = _quantity_bytes(requested)
                if req_bytes is None:
                    continue
                storage_used[ns] = storage_used.get(ns, 0) + req_bytes

        quota_path = os.getenv("AE_STORAGE_QUOTAS")
        if quota_path:
            quotas = load_storage_quotas(Path(quota_path))
            for quota in quotas:
                limit = _quantity_bytes(quota.hard_storage)
                if limit is None:
                    continue
                storage_quotas[quota.namespace] = limit

        return MetricsSnapshot(
            total_apps=total_apps,
            ready_apps=ready_apps,
            progressing_apps=progressing_apps,
            degraded_apps=degraded_apps,
            total_replicas=total_replicas,
            ready_replicas=ready_replicas,
            live_replicas=live_replicas,
            total_nodes=total_nodes,
            ready_nodes=ready_nodes,
            stale_nodes=stale_nodes,
            total_services=total_services,
            total_pvs=total_pvs,
            healthy_pvs=healthy_pvs,
            unhealthy_pvs=unhealthy_pvs,
            storage_used_bytes=storage_used,
            storage_quota_bytes=storage_quotas,
        )


def _pv_host_backing(pv) -> tuple[Path, Path] | None:
    meta = getattr(pv, "metadata", None) or {}
    annotations = meta.get("annotations") if isinstance(meta, dict) else {}
    if not isinstance(annotations, dict):
        return None
    host_root = annotations.get("k1s.io/nfs-host-root") or annotations.get("k1s.io/local-host-root")
    host_path = annotations.get("k1s.io/nfs-host-path") or annotations.get("k1s.io/local-host-path")
    if not host_root or not host_path:
        return None
    root = Path(str(host_root)).expanduser()
    path = Path(str(host_path)).expanduser()
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            return None
    except AttributeError:
        try:
            path.resolve().relative_to(root.resolve())
        except Exception:
            return None
    except Exception:
        return None
    return root, path


def _pvc_requested_storage(pvc) -> str | None:
    spec = getattr(pvc, "spec", None)
    if not isinstance(spec, dict):
        return None
    resources = spec.get("resources") if isinstance(spec, dict) else None
    resources = resources if isinstance(resources, dict) else {}
    requests = resources.get("requests") if isinstance(resources, dict) else None
    requests = requests if isinstance(requests, dict) else {}
    storage = requests.get("storage")
    return str(storage) if storage else None


def _quantity_bytes(raw: str | None) -> int | None:
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
            "t": 1024**4,
            "tb": 1024**4,
            "ti": 1024**4,
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


# ruff: noqa
# ruff: noqa: E501,UP017,B007
