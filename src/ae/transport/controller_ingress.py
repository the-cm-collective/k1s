"""NATS ingress for controller-side lease/result handling (Phase 2)."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ae.controller.state import SQLiteStateStore
from ae.transport.nats_client import NatsClient, NatsClientError, NatsMessage

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class LeaseResponse:
    accepted: bool
    controller_epoch: int
    lease_id: str | None
    lease_ttl_ms: int
    renew_after_ms: int
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "controller_epoch": self.controller_epoch,
            "lease_id": self.lease_id,
            "lease_ttl_ms": self.lease_ttl_ms,
            "renew_after_ms": self.renew_after_ms,
            "reason": self.reason,
        }


class NatsControllerIngress:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        url: str,
        creds: Path | None = None,
        controller_epoch: int | None = None,
        lease_ttl_ms: int | None = None,
        renew_after_ms: int | None = None,
    ) -> None:
        self._store = store
        self._client = NatsClient(url=url, creds=creds, name="k1s-controller-ingress")
        self._epoch = controller_epoch or int(os.getenv("AE_CONTROLLER_EPOCH", "1") or 1)
        self._lease_ttl_ms = lease_ttl_ms or int(os.getenv("AE_LEASE_TTL_MS", "60000") or 60000)
        self._renew_after_ms = renew_after_ms or int(
            os.getenv("AE_LEASE_RENEW_AFTER_MS", "20000") or 20000
        )
        self._subs: list[str] = []

    def start(self) -> None:
        try:
            self._client.connect()
        except NatsClientError as exc:
            LOGGER.warning("nats ingress connect failed: %s", exc)
            return
        self._subs.append(
            self._client.subscribe("k1s.v1.site.*.lease.acquire", self._on_lease_acquire)
        )
        self._subs.append(
            self._client.subscribe("k1s.v1.site.*.lease.renew", self._on_lease_renew)
        )
        self._subs.append(
            self._client.subscribe("k1s.v1.site.*.work.pull", self._on_work_pull)
        )
        self._subs.append(
            self._client.subscribe("k1s.v1.site.*.work.ack", self._on_work_ack)
        )
        self._subs.append(self._client.subscribe("k1s.v1.site.*.result", self._on_result))
        LOGGER.info("nats ingress started (%s subs)", len(self._subs))

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

    def _on_lease_acquire(self, msg: NatsMessage) -> None:
        payload = self._safe_json(msg)
        site_id = _site_id_from_subject(msg.subject) or str(payload.get("site_id") or "")
        node_id = str(payload.get("node_id") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if not site_id or not node_id:
            resp = LeaseResponse(
                accepted=False,
                controller_epoch=self._epoch,
                lease_id=None,
                lease_ttl_ms=self._lease_ttl_ms,
                renew_after_ms=self._renew_after_ms,
                reason="site_id or node_id missing",
            )
            self._reply(msg, resp.as_dict())
            return
        if payload.get("site_id") and str(payload.get("site_id")) != site_id:
            resp = LeaseResponse(
                accepted=False,
                controller_epoch=self._epoch,
                lease_id=None,
                lease_ttl_ms=self._lease_ttl_ms,
                renew_after_ms=self._renew_after_ms,
                reason="site_id mismatch",
            )
            self._reply(msg, resp.as_dict())
            return
        labels = payload.get("labels") or {}
        if isinstance(labels, dict):
            labels = {str(k): str(v) for k, v in labels.items()}
        else:
            labels = {}
        labels.setdefault("site", site_id)
        backend = str(payload.get("backend") or "nats")
        try:
            self._store.upsert_node(
                node_id,
                name=node_id,
                labels=labels,
                taints=[],
                backend=backend,
                endpoint=None,
                pod_cidr=None,
                wg_pubkey=None,
            )
            self._store.record_heartbeat(node_id, "Ready")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("lease acquire store update failed: %s", exc)
        lease_id = f"local:{site_id}:{node_id}:{session_id or int(time.time())}"
        resp = LeaseResponse(
            accepted=True,
            controller_epoch=self._epoch,
            lease_id=lease_id,
            lease_ttl_ms=self._lease_ttl_ms,
            renew_after_ms=self._renew_after_ms,
        )
        self._reply(msg, resp.as_dict())

    def _on_lease_renew(self, msg: NatsMessage) -> None:
        payload = self._safe_json(msg)
        site_id = _site_id_from_subject(msg.subject) or str(payload.get("site_id") or "")
        node_id = str(payload.get("node_id") or "").strip()
        if not site_id or not node_id:
            resp = LeaseResponse(
                accepted=False,
                controller_epoch=self._epoch,
                lease_id=None,
                lease_ttl_ms=self._lease_ttl_ms,
                renew_after_ms=self._renew_after_ms,
                reason="site_id or node_id missing",
            )
            self._reply(msg, resp.as_dict())
            return
        try:
            self._store.record_heartbeat(node_id, "Ready")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("lease renew heartbeat failed: %s", exc)
        resp = LeaseResponse(
            accepted=True,
            controller_epoch=self._epoch,
            lease_id=str(payload.get("lease_id") or ""),
            lease_ttl_ms=self._lease_ttl_ms,
            renew_after_ms=self._renew_after_ms,
        )
        self._reply(msg, resp.as_dict())

    def _on_result(self, msg: NatsMessage) -> None:
        payload = self._safe_json(msg)
        work_id = payload.get("work_id")
        status = payload.get("status")
        site_id = _site_id_from_subject(msg.subject) or payload.get("site_id")
        if work_id:
            LOGGER.info("work_result site=%s work_id=%s status=%s", site_id, work_id, status)
        else:
            LOGGER.info("site_result site=%s payload=%s", site_id, payload)

    def _on_work_pull(self, msg: NatsMessage) -> None:
        payload = self._safe_json(msg)
        site_id = _site_id_from_subject(msg.subject) or payload.get("site_id")
        if not msg.reply:
            return
        # TODO: wire to SoT-backed outbox for lab-edge work.pull.
        resp = {
            "accepted": True,
            "work": [],
            "lease_ids": [],
            "visibility_timeout_ms": int(payload.get("visibility_timeout_ms") or 0),
            "reason": None,
        }
        LOGGER.debug("work.pull site=%s returning empty batch", site_id)
        self._reply(msg, resp)

    def _on_work_ack(self, msg: NatsMessage) -> None:
        payload = self._safe_json(msg)
        site_id = _site_id_from_subject(msg.subject) or payload.get("site_id")
        lease_ids = payload.get("lease_ids") or []
        LOGGER.debug("work.ack site=%s leases=%s", site_id, lease_ids)
        self._reply(msg, {"accepted": True, "reason": None})

    def _reply(self, msg: NatsMessage, payload: dict) -> None:
        if not msg.reply:
            return
        try:
            self._client.publish_json(msg.reply, payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("nats reply failed: %s", exc)

    @staticmethod
    def _safe_json(msg: NatsMessage) -> dict:
        try:
            if not msg.data:
                return {}
            return json.loads(msg.data.decode("utf-8"))
        except Exception:
            return {}


def _site_id_from_subject(subject: str) -> str | None:
    parts = subject.split(".")
    if len(parts) < 4:
        return None
    if parts[0] != "k1s" or parts[1] != "v1" or parts[2] != "site":
        return None
    return parts[3]


__all__ = ["NatsControllerIngress"]
