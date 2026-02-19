"""JetStream monitoring poller for Phase 6 operability signals."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ae.observability.http_api import (
    record_js_consumer_stats,
    record_js_stream_stats,
)
from ae.transport.nats_client import NatsClient, NatsClientError

LOGGER = logging.getLogger(__name__)


def _read(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(obj, dict) and name in obj:
                return obj[name]
            if hasattr(obj, name):
                return getattr(obj, name)
        except Exception:
            continue
    return default


@dataclass(slots=True)
class JetStreamMonitorConfig:
    interval_s: float = 10.0
    stream_name: str = "K1S_WORK"
    consumer_prefix: str = "WORK_SITE_"
    site_ids: list[str] = field(default_factory=list)


class JetStreamMonitor:
    def __init__(
        self,
        *,
        nats_url: str,
        nats_creds=None,
        config: JetStreamMonitorConfig | None = None,
    ) -> None:
        self._client = NatsClient(url=nats_url, creds=nats_creds, name="k1s-js-monitor")
        self._config = config or JetStreamMonitorConfig()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False
        self._stop = False

    def start(self) -> None:
        if self._started:
            return
        try:
            self._client.connect()
        except NatsClientError as exc:
            LOGGER.warning("js monitor connect failed: %s", exc)
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
                LOGGER.debug("js monitor loop error: %s", exc)
            time.sleep(self._config.interval_s)

    def run_once(self) -> None:
        stream = self._config.stream_name
        try:
            info = self._client.stream_info(stream)
            state = _read(info, "state")
            config = _read(info, "config")
            bytes_used = int(_read(state, "bytes", default=0) or 0)
            messages = int(_read(state, "messages", "msgs", default=0) or 0)
            max_bytes = int(_read(config, "max_bytes", default=0) or 0)
            record_js_stream_stats(
                stream=stream,
                bytes_used=bytes_used,
                messages=messages,
                max_bytes=max_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("js monitor stream info failed: %s", exc)

        for site_id in self._config.site_ids:
            consumer = f"{self._config.consumer_prefix}{site_id}"
            try:
                info = self._client.consumer_info(stream, consumer)
                pending = int(_read(info, "num_pending", default=0) or 0)
                ack_pending = int(_read(info, "num_ack_pending", default=0) or 0)
                redelivered = int(_read(info, "num_redelivered", default=0) or 0)
                waiting = int(_read(info, "num_waiting", default=0) or 0)
                record_js_consumer_stats(
                    stream=stream,
                    consumer=consumer,
                    site_id=site_id,
                    pending=pending,
                    ack_pending=ack_pending,
                    redelivered=redelivered,
                    waiting=waiting,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("js monitor consumer info failed (%s): %s", consumer, exc)


__all__ = ["JetStreamMonitor", "JetStreamMonitorConfig"]
