"""Background HA dashboard probes for the integrated Hive dashboard."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypeVar

from ae.config.transport import desired_js_replicas
from ae.ha.ops import (
    etcd_endpoint_healthy,
    evaluate_nats_edge_site,
    evaluate_nats_hub_cluster,
    fetch_nats_edge_monitor_record,
    fetch_nats_hub_monitor_record,
    parse_nats_edge_site_target,
    parse_nats_hub_node_target,
    split_csv,
)

LOGGER = logging.getLogger(__name__)

_TargetT = TypeVar("_TargetT")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_targets(
    raw: str | None,
    parser: Callable[[str], _TargetT],
    *,
    kind: str,
) -> tuple[_TargetT, ...]:
    targets: list[_TargetT] = []
    for item in split_csv(raw):
        try:
            targets.append(parser(item))
        except ValueError as exc:
            LOGGER.warning("ignoring invalid HA dashboard %s target %r: %s", kind, item, exc)
    return tuple(targets)


@dataclass(frozen=True, slots=True)
class HaDashboardProbeConfig:
    interval_s: float
    timeout_s: float
    etcd_endpoints: tuple[str, ...]
    hub_targets: tuple[Any, ...]
    edge_targets: tuple[Any, ...]
    expected_domain: str | None
    expected_stream: str
    expected_replicas: int
    expected_consumers: tuple[str, ...]
    expected_leaf_min: int | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "HaDashboardProbeConfig | None":
        env_map = env or os.environ
        if not _truthy(env_map.get("AE_HA_DASHBOARD_PROBES")):
            return None
        hub_targets = _parse_targets(
            env_map.get("AE_HA_DASHBOARD_HUB_MONITORS"),
            parse_nats_hub_node_target,
            kind="hub",
        )
        edge_targets = _parse_targets(
            env_map.get("AE_HA_DASHBOARD_EDGE_MONITORS"),
            parse_nats_edge_site_target,
            kind="edge",
        )
        consumer_prefix = (
            str(env_map.get("AE_JS_CONSUMER_PREFIX") or "WORK_SITE_").strip() or "WORK_SITE_"
        )
        return cls(
            interval_s=max(5.0, _env_float(env_map, "AE_HA_DASHBOARD_ETCD_PROBE_INTERVAL_S", 30.0)),
            timeout_s=max(0.5, _env_float(env_map, "AE_HA_DASHBOARD_PROBE_TIMEOUT_S", 2.0)),
            etcd_endpoints=tuple(split_csv(env_map.get("AE_ETCD_ENDPOINTS"))),
            hub_targets=hub_targets,
            edge_targets=edge_targets,
            expected_domain=(str(env_map.get("AE_JS_DOMAIN") or "").strip() or None),
            expected_stream=str(env_map.get("AE_JS_STREAM_NAME") or "K1S_WORK").strip()
            or "K1S_WORK",
            expected_replicas=max(1, desired_js_replicas(env_map)),
            expected_consumers=tuple(
                f"{consumer_prefix}{target.site_id}" for target in edge_targets
            ),
            expected_leaf_min=(len(edge_targets) if edge_targets else None),
        )


class HaDashboardProbeCache:
    def __init__(self, config: HaDashboardProbeConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] = {
            "enabled": True,
            "last_probe_ts": None,
            "etcd": {
                "members": [],
                "healthy_endpoints": 0,
                "unhealthy_endpoints": 0,
            },
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "HaDashboardProbeCache | None":
        config = HaDashboardProbeConfig.from_env(env)
        if config is None:
            return None
        return cls(config)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="ha-dashboard-probes",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._snapshot)

    def run_once(self) -> dict[str, Any]:
        now = time.time()
        payload: dict[str, Any] = {
            "enabled": True,
            "last_probe_ts": now,
            "etcd": self._probe_etcd(),
        }
        if self._config.hub_targets:
            payload["hubs"] = self._probe_hubs()
        if self._config.edge_targets:
            payload["edges"] = self._probe_edges()
        with self._lock:
            self._snapshot = payload
        return payload

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("HA dashboard probe loop failed: %s", exc)
            self._stop.wait(self._config.interval_s)

    def _probe_etcd(self) -> dict[str, Any]:
        members: list[dict[str, Any]] = []
        healthy = 0
        for endpoint in self._config.etcd_endpoints:
            ok = False
            detail = "missing endpoint"
            try:
                ok, detail = etcd_endpoint_healthy(endpoint, timeout_s=self._config.timeout_s)
            except Exception as exc:  # noqa: BLE001
                detail = str(exc)
            if ok:
                healthy += 1
            members.append(
                {
                    "endpoint": endpoint,
                    "name": endpoint,
                    "healthy": bool(ok),
                    "detail": str(detail or ""),
                }
            )
        return {
            "members": members,
            "healthy_endpoints": healthy,
            "unhealthy_endpoints": max(0, len(members) - healthy),
        }

    def _probe_hubs(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        records = []
        errors: list[dict[str, str]] = []
        for target in self._config.hub_targets:
            try:
                record = fetch_nats_hub_monitor_record(
                    target,
                    timeout_s=self._config.timeout_s,
                    include_leafz=True,
                )
                records.append(record)
                nodes.append(
                    {
                        "name": record.name,
                        "monitor_url": record.monitor_url,
                        "server_name": record.server_name,
                        "server_id": record.server_id,
                        "version": record.version,
                        "git_commit": record.git_commit,
                        "cluster_name": record.cluster_name,
                        "jetstream_domain": record.jetstream_domain,
                        "meta_leader": record.meta_leader,
                        "route_count": record.route_count,
                        "route_peers": list(record.route_peers),
                        "leaf_count": record.leaf_count,
                        "streams": [
                            {
                                "name": stream_name,
                                "leader": record.stream_leaders.get(stream_name),
                                "replicas": int(record.stream_replicas.get(stream_name, 0) or 0),
                                "offline": list(record.stream_offline.get(stream_name) or ()),
                            }
                            for stream_name in sorted(record.stream_replicas)
                        ],
                        "consumers": [
                            {
                                "name": consumer_name,
                                "leader": record.consumer_leaders.get(consumer_name),
                                "replicas": int(
                                    record.consumer_replicas.get(consumer_name, 0) or 0
                                ),
                                "offline": list(record.consumer_offline.get(consumer_name) or ()),
                            }
                            for consumer_name in sorted(record.consumer_replicas)
                        ],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "name": target.name,
                        "monitor_url": target.monitor_url,
                        "message": str(exc),
                    }
                )
        expected_domain = self._config.expected_domain
        if not expected_domain:
            for record in records:
                if record.jetstream_domain:
                    expected_domain = record.jetstream_domain
                    break
        issues = evaluate_nats_hub_cluster(
            records,
            expected_domain=expected_domain or "",
            expected_stream=self._config.expected_stream,
            expected_replicas=self._config.expected_replicas,
            expected_consumers=list(self._config.expected_consumers),
            expected_leaf_min=self._config.expected_leaf_min,
        )
        return {
            "expected_stream": self._config.expected_stream,
            "expected_replicas": self._config.expected_replicas,
            "expected_domain": expected_domain or "",
            "nodes": nodes,
            "issues": issues,
            "errors": errors,
            "healthy": not issues and not errors,
        }

    def _probe_edges(self) -> dict[str, Any]:
        sites: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for target in self._config.edge_targets:
            try:
                record = fetch_nats_edge_monitor_record(
                    target,
                    timeout_s=self._config.timeout_s,
                    include_leafz=True,
                )
                issues = evaluate_nats_edge_site(
                    record,
                    expected_leaf_min=1,
                )
                sites.append(
                    {
                        "site_id": record.site_id,
                        "monitor_url": record.monitor_url,
                        "server_name": record.server_name,
                        "server_id": record.server_id,
                        "version": record.version,
                        "git_commit": record.git_commit,
                        "leaf_count": record.leaf_count,
                        "issues": issues,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "site_id": target.site_id,
                        "monitor_url": target.monitor_url,
                        "message": str(exc),
                    }
                )
        return {
            "sites": sites,
            "errors": errors,
            "healthy": not errors and all(not site["issues"] for site in sites),
        }


__all__ = ["HaDashboardProbeCache", "HaDashboardProbeConfig"]
