"""Outbox publisher loop for JetStream dispatch (Phase 4)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from ae.controller.state import SQLiteStateStore
from ae.observability.http_api import record_outbox_publish
from ae.transport.nats_client import NatsClient, NatsClientError

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class OutboxPublisherConfig:
    interval_s: float
    batch_size: int


class OutboxPublisher:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        nats_url: str,
        nats_creds=None,
        config: OutboxPublisherConfig | None = None,
        authority=None,
    ) -> None:
        self._store = store
        self._client = NatsClient(url=nats_url, creds=nats_creds, name="k1s-outbox")
        self._config = config or OutboxPublisherConfig(interval_s=0.5, batch_size=100)
        self._authority = authority
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False
        self._stop = False

    def start(self) -> None:
        if self._started:
            return
        try:
            self._client.connect()
        except NatsClientError as exc:
            LOGGER.warning("outbox publisher connect failed: %s", exc)
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        try:
            self._client.close()
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("outbox publish loop error: %s", exc)
            time.sleep(self._config.interval_s)

    def run_once(self) -> None:
        if not self._is_leader():
            return
        entries = self._store.list_outbox_unpublished(limit=self._config.batch_size)
        if not entries:
            return
        for entry in entries:
            if not self._is_leader():
                return
            msg_id = str(
                getattr(entry, "publish_msg_id", "")
                or entry.payload.get("operation_id")
                or f"{entry.work_id}:{entry.attempt}"
            )
            headers = {"Nats-Msg-Id": msg_id}
            subject = str(
                getattr(entry, "publish_subject", "") or f"k1s.v1.work.site.{entry.site_id}"
            )
            try:
                self._client.publish_js_json(subject, entry.payload, headers=headers)
                if not self._is_leader():
                    LOGGER.warning(
                        "outbox authority lost after publish work_id=%s attempt=%s msg_id=%s",
                        entry.work_id,
                        entry.attempt,
                        msg_id,
                    )
                    return
                self._store.mark_outbox_published(entry.work_id, entry.attempt)
                self._store.update_work_state(
                    work_id=entry.work_id,
                    attempt=entry.attempt,
                    state="Dispatched",
                )
                record_outbox_publish(True)
                LOGGER.debug(
                    "published outbox work_id=%s attempt=%s msg_id=%s",
                    entry.work_id,
                    entry.attempt,
                    msg_id,
                )
            except Exception as exc:  # noqa: BLE001
                self._store.record_outbox_publish_attempt(
                    entry.work_id,
                    entry.attempt,
                    error=str(exc),
                )
                record_outbox_publish(False)
                LOGGER.debug(
                    "outbox publish failed work_id=%s attempt=%s msg_id=%s: %s",
                    entry.work_id,
                    entry.attempt,
                    msg_id,
                    exc,
                )

    def _is_leader(self) -> bool:
        if self._authority is None:
            return True
        try:
            return bool(self._authority.snapshot().is_leader)
        except Exception:
            return False


__all__ = ["OutboxPublisher", "OutboxPublisherConfig"]
