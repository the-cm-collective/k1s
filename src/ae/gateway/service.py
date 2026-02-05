"""Site Gateway skeleton (Phase 2)."""

from __future__ import annotations

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
    from ae.transport.nats_client import NatsClient

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
            if now - self._stats.last_report_at >= self._status_interval_s:
                self._stats.last_report_at = now
                LOGGER.info(
                    "gateway stats inflight=%s accepted=%s completed=%s failed=%s",
                    self._stats.inflight,
                    self._stats.accepted,
                    self._stats.completed,
                    self._stats.failed,
                )


def render_subjects(site_id: str, node_id: str) -> list[str]:
    gateway = SiteGateway(
        site_id=site_id,
        node_id=node_id,
        nats_url=None,
        js_config=GatewayJetStreamConfig.from_env({}),
        status_interval_s=30,
    )
    return gateway._subjects()


__all__ = ["SiteGateway", "GatewayStats", "render_subjects"]
