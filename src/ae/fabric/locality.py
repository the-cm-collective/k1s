from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")


@dataclass(slots=True)
class FabricChunkRecord:
    chunk_id: str
    namespace: str
    name: str
    digest: str
    size_bytes: int
    source_kind: str
    source_ref: str
    labels: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class FabricResidencyRecord:
    chunk_id: str
    node_id: str
    storage_device_id: str
    path: str
    state: str
    integrity_state: str
    epoch: int
    digest: str
    verified_at: datetime | None
    updated_at: datetime


@dataclass(slots=True)
class FabricMovementRecord:
    movement_id: str
    chunk_id: str
    direction: str
    source_node_id: str
    target_node_id: str
    status: str
    requested_by: str
    digest: str
    epoch: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


@dataclass(slots=True)
class FabricAdvisoryRequestRecord:
    request_id: str
    subject_type: str
    subject_id: str
    intent: str
    facts_ref: str
    locality_snapshot_ref: str
    max_candidates: int
    time_budget_ms: int
    policy_mode: str
    created_at: datetime


@dataclass(slots=True)
class FabricAdvisoryResponseRecord:
    request_id: str
    provider: str
    status: str
    recommendation: str
    confidence: float | None
    evidence_refs: list[Any]
    authoritative: bool
    created_at: datetime


@dataclass(slots=True)
class FabricDecisionTraceRecord:
    trace_id: str
    request_id: str
    deterministic_baseline: dict[str, Any]
    advisory_response: dict[str, Any]
    accepted: bool | None
    divergence_reason: str | None
    replay_status: str
    continuity_signals: dict[str, Any]
    coherence_signals: dict[str, Any]
    created_at: datetime


@dataclass(slots=True)
class FabricTransferCapabilityRecord:
    capability_id: str
    node_id: str
    peer_node_id: str
    transport: str
    status: str
    priority: int
    capabilities: dict[str, Any]
    fallback_transport: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class FabricTransferLeaseRecord:
    lease_id: str
    chunk_id: str
    source_node_id: str
    target_node_id: str
    transport: str
    status: str
    holder: str
    landing_zone_id: str
    digest: str
    epoch: int
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class FabricLandingZoneRecord:
    zone_id: str
    node_id: str
    path: str
    capacity_bytes: int
    reserved_bytes: int
    safety_state: str
    cleanup_policy: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class FabricTransportAttemptRecord:
    attempt_id: str
    lease_id: str
    chunk_id: str
    transport: str
    status: str
    fallback_used: bool
    fallback_transport: str
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@dataclass(slots=True)
class FabricDasCellBundleRecord:
    bundle_id: str
    site_id: str
    cell_id: str
    version: str
    storage_ref: str
    facts_ref: str
    status: str
    labels: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class FabricDasQueryTraceRecord:
    trace_id: str
    bundle_id: str
    site_id: str
    query_id: str
    query_kind: str
    local_first: bool
    warmed_refs: list[Any]
    promoted_refs: list[Any]
    fallback_sites: list[Any]
    result_ref: str
    created_at: datetime


@dataclass(slots=True)
class FabricDasReplicationRecord:
    replication_id: str
    bundle_id: str
    source_site_id: str
    target_site_id: str
    mode: str
    status: str
    approved_by: str
    reason: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class FabricCognitiveSignalRecord:
    signal_id: str
    subject_type: str
    subject_id: str
    signal_kind: str
    continuity_ref: str
    coherence_score: float | None
    overload_state: str
    review_gate: str
    advisory_trace_id: str
    created_at: datetime


def normalize_chunk_id(value: str) -> str:
    raw = str(value or "").strip()
    if not _SHA256_RE.match(raw):
        raise ValueError("chunk_id must be a sha256 content address")
    digest = raw.split(":", 1)[1] if raw.lower().startswith("sha256:") else raw
    return f"sha256:{digest.lower()}"


def is_content_addressed_chunk_id(value: str) -> bool:
    try:
        normalize_chunk_id(value)
    except ValueError:
        return False
    return True


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: Any, *, default: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return default
    raw = str(value).strip()
    if not raw:
        return default
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return default


def chunk_record_payload(record: FabricChunkRecord) -> dict[str, Any]:
    return {
        "chunk_id": normalize_chunk_id(record.chunk_id),
        "namespace": record.namespace,
        "name": record.name,
        "digest": record.digest,
        "size_bytes": int(record.size_bytes),
        "source_kind": record.source_kind,
        "source_ref": record.source_ref,
        "labels": dict(record.labels),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def residency_record_payload(record: FabricResidencyRecord) -> dict[str, Any]:
    return {
        "chunk_id": normalize_chunk_id(record.chunk_id),
        "node_id": record.node_id,
        "storage_device_id": record.storage_device_id,
        "path": record.path,
        "state": record.state,
        "integrity_state": record.integrity_state,
        "epoch": int(record.epoch),
        "digest": record.digest,
        "verified_at": _iso_or_none(record.verified_at),
        "updated_at": _iso(record.updated_at),
    }


def movement_record_payload(record: FabricMovementRecord) -> dict[str, Any]:
    return {
        "movement_id": record.movement_id,
        "chunk_id": normalize_chunk_id(record.chunk_id),
        "direction": record.direction,
        "source_node_id": record.source_node_id,
        "target_node_id": record.target_node_id,
        "status": record.status,
        "requested_by": record.requested_by,
        "digest": record.digest,
        "epoch": int(record.epoch),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "started_at": _iso_or_none(record.started_at),
        "finished_at": _iso_or_none(record.finished_at),
        "error": record.error,
    }


def advisory_request_payload(record: FabricAdvisoryRequestRecord) -> dict[str, Any]:
    return {
        "request_id": record.request_id,
        "subject_type": record.subject_type,
        "subject_id": record.subject_id,
        "intent": record.intent,
        "facts_ref": record.facts_ref,
        "locality_snapshot_ref": record.locality_snapshot_ref,
        "max_candidates": int(record.max_candidates),
        "time_budget_ms": int(record.time_budget_ms),
        "policy_mode": record.policy_mode,
        "created_at": _iso(record.created_at),
    }


def advisory_response_payload(record: FabricAdvisoryResponseRecord) -> dict[str, Any]:
    return {
        "request_id": record.request_id,
        "provider": record.provider,
        "status": record.status,
        "recommendation": record.recommendation,
        "confidence": record.confidence,
        "evidence_refs": list(record.evidence_refs),
        "authoritative": bool(record.authoritative),
        "created_at": _iso(record.created_at),
    }


def decision_trace_payload(record: FabricDecisionTraceRecord) -> dict[str, Any]:
    return {
        "trace_id": record.trace_id,
        "request_id": record.request_id,
        "deterministic_baseline": dict(record.deterministic_baseline),
        "advisory_response": dict(record.advisory_response),
        "accepted": record.accepted,
        "divergence_reason": record.divergence_reason,
        "replay_status": record.replay_status,
        "continuity_signals": dict(record.continuity_signals),
        "coherence_signals": dict(record.coherence_signals),
        "created_at": _iso(record.created_at),
    }


def transfer_capability_payload(record: FabricTransferCapabilityRecord) -> dict[str, Any]:
    return {
        "capability_id": record.capability_id,
        "node_id": record.node_id,
        "peer_node_id": record.peer_node_id,
        "transport": record.transport,
        "status": record.status,
        "priority": int(record.priority),
        "capabilities": dict(record.capabilities),
        "fallback_transport": record.fallback_transport,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def transfer_lease_payload(record: FabricTransferLeaseRecord) -> dict[str, Any]:
    return {
        "lease_id": record.lease_id,
        "chunk_id": normalize_chunk_id(record.chunk_id),
        "source_node_id": record.source_node_id,
        "target_node_id": record.target_node_id,
        "transport": record.transport,
        "status": record.status,
        "holder": record.holder,
        "landing_zone_id": record.landing_zone_id,
        "digest": record.digest,
        "epoch": int(record.epoch),
        "expires_at": _iso_or_none(record.expires_at),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def landing_zone_payload(record: FabricLandingZoneRecord) -> dict[str, Any]:
    return {
        "zone_id": record.zone_id,
        "node_id": record.node_id,
        "path": record.path,
        "capacity_bytes": int(record.capacity_bytes),
        "reserved_bytes": int(record.reserved_bytes),
        "safety_state": record.safety_state,
        "cleanup_policy": record.cleanup_policy,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def transport_attempt_payload(record: FabricTransportAttemptRecord) -> dict[str, Any]:
    return {
        "attempt_id": record.attempt_id,
        "lease_id": record.lease_id,
        "chunk_id": normalize_chunk_id(record.chunk_id),
        "transport": record.transport,
        "status": record.status,
        "fallback_used": bool(record.fallback_used),
        "fallback_transport": record.fallback_transport,
        "error": record.error,
        "started_at": _iso_or_none(record.started_at),
        "finished_at": _iso_or_none(record.finished_at),
        "created_at": _iso(record.created_at),
    }


def das_cell_bundle_payload(record: FabricDasCellBundleRecord) -> dict[str, Any]:
    return {
        "bundle_id": record.bundle_id,
        "site_id": record.site_id,
        "cell_id": record.cell_id,
        "version": record.version,
        "storage_ref": record.storage_ref,
        "facts_ref": record.facts_ref,
        "status": record.status,
        "labels": dict(record.labels),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def das_query_trace_payload(record: FabricDasQueryTraceRecord) -> dict[str, Any]:
    return {
        "trace_id": record.trace_id,
        "bundle_id": record.bundle_id,
        "site_id": record.site_id,
        "query_id": record.query_id,
        "query_kind": record.query_kind,
        "local_first": bool(record.local_first),
        "warmed_refs": list(record.warmed_refs),
        "promoted_refs": list(record.promoted_refs),
        "fallback_sites": list(record.fallback_sites),
        "result_ref": record.result_ref,
        "created_at": _iso(record.created_at),
    }


def das_replication_payload(record: FabricDasReplicationRecord) -> dict[str, Any]:
    return {
        "replication_id": record.replication_id,
        "bundle_id": record.bundle_id,
        "source_site_id": record.source_site_id,
        "target_site_id": record.target_site_id,
        "mode": record.mode,
        "status": record.status,
        "approved_by": record.approved_by,
        "reason": record.reason,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def cognitive_signal_payload(record: FabricCognitiveSignalRecord) -> dict[str, Any]:
    return {
        "signal_id": record.signal_id,
        "subject_type": record.subject_type,
        "subject_id": record.subject_id,
        "signal_kind": record.signal_kind,
        "continuity_ref": record.continuity_ref,
        "coherence_score": record.coherence_score,
        "overload_state": record.overload_state,
        "review_gate": record.review_gate,
        "advisory_trace_id": record.advisory_trace_id,
        "created_at": _iso(record.created_at),
    }


def chunk_record_from_payload(payload: dict[str, Any]) -> FabricChunkRecord:
    now = utc_now()
    return FabricChunkRecord(
        chunk_id=normalize_chunk_id(str(payload.get("chunk_id") or payload.get("digest") or "")),
        namespace=str(payload.get("namespace") or "default"),
        name=str(payload.get("name") or ""),
        digest=str(payload.get("digest") or payload.get("chunk_id") or ""),
        size_bytes=max(0, _int(payload.get("size_bytes"))),
        source_kind=str(payload.get("source_kind") or ""),
        source_ref=str(payload.get("source_ref") or ""),
        labels=_dict(payload.get("labels")),
        created_at=parse_datetime(payload.get("created_at"), default=now) or now,
        updated_at=parse_datetime(payload.get("updated_at"), default=now) or now,
    )


def residency_record_from_payload(payload: dict[str, Any]) -> FabricResidencyRecord:
    now = utc_now()
    return FabricResidencyRecord(
        chunk_id=normalize_chunk_id(str(payload.get("chunk_id") or "")),
        node_id=str(payload.get("node_id") or ""),
        storage_device_id=str(payload.get("storage_device_id") or ""),
        path=str(payload.get("path") or ""),
        state=str(payload.get("state") or "unknown"),
        integrity_state=str(payload.get("integrity_state") or "unknown"),
        epoch=max(0, _int(payload.get("epoch"))),
        digest=str(payload.get("digest") or ""),
        verified_at=parse_datetime(payload.get("verified_at")),
        updated_at=parse_datetime(payload.get("updated_at"), default=now) or now,
    )


def movement_record_from_payload(payload: dict[str, Any]) -> FabricMovementRecord:
    now = utc_now()
    created = parse_datetime(payload.get("created_at"), default=now) or now
    return FabricMovementRecord(
        movement_id=str(payload.get("movement_id") or ""),
        chunk_id=normalize_chunk_id(str(payload.get("chunk_id") or "")),
        direction=str(payload.get("direction") or ""),
        source_node_id=str(payload.get("source_node_id") or ""),
        target_node_id=str(payload.get("target_node_id") or ""),
        status=str(payload.get("status") or "unknown"),
        requested_by=str(payload.get("requested_by") or ""),
        digest=str(payload.get("digest") or ""),
        epoch=max(0, _int(payload.get("epoch"))),
        created_at=created,
        updated_at=parse_datetime(payload.get("updated_at"), default=created) or created,
        started_at=parse_datetime(payload.get("started_at")),
        finished_at=parse_datetime(payload.get("finished_at")),
        error=str(payload.get("error")) if payload.get("error") is not None else None,
    )


def advisory_request_from_payload(payload: dict[str, Any]) -> FabricAdvisoryRequestRecord:
    now = utc_now()
    return FabricAdvisoryRequestRecord(
        request_id=str(payload.get("request_id") or ""),
        subject_type=str(payload.get("subject_type") or ""),
        subject_id=str(payload.get("subject_id") or ""),
        intent=str(payload.get("intent") or ""),
        facts_ref=str(payload.get("facts_ref") or ""),
        locality_snapshot_ref=str(payload.get("locality_snapshot_ref") or ""),
        max_candidates=max(0, _int(payload.get("max_candidates"))),
        time_budget_ms=max(0, _int(payload.get("time_budget_ms"))),
        policy_mode=str(payload.get("policy_mode") or "advisory"),
        created_at=parse_datetime(payload.get("created_at"), default=now) or now,
    )


def advisory_response_from_payload(payload: dict[str, Any]) -> FabricAdvisoryResponseRecord:
    now = utc_now()
    return FabricAdvisoryResponseRecord(
        request_id=str(payload.get("request_id") or ""),
        provider=str(payload.get("provider") or ""),
        status=str(payload.get("status") or ""),
        recommendation=str(payload.get("recommendation") or ""),
        confidence=_float_or_none(payload.get("confidence")),
        evidence_refs=_list(payload.get("evidence_refs")),
        authoritative=bool(payload.get("authoritative")),
        created_at=parse_datetime(payload.get("created_at"), default=now) or now,
    )


def decision_trace_from_payload(payload: dict[str, Any]) -> FabricDecisionTraceRecord:
    now = utc_now()
    accepted_raw = payload.get("accepted")
    accepted = accepted_raw if isinstance(accepted_raw, bool) else None
    return FabricDecisionTraceRecord(
        trace_id=str(payload.get("trace_id") or ""),
        request_id=str(payload.get("request_id") or ""),
        deterministic_baseline=_dict(payload.get("deterministic_baseline")),
        advisory_response=_dict(payload.get("advisory_response")),
        accepted=accepted,
        divergence_reason=(
            str(payload.get("divergence_reason"))
            if payload.get("divergence_reason") is not None
            else None
        ),
        replay_status=str(payload.get("replay_status") or ""),
        continuity_signals=_dict(payload.get("continuity_signals")),
        coherence_signals=_dict(payload.get("coherence_signals")),
        created_at=parse_datetime(payload.get("created_at"), default=now) or now,
    )


def transfer_capability_from_payload(payload: dict[str, Any]) -> FabricTransferCapabilityRecord:
    now = utc_now()
    created = parse_datetime(payload.get("created_at"), default=now) or now
    return FabricTransferCapabilityRecord(
        capability_id=str(payload.get("capability_id") or ""),
        node_id=str(payload.get("node_id") or ""),
        peer_node_id=str(payload.get("peer_node_id") or ""),
        transport=str(payload.get("transport") or ""),
        status=str(payload.get("status") or "unknown"),
        priority=_int(payload.get("priority")),
        capabilities=_dict(payload.get("capabilities")),
        fallback_transport=str(payload.get("fallback_transport") or ""),
        created_at=created,
        updated_at=parse_datetime(payload.get("updated_at"), default=created) or created,
    )


def transfer_lease_from_payload(payload: dict[str, Any]) -> FabricTransferLeaseRecord:
    now = utc_now()
    created = parse_datetime(payload.get("created_at"), default=now) or now
    return FabricTransferLeaseRecord(
        lease_id=str(payload.get("lease_id") or ""),
        chunk_id=normalize_chunk_id(str(payload.get("chunk_id") or "")),
        source_node_id=str(payload.get("source_node_id") or ""),
        target_node_id=str(payload.get("target_node_id") or ""),
        transport=str(payload.get("transport") or ""),
        status=str(payload.get("status") or "unknown"),
        holder=str(payload.get("holder") or ""),
        landing_zone_id=str(payload.get("landing_zone_id") or ""),
        digest=str(payload.get("digest") or ""),
        epoch=max(0, _int(payload.get("epoch"))),
        expires_at=parse_datetime(payload.get("expires_at")),
        created_at=created,
        updated_at=parse_datetime(payload.get("updated_at"), default=created) or created,
    )


def landing_zone_from_payload(payload: dict[str, Any]) -> FabricLandingZoneRecord:
    now = utc_now()
    created = parse_datetime(payload.get("created_at"), default=now) or now
    return FabricLandingZoneRecord(
        zone_id=str(payload.get("zone_id") or ""),
        node_id=str(payload.get("node_id") or ""),
        path=str(payload.get("path") or ""),
        capacity_bytes=max(0, _int(payload.get("capacity_bytes"))),
        reserved_bytes=max(0, _int(payload.get("reserved_bytes"))),
        safety_state=str(payload.get("safety_state") or "unknown"),
        cleanup_policy=str(payload.get("cleanup_policy") or ""),
        created_at=created,
        updated_at=parse_datetime(payload.get("updated_at"), default=created) or created,
    )


def transport_attempt_from_payload(payload: dict[str, Any]) -> FabricTransportAttemptRecord:
    now = utc_now()
    return FabricTransportAttemptRecord(
        attempt_id=str(payload.get("attempt_id") or ""),
        lease_id=str(payload.get("lease_id") or ""),
        chunk_id=normalize_chunk_id(str(payload.get("chunk_id") or "")),
        transport=str(payload.get("transport") or ""),
        status=str(payload.get("status") or "unknown"),
        fallback_used=bool(payload.get("fallback_used")),
        fallback_transport=str(payload.get("fallback_transport") or ""),
        error=str(payload.get("error")) if payload.get("error") is not None else None,
        started_at=parse_datetime(payload.get("started_at")),
        finished_at=parse_datetime(payload.get("finished_at")),
        created_at=parse_datetime(payload.get("created_at"), default=now) or now,
    )


def das_cell_bundle_from_payload(payload: dict[str, Any]) -> FabricDasCellBundleRecord:
    now = utc_now()
    created = parse_datetime(payload.get("created_at"), default=now) or now
    return FabricDasCellBundleRecord(
        bundle_id=str(payload.get("bundle_id") or ""),
        site_id=str(payload.get("site_id") or ""),
        cell_id=str(payload.get("cell_id") or ""),
        version=str(payload.get("version") or ""),
        storage_ref=str(payload.get("storage_ref") or ""),
        facts_ref=str(payload.get("facts_ref") or ""),
        status=str(payload.get("status") or "unknown"),
        labels=_dict(payload.get("labels")),
        created_at=created,
        updated_at=parse_datetime(payload.get("updated_at"), default=created) or created,
    )


def das_query_trace_from_payload(payload: dict[str, Any]) -> FabricDasQueryTraceRecord:
    now = utc_now()
    return FabricDasQueryTraceRecord(
        trace_id=str(payload.get("trace_id") or ""),
        bundle_id=str(payload.get("bundle_id") or ""),
        site_id=str(payload.get("site_id") or ""),
        query_id=str(payload.get("query_id") or ""),
        query_kind=str(payload.get("query_kind") or ""),
        local_first=bool(payload.get("local_first")),
        warmed_refs=_list(payload.get("warmed_refs")),
        promoted_refs=_list(payload.get("promoted_refs")),
        fallback_sites=_list(payload.get("fallback_sites")),
        result_ref=str(payload.get("result_ref") or ""),
        created_at=parse_datetime(payload.get("created_at"), default=now) or now,
    )


def das_replication_from_payload(payload: dict[str, Any]) -> FabricDasReplicationRecord:
    now = utc_now()
    created = parse_datetime(payload.get("created_at"), default=now) or now
    return FabricDasReplicationRecord(
        replication_id=str(payload.get("replication_id") or ""),
        bundle_id=str(payload.get("bundle_id") or ""),
        source_site_id=str(payload.get("source_site_id") or ""),
        target_site_id=str(payload.get("target_site_id") or ""),
        mode=str(payload.get("mode") or ""),
        status=str(payload.get("status") or "unknown"),
        approved_by=str(payload.get("approved_by") or ""),
        reason=str(payload.get("reason") or ""),
        created_at=created,
        updated_at=parse_datetime(payload.get("updated_at"), default=created) or created,
    )


def cognitive_signal_from_payload(payload: dict[str, Any]) -> FabricCognitiveSignalRecord:
    now = utc_now()
    return FabricCognitiveSignalRecord(
        signal_id=str(payload.get("signal_id") or ""),
        subject_type=str(payload.get("subject_type") or ""),
        subject_id=str(payload.get("subject_id") or ""),
        signal_kind=str(payload.get("signal_kind") or ""),
        continuity_ref=str(payload.get("continuity_ref") or ""),
        coherence_score=_float_or_none(payload.get("coherence_score")),
        overload_state=str(payload.get("overload_state") or ""),
        review_gate=str(payload.get("review_gate") or ""),
        advisory_trace_id=str(payload.get("advisory_trace_id") or ""),
        created_at=parse_datetime(payload.get("created_at"), default=now) or now,
    )


def _iso(value: datetime) -> str:
    return value.isoformat()


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
