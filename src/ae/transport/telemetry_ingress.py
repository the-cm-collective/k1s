"""NATS ingress for site telemetry (status/logs/caps)."""

from __future__ import annotations

import json
import logging

from ae.transport.nats_client import NatsClient, NatsClientError, NatsMessage
from ae.observability.http_api import record_gateway_metrics, record_site_seen

LOGGER = logging.getLogger(__name__)


class TelemetryIngress:
    def __init__(self, *, url: str, creds=None) -> None:
        self._client = NatsClient(url=url, creds=creds, name="k1s-telemetry-ingress")
        self._subs: list[str] = []

    def start(self) -> None:
        try:
            self._client.connect()
        except NatsClientError as exc:
            LOGGER.warning("telemetry ingress connect failed: %s", exc)
            return
        self._subs.append(self._client.subscribe("k1s.v1.site.*.status", self._on_status))
        self._subs.append(self._client.subscribe("k1s.v1.site.*.logs", self._on_logs))
        self._subs.append(self._client.subscribe("k1s.v1.site.*.caps", self._on_caps))
        LOGGER.info("telemetry ingress started (%s subs)", len(self._subs))

    def close(self) -> None:
        for sid in self._subs:
            try:
                self._client.unsubscribe(sid)
            except Exception:
                pass
        self._subs = []
        try:
            self._client.close()
        except Exception:
            pass

    def _on_status(self, msg: NatsMessage) -> None:
        payload = _safe_json(msg.data)
        site_id = _site_id_from_subject(msg.subject)
        if site_id:
            record_site_seen(site_id)
            metrics = payload.get("metrics") if isinstance(payload, dict) else None
            if isinstance(metrics, dict):
                record_gateway_metrics(
                    site_id,
                    work_stale_total=metrics.get("work_stale_total"),
                    work_nak_total=metrics.get("work_nak_total"),
                )
            LOGGER.debug("site status %s: %s", site_id, payload)

    def _on_logs(self, msg: NatsMessage) -> None:
        payload = _safe_json(msg.data)
        site_id = _site_id_from_subject(msg.subject)
        if site_id:
            record_site_seen(site_id)
            LOGGER.debug("site log %s: %s", site_id, payload)

    def _on_caps(self, msg: NatsMessage) -> None:
        payload = _safe_json(msg.data)
        site_id = _site_id_from_subject(msg.subject)
        if site_id:
            record_site_seen(site_id)
            LOGGER.debug("site caps %s: %s", site_id, payload)


def _safe_json(payload: bytes) -> dict:
    try:
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _site_id_from_subject(subject: str) -> str | None:
    parts = subject.split(".")
    if len(parts) < 4:
        return None
    if parts[0] != "k1s" or parts[1] != "v1" or parts[2] != "site":
        return None
    return parts[3]


__all__ = ["TelemetryIngress"]
