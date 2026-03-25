from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

try:
    import requests  # noqa: F401
except ImportError:
    requests_stub = types.ModuleType("requests")

    class _RequestException(Exception):
        pass

    def _get(*_args, **_kwargs):
        raise _RequestException("requests stub")

    requests_stub.RequestException = _RequestException
    requests_stub.get = _get
    sys.modules["requests"] = requests_stub

try:
    import yaml  # noqa: F401
except ImportError:
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_load = lambda *_args, **_kwargs: {}
    yaml_stub.safe_dump = lambda *_args, **_kwargs: ""
    sys.modules["yaml"] = yaml_stub

from ae.observability.http_api import (
    _ApiHandler,
    _GATEWAY_WORK_METRICS,
    _HA_FENCE_METRICS,
    _HPA_ACTIVITY_METRICS,
    _JS_CONSUMER_STATS,
    _JS_STREAM_STATS,
    _ROUTE_BUNDLE_METRICS,
    _SITE_GATEWAY_BUILD_INFO,
    _SITE_GATEWAY_LAST_SEEN,
    _SITE_LAST_SEEN,
)


def _make_handler(
    tmp_path: Path,
    *,
    authority_info_fn=None,
    authority_members_fn=None,
    system_info_fn=None,
) -> _ApiHandler:
    handler = object.__new__(_ApiHandler)
    _ = tmp_path
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]
    if authority_info_fn is not None:
        handler.authority_info_fn = staticmethod(authority_info_fn)  # type: ignore[attr-defined]
    if authority_members_fn is not None:
        handler.authority_members_fn = staticmethod(authority_members_fn)  # type: ignore[attr-defined]
    if system_info_fn is not None:
        handler.system_info_fn = staticmethod(system_info_fn)  # type: ignore[attr-defined]

    def _noop(*_args, **_kwargs):
        return None

    handler.send_response = _noop  # type: ignore[attr-defined]
    handler.send_header = _noop  # type: ignore[attr-defined]
    handler.end_headers = _noop  # type: ignore[attr-defined]
    return handler


def _read_system_payload(handler: _ApiHandler) -> dict:
    _ApiHandler._handle_system(handler)  # type: ignore[arg-type]
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _restore_observability_state(original: dict[str, object]) -> None:
    _SITE_LAST_SEEN.clear()
    _SITE_LAST_SEEN.update(original["site_last_seen"])  # type: ignore[arg-type]
    _SITE_GATEWAY_LAST_SEEN.clear()
    _SITE_GATEWAY_LAST_SEEN.update(original["site_gateway_last_seen"])  # type: ignore[arg-type]
    _SITE_GATEWAY_BUILD_INFO.clear()
    _SITE_GATEWAY_BUILD_INFO.update(original["site_gateway_build_info"])  # type: ignore[arg-type]
    _JS_STREAM_STATS.clear()
    _JS_STREAM_STATS.update(original["js_stream_stats"])  # type: ignore[arg-type]
    _JS_CONSUMER_STATS.clear()
    _JS_CONSUMER_STATS.update(original["js_consumer_stats"])  # type: ignore[arg-type]
    _GATEWAY_WORK_METRICS.clear()
    _GATEWAY_WORK_METRICS.update(original["gateway_metrics"])  # type: ignore[arg-type]
    _ROUTE_BUNDLE_METRICS.clear()
    _ROUTE_BUNDLE_METRICS.update(original["route_metrics"])  # type: ignore[arg-type]
    _HA_FENCE_METRICS.clear()
    _HA_FENCE_METRICS.update(original["ha_fence_metrics"])  # type: ignore[arg-type]
    _HPA_ACTIVITY_METRICS.clear()
    _HPA_ACTIVITY_METRICS.update(original["hpa_activity"])  # type: ignore[arg-type]


def _snapshot_observability_state() -> dict[str, object]:
    return {
        "site_last_seen": dict(_SITE_LAST_SEEN),
        "site_gateway_last_seen": dict(_SITE_GATEWAY_LAST_SEEN),
        "site_gateway_build_info": dict(_SITE_GATEWAY_BUILD_INFO),
        "js_stream_stats": dict(_JS_STREAM_STATS),
        "js_consumer_stats": dict(_JS_CONSUMER_STATS),
        "gateway_metrics": dict(_GATEWAY_WORK_METRICS),
        "route_metrics": dict(_ROUTE_BUNDLE_METRICS),
        "ha_fence_metrics": dict(_HA_FENCE_METRICS),
        "hpa_activity": dict(_HPA_ACTIVITY_METRICS),
    }


def test_system_exposes_ha_snapshot_and_merges_probe_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_CONTROLLER_ID", "ctrl-a")
    monkeypatch.setenv("AE_CONTROLLER_ADVERTISE_ADDR", "http://ctrl-a:9108")
    monkeypatch.setenv("AE_ETCD_ENDPOINTS", "http://etcd-a:2379,http://etcd-b:2379")
    monkeypatch.setenv("AE_TRANSPORT_BACKEND", "nats-js")
    monkeypatch.setenv("AE_JS_DOMAIN", "K1S")
    monkeypatch.setenv("AE_CONTROLPLANE_LEASE_TTL_SECONDS", "15")
    monkeypatch.setenv("AE_CONTROLPLANE_KEEPALIVE_SECONDS", "5")
    monkeypatch.setattr("ae.observability.http_api.time.time", lambda: 200.0)
    original = _snapshot_observability_state()
    try:
        _SITE_LAST_SEEN.clear()
        _SITE_LAST_SEEN["sea"] = 1.0
        _SITE_GATEWAY_LAST_SEEN.clear()
        _SITE_GATEWAY_LAST_SEEN[("sea", "edge-1")] = 1.0
        _SITE_GATEWAY_BUILD_INFO.clear()
        _SITE_GATEWAY_BUILD_INFO[("sea", "edge-1")] = ("0.1.3.dev0", "sha-edge", "2026-03-18")
        _JS_STREAM_STATS.clear()
        _JS_STREAM_STATS["K1S_WORK"] = {"bytes_used": 12.0, "messages": 3.0, "max_bytes": 99.0}
        _JS_CONSUMER_STATS.clear()
        _JS_CONSUMER_STATS[("K1S_WORK", "WORK_SITE_sea")] = {
            "site_id": "sea",
            "pending": 4.0,
            "ack_pending": 2.0,
            "redelivered": 1.0,
            "waiting": 0.0,
        }
        _GATEWAY_WORK_METRICS.clear()
        _GATEWAY_WORK_METRICS["sea"] = {
            "work_nak_total": 1.0,
            "work_stale_total": 2.0,
            "lease_retry_total": 3.0,
            "result_replay_total": 4.0,
            "result_replay_fail_total": 0.0,
            "result_replay_backlog": 5.0,
        }
        _ROUTE_BUNDLE_METRICS.clear()
        _ROUTE_BUNDLE_METRICS["sea"] = {
            "apply_ok_total": 7.0,
            "apply_fail_total": 0.0,
            "publish_ok_total": 6.0,
            "publish_fail_total": 0.0,
            "pending": 1.0,
            "ack_age_s": 8.5,
            "last_latency_s": 1.2,
        }
        _HA_FENCE_METRICS.clear()
        _HA_FENCE_METRICS["gateway.lease_acquire"] = {
            "stale_total": 1.0,
            "duplicate_total": 1.0,
            "epoch_advance_total": 0.0,
        }
        _HPA_ACTIVITY_METRICS.clear()
        _HPA_ACTIVITY_METRICS.update(
            {
                "reconcile_total": 1.0,
                "scale_total": 1.0,
                "metrics_stale_total": 1.0,
                "metrics_missing_total": 0.0,
                "snapshot_age_seconds": 12.5,
            }
        )

        handler = _make_handler(
            tmp_path,
            authority_info_fn=lambda: SimpleNamespace(
                enabled=True,
                controller_id="ctrl-a",
                is_leader=False,
                leader_info=SimpleNamespace(
                    controller_id="ctrl-b",
                    controller_epoch=19,
                    advertise_addr="http://ctrl-b:9108",
                ),
                controller_epoch=19,
            ),
            authority_members_fn=lambda: [
                {
                    "controller_id": "ctrl-c",
                    "advertise_addr": "http://ctrl-c:9108",
                    "version": "0.1.0",
                },
                {
                    "controller_id": "ctrl-a",
                    "advertise_addr": "http://ctrl-a:9108",
                    "heartbeat_at": "1970-01-01T00:03:00+00:00",
                    "version": "0.1.0",
                },
                {
                    "controller_id": "ctrl-b",
                    "advertise_addr": "http://ctrl-b:9108",
                    "heartbeat_at": "1970-01-01T00:03:15+00:00",
                    "version": "0.1.1",
                },
            ],
            system_info_fn=lambda: {
                "ha_probes": {
                    "enabled": True,
                    "last_probe_ts": 1234.0,
                    "etcd": {
                        "healthy_endpoints": 1,
                        "unhealthy_endpoints": 1,
                        "members": [
                            {"name": "http://etcd-a:2379", "healthy": True, "detail": "ok"},
                            {"name": "http://etcd-b:2379", "healthy": False, "detail": "timeout"},
                        ],
                    },
                    "hubs": {"nodes": [{"name": "hub-a"}], "issues": [], "errors": []},
                    "edges": {"sites": [{"site_id": "sea", "issues": []}], "errors": []},
                }
            },
        )

        payload = _read_system_payload(handler)
    finally:
        _restore_observability_state(original)

    ha = payload["ha"]
    assert ha["enabled"] is True
    assert ha["authority"]["healthy"] is True
    assert ha["authority"]["leader_id"] == "ctrl-b"
    assert ha["authority"]["controller_epoch"] == 19
    assert ha["authority"]["member_count"] == 3
    assert [member["controller_id"] for member in ha["authority"]["members"]] == [
        "ctrl-b",
        "ctrl-a",
        "ctrl-c",
    ]
    assert ha["authority"]["members"][0]["role"] == "leader"
    assert ha["authority"]["members"][0]["is_leader"] is True
    assert ha["authority"]["members"][0]["freshness"] == "fresh"
    assert ha["authority"]["members"][0]["last_heartbeat_at"] == "1970-01-01T00:03:15+00:00"
    assert ha["authority"]["members"][0]["last_heartbeat_age_s"] == 5.0
    assert ha["authority"]["members"][1]["is_local"] is True
    assert ha["authority"]["members"][1]["freshness"] == "stale"
    assert ha["authority"]["members"][1]["last_heartbeat_age_s"] == 20.0
    assert ha["authority"]["members"][1]["stale_after_seconds"] == 10.0
    assert ha["authority"]["members"][2]["freshness"] == "unknown"
    assert ha["authority"]["members"][2]["last_heartbeat_at"] is None
    assert ha["etcd"]["healthy_endpoints"] == 1
    assert ha["etcd"]["unhealthy_endpoints"] == 1
    assert ha["transport"]["backend"] == "nats-js"
    assert ha["transport"]["jetstream"]["consumer_count"] == 1
    assert ha["transport"]["gateway"]["result_replay_backlog"] == 5.0
    assert ha["transport"]["routes"]["max_ack_age_s"] == 8.5
    assert ha["transport"]["fence"]["stale_total"] == 1.0
    assert ha["transport"]["sites"][0]["site_id"] == "sea"
    issue_codes = {issue["code"] for issue in ha["issues"]}
    assert "etcd_probe_degraded" in issue_codes
    assert "gateway_replay_backlog" in issue_codes
    assert "route_publish_pending" in issue_codes
    assert "ha_fence_activity" in issue_codes
    assert "hpa_metrics_quality" in issue_codes


def test_system_marks_authority_unhealthy_when_no_leader_visible(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    handler = _make_handler(
        tmp_path,
        authority_info_fn=lambda: SimpleNamespace(
            enabled=True,
            controller_id="ctrl-a",
            is_leader=False,
            leader_info=None,
            controller_epoch=0,
        ),
    )

    payload = _read_system_payload(handler)

    assert payload["ha"]["enabled"] is True
    assert payload["ha"]["authority"]["healthy"] is False
    assert payload["ha"]["issues"][0]["code"] == "authority_unhealthy"


def test_system_reports_ha_disabled_when_not_in_ha_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AE_HA_MODE", raising=False)
    monkeypatch.delenv("AE_ETCD_ENDPOINTS", raising=False)
    handler = _make_handler(tmp_path)

    payload = _read_system_payload(handler)

    assert payload["ha"]["enabled"] is False
    assert payload["ha"]["authority"]["healthy"] is True
    assert payload["ha"]["etcd"]["configured_endpoints"] == []
    assert payload["ha"]["issues"] == []
