"""Route bundle publisher for edge-local mode (stub)."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ae.controller.state import SQLiteStateStore
from ae.controller.spec import app_key
from ae.ha.fencing import MutationEnvelope, merge_envelope, resolve_controller_identity, route_operation
from ae.ingress.edge_docs import normalize_policy_doc, normalize_route_doc
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
    operation_id: str | None = None
    controller_id: str | None = None
    controller_epoch: int = 0


class RouteBundlePublisher:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        nats_url: str,
        nats_creds=None,
        config: RouteBundlePublisherConfig | None = None,
        authority=None,
    ) -> None:
        self._store = store
        self._client = NatsClient(url=nats_url, creds=nats_creds, name="k1s-route-bundle")
        self._config = config or RouteBundlePublisherConfig()
        self._authority = authority
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
        if self._authority is not None and not self._authority.snapshot().is_leader:
            return
        site_ids = self._store.list_route_bundle_site_ids()
        LOGGER.debug("route bundle discovered sites=%s", site_ids)
        now = time.monotonic()
        for site_id in site_ids:
            if self._authority is not None and not self._authority.snapshot().is_leader:
                return
            state = self._state.setdefault(site_id, _BundleState())
            routes, policies, service_endpoints = _collect_bundle_payload(
                self._store, site_id
            )
            bundle_hash = _bundle_hash(site_id, routes, policies, service_endpoints)
            if bundle_hash != state.hash:
                state.rev += 1
                state.hash = bundle_hash
                state.backoff_s = 1.0
                state.next_send_at = 0.0
            if state.acked_rev >= state.rev:
                continue
            if now < state.next_send_at:
                continue
            identity = resolve_controller_identity(self._authority)
            operation_id = route_operation(site_id, state.rev, identity.controller_epoch)
            state.operation_id = operation_id
            state.controller_id = identity.controller_id
            state.controller_epoch = identity.controller_epoch
            bundle = _build_bundle(
                site_id,
                state.rev,
                state.hash,
                routes,
                policies,
                service_endpoints,
                controller_id=identity.controller_id,
                controller_epoch=identity.controller_epoch,
                operation_id=operation_id,
            )
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


def _build_bundle(
    site_id: str,
    rev: int,
    bundle_hash: str,
    routes: list[dict],
    policies: list[dict],
    service_endpoints: dict[str, list[dict[str, Any]]],
    *,
    controller_id: str,
    controller_epoch: int,
    operation_id: str,
) -> dict:
    bundle = {
        "site_id": site_id,
        "bundle_rev": rev,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "routes": routes,
        "policies": policies,
        "service_endpoints": service_endpoints,
    }
    bundle["hash"] = bundle_hash
    return merge_envelope(
        bundle,
        MutationEnvelope(
            controller_id=controller_id,
            controller_epoch=controller_epoch,
            operation_id=operation_id,
        ),
    )


def _collect_bundle_payload(
    store: SQLiteStateStore, site_id: str
) -> tuple[list[dict], list[dict], dict[str, list[dict[str, Any]]]]:
    routes: list[dict] = []
    policies: list[dict] = []
    policy_keys: set[tuple[str, str]] = set()
    service_map: dict[str, str] = {}
    for record in store.list_edge_ingress_routes_for_site(site_id):
        doc = normalize_route_doc(record)
        if not _route_is_edge_local(doc):
            continue
        routes.append(doc)
        for service_key, app_name in _route_service_refs(doc):
            service_map.setdefault(service_key, app_name)
        if record.policy_name:
            policy_ns = record.policy_namespace or record.namespace
            policy_keys.add((record.policy_name, policy_ns))
    for name, namespace in sorted(policy_keys):
        policy = store.get_edge_ingress_policy(name=name, namespace=namespace)
        if policy:
            policies.append(normalize_policy_doc(policy))
        else:
            LOGGER.debug(
                "route bundle missing policy name=%s namespace=%s", name, namespace
            )
    routes = _sorted_docs(routes)
    policies = _sorted_docs(policies)
    service_endpoints = _collect_service_endpoints(store, service_map)
    return routes, policies, service_endpoints


def _route_is_edge_local(doc: dict) -> bool:
    spec = doc.get("spec") or {}
    exposure = spec.get("exposure") or {}
    mode = str(exposure.get("mode") or "").strip().lower()
    return mode == "edge-local"


def _sorted_docs(docs: list[dict]) -> list[dict]:
    return sorted(
        docs,
        key=lambda d: json.dumps(d, sort_keys=True, separators=(",", ":")),
    )


def _bundle_hash(
    site_id: str,
    routes: list[dict],
    policies: list[dict],
    service_endpoints: dict[str, list[dict[str, Any]]],
) -> str:
    payload = json.dumps(
        {
            "site_id": site_id,
            "routes": routes,
            "policies": policies,
            "service_endpoints": service_endpoints,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"sha256:{digest}"


def _route_service_refs(doc: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    route_ns = str(meta.get("namespace") or "default").strip() or "default"
    paths = spec.get("paths") if isinstance(spec.get("paths"), list) else []
    if not paths:
        service_ref = spec.get("serviceRef") if isinstance(spec.get("serviceRef"), dict) else {}
        pair = _service_ref_pair(service_ref, route_ns)
        if pair:
            out.append(pair)
        return out
    for entry in paths:
        if not isinstance(entry, dict):
            continue
        service_ref = (
            entry.get("serviceRef") if isinstance(entry.get("serviceRef"), dict) else {}
        )
        pair = _service_ref_pair(service_ref, route_ns)
        if pair:
            out.append(pair)
    return out


def _service_ref_pair(service_ref: dict, route_ns: str) -> tuple[str, str] | None:
    name = str(service_ref.get("name") or "").strip()
    if not name:
        return None
    namespace = str(service_ref.get("namespace") or route_ns).strip() or route_ns
    service_key = f"{namespace}/{name}"
    return service_key, app_key(name, namespace)


def _collect_service_endpoints(
    store: SQLiteStateStore, service_map: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for service_key, app_name in sorted(service_map.items()):
        try:
            endpoints = store.list_service_endpoints(app_name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug(
                "route bundle failed to read service endpoints service=%s app=%s: %s",
                service_key,
                app_name,
                exc,
            )
            endpoints = []

        dedup: set[tuple[str, int, int, bool]] = set()
        rows: list[dict[str, Any]] = []
        for ep in endpoints:
            ip = str(getattr(ep, "ip", "") or "").strip()
            target_port = _coerce_int(getattr(ep, "target_port", None))
            service_port = _coerce_int(getattr(ep, "port", None))
            ready = bool(getattr(ep, "ready", False))
            if not ip or target_port is None or service_port is None or not ready:
                continue
            row = (ip, service_port, target_port, ready)
            if row in dedup:
                continue
            dedup.add(row)
            rows.append(
                {
                    "ip": ip,
                    "service_port": service_port,
                    "target_port": target_port,
                    "ready": ready,
                }
            )
        rows.sort(
            key=lambda item: (
                int(item["service_port"]),
                int(item["target_port"]),
                str(item["ip"]),
            )
        )
        out[service_key] = rows
    return out


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


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
