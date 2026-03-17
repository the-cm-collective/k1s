"""NATS ingress for controller-side lease/result handling (Phase 2)."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
import threading

from ae.controller.node_identity import scoped_node_id
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
        js_provision: bool = False,
        edge_renderer=None,
        authority=None,
    ) -> None:
        self._store = store
        self._client = NatsClient(url=url, creds=creds, name="k1s-controller-ingress")
        self._epoch = controller_epoch or int(os.getenv("AE_CONTROLLER_EPOCH", "1") or 1)
        self._lease_ttl_ms = lease_ttl_ms or int(os.getenv("AE_LEASE_TTL_MS", "60000") or 60000)
        self._renew_after_ms = renew_after_ms or int(
            os.getenv("AE_LEASE_RENEW_AFTER_MS", "20000") or 20000
        )
        self._js_provision = js_provision
        self._js_stream_name = os.getenv("AE_JS_STREAM_NAME", "K1S_WORK")
        self._js_work_subject = os.getenv("AE_JS_WORK_SUBJECT", "k1s.v1.work.site.>")
        self._js_storage = os.getenv("AE_JS_STORAGE", "file")
        self._js_ack_wait_s = _parse_duration_seconds(
            os.getenv("AE_GATEWAY_JS_ACK_WAIT"), default=30.0
        )
        self._js_max_ack_pending = int(os.getenv("AE_GATEWAY_JS_MAX_ACK_PENDING", "32") or 32)
        self._js_max_deliver = int(os.getenv("AE_GATEWAY_JS_MAX_DELIVER", "20") or 20)
        self._js_max_waiting = int(os.getenv("AE_GATEWAY_JS_MAX_WAITING", "512") or 512)
        self._js_sites: set[str] = set()
        self._js_provisioning: set[str] = set()
        self._js_lock = threading.Lock()
        self._js_stream_ready = False
        self._core_proxy_enabled = str(
            os.getenv("AE_EDGE_INGRESS_CORE_PROXY", "0") or "0"
        ).lower() in {"1", "true", "yes", "on"}
        self._core_proxy_port_min = int(os.getenv("AE_CORE_PROXY_PORT_MIN", "18080") or 18080)
        self._core_proxy_port_max = int(os.getenv("AE_CORE_PROXY_PORT_MAX", "18999") or 18999)
        self._edge_renderer = edge_renderer
        self._authority = authority
        self._authority_poll_s = max(
            0.1,
            float(os.getenv("AE_CONTROLLER_INGRESS_AUTHORITY_POLL_SECONDS", "0.5") or 0.5),
        )
        self._authority_stop = threading.Event()
        self._authority_thread: threading.Thread | None = None
        self._subs: list[str] = []

    def start(self) -> None:
        try:
            self._client.connect()
        except NatsClientError as exc:
            LOGGER.warning("nats ingress connect failed: %s", exc)
            return
        self.sync_authority()
        if self._authority is not None:
            self._authority_thread = threading.Thread(target=self._watch_authority, daemon=True)
            self._authority_thread.start()
        LOGGER.info("nats ingress started (%s subs)", len(self._subs))

    def close(self) -> None:
        self._authority_stop.set()
        if self._authority_thread is not None:
            try:
                self._authority_thread.join(timeout=1.0)
            except Exception:
                pass
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

    def sync_authority(self) -> None:
        if self._authority is None:
            if not self._subs:
                self._subscribe_mutating()
            return
        snapshot = self._authority.snapshot()
        if snapshot.leader_info is not None and snapshot.leader_info.controller_epoch:
            self._epoch = int(snapshot.leader_info.controller_epoch)
        if snapshot.is_leader:
            if not self._subs:
                self._subscribe_mutating()
        elif self._subs:
            self._unsubscribe_mutating()

    def _watch_authority(self) -> None:
        while not self._authority_stop.wait(self._authority_poll_s):
            try:
                self.sync_authority()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("nats ingress authority sync failed: %s", exc)

    def _subscribe_mutating(self) -> None:
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

    def _unsubscribe_mutating(self) -> None:
        for sid in self._subs:
            try:
                self._client.unsubscribe(sid)
            except Exception:
                pass
        self._subs = []

    def _is_leader(self) -> bool:
        if self._authority is None:
            return True
        snapshot = self._authority.snapshot()
        if snapshot.leader_info is not None and snapshot.leader_info.controller_epoch:
            self._epoch = int(snapshot.leader_info.controller_epoch)
        return bool(snapshot.is_leader)

    def _leader_hint(self) -> dict[str, object]:
        if self._authority is None:
            return {}
        snapshot = self._authority.snapshot()
        info = snapshot.leader_info
        if info is None:
            return {}
        payload: dict[str, object] = {
            "controller_id": info.controller_id,
            "controller_epoch": info.controller_epoch,
        }
        if info.advertise_addr:
            payload["advertise_addr"] = info.advertise_addr
        return payload

    def _on_lease_acquire(self, msg: NatsMessage) -> None:
        if not self._is_leader():
            self._reply(
                msg,
                {
                    "accepted": False,
                    "reason": "not_leader",
                    "controller_epoch": self._epoch,
                    **self._leader_hint(),
                },
            )
            return
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
            node_key = scoped_node_id(site_id, node_id)
            lease = self._store.acquire_lease(
                site_id=site_id,
                node_id=node_key,
                session_id=session_id or str(int(time.time())),
                lease_ttl_ms=self._lease_ttl_ms,
                renew_after_ms=self._renew_after_ms,
                controller_epoch=self._epoch,
            )
            self._store.upsert_node(
                node_key,
                name=node_id,
                labels=labels,
                taints=[],
                backend=backend,
                endpoint=None,
                pod_cidr=None,
                wg_pubkey=None,
            )
            self._store.record_heartbeat(node_key, "Ready")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("lease acquire store update failed: %s", exc)
            resp = LeaseResponse(
                accepted=False,
                controller_epoch=self._epoch,
                lease_id=None,
                lease_ttl_ms=self._lease_ttl_ms,
                renew_after_ms=self._renew_after_ms,
                reason=str(exc),
            )
            self._reply(msg, resp.as_dict())
            return
        resp = LeaseResponse(
            accepted=True,
            controller_epoch=self._epoch,
            lease_id=lease.lease_id,
            lease_ttl_ms=lease.lease_ttl_ms,
            renew_after_ms=lease.renew_after_ms,
        )
        self._ensure_js_consumer(site_id)
        self._ensure_core_proxy_port(site_id)
        self._reply(msg, resp.as_dict())

    def _on_lease_renew(self, msg: NatsMessage) -> None:
        if not self._is_leader():
            self._reply(
                msg,
                {
                    "accepted": False,
                    "reason": "not_leader",
                    "controller_epoch": self._epoch,
                    **self._leader_hint(),
                },
            )
            return
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
        lease_id = str(payload.get("lease_id") or "").strip()
        if not lease_id:
            resp = LeaseResponse(
                accepted=False,
                controller_epoch=self._epoch,
                lease_id=None,
                lease_ttl_ms=self._lease_ttl_ms,
                renew_after_ms=self._renew_after_ms,
                reason="lease_id required",
            )
            self._reply(msg, resp.as_dict())
            return
        node_key = scoped_node_id(site_id, node_id)
        lease, reason = self._store.renew_lease(
            node_key,
            session_id=str(payload.get("session_id") or ""),
            lease_id=lease_id,
        )
        if lease is None:
            resp = LeaseResponse(
                accepted=False,
                controller_epoch=self._epoch,
                lease_id=lease_id,
                lease_ttl_ms=self._lease_ttl_ms,
                renew_after_ms=self._renew_after_ms,
                reason=reason,
            )
            self._reply(msg, resp.as_dict())
            return
        try:
            self._store.record_heartbeat(node_key, "Ready")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("lease renew heartbeat failed: %s", exc)
        resp = LeaseResponse(
            accepted=True,
            controller_epoch=self._epoch,
            lease_id=lease.lease_id,
            lease_ttl_ms=lease.lease_ttl_ms,
            renew_after_ms=lease.renew_after_ms,
        )
        # Retry JS consumer provisioning on renew in case initial attempt failed.
        self._ensure_js_consumer(site_id)
        self._reply(msg, resp.as_dict())

    def _on_result(self, msg: NatsMessage) -> None:
        if not self._is_leader():
            return
        payload = self._safe_json(msg)
        work_id = payload.get("work_id")
        status = payload.get("status")
        site_id = _site_id_from_subject(msg.subject) or payload.get("site_id")
        if work_id:
            LOGGER.info("work_result site=%s work_id=%s status=%s", site_id, work_id, status)
            try:
                attempt = int(payload.get("attempt") or 0)
                if attempt:
                    status_norm = str(status or "").lower()
                    node_id = payload.get("node_id")
                    observed_generation = payload.get("observed_generation")
                    node_key = (
                        scoped_node_id(str(site_id), str(node_id))
                        if site_id and node_id
                        else (str(node_id) if node_id else None)
                    )
                    if status_norm in {"running"}:
                        self._store.update_work_state(
                            work_id=str(work_id),
                            attempt=attempt,
                            state="Running",
                            assigned_node_id=node_key,
                            observed_generation=(
                                int(observed_generation)
                                if observed_generation is not None
                                else None
                            ),
                            result=payload,
                        )
                    elif status_norm in {"succeeded", "success", "completed"}:
                        self._store.update_work_state(
                            work_id=str(work_id),
                            attempt=attempt,
                            state="Succeeded",
                            assigned_node_id=node_key,
                            observed_generation=(
                                int(observed_generation)
                                if observed_generation is not None
                                else None
                            ),
                            result=payload,
                        )
                        self._store.mark_work_done(str(work_id), attempt)
                    elif status_norm in {"failed", "error"}:
                        self._store.update_work_state(
                            work_id=str(work_id),
                            attempt=attempt,
                            state="Failed",
                            assigned_node_id=node_key,
                            observed_generation=(
                                int(observed_generation)
                                if observed_generation is not None
                                else None
                            ),
                            result=payload,
                        )
                        self._store.mark_work_done(str(work_id), attempt)
            except Exception:
                pass
        else:
            LOGGER.info("site_result site=%s payload=%s", site_id, payload)

    def _on_work_pull(self, msg: NatsMessage) -> None:
        if not self._is_leader():
            self._reply(
                msg,
                {
                    "accepted": False,
                    "reason": "not_leader",
                    "controller_epoch": self._epoch,
                    **self._leader_hint(),
                },
            )
            return
        payload = self._safe_json(msg)
        site_id = _site_id_from_subject(msg.subject) or payload.get("site_id")
        if not msg.reply:
            return
        if not site_id:
            self._reply(msg, {"accepted": False, "reason": "site_id required"})
            return
        try:
            limit = int(payload.get("limit") or 1)
        except Exception:
            limit = 1
        try:
            visibility_timeout_ms = int(
                payload.get("visibility_timeout_ms") or self._lease_ttl_ms
            )
        except Exception:
            visibility_timeout_ms = self._lease_ttl_ms
        try:
            leases = self._store.pull_work(site_id, limit, visibility_timeout_ms)
            resp = {
                "accepted": True,
                "work": [lease.payload for lease in leases],
                "lease_ids": [lease.lease_id for lease in leases],
                "visibility_timeout_ms": visibility_timeout_ms,
                "reason": None,
            }
            self._reply(msg, resp)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("work.pull failed: %s", exc)
            self._reply(msg, {"accepted": False, "reason": str(exc)})

    def _on_work_ack(self, msg: NatsMessage) -> None:
        if not self._is_leader():
            self._reply(
                msg,
                {
                    "accepted": False,
                    "reason": "not_leader",
                    "controller_epoch": self._epoch,
                    **self._leader_hint(),
                },
            )
            return
        payload = self._safe_json(msg)
        site_id = _site_id_from_subject(msg.subject) or payload.get("site_id")
        lease_ids = payload.get("lease_ids") or []
        LOGGER.debug("work.ack site=%s leases=%s", site_id, lease_ids)
        try:
            updated = self._store.ack_work([str(x) for x in lease_ids if x])
        except Exception:
            updated = 0
        self._reply(msg, {"accepted": True, "reason": None, "acked": updated})

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

    def _ensure_js_consumer(self, site_id: str) -> None:
        if not self._js_provision:
            return
        if not site_id or site_id in self._js_sites:
            return
        with self._js_lock:
            if site_id in self._js_provisioning or site_id in self._js_sites:
                return
            self._js_provisioning.add(site_id)

        def _provision() -> None:
            try:
                if not self._js_stream_ready:
                    self._client.ensure_stream(
                        name=self._js_stream_name,
                        subjects=[self._js_work_subject],
                        storage=self._js_storage,
                        retention="workqueue",
                    )
                    self._js_stream_ready = True
                self._client.ensure_consumer(
                    stream=self._js_stream_name,
                    durable=f"WORK_SITE_{site_id}",
                    filter_subject=f"k1s.v1.work.site.{site_id}",
                    ack_wait_s=self._js_ack_wait_s,
                    max_ack_pending=self._js_max_ack_pending,
                    max_deliver=self._js_max_deliver,
                    max_waiting=self._js_max_waiting,
                )
                self._js_sites.add(site_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "js consumer ensure failed site=%s (%s): %r",
                    site_id,
                    type(exc).__name__,
                    exc,
                )
            finally:
                with self._js_lock:
                    self._js_provisioning.discard(site_id)

        threading.Thread(target=_provision, daemon=True).start()

    def _ensure_core_proxy_port(self, site_id: str) -> None:
        if not self._core_proxy_enabled:
            return
        try:
            port = self._store.ensure_site_ingress_port(
                site_id,
                port_min=self._core_proxy_port_min,
                port_max=self._core_proxy_port_max,
            )
            LOGGER.info("site core-proxy port assigned site=%s port=%s", site_id, port)
            if self._edge_renderer is not None:
                try:
                    self._edge_renderer.render()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("edge ingress render failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("core-proxy port allocation failed site=%s: %s", site_id, exc)


def _parse_duration_seconds(value: str | None, default: float) -> float:
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


def _site_id_from_subject(subject: str) -> str | None:
    parts = subject.split(".")
    if len(parts) < 4:
        return None
    if parts[0] != "k1s" or parts[1] != "v1" or parts[2] != "site":
        return None
    return parts[3]


__all__ = ["NatsControllerIngress"]
