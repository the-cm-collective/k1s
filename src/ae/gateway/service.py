"""Site Gateway skeleton (Phase 2)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING
from ae.config.transport import GatewayJetStreamConfig, check_nats_connectivity
from ae.gateway.spool import GatewaySpool
from ae.ingress.edge_local import build_edge_local_renderer
from ae.observability.http_api import record_route_bundle_apply
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
        self._js_stream = os.getenv("AE_JS_STREAM_NAME", "K1S_WORK")
        self._spool = GatewaySpool(self._js_config.spool_path)
        self._spool_enabled = True
        self._keep_spool = _truthy_env("AE_GATEWAY_KEEP_SPOOL")
        self._inflight: dict[str, JetStreamMessage] = {}
        self._inflight_progress: dict[str, float] = {}
        self._inflight_heartbeat: dict[str, float] = {}
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
        self._last_result_retry = 0.0
        self._result_retry_interval_s = max(
            2.0,
            float(os.getenv("AE_GATEWAY_RESULT_RETRY_INTERVAL", "5") or 5),
        )
        self._route_bundle_rev = 0
        self._route_bundle_hash: str | None = None
        self._edge_local_renderer = build_edge_local_renderer()

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
        try:
            self._spool.init()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("spool init failed: %s", exc)
            self._spool_enabled = False
        if self._nats_client is not None:
            try:
                self._nats_client.connect()
                LOGGER.info("nats client connected")
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("nats client connect failed: %s", exc)
            else:
                self._js_enabled = self._backend == "nats-js"
                try:
                    self._subscribe_local_results()
                    self._subscribe_local_progress()
                    self._subscribe_route_bundles()
                    self._acquire_lease()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("failed to subscribe local results: %s", exc)
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
                self._run_progress(now)
                self._maybe_renew(now)
                if self._nats_client is not None:
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

    def _on_local_result(self, msg) -> None:  # type: ignore[override]
        payload = _safe_json(msg.data)
        if not isinstance(payload, dict):
            return
        payload.setdefault("site_id", self._site_id)
        payload.setdefault("node_id", self._node_id)
        work_key = _work_key(payload)
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
        site_id = payload.get("site_id") or self._site_id
        if str(site_id) != str(self._site_id):
            return
        bundle_rev = int(payload.get("bundle_rev") or 0)
        bundle_hash = payload.get("hash")
        ok = True
        error = None
        if bundle_rev < self._route_bundle_rev:
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
        ack = {
            "site_id": self._site_id,
            "bundle_rev": bundle_rev,
            "hash": bundle_hash,
            "applied_at": time.time(),
            "ok": ok,
            "error": error,
        }
        try:
            if self._nats_client is not None:
                self._nats_client.publish_json(hub_route_ack_subject(self._site_id), ack)
        except Exception:
            pass

    def _replay_spool_results(self, now: float) -> None:
        if not self._spool_enabled or self._nats_client is None:
            return
        if now - self._last_result_retry < self._result_retry_interval_s:
            return
        self._last_result_retry = now
        for record in self._spool.list_undelivered_results(limit=100):
            try:
                self._nats_client.publish_json(
                    hub_result_subject(self._site_id), record.payload
                )
                self._spool.mark_result_delivered(record.work_id, record.attempt)
            except Exception:
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
        accepted_leases: list[str] = []
        for idx, item in enumerate(work_items):
            if not isinstance(item, dict):
                continue
            if self._dispatch_work(item):
                if idx < len(lease_ids) and lease_ids[idx]:
                    accepted_leases.append(str(lease_ids[idx]))
        if accepted_leases:
            ack_req = {
                "site_id": self._site_id,
                "gateway_id": self._node_id,
                "lease_ids": accepted_leases,
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
                    )
                except Exception:
                    pass
            if not self._dispatch_work(payload):
                try:
                    js_msg.nak(self._nak_delay_s)
                    self._stats.nacked += 1
                except Exception:
                    pass
                continue
            if work_key:
                self._inflight[work_key] = js_msg
                self._inflight_progress[work_key] = now
                self._inflight_heartbeat[work_key] = now
                self._stats.inflight = len(self._inflight)
                self._stats.accepted += 1

    def _dispatch_work(self, payload: dict) -> bool:
        if self._nats_client is None:
            return False
        node_id = payload.get("preferred_node") or payload.get("node_id") or self._node_id
        try:
            self._nats_client.publish_json(local_work_subject(str(node_id)), payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("local work dispatch failed: %s", exc)
            return False
        self._emit_running(payload, str(node_id))
        return True

    def _emit_running(self, payload: dict, node_id: str) -> None:
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
        running = {
            "work_id": payload.get("work_id"),
            "attempt": payload.get("attempt"),
            "site_id": self._site_id,
            "node_id": node_id,
            "status": "running",
            "observed_generation": payload.get("desired_generation"),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
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

    def _acquire_lease(self) -> None:
        if self._nats_client is None:
            return
        req = {
            "site_id": self._site_id,
            "node_id": self._node_id,
            "session_id": self._session_id,
            "timestamp": time.time(),
        }
        try:
            resp = self._nats_client.request_json(
                hub_lease_acquire_subject(self._site_id),
                req,
                timeout_s=self._lease_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("lease acquire failed: %s", exc)
            return
        if not resp or not resp.get("accepted"):
            LOGGER.warning("lease acquire rejected: %s", resp.get("reason"))
            return
        self._lease_id = str(resp.get("lease_id") or "")
        self._lease_ttl_ms = int(resp.get("lease_ttl_ms") or 0)
        self._renew_after_ms = int(resp.get("renew_after_ms") or 0)
        self._next_renew_at = time.monotonic() + max(1.0, self._renew_after_ms / 1000.0)
        LOGGER.info("lease acquired id=%s ttl_ms=%s", self._lease_id, self._lease_ttl_ms)

    def _maybe_renew(self, now: float) -> None:
        if self._nats_client is None:
            return
        if not self._lease_id:
            return
        if now < self._next_renew_at:
            return
        req = {
            "site_id": self._site_id,
            "node_id": self._node_id,
            "session_id": self._session_id,
            "lease_id": self._lease_id,
            "timestamp": time.time(),
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
            self._lease_id = None
            self._next_renew_at = now + 1.0
            self._acquire_lease()
            return
        self._lease_ttl_ms = int(resp.get("lease_ttl_ms") or self._lease_ttl_ms)
        self._renew_after_ms = int(resp.get("renew_after_ms") or self._renew_after_ms)
        self._next_renew_at = now + max(1.0, self._renew_after_ms / 1000.0)

    def _publish_telemetry(self, now: float) -> None:
        if self._nats_client is None:
            return
        if now - self._last_status_publish >= self._status_every_s:
            self._last_status_publish = now
            if _should_sample(self._status_sample_rate):
                status = {
                    "site_id": self._site_id,
                    "node_id": self._node_id,
                    "inflight": self._stats.inflight,
                    "accepted": self._stats.accepted,
                    "completed": self._stats.completed,
                    "failed": self._stats.failed,
                    "metrics": {
                        "work_stale_total": self._stats.stale,
                        "work_nak_total": self._stats.nacked,
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


def _work_key(payload: dict) -> str | None:
    work_id = payload.get("work_id")
    attempt = payload.get("attempt")
    if work_id is None or attempt is None:
        return None
    return f"{work_id}:{attempt}"


def _truthy_env(name: str, default: str = "0") -> bool:
    raw = os.getenv(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


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
