"""Site Gateway skeleton (Phase 2)."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from ae.config.transport import GatewayJetStreamConfig, check_nats_connectivity
from ae.transport import (
    hub_caps_subject,
    hub_lease_acquire_subject,
    hub_lease_renew_subject,
    hub_logs_subject,
    hub_result_subject,
    hub_status_subject,
    hub_work_ack_subject,
    hub_work_pull_subject,
    local_caps_subject,
    local_logs_subject,
    local_result_subject,
    local_status_subject,
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
        self._inflight: dict[str, JetStreamMessage] = {}
        self._inflight_progress: dict[str, float] = {}
        self._completed: dict[str, float] = {}
        self._js_enabled = False
        self._last_pull_at = 0.0
        self._pull_interval_s = 1.0
        self._progress_interval_s = _parse_duration_seconds(
            js_config.progress_interval, default=10.0
        )
        self._ack_wait_s = _parse_duration_seconds(js_config.ack_wait, default=30.0)

    def _subjects(self) -> list[str]:
        return [
            local_work_subject(self._node_id),
            local_result_subject(),
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

    def _log_connectivity(self) -> None:
        if not self._nats_url:
            LOGGER.warning("AE_NATS_URL not set; skipping nats connectivity check")
            return
        ok, detail = check_nats_connectivity(self._nats_url)
        if ok:
            LOGGER.info("nats connectivity ok (%s)", detail)
        else:
            LOGGER.warning("nats connectivity failed (%s)", detail)

    def start(self, *, once: bool = False) -> None:
        self._log_config()
        self._log_connectivity()
        if self._nats_client is not None:
            try:
                self._nats_client.connect()
                LOGGER.info("nats client connected")
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("nats client connect failed: %s", exc)
            else:
                self._js_enabled = (
                    os.getenv("AE_TRANSPORT_BACKEND", "http").lower() == "nats-js"
                )
                try:
                    self._subscribe_local_results()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("failed to subscribe local results: %s", exc)
        self._log_subjects()
        if once:
            if self._nats_client is not None:
                try:
                    self._nats_client.close()
                except Exception:
                    pass
            return
        LOGGER.info("gateway skeleton running; no transport backend wired yet")
        self._stats.last_report_at = time.monotonic()
        while True:
            time.sleep(1)
            now = time.monotonic()
            self._run_progress(now)
            if self._nats_client is not None:
                if self._js_enabled:
                    self._poll_js(now)
                else:
                    self._poll_work_pull(now)
            if now - self._stats.last_report_at >= self._status_interval_s:
                self._stats.last_report_at = now
                LOGGER.info(
                    "gateway stats inflight=%s accepted=%s completed=%s failed=%s",
                    self._stats.inflight,
                    self._stats.accepted,
                    self._stats.completed,
                    self._stats.failed,
                )

    def _subscribe_local_results(self) -> None:
        if self._nats_client is None:
            return
        self._nats_client.subscribe(local_result_subject(), self._on_local_result)

    def _on_local_result(self, msg) -> None:  # type: ignore[override]
        payload = _safe_json(msg.data)
        if not isinstance(payload, dict):
            return
        payload.setdefault("site_id", self._site_id)
        payload.setdefault("node_id", self._node_id)
        try:
            if self._nats_client is not None:
                self._nats_client.publish_json(hub_result_subject(self._site_id), payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("publish work_result failed: %s", exc)
        work_key = _work_key(payload)
        if work_key and work_key in self._inflight:
            msg_js = self._inflight.pop(work_key)
            self._inflight_progress.pop(work_key, None)
            self._completed[work_key] = time.monotonic()
            try:
                msg_js.ack_sync()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("js ack_sync failed for %s: %s", work_key, exc)
        if payload.get("status") in {"failed", "Failed"}:
            self._stats.failed += 1
        else:
            self._stats.completed += 1

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
        for item in work_items:
            if not isinstance(item, dict):
                continue
            self._dispatch_work(item)
        if lease_ids:
            ack_req = {
                "site_id": self._site_id,
                "gateway_id": self._node_id,
                "lease_ids": lease_ids,
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
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("js fetch failed: %s", exc)
            return
        for js_msg in msgs:
            payload = _safe_json(js_msg.data)
            if not isinstance(payload, dict):
                continue
            work_key = _work_key(payload)
            if work_key and work_key in self._inflight:
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
            self._dispatch_work(payload)
            if work_key:
                self._inflight[work_key] = js_msg
                self._inflight_progress[work_key] = now
                self._stats.inflight = len(self._inflight)
                self._stats.accepted += 1

    def _dispatch_work(self, payload: dict) -> None:
        if self._nats_client is None:
            return
        node_id = payload.get("preferred_node") or payload.get("node_id") or self._node_id
        try:
            self._nats_client.publish_json(local_work_subject(str(node_id)), payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("local work dispatch failed: %s", exc)

    def _run_progress(self, now: float) -> None:
        if not self._inflight:
            return
        for work_key, js_msg in list(self._inflight.items()):
            last = self._inflight_progress.get(work_key, 0.0)
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


def _work_key(payload: dict) -> str | None:
    work_id = payload.get("work_id")
    attempt = payload.get("attempt")
    if work_id is None or attempt is None:
        return None
    return f"{work_id}:{attempt}"


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


__all__ = ["SiteGateway", "GatewayStats", "render_subjects"]
