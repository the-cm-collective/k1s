from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal, TypedDict, cast

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


def phase_id(value: str) -> PhaseId:
    if value not in PHASE_REQUIREMENTS:
        known = ", ".join(PHASE_ORDER)
        raise ValueError(f"unknown fabric phase {value!r}; expected one of: {known}")
    return cast(PhaseId, value)
