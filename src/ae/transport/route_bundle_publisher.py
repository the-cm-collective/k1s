"""Route bundle publisher for edge-local mode (stub)."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ae.controller.state import SQLiteStateStore
from ae.transport.nats_client import NatsClient, NatsClientError, NatsMessage
from ae.transport.subjects import hub_route_ack_subject, hub_route_bundle_subject

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RouteBundlePublisherConfig:
    interval_s: float = 5.0


@dataclass(slots=True)
class _BundleState:
    rev: int = 0
    hash: str = ""
    acked_rev: int = 0
    backoff_s: float = 1.0
    next_send_at: float = 0.0


class RouteBundlePublisher:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        nats_url: str,
        nats_creds=None,
        config: RouteBundlePublisherConfig | None = None,
    ) -> None:
        self._store = store
        self._client = NatsClient(url=nats_url, creds=nats_creds, name="k1s-route-bundle")
        self._config = config or RouteBundlePublisherConfig()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False
        self._stop = False
        self._state: dict[str, _BundleState] = {}

    def start(self) -> None:
        if self._started:
            return
        try:
            self._client.connect()
        except NatsClientError as exc:
            LOGGER.warning("route bundle connect failed: %s", exc)
            return
        self._client.subscribe("k1s.v1.site.*.routes.ack", self._on_ack)
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
                LOGGER.debug("route bundle loop error: %s", exc)
            time.sleep(self._config.interval_s)

    def run_once(self) -> None:
        site_ids = self._store.list_site_ids()
        now = time.monotonic()
        for site_id in site_ids:
            state = self._state.setdefault(site_id, _BundleState())
            bundle = _build_bundle(site_id, state.rev, state.hash)
            if bundle["hash"] != state.hash:
                state.rev += 1
                state.hash = bundle["hash"]
                state.backoff_s = 1.0
                state.next_send_at = 0.0
            if state.acked_rev >= state.rev:
                continue
            if now < state.next_send_at:
                continue
            self._publish(site_id, bundle)
            state.backoff_s = _next_backoff(state.backoff_s)
            state.next_send_at = now + state.backoff_s

    def _publish(self, site_id: str, bundle: dict) -> None:
        try:
            self._client.publish_json(hub_route_bundle_subject(site_id), bundle)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("route bundle publish failed site=%s: %s", site_id, exc)

    def _on_ack(self, msg: NatsMessage) -> None:
        payload = _safe_json(msg.data)
        site_id = _site_id_from_subject(msg.subject) or payload.get("site_id")
        if not site_id:
            return
        try:
            bundle_rev = int(payload.get("bundle_rev") or 0)
        except Exception:
            return
        ok = payload.get("ok", True)
        if not ok:
            return
        state = self._state.setdefault(site_id, _BundleState())
        if bundle_rev > state.acked_rev:
            state.acked_rev = bundle_rev
            state.backoff_s = 1.0
            state.next_send_at = 0.0


def _build_bundle(site_id: str, rev: int, prev_hash: str) -> dict:
    bundle = {
        "site_id": site_id,
        "bundle_rev": rev,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "routes": [],
        "policies": [],
    }
    payload = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    bundle["hash"] = f"sha256:{digest}"
    if prev_hash and bundle["hash"] == prev_hash:
        bundle["bundle_rev"] = rev
    return bundle


def _next_backoff(value: float) -> float:
    if value < 2.0:
        return 2.0
    if value < 5.0:
        return 5.0
    if value < 10.0:
        return 10.0
    if value < 30.0:
        return 30.0
    if value < 60.0:
        return 60.0
    return 120.0


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


__all__ = ["RouteBundlePublisher", "RouteBundlePublisherConfig"]
