from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal, TypedDict, cast

from ae.accelerators import (
    accelerator_inventory,
    has_identity_role_separation,
    link_metric_inventory,
    network_interface_inventory,
    normalize_capabilities,
    project_gpu_labels,
    rdma_device_inventory,
    storage_device_inventory,
)
from ae.fabric.locality import is_content_addressed_chunk_id

PhaseId = Literal["F0n-nvidia-dev", "F0", "F1", "F2", "F3", "F4", "F5"]
PhaseStatus = Literal["present", "missing"]

API_VERSION: Final = "k1s.fabric.phase-assurance/v1"
KIND: Final = "FabricPhaseAssuranceReport"

PHASE_ORDER: Final[tuple[PhaseId, ...]] = (
    "F0n-nvidia-dev",
    "F0",
    "F1",
    "F2",
    "F3",
    "F4",
    "F5",
)

PHASE_REQUIREMENTS: Final[dict[PhaseId, tuple[str, ...]]] = {
    "F0n-nvidia-dev": (
        "gpu_nodes_controller_visible",
        "single_node_cells_ready",
        "two_host_pp2_cell_ready",
        "restart_delete_teardown_repeatable",
        "standard_ethernet_evidence",
        "non_substitutive_for_d0",
    ),
    "F0": (
        "inference_cell_ready",
        "fabric_sessions_controller_visible",
        "member_status_controller_visible",
        "rollback_signal_controller_visible",
        "vm_gpu_validation_artifacts",
    ),
    "F1": (
        "typed_node_capabilities",
        "typed_accelerators",
        "typed_storage_media",
        "typed_link_topology",
        "typed_rnic_rdma",
        "identity_role_separation",
        "gpu_label_projection",
    ),
    "F2": (
        "content_addressed_chunks",
        "residency_state",
        "controlled_push_pull",
        "integrity_epoch_semantics",
    ),
    "F3": (
        "advisory_contract",
        "decision_traces",
        "divergence_logging",
        "replay_evaluation",
        "bounded_planning",
        "continuity_coherence_signals",
    ),
    "F4": (
        "capability_negotiation",
        "transfer_leases",
        "landing_zone_safety",
        "roce_development_path",
        "standard_transport_fallback",
    ),
    "F5": (
        "das_cell_bundles",
        "local_first_query_warming_promotion",
        "controlled_cross_site_replication",
        "cognitive_fabric_substrate",
    ),
}

PHASE_DEPENDENCIES: Final[dict[PhaseId, tuple[PhaseId, ...]]] = {
    "F0n-nvidia-dev": (),
    "F0": (),
    "F1": ("F0",),
    "F2": ("F1",),
    "F3": ("F1", "F2"),
    "F4": ("F2", "F3"),
    "F5": ("F2", "F3"),
}


class PhaseGateReport(TypedDict):
    ready: bool
    blocked_by: list[PhaseId]


class PhaseReport(TypedDict):
    phase: PhaseId
    status: PhaseStatus
    required: list[str]
    present: list[str]
    missing: list[str]
    evidence: dict[str, Any]
    gate: PhaseGateReport


class FabricPhaseAssuranceReport(TypedDict):
    api_version: str
    kind: str
    phase_order: list[PhaseId]
    phases: dict[PhaseId, PhaseReport]
    ready_phases: list[PhaseId]


def normalize_phase_evidence(data: Mapping[str, Any] | None) -> dict[PhaseId, dict[str, Any]]:
    """Normalize direct evidence maps and prior assurance reports into phase evidence."""

    if data is None:
        return {phase_id: {} for phase_id in PHASE_ORDER}

    source = data.get("phases")
    if isinstance(source, Mapping):
        return _normalize_phases_mapping(source)
    return _normalize_phases_mapping(data)


def assess_fabric_phases(
    evidence: Mapping[str, Any] | None = None,
) -> FabricPhaseAssuranceReport:
    normalized = normalize_phase_evidence(evidence)
    phases: dict[PhaseId, PhaseReport] = {}

    for phase_id in PHASE_ORDER:
        phase_evidence = normalized.get(phase_id, {})
        required = list(PHASE_REQUIREMENTS[phase_id])
        present = [key for key in required if _truthy(phase_evidence.get(key))]
        missing = [key for key in required if key not in present]
        status: PhaseStatus = "present" if not missing else "missing"
        phases[phase_id] = {
            "phase": phase_id,
            "status": status,
            "required": required,
            "present": present,
            "missing": missing,
            "evidence": dict(phase_evidence),
            "gate": {"ready": False, "blocked_by": []},
        }

    for phase_id in PHASE_ORDER:
        blocked_by = [
            dependency
            for dependency in PHASE_DEPENDENCIES[phase_id]
            if phases[dependency]["status"] != "present"
        ]
        phases[phase_id]["gate"] = {
            "ready": phases[phase_id]["status"] == "present" and not blocked_by,
            "blocked_by": blocked_by,
        }

    ready_phases = [phase_id for phase_id in PHASE_ORDER if phases[phase_id]["gate"]["ready"]]
    return {
        "api_version": API_VERSION,
        "kind": KIND,
        "phase_order": list(PHASE_ORDER),
        "phases": phases,
        "ready_phases": ready_phases,
    }


def phase_ready(report: FabricPhaseAssuranceReport, phase_id: PhaseId) -> bool:
    return bool(report["phases"][phase_id]["gate"]["ready"])


def phase_blockers(report: FabricPhaseAssuranceReport, phase_id: PhaseId) -> list[PhaseId]:
    return list(report["phases"][phase_id]["gate"]["blocked_by"])


def f1_evidence_from_nodes(nodes: Iterable[Any]) -> dict[str, Any]:
    """Build F1 phase evidence from controller node records or node-like dicts."""

    node_facts = [_node_fact(item) for item in nodes]
    capability_nodes = [fact for fact in node_facts if fact["capabilities"]]

    accelerator_count = sum(
        len(accelerator_inventory(fact["capabilities"])) for fact in node_facts
    )
    storage_count = sum(
        len(storage_device_inventory(fact["capabilities"])) for fact in node_facts
    )
    network_count = sum(
        len(network_interface_inventory(fact["capabilities"])) for fact in node_facts
    )
    link_count = sum(len(link_metric_inventory(fact["capabilities"])) for fact in node_facts)
    rdma_count = sum(len(rdma_device_inventory(fact["capabilities"])) for fact in node_facts)
    identity_nodes = [
        fact["node_id"]
        for fact in node_facts
        if has_identity_role_separation(fact["capabilities"])
    ]
    gpu_projection_nodes = [
        fact["node_id"] for fact in node_facts if _has_projected_gpu_labels(fact)
    ]

    return {
        "typed_node_capabilities": _detail(
            capability_nodes,
            node_ids=[fact["node_id"] for fact in capability_nodes],
        ),
        "typed_accelerators": _detail(accelerator_count, accelerator_count=accelerator_count),
        "typed_storage_media": _detail(storage_count, storage_device_count=storage_count),
        "typed_link_topology": _detail(
            network_count or link_count,
            network_interface_count=network_count,
            link_metric_count=link_count,
        ),
        "typed_rnic_rdma": _detail(rdma_count, rdma_device_count=rdma_count),
        "identity_role_separation": _detail(identity_nodes, node_ids=identity_nodes),
        "gpu_label_projection": _detail(gpu_projection_nodes, node_ids=gpu_projection_nodes),
    }


def f2_evidence_from_store(store: Any) -> dict[str, Any]:
    """Build F2 locality evidence from controller-owned chunk/residency/movement records."""

    chunks = _safe_list(store, "list_fabric_chunks")
    residencies = _safe_list(store, "list_fabric_residencies")
    movements = _safe_list(store, "list_fabric_movements")

    canonical_chunks = [
        record
        for record in chunks
        if is_content_addressed_chunk_id(_record_field(record, "chunk_id"))
    ]
    chunk_ids = {_record_field(record, "chunk_id") for record in canonical_chunks}
    valid_residencies = [
        record
        for record in residencies
        if _record_field(record, "chunk_id") in chunk_ids and _record_field(record, "node_id")
    ]
    controlled_movements = [
        record
        for record in movements
        if _record_field(record, "direction").lower() in {"push", "pull"}
        and _record_field(record, "chunk_id") in chunk_ids
        and _record_field(record, "source_node_id")
        and _record_field(record, "target_node_id")
        and _record_field(record, "status")
        and _record_field(record, "requested_by")
    ]
    invalid_integrity_records = [
        record
        for record in [*valid_residencies, *controlled_movements]
        if not _has_digest_epoch(record)
    ]
    integrity_ready = (
        bool(valid_residencies)
        and bool(controlled_movements)
        and not invalid_integrity_records
    )

    return {
        "content_addressed_chunks": _detail(
            canonical_chunks and len(canonical_chunks) == len(chunks),
            chunk_count=len(chunks),
            canonical_chunk_count=len(canonical_chunks),
            chunk_ids=sorted(chunk_ids),
        ),
        "residency_state": _detail(
            valid_residencies,
            residency_count=len(valid_residencies),
            node_ids=sorted({_record_field(record, "node_id") for record in valid_residencies}),
        ),
        "controlled_push_pull": _detail(
            controlled_movements,
            movement_count=len(controlled_movements),
            directions=sorted(
                {_record_field(record, "direction").lower() for record in controlled_movements}
            ),
        ),
        "integrity_epoch_semantics": _detail(
            integrity_ready,
            residency_count=len(valid_residencies),
            movement_count=len(controlled_movements),
            invalid_record_count=len(invalid_integrity_records),
        ),
    }


def f3_evidence_from_store(store: Any) -> dict[str, Any]:
    """Build F3 advisory evidence from advisory request/response/trace records."""

    requests = _safe_list(store, "list_fabric_advisory_requests")
    responses = _safe_list(store, "list_fabric_advisory_responses")
    traces = _safe_list(store, "list_fabric_decision_traces")

    request_ids = {_record_field(record, "request_id") for record in requests}
    advisory_responses = [
        record
        for record in responses
        if _record_field(record, "request_id") in request_ids
        and not bool(_record_field(record, "authoritative", False))
    ]
    traced_requests = [
        record for record in traces if _record_field(record, "request_id") in request_ids
    ]
    divergence_traces = [
        record
        for record in traced_requests
        if _record_field(record, "accepted", None) is not None
        or bool(_record_field(record, "divergence_reason"))
    ]
    replay_traces = [
        record for record in traced_requests if bool(_record_field(record, "replay_status"))
    ]
    bounded_requests = [
        record
        for record in requests
        if _record_int(record, "max_candidates") > 0 and _record_int(record, "time_budget_ms") > 0
    ]
    continuity_traces = [
        record
        for record in traced_requests
        if bool(_record_field(record, "continuity_signals"))
        and bool(_record_field(record, "coherence_signals"))
    ]

    return {
        "advisory_contract": _detail(
            advisory_responses,
            request_count=len(requests),
            advisory_response_count=len(advisory_responses),
            authoritative=False,
        ),
        "decision_traces": _detail(
            traced_requests,
            trace_count=len(traced_requests),
        ),
        "divergence_logging": _detail(
            divergence_traces,
            divergence_trace_count=len(divergence_traces),
        ),
        "replay_evaluation": _detail(
            replay_traces,
            replay_trace_count=len(replay_traces),
        ),
        "bounded_planning": _detail(
            bounded_requests,
            bounded_request_count=len(bounded_requests),
        ),
        "continuity_coherence_signals": _detail(
            continuity_traces,
            signal_trace_count=len(continuity_traces),
        ),
    }


def _normalize_phases_mapping(source: Mapping[str, Any]) -> dict[PhaseId, dict[str, Any]]:
    normalized: dict[PhaseId, dict[str, Any]] = {phase_id: {} for phase_id in PHASE_ORDER}
    for phase_id in PHASE_ORDER:
        value = source.get(phase_id)
        if not isinstance(value, Mapping):
            continue
        evidence = value.get("evidence")
        if isinstance(evidence, Mapping):
            normalized[phase_id] = dict(evidence)
            continue
        if isinstance(value.get("present"), list) or isinstance(value.get("missing"), list):
            phase_evidence: dict[str, Any] = {}
            for key in value.get("present", []):
                if isinstance(key, str):
                    phase_evidence[key] = True
            for key in value.get("missing", []):
                if isinstance(key, str):
                    phase_evidence.setdefault(key, False)
            normalized[phase_id] = phase_evidence
            continue
        normalized[phase_id] = dict(value)
    return normalized


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "complete",
            "ok",
            "present",
            "ready",
            "true",
            "yes",
        }
    return bool(value)


def _detail(value: object, **fields: Any) -> dict[str, Any] | bool:
    if not value:
        return False
    return dict(fields)


def _node_fact(item: Any) -> dict[str, Any]:
    node = item[0] if isinstance(item, tuple) and item else item
    if isinstance(node, Mapping):
        node_id = str(node.get("node_id") or node.get("id") or "")
        labels = node.get("labels") if isinstance(node.get("labels"), Mapping) else {}
        capabilities = normalize_capabilities(node.get("capabilities"))
    else:
        node_id = str(getattr(node, "node_id", "") or getattr(node, "id", "") or "")
        labels = getattr(node, "labels", {}) or {}
        if not isinstance(labels, Mapping):
            labels = {}
        capabilities = normalize_capabilities(getattr(node, "capabilities", {}) or {})
    return {
        "node_id": node_id,
        "labels": dict(labels),
        "capabilities": capabilities,
    }


def _has_projected_gpu_labels(fact: Mapping[str, Any]) -> bool:
    capabilities = fact.get("capabilities")
    labels = fact.get("labels")
    if not isinstance(labels, Mapping):
        return False
    projected = project_gpu_labels(capabilities)
    if not projected:
        return False
    return all(str(labels.get(key) or "") == str(value) for key, value in projected.items())


def _safe_list(store: Any, method_name: str) -> list[Any]:
    method = getattr(store, method_name, None)
    if not callable(method):
        return []
    try:
        result = method()
    except Exception:
        return []
    return result if isinstance(result, list) else list(result or [])


def _record_field(record: Any, field: str, default: Any = "") -> Any:
    if isinstance(record, Mapping):
        return record.get(field, default)
    return getattr(record, field, default)


def _record_int(record: Any, field: str) -> int:
    try:
        return int(_record_field(record, field, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _has_digest_epoch(record: Any) -> bool:
    digest = str(_record_field(record, "digest") or "").strip()
    return bool(digest) and _record_int(record, "epoch") >= 0


def phase_id(value: str) -> PhaseId:
    if value not in PHASE_REQUIREMENTS:
        known = ", ".join(PHASE_ORDER)
        raise ValueError(f"unknown fabric phase {value!r}; expected one of: {known}")
    return cast(PhaseId, value)
