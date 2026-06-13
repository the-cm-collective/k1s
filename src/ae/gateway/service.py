"""Site Gateway skeleton (Phase 2)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ae import build_info as AE_BUILD_INFO
from ae.config.transport import GatewayJetStreamConfig, check_nats_connectivity
from ae.gateway.spool import GatewaySpool
from ae.ha.fencing import MutationEnvelope, SQLiteFenceStore, merge_envelope, parse_envelope
from ae.ingress.edge_local import build_edge_local_renderer
from ae.observability.http_api import record_ha_fence_event, record_route_bundle_apply
from ae.transport import (
    hub_caps_subject,
    hub_lease_acquire_subject,
    hub_lease_renew_subject,
    hub_logs_subject,
    hub_route_ack_subject,
    hub_route_bundle_subject,
    hub_result_subject,
    hub_status_subject,
    hub_work_ack_subject,
    hub_work_pull_subject,
    local_caps_subject,
    local_logs_subject,
    local_result_subject,
    local_status_subject,
    local_work_progress_subject,
    local_work_subject,
    work_stream_subject,
)

if TYPE_CHECKING:
    from ae.transport.nats_client import JetStreamMessage, NatsClient

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayStats:
    inflight: int = 0
    accepted: int = 0
    completed: int = 0
    failed: int = 0
    stale: int = 0
    nacked: int = 0
    lease_retry_total: int = 0
    result_replay_total: int = 0
    result_replay_fail_total: int = 0
    last_report_at: float = 0.0


class SiteGateway:
    def __init__(
        self,
        *,
        site_id: str,
        node_id: str | None,
        nats_url: str | None,
        js_config: GatewayJetStreamConfig,
        status_interval_s: int,
        nats_client: "NatsClient | None" = None,
    ) -> None:
        self._site_id = site_id
        self._node_id = node_id or os.getenv("AE_NODE_ID") or "unknown-node"
        self._nats_url = nats_url
        self._js_config = js_config
        self._status_interval_s = max(5, status_interval_s)
        self._stats = GatewayStats()
        self._nats_client = nats_client
        self._backend = os.getenv("AE_TRANSPORT_BACKEND", "http").lower()
        self._gateway_backend = os.getenv("AE_GATEWAY_BACKEND") or "gateway"
        self._node_labels = _merge_node_labels(self._site_id, self._node_id)
        self._js_stream = os.getenv("AE_JS_STREAM_NAME", "K1S_WORK")
        self._spool = GatewaySpool(self._js_config.spool_path)
        self._fence = SQLiteFenceStore(_gateway_fence_db_path(self._js_config.spool_path))
        self._fence_scope = f"site:{self._site_id}"
        self._spool_enabled = True
        self._keep_spool = _truthy_env("AE_GATEWAY_KEEP_SPOOL")
        self._inflight: dict[str, JetStreamMessage] = {}
        self._inflight_progress: dict[str, float] = {}
        self._inflight_heartbeat: dict[str, float] = {}
        self._inflight_envelopes: dict[str, MutationEnvelope] = {}
        self._completed: dict[str, float] = {}
        self._running_sent: dict[str, float] = {}
        self._js_enabled = False
        self._last_pull_at = 0.0
        self._pull_interval_s = 1.0
        self._progress_interval_s = _parse_duration_seconds(
            js_config.progress_interval, default=10.0
        )
        self._ack_wait_s = _parse_duration_seconds(js_config.ack_wait, default=30.0)
        self._heartbeat_timeout_s = self._resolve_heartbeat_timeout()
        self._nak_delay_s = self._resolve_nak_delay()
        self._session_id = str(uuid.uuid4())
        self._lease_id: str | None = None
        self._lease_acquire_request_id: str | None = None
        self._lease_renew_request_id: str | None = None
        self._lease_ttl_ms = 0
        self._renew_after_ms = 0
        self._next_renew_at = 0.0
        self._status_every_s = max(
            5.0, float(os.getenv("AE_GATEWAY_STATUS_PUBLISH_INTERVAL", "10") or 10)
        )
        self._logs_every_s = max(
            5.0, float(os.getenv("AE_GATEWAY_LOGS_PUBLISH_INTERVAL", "15") or 15)
        )
        self._last_status_publish = 0.0
        self._last_logs_publish = 0.0
        self._status_sample_rate = _parse_float(
            os.getenv("AE_GATEWAY_STATUS_SAMPLE_RATE"), 1.0
        )
        self._logs_sample_rate = _parse_float(
            os.getenv("AE_GATEWAY_LOGS_SAMPLE_RATE"), 1.0
        )
        self._lease_timeout_s = _parse_duration_seconds(
            os.getenv("AE_GATEWAY_LEASE_TIMEOUT"), default=5.0
        )
        self._lease_retry_min_s = max(
            0.5,
            _parse_duration_seconds(
                os.getenv("AE_GATEWAY_LEASE_RETRY_MIN", "") or "", default=1.0
            ),
        )
        self._lease_retry_max_s = max(
            self._lease_retry_min_s,
            _parse_duration_seconds(
                os.getenv("AE_GATEWAY_LEASE_RETRY_MAX", "") or "", default=30.0
            ),
        )
        self._lease_retry_jitter_pct = max(
            0.0,
            _parse_float(os.getenv("AE_GATEWAY_LEASE_RETRY_JITTER"), 0.2),
        )
        self._lease_next_attempt_at = 0.0
        self._lease_backoff_s = self._lease_retry_min_s
        self._result_retry_min_s = max(
            1.0,
            _parse_duration_seconds(
                os.getenv("AE_GATEWAY_RESULT_RETRY_MIN", "") or "", default=2.0
            ),
        )
        self._result_retry_max_s = max(
            self._result_retry_min_s,
            _parse_duration_seconds(
                os.getenv("AE_GATEWAY_RESULT_RETRY_MAX", "") or "", default=30.0
            ),
        )
        self._route_bundle_rev = 0
        self._route_bundle_hash: str | None = None
        self._edge_local_renderer = build_edge_local_renderer()
        self._transport_ready = False
        self._transport_listener_registered = False
        self._transport_retry_min_s = max(
            0.5,
            _parse_duration_seconds(
                os.getenv("AE_GATEWAY_TRANSPORT_RETRY_MIN", "") or "", default=1.0
            ),
        )
        self._transport_retry_max_s = max(
            self._transport_retry_min_s,
            _parse_duration_seconds(
                os.getenv("AE_GATEWAY_TRANSPORT_RETRY_MAX", "") or "", default=30.0
            ),
        )
        self._transport_backoff_s = self._transport_retry_min_s
        self._transport_next_connect_at = 0.0

    def _subjects(self) -> list[str]:
        return [
            local_work_subject(self._node_id),
            local_result_subject(),
            local_work_progress_subject(),
            local_status_subject(self._node_id),
            local_logs_subject(self._node_id),
            local_caps_subject(self._node_id),
            hub_lease_acquire_subject(self._site_id),
            hub_lease_renew_subject(self._site_id),
            hub_result_subject(self._site_id),
            hub_status_subject(self._site_id),
            hub_logs_subject(self._site_id),
            hub_caps_subject(self._site_id),
            hub_work_pull_subject(self._site_id),
            hub_work_ack_subject(self._site_id),
            hub_route_bundle_subject(self._site_id),
            hub_route_ack_subject(self._site_id),
            work_stream_subject(self._site_id),
        ]

    def _log_subjects(self) -> None:
        subjects = self._subjects()
        LOGGER.info("gateway subjects:")
        for subj in subjects:
            LOGGER.info("  %s", subj)

    def _log_config(self) -> None:
        LOGGER.info("site_id=%s node_id=%s", self._site_id, self._node_id)
        LOGGER.info("nats_url=%s", self._nats_url or "<unset>")
        LOGGER.info(
            "js ack_wait=%s progress=%s jitter_pct=%s max_ack_pending=%s max_deliver=%s max_waiting=%s",
            self._js_config.ack_wait,
            self._js_config.progress_interval,
            self._js_config.progress_jitter_pct,
            self._js_config.max_ack_pending,
            self._js_config.max_deliver,
            self._js_config.max_waiting,
        )
        LOGGER.info("spool_path=%s", self._js_config.spool_path)
        LOGGER.info("spool_keep=%s", self._keep_spool)
        LOGGER.info(
            "work heartbeat timeout=%.1fs nak_delay=%.1fs",
            self._heartbeat_timeout_s,
            self._nak_delay_s,
        )
        LOGGER.info(
            "lease retry min=%.1fs max=%.1fs jitter_pct=%.2f",
            self._lease_retry_min_s,
            self._lease_retry_max_s,
            self._lease_retry_jitter_pct,
        )

    def _log_connectivity(self) -> None:
        if not self._nats_url:
            LOGGER.warning("AE_NATS_URL not set; skipping nats connectivity check")
            return
        ok, detail = check_nats_connectivity(self._nats_url)
        if ok:
            LOGGER.info("nats connectivity ok (%s)", detail)
        else:
            LOGGER.warning("nats connectivity failed (%s)", detail)

    def _resolve_heartbeat_timeout(self) -> float:
        raw = os.getenv("AE_GATEWAY_WORK_HEARTBEAT_TIMEOUT")
        default = max(self._progress_interval_s * 2.0, self._ack_wait_s * 0.8)
        if raw:
            return _parse_duration_seconds(raw, default=default)
        return default

    def _resolve_nak_delay(self) -> float:
        raw = os.getenv("AE_GATEWAY_WORK_NAK_DELAY")
        default = min(5.0, max(1.0, self._progress_interval_s))
        if raw:
            return _parse_duration_seconds(raw, default=default)
        return default

    def start(self, *, once: bool = False) -> None:
        self._log_config()
        self._log_connectivity()
        self._fence.init()
        try:
            self._spool.init()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("spool init failed: %s", exc)
            self._spool_enabled = False
        now = time.monotonic()
        self._ensure_transport_connected(now)
        self._log_subjects()
        if once:
            self._shutdown()
            return
        LOGGER.info("gateway running (backend=%s)", self._backend)
        self._stats.last_report_at = time.monotonic()
        try:
            while True:
                time.sleep(1)
                now = time.monotonic()
                transport_ready = self._ensure_transport_connected(now)
                self._run_progress(now)
                if transport_ready:
                    self._maybe_acquire(now)
                    self._maybe_renew(now)
                if self._nats_client is not None and transport_ready:
                    if self._js_enabled:
                        self._poll_js(now)
                    else:
                        self._poll_work_pull(now)
                    self._replay_spool_results(now)
                    self._publish_telemetry(now)
                if now - self._stats.last_report_at >= self._status_interval_s:
                    self._stats.last_report_at = now
                    LOGGER.info(
                        "gateway stats inflight=%s accepted=%s completed=%s failed=%s",
                        self._stats.inflight,
                        self._stats.accepted,
                        self._stats.completed,
                        self._stats.failed,
                    )
        except KeyboardInterrupt:
            LOGGER.info("gateway shutdown requested")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        if self._nats_client is not None:
            try:
                self._nats_client.close()
            except Exception:
                pass
        if not self._keep_spool:
            LOGGER.info("clearing gateway spool path=%s", self._js_config.spool_path)
            self._spool.cleanup()

    def _subscribe_local_results(self) -> None:
        if self._nats_client is None:
            return
        self._nats_client.subscribe(local_result_subject(), self._on_local_result)

    def _subscribe_local_progress(self) -> None:
        if self._nats_client is None:
            return
        self._nats_client.subscribe(local_work_progress_subject(), self._on_local_progress)

    def _subscribe_route_bundles(self) -> None:
        if self._nats_client is None:
            return
        self._nats_client.subscribe(
            hub_route_bundle_subject(self._site_id), self._on_route_bundle
        )

    def _ensure_transport_connected(self, now: float) -> bool:
        if self._nats_client is None:
            return False
        if self._transport_ready:
            if getattr(self._nats_client, "connected", True):
                return True
            self._transport_ready = False
        if now < self._transport_next_connect_at:
            return False
        try:
            self._nats_client.connect()
            if not self._transport_listener_registered and hasattr(
                self._nats_client, "add_reconnect_listener"
            ):
                self._nats_client.add_reconnect_listener(self._on_transport_reconnect)
                self._transport_listener_registered = True
            self._js_enabled = self._backend == "nats-js"
            self._subscribe_local_results()
            self._subscribe_local_progress()
            self._subscribe_route_bundles()
        except Exception as exc:  # noqa: BLE001
            self._transport_ready = False
            self._schedule_transport_retry(now, str(exc))
            return False
        self._transport_ready = True
        self._transport_backoff_s = self._transport_retry_min_s
        self._transport_next_connect_at = 0.0
        LOGGER.info("nats client connected")
        return True

    def _schedule_transport_retry(self, now: float, reason: str | None = None) -> None:
        delay = self._transport_backoff_s
        self._transport_next_connect_at = now + delay
        self._transport_backoff_s = min(
            self._transport_backoff_s * 2.0, self._transport_retry_max_s
        )
        if reason:
            LOGGER.warning("nats client connect failed; retry in %.1fs (%s)", delay, reason)
        else:
            LOGGER.warning("nats client connect retry in %.1fs", delay)

    def _on_local_result(self, msg) -> None:  # type: ignore[override]
        payload = _safe_json(msg.data)
        if not isinstance(payload, dict):
            return
        payload.setdefault("site_id", self._site_id)
        payload.setdefault("node_id", self._node_id)
        work_key = _work_key(payload)
        envelope = self._resolve_result_envelope(payload, work_key)
        if envelope is None:
            LOGGER.warning("dropping local result without fencing envelope site=%s", self._site_id)
            return
        payload = merge_envelope(payload, envelope)
        if self._spool_enabled and work_key:
            try:
                self._spool.record_result(
                    work_id=str(payload.get("work_id")),
                    attempt=int(payload.get("attempt") or 0),
                    status=str(payload.get("status") or ""),
                    payload=payload,
                )
                self._spool.update_inflight_state(
                    str(payload.get("work_id")),
                    int(payload.get("attempt") or 0),
                    "terminal",
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("spool record_result failed: %s", exc)
                return
        try:
            if self._nats_client is not None:
                self._nats_client.publish_json(hub_result_subject(self._site_id), payload)
                if self._spool_enabled and work_key:
                    try:
                        self._spool.mark_result_delivered(
                            work_id=str(payload.get("work_id")),
                            attempt=int(payload.get("attempt") or 0),
                        )
                    except Exception:
                        pass
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("publish work_result failed: %s", exc)
        if work_key and work_key in self._inflight:
            msg_js = self._inflight.pop(work_key)
            self._inflight_progress.pop(work_key, None)
            self._inflight_heartbeat.pop(work_key, None)
            self._inflight_envelopes.pop(work_key, None)
            self._running_sent.pop(work_key, None)
            self._completed[work_key] = time.monotonic()
            try:
                msg_js.ack_sync()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("js ack_sync failed for %s: %s", work_key, exc)
        if payload.get("status") in {"failed", "Failed"}:
            self._stats.failed += 1
        else:
            self._stats.completed += 1

    def _on_local_progress(self, msg) -> None:  # type: ignore[override]
        payload = _safe_json(msg.data)
        if not isinstance(payload, dict):
            return
        work_key = _work_key(payload)
        if not work_key:
            return
        self._inflight_heartbeat[work_key] = time.monotonic()

    def _on_route_bundle(self, msg) -> None:  # type: ignore[override]
        payload = _safe_json(msg.data)
        if not isinstance(payload, dict):
            return
        envelope = parse_envelope(payload)
        if envelope is None:
            LOGGER.warning("route bundle missing fencing envelope site=%s", self._site_id)
            return
        decision = self._fence.begin(self._fence_scope, envelope)
        _record_fence_decision(
            "gateway.route_bundle",
            decision,
            envelope,
            site_id=self._site_id,
            scope=self._fence_scope,
        )
        if decision.stale:
            LOGGER.warning(
                "route bundle rejected stale epoch site=%s op=%s",
                self._site_id,
                envelope.operation_id,
            )
            return
        site_id = payload.get("site_id") or self._site_id
        if str(site_id) != str(self._site_id):
            return
        bundle_rev = int(payload.get("bundle_rev") or 0)
        bundle_hash = payload.get("hash")
        if decision.epoch_advanced:
            self._route_bundle_rev = 0
            self._route_bundle_hash = None
        ok = True
        error = None
        if decision.duplicate:
            if bundle_rev > self._route_bundle_rev or (
                bundle_rev == self._route_bundle_rev and self._route_bundle_hash != bundle_hash
            ):
                if self._edge_local_renderer is not None:
                    ok, error = self._edge_local_renderer.apply_bundle(payload)
                if ok:
                    self._route_bundle_rev = bundle_rev
                    self._route_bundle_hash = bundle_hash
            else:
                ok = True
        elif bundle_rev < self._route_bundle_rev:
            ok = True
        elif bundle_rev == self._route_bundle_rev:
            if self._route_bundle_hash and bundle_hash != self._route_bundle_hash:
                ok = False
                error = "hash_mismatch"
        else:
            if self._edge_local_renderer is not None:
                ok, error = self._edge_local_renderer.apply_bundle(payload)
            if ok:
                self._route_bundle_rev = bundle_rev
                self._route_bundle_hash = bundle_hash
        latency_s = _bundle_latency_seconds(payload)
        try:
            record_route_bundle_apply(self._site_id, ok=ok, latency_seconds=latency_s)
        except Exception:
            pass
        if ok and not decision.duplicate:
            self._fence.commit(self._fence_scope, envelope)
        ack = merge_envelope(
            {
                "site_id": self._site_id,
                "bundle_rev": bundle_rev,
                "hash": bundle_hash,
                "applied_at": time.time(),
                "ok": ok,
                "error": error,
                "duplicate": decision.duplicate,
            },
            envelope,
        )
        try:
            if self._nats_client is not None:
                self._nats_client.publish_json(hub_route_ack_subject(self._site_id), ack)
        except Exception:
            pass

    def _replay_spool_results(self, now: float) -> None:
        if not self._spool_enabled or self._nats_client is None:
            return
        _ = now
        for record in self._spool.list_replay_ready_results(limit=100):
            if record.operation_id is None:
                continue
            try:
                self._nats_client.publish_json(
                    hub_result_subject(self._site_id), record.payload
                )
                self._spool.mark_result_delivered(record.work_id, record.attempt)
                self._stats.result_replay_total += 1
            except Exception as exc:
                retry_delay_s = _next_result_retry_delay(
                    record.replay_attempts + 1,
                    min_delay_s=self._result_retry_min_s,
                    max_delay_s=self._result_retry_max_s,
                )
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay_s)
                self._stats.result_replay_fail_total += 1
                LOGGER.debug(
                    "gateway replay failed site=%s work=%s/%s retry_in=%.1fs: %s",
                    self._site_id,
                    record.work_id,
                    record.attempt,
                    retry_delay_s,
                    exc,
                )
                try:
                    self._spool.record_result_delivery_attempt(
                        record.work_id,
                        record.attempt,
                        error=str(exc),
                        retry_at=retry_at,
                    )
                except Exception:
                    pass
                continue

    def _poll_work_pull(self, now: float) -> None:
        if now - self._last_pull_at < self._pull_interval_s:
            return
        self._last_pull_at = now
        if self._nats_client is None:
            return
        req = {
            "site_id": self._site_id,
            "gateway_id": self._node_id,
            "limit": max(1, int(self._js_config.max_ack_pending or 1)),
            "visibility_timeout_ms": int(self._ack_wait_s * 1000),
            "timestamp": time.time(),
        }
        try:
            resp = self._nats_client.request_json(hub_work_pull_subject(self._site_id), req)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("work.pull failed: %s", exc)
            return
        if not resp or not resp.get("accepted"):
            return
        work_items = resp.get("work") or []
        lease_ids = resp.get("lease_ids") or []
        ack_items: list[dict[str, object]] = []
        for idx, item in enumerate(work_items):
            if not isinstance(item, dict):
                continue
            envelope = parse_envelope(item)
            if envelope is None:
                LOGGER.warning("work.pull item missing fencing envelope site=%s", self._site_id)
                continue
            decision = self._fence.begin(self._fence_scope, envelope)
            _record_fence_decision(
                "gateway.work_pull",
                decision,
                envelope,
                site_id=self._site_id,
                scope=self._fence_scope,
            )
            lease_id = str(lease_ids[idx]) if idx < len(lease_ids) and lease_ids[idx] else ""
            if decision.accepted:
                if not self._dispatch_work(item, envelope):
                    continue
                self._fence.commit(self._fence_scope, envelope)
            if decision.accepted or decision.duplicate or decision.stale:
                if lease_id:
                    ack_items.append(
                        {
                            "lease_id": lease_id,
                            "work_id": item.get("work_id"),
                            "attempt": item.get("attempt"),
                            **envelope.as_dict(),
                        }
                    )
        if ack_items:
            ack_req = {
                "site_id": self._site_id,
                "gateway_id": self._node_id,
                "lease_ids": [str(item["lease_id"]) for item in ack_items],
                "ack_items": ack_items,
                "accepted_at": time.time(),
                "timestamp": time.time(),
            }
            try:
                self._nats_client.request_json(
                    hub_work_ack_subject(self._site_id), ack_req
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("work.ack failed: %s", exc)

    def _poll_js(self, now: float) -> None:
        if self._nats_client is None:
            return
        try:
            msgs = self._nats_client.fetch_js_messages(
                subject=work_stream_subject(self._site_id),
                durable=f"WORK_SITE_{self._site_id}",
                batch=max(1, int(self._js_config.max_ack_pending or 1)),
                timeout_s=1.0,
                stream=self._js_stream,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("js fetch failed: %s", exc)
            return
        for js_msg in msgs:
            payload = _safe_json(js_msg.data)
            if not isinstance(payload, dict):
                continue
            envelope = parse_envelope(payload)
            if envelope is None:
                try:
                    js_msg.ack_sync()
                except Exception:
                    pass
                continue
            decision = self._fence.begin(self._fence_scope, envelope)
            _record_fence_decision(
                "gateway.work_js",
                decision,
                envelope,
                site_id=self._site_id,
                scope=self._fence_scope,
            )
            if decision.stale or decision.duplicate:
                try:
                    js_msg.ack_sync()
                except Exception:
                    pass
                continue
            work_key = _work_key(payload)
            work_id = payload.get("work_id")
            attempt = payload.get("attempt")
            if (
                self._spool_enabled
                and self._js_enabled
                and work_id is not None
                and attempt is not None
            ):
                try:
                    result = self._spool.get_result(str(work_id), int(attempt))
                except Exception:
                    result = None
                if result is not None:
                    try:
                        js_msg.ack_sync()
                    except Exception:
                        pass
                    if work_key:
                        self._completed[work_key] = now
                    continue
                try:
                    inflight_state = self._spool.get_inflight_state(
                        str(work_id), int(attempt)
                    )
                except Exception:
                    inflight_state = None
                if inflight_state and inflight_state != "abandoned":
                    if work_key:
                        self._inflight[work_key] = js_msg
                        self._inflight_progress.setdefault(work_key, now)
                        self._inflight_heartbeat.setdefault(work_key, now)
                        self._stats.inflight = len(self._inflight)
                    continue
            if work_key and work_key in self._inflight:
                heartbeat_at = self._inflight_heartbeat.get(
                    work_key, self._inflight_progress.get(work_key, now)
                )
                if now - heartbeat_at > self._heartbeat_timeout_s:
                    self._stats.stale += 1
                    try:
                        js_msg.nak(self._nak_delay_s)
                        self._stats.nacked += 1
                    except Exception:
                        pass
                    if self._spool_enabled and work_id is not None and attempt is not None:
                        try:
                            self._spool.update_inflight_state(
                                str(work_id), int(attempt), "abandoned"
                            )
                        except Exception:
                            pass
                    self._inflight.pop(work_key, None)
                    self._inflight_progress.pop(work_key, None)
                    self._inflight_heartbeat.pop(work_key, None)
                    self._inflight_envelopes.pop(work_key, None)
                    self._running_sent.pop(work_key, None)
                    self._stats.inflight = len(self._inflight)
                else:
                    try:
                        js_msg.in_progress()
                    except Exception:
                        pass
                continue
            if work_key and work_key in self._completed:
                try:
                    js_msg.ack_sync()
                except Exception:
                    pass
                continue
            if self._spool_enabled and self._js_enabled and work_id is not None and attempt is not None:
                try:
                    self._spool.record_inflight(
                        work_id=str(work_id),
                        attempt=int(attempt),
                        js_stream=str(js_msg.stream or self._js_stream),
                        js_consumer=str(js_msg.consumer or f"WORK_SITE_{self._site_id}"),
                        js_seq=int(js_msg.seq or 0),
                        node_id=None,
                        state="accepted",
                        payload=payload,
                    )
                except Exception:
                    pass
            if not self._dispatch_work(payload, envelope):
                try:
                    js_msg.nak(self._nak_delay_s)
                    self._stats.nacked += 1
                except Exception:
                    pass
                continue
            self._fence.commit(self._fence_scope, envelope)
            if work_key:
                self._inflight[work_key] = js_msg
                self._inflight_progress[work_key] = now
                self._inflight_heartbeat[work_key] = now
                self._inflight_envelopes[work_key] = envelope
                self._stats.inflight = len(self._inflight)
                self._stats.accepted += 1

    def _dispatch_work(self, payload: dict, envelope: MutationEnvelope) -> bool:
        if self._nats_client is None:
            return False
        node_id = payload.get("preferred_node") or payload.get("node_id") or self._node_id
        try:
            self._nats_client.publish_json(local_work_subject(str(node_id)), payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("local work dispatch failed: %s", exc)
            return False
        if self._spool_enabled and not self._js_enabled:
            try:
                self._spool.record_inflight(
                    work_id=str(payload.get("work_id")),
                    attempt=int(payload.get("attempt") or 0),
                    js_stream="WORK_PULL",
                    js_consumer=f"WORK_PULL_{self._site_id}",
                    js_seq=0,
                    node_id=str(node_id),
                    state="accepted",
                    payload=payload,
                )
            except Exception:
                pass
        work_key = _work_key(payload)
        if work_key:
            self._inflight_envelopes[work_key] = envelope
        self._emit_running(payload, str(node_id), envelope)
        return True

    def _emit_running(self, payload: dict, node_id: str, envelope: MutationEnvelope) -> None:
        if self._nats_client is None:
            return
        work_key = _work_key(payload)
        if not work_key or work_key in self._running_sent:
            return
        if self._spool_enabled and self._js_enabled:
            try:
                self._spool.update_inflight_state(
                    str(payload.get("work_id")),
                    int(payload.get("attempt") or 0),
                    "running",
                    node_id=node_id,
                )
            except Exception:
                pass
        running = merge_envelope(
            {
                "work_id": payload.get("work_id"),
                "attempt": payload.get("attempt"),
                "site_id": self._site_id,
                "node_id": node_id,
                "status": "running",
                "observed_generation": payload.get("desired_generation"),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            envelope,
        )
        try:
            self._nats_client.publish_json(hub_result_subject(self._site_id), running)
            self._running_sent[work_key] = time.monotonic()
        except Exception:  # noqa: BLE001
            pass

    def _run_progress(self, now: float) -> None:
        if not self._inflight:
            return
        for work_key, js_msg in list(self._inflight.items()):
            last = self._inflight_progress.get(work_key, 0.0)
            heartbeat_at = self._inflight_heartbeat.get(work_key, last or now)
            if now - heartbeat_at > self._heartbeat_timeout_s:
                self._stats.stale += 1
                try:
                    js_msg.nak(self._nak_delay_s)
                    self._stats.nacked += 1
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("js nak failed for %s: %s", work_key, exc)
                if self._spool_enabled:
                    try:
                        work_id, attempt = work_key.split(":", 1)
                        self._spool.update_inflight_state(work_id, int(attempt), "abandoned")
                    except Exception:
                        pass
                self._inflight.pop(work_key, None)
                self._inflight_progress.pop(work_key, None)
                self._inflight_heartbeat.pop(work_key, None)
                self._inflight_envelopes.pop(work_key, None)
                self._running_sent.pop(work_key, None)
                self._stats.inflight = len(self._inflight)
                continue
            if now - last >= self._progress_interval_s:
                try:
                    js_msg.in_progress()
                    self._inflight_progress[work_key] = now
                except Exception:
                    pass
        # prune completed cache
        ttl = max(30.0, self._ack_wait_s * 3)
        for work_key, ts in list(self._completed.items()):
            if now - ts > ttl:
                self._completed.pop(work_key, None)
        for work_key, ts in list(self._running_sent.items()):
            if now - ts > ttl:
                self._running_sent.pop(work_key, None)

    def _acquire_lease(self, now: float | None = None) -> bool:
        if self._nats_client is None:
            return False
        if now is None:
            now = time.monotonic()
        request_id = self._lease_acquire_request_id or self._lease_request_id("lease.acquire")
        self._lease_acquire_request_id = request_id
        req = {
            "site_id": self._site_id,
            "node_id": self._node_id,
            "session_id": self._session_id,
            "timestamp": time.time(),
            "backend": self._gateway_backend,
            "labels": dict(self._node_labels or {}),
            "request_id": request_id,
        }
        try:
            resp = self._nats_client.request_json(
                hub_lease_acquire_subject(self._site_id),
                req,
                timeout_s=self._lease_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("lease acquire failed: %s", exc)
            self._schedule_lease_retry(now, str(exc))
            return False
        if not resp or not resp.get("accepted"):
            LOGGER.warning("lease acquire rejected: %s", resp.get("reason"))
            self._lease_acquire_request_id = None
            self._schedule_lease_retry(now, str(resp.get("reason") or "rejected"))
            return False
        envelope = parse_envelope(resp)
        if envelope is None or envelope.operation_id != request_id:
            LOGGER.warning("lease acquire missing/invalid fencing envelope")
            self._schedule_lease_retry(now, "missing_envelope")
            return False
        decision = self._fence.begin(self._fence_scope, envelope)
        _record_fence_decision(
            "gateway.lease_acquire",
            decision,
            envelope,
            site_id=self._site_id,
            scope=self._fence_scope,
        )
        if decision.stale:
            LOGGER.warning("lease acquire rejected stale epoch op=%s", envelope.operation_id)
            self._lease_acquire_request_id = None
            self._schedule_lease_retry(now, "stale_epoch")
            return False
        if decision.duplicate:
            self._lease_acquire_request_id = None
            return True
        self._lease_id = str(resp.get("lease_id") or "")
        self._lease_ttl_ms = int(resp.get("lease_ttl_ms") or 0)
        self._renew_after_ms = int(resp.get("renew_after_ms") or 0)
        self._next_renew_at = time.monotonic() + max(1.0, self._renew_after_ms / 1000.0)
        self._fence.commit(self._fence_scope, envelope)
        self._lease_acquire_request_id = None
        self._reset_lease_retry()
        LOGGER.info("lease acquired id=%s ttl_ms=%s", self._lease_id, self._lease_ttl_ms)
        return True

    def _maybe_renew(self, now: float) -> None:
        if self._nats_client is None:
            return
        if not self._lease_id:
            return
        if now < self._next_renew_at:
            return
        request_id = self._lease_renew_request_id or self._lease_request_id("lease.renew")
        self._lease_renew_request_id = request_id
        req = {
            "site_id": self._site_id,
            "node_id": self._node_id,
            "session_id": self._session_id,
            "lease_id": self._lease_id,
            "timestamp": time.time(),
            "request_id": request_id,
        }
        try:
            resp = self._nats_client.request_json(
                hub_lease_renew_subject(self._site_id),
                req,
                timeout_s=self._lease_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("lease renew failed: %s", exc)
            self._next_renew_at = now + 2.0
            return
        if not resp or not resp.get("accepted"):
            LOGGER.warning("lease renew rejected: %s", resp.get("reason"))
            self._lease_renew_request_id = None
            self._lease_id = None
            self._next_renew_at = now + 1.0
            self._schedule_lease_retry(now, str(resp.get("reason") or "rejected"))
            return
        envelope = parse_envelope(resp)
        if envelope is None or envelope.operation_id != request_id:
            LOGGER.warning("lease renew missing/invalid fencing envelope")
            self._next_renew_at = now + 1.0
            return
        decision = self._fence.begin(self._fence_scope, envelope)
        _record_fence_decision(
            "gateway.lease_renew",
            decision,
            envelope,
            site_id=self._site_id,
            scope=self._fence_scope,
        )
        if decision.stale:
            LOGGER.warning("lease renew rejected stale epoch op=%s", envelope.operation_id)
            self._lease_renew_request_id = None
            self._lease_id = None
            self._next_renew_at = now + 1.0
            self._schedule_lease_retry(now, "stale_epoch")
            return
        if decision.duplicate:
            self._lease_renew_request_id = None
            return
        self._lease_ttl_ms = int(resp.get("lease_ttl_ms") or self._lease_ttl_ms)
        self._renew_after_ms = int(resp.get("renew_after_ms") or self._renew_after_ms)
        self._next_renew_at = now + max(1.0, self._renew_after_ms / 1000.0)
        self._fence.commit(self._fence_scope, envelope)
        self._lease_renew_request_id = None

    def _maybe_acquire(self, now: float) -> None:
        if self._lease_id:
            return
        if now < self._lease_next_attempt_at:
            return
        self._acquire_lease(now)

    def _reset_lease_retry(self) -> None:
        self._lease_backoff_s = self._lease_retry_min_s
        self._lease_next_attempt_at = 0.0

    def _schedule_lease_retry(self, now: float, reason: str | None = None) -> None:
        import random

        self._stats.lease_retry_total += 1
        jitter = self._lease_backoff_s * self._lease_retry_jitter_pct
        if jitter:
            jitter = random.uniform(-jitter, jitter)
        delay = max(0.0, self._lease_backoff_s + jitter)
        self._lease_next_attempt_at = now + delay
        self._lease_backoff_s = min(self._lease_backoff_s * 2.0, self._lease_retry_max_s)
        if reason:
            LOGGER.warning("lease retry in %.1fs (reason=%s)", delay, reason)
        else:
            LOGGER.warning("lease retry in %.1fs", delay)

    def _lease_request_id(self, prefix: str) -> str:
        return f"{prefix}:{uuid.uuid4().hex}"

    def _resolve_result_envelope(
        self,
        payload: dict,
        work_key: str | None,
    ) -> MutationEnvelope | None:
        envelope = parse_envelope(payload)
        if envelope is not None:
            return envelope
        if work_key:
            cached = self._inflight_envelopes.get(work_key)
            if cached is not None:
                return cached
        work_id = str(payload.get("work_id") or "")
        attempt = int(payload.get("attempt") or 0)
        if not work_id or attempt <= 0 or not self._spool_enabled:
            return None
        try:
            record = self._spool.get_inflight_record(work_id, attempt)
        except Exception:
            record = None
        if record is None or not record.operation_id or not record.controller_id:
            return None
        epoch = int(record.controller_epoch or 0)
        if epoch <= 0:
            return None
        return MutationEnvelope(
            controller_id=record.controller_id,
            controller_epoch=epoch,
            operation_id=record.operation_id,
        )

    def _publish_telemetry(self, now: float) -> None:
        if self._nats_client is None:
            return
        if now - self._last_status_publish >= self._status_every_s:
            self._last_status_publish = now
            if _should_sample(self._status_sample_rate):
                result_replay_backlog = 0
                if self._spool_enabled:
                    try:
                        result_replay_backlog = self._spool.count_undelivered_results()
                    except Exception:
                        result_replay_backlog = 0
                status = {
                    "site_id": self._site_id,
                    "node_id": self._node_id,
                    "build": AE_BUILD_INFO(),
                    "inflight": self._stats.inflight,
                    "accepted": self._stats.accepted,
                    "completed": self._stats.completed,
                    "failed": self._stats.failed,
                    "metrics": {
                        "work_stale_total": self._stats.stale,
                        "work_nak_total": self._stats.nacked,
                        "lease_retry_total": self._stats.lease_retry_total,
                        "result_replay_total": self._stats.result_replay_total,
                        "result_replay_fail_total": self._stats.result_replay_fail_total,
                        "result_replay_backlog": result_replay_backlog,
                    },
                    "timestamp": time.time(),
                }
                try:
                    self._nats_client.publish_json(
                        hub_status_subject(self._site_id), status
                    )
                except Exception:
                    pass
        if now - self._last_logs_publish >= self._logs_every_s:
            self._last_logs_publish = now
            if _should_sample(self._logs_sample_rate):
                log = {
                    "site_id": self._site_id,
                    "node_id": self._node_id,
                    "level": "info",
                    "message": "gateway heartbeat",
                    "timestamp": time.time(),
                }
                try:
                    self._nats_client.publish_json(hub_logs_subject(self._site_id), log)
                except Exception:
                    pass

    def _on_transport_reconnect(self) -> None:
        if not self._spool_enabled:
            return
        try:
            self._spool.reset_replay_schedule()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("gateway reconnect replay reset failed: %s", exc)
            return
        LOGGER.info("gateway reconnect reset replay schedule site=%s", self._site_id)


def render_subjects(site_id: str, node_id: str) -> list[str]:
    gateway = SiteGateway(
        site_id=site_id,
        node_id=node_id,
        nats_url=None,
        js_config=GatewayJetStreamConfig.from_env({}),
        status_interval_s=30,
    )
    return gateway._subjects()


def _safe_json(payload: bytes) -> dict:
    try:
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _bundle_latency_seconds(payload: dict) -> float | None:
    raw = payload.get("generated_at")
    if not raw:
        return None
    try:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = datetime.fromisoformat(text)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (now - ts).total_seconds())
    except Exception:
        return None


def _gateway_fence_db_path(spool_path: Path) -> Path:
    raw = os.getenv("AE_GATEWAY_FENCE_DB")
    if raw:
        return Path(raw)
    return Path(spool_path).with_name("fence.db")


def _record_fence_decision(
    surface: str,
    decision,
    envelope: MutationEnvelope,
    *,
    site_id: str,
    scope: str,
) -> None:
    current = getattr(decision, "current", None)
    current_epoch = int(getattr(current, "controller_epoch", 0) or 0)
    if decision.stale:
        record_ha_fence_event(surface, stale=True)
        return
    if decision.duplicate:
        record_ha_fence_event(surface, duplicate=True)
        return
    if decision.accepted and int(envelope.controller_epoch) > current_epoch:
        record_ha_fence_event(surface, epoch_advanced=True)
        LOGGER.info(
            "ha fence epoch advanced surface=%s site=%s scope=%s from=%s to=%s controller=%s",
            surface,
            site_id,
            scope,
            current_epoch,
            envelope.controller_epoch,
            envelope.controller_id,
        )


def _work_key(payload: dict) -> str | None:
    work_id = payload.get("work_id")
    attempt = payload.get("attempt")
    if work_id is None or attempt is None:
        return None
    return f"{work_id}:{attempt}"


def _next_result_retry_delay(
    attempt_number: int,
    *,
    min_delay_s: float,
    max_delay_s: float,
) -> float:
    if attempt_number <= 1:
        return min_delay_s
    delay = min_delay_s * (2 ** (attempt_number - 1))
    return min(delay, max_delay_s)


def _truthy_env(name: str, default: str = "0") -> bool:
    raw = os.getenv(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_labels(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    labels: dict[str, str] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            labels[k.strip()] = v.strip()
        else:
            labels[part.strip()] = ""
    return labels


def _merge_node_labels(site_id: str | None, node_id: str | None) -> dict[str, str]:
    labels = _parse_labels(os.getenv("AE_NODE_LABELS"))
    role = (os.getenv("AE_NODE_ROLE") or "").strip()
    profile = (os.getenv("AE_NODE_PROFILE") or "").strip()
    if node_id:
        labels.setdefault("node_id", str(node_id))
    if role:
        labels.setdefault("role", role)
    if profile:
        labels.setdefault("profile", profile)
    labels.setdefault("role", "gateway")
    if site_id:
        labels.setdefault("site", site_id)
    return labels


def _parse_duration_seconds(value: str, default: float) -> float:
    if not value:
        return default
    raw = str(value).strip().lower()
    try:
        if raw.endswith("ms"):
            return float(raw[:-2]) / 1000.0
        if raw.endswith("s"):
            return float(raw[:-1])
        return float(raw)
    except Exception:
        return default


def _parse_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _should_sample(rate: float) -> bool:
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    import random

    return random.random() <= rate


__all__ = ["SiteGateway", "GatewayStats", "render_subjects"]
