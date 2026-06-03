from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

AI_RUNTIME_PROFILE_API_VERSION: Final = "k1s.fabric.ai-runtime-profile/v1"
AI_RUNTIME_PROFILE_KIND: Final = "AIFabricRuntimeProfile"
AI_RUNTIME_PROFILE_ADMISSION_API_VERSION: Final = (
    "k1s.fabric.ai-runtime-profile-admission/v1"
)
AI_RUNTIME_PROFILE_ADMISSION_KIND: Final = "AIFabricRuntimeProfileAdmissionReport"
AI_RUNTIME_PROFILE_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "api_version",
    "kind",
    "run_id",
    "track",
    "model_lanes",
    "context_budget_tokens",
    "adapter_hotset",
    "observed_vram_growth_mib",
    "evidence",
    "authoritative",
    "controller_authority",
)
AI_RUNTIME_PROFILE_REQUIRED_LANES: Final[tuple[str, ...]] = ("coordinator", "expert")


def validate_ai_runtime_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []

    for field in AI_RUNTIME_PROFILE_REQUIRED_FIELDS:
        if field not in profile:
            findings.append(_finding("error", "AI_RUNTIME_PROFILE_FIELD", field))

    if profile.get("api_version") != AI_RUNTIME_PROFILE_API_VERSION:
        findings.append(_finding("error", "AI_RUNTIME_PROFILE_API_VERSION", "api_version"))
    if profile.get("kind") != AI_RUNTIME_PROFILE_KIND:
        findings.append(_finding("error", "AI_RUNTIME_PROFILE_KIND", "kind"))
    if not isinstance(profile.get("run_id"), str) or not str(profile.get("run_id")).strip():
        findings.append(_finding("error", "AI_RUNTIME_PROFILE_RUN_ID", "run_id"))
    if profile.get("authoritative") is not False:
        findings.append(_finding("error", "AI_RUNTIME_PROFILE_AUTHORITY", "authoritative"))
    if profile.get("controller_authority") != "k1s":
        findings.append(
            _finding("error", "AI_RUNTIME_PROFILE_CONTROLLER_AUTHORITY", "controller_authority")
        )

    lanes = profile.get("model_lanes")
    if not isinstance(lanes, Mapping):
        findings.append(_finding("error", "AI_RUNTIME_PROFILE_MODEL_LANES", "model_lanes"))
    else:
        findings.extend(_validate_lanes(lanes))

    context_budget = profile.get("context_budget_tokens")
    if not isinstance(context_budget, Mapping):
        findings.append(
            _finding("error", "AI_RUNTIME_PROFILE_CONTEXT_BUDGET", "context_budget_tokens")
        )
    else:
        for lane in AI_RUNTIME_PROFILE_REQUIRED_LANES:
            value = context_budget.get(lane)
            if not isinstance(value, int) or value <= 0:
                findings.append(
                    _finding("error", "AI_RUNTIME_PROFILE_CONTEXT_BUDGET", lane)
                )

    if not isinstance(profile.get("adapter_hotset"), list):
        findings.append(_finding("error", "AI_RUNTIME_PROFILE_ADAPTER_HOTSET", "adapter_hotset"))

    growth = profile.get("observed_vram_growth_mib")
    if growth is not None and not isinstance(growth, int):
        findings.append(_finding("error", "AI_RUNTIME_PROFILE_VRAM_GROWTH", "observed_vram_growth_mib"))

    evidence = profile.get("evidence")
    if not isinstance(evidence, Mapping):
        findings.append(_finding("error", "AI_RUNTIME_PROFILE_EVIDENCE", "evidence"))
    else:
        findings.extend(_validate_evidence(evidence))

    return {"ok": not [item for item in findings if item["level"] == "error"], "findings": findings}


def evaluate_ai_runtime_profile_admission(
    profile: Mapping[str, Any], *, workerbee_status: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Evaluate a WorkerBee AI runtime profile as k1s dry-run admission evidence."""

    validation = validate_ai_runtime_profile(profile)
    findings = [dict(item) for item in validation.get("findings", [])]
    findings.extend(_promotion_findings(profile, workerbee_status=workerbee_status))
    errors = [item for item in findings if item.get("level") == "error"]
    admitted = not errors
    return {
        "api_version": AI_RUNTIME_PROFILE_ADMISSION_API_VERSION,
        "kind": AI_RUNTIME_PROFILE_ADMISSION_KIND,
        "mode": "dry-run",
        "ok": admitted,
        "admitted": admitted,
        "authoritative": False,
        "controller_authority": "k1s",
        "profile_ref": {
            "api_version": str(profile.get("api_version") or ""),
            "kind": str(profile.get("kind") or ""),
            "run_id": str(profile.get("run_id") or ""),
            "track": str(profile.get("track") or ""),
        },
        "requirements": _profile_requirements(profile),
        "evidence": _profile_evidence(profile, workerbee_status=workerbee_status),
        "findings": findings,
    }


def _validate_lanes(lanes: Mapping[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for lane in AI_RUNTIME_PROFILE_REQUIRED_LANES:
        lane_profile = lanes.get(lane)
        if not isinstance(lane_profile, Mapping):
            findings.append(_finding("error", "AI_RUNTIME_PROFILE_LANE", lane))
            continue
        for field in ("model", "revision", "served_model_name", "context_budget_tokens"):
            value = lane_profile.get(field)
            if field == "context_budget_tokens":
                if not isinstance(value, int) or value <= 0:
                    findings.append(_finding("error", "AI_RUNTIME_PROFILE_LANE_FIELD", f"{lane}.{field}"))
                continue
            if not isinstance(value, str) or not value.strip():
                findings.append(_finding("error", "AI_RUNTIME_PROFILE_LANE_FIELD", f"{lane}.{field}"))
    return findings


def _validate_evidence(evidence: Mapping[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for field in (
        "runtime_validation_ref",
        "workerbee_status_ref",
        "f5_evidence_ref",
        "advisor_scenarios_ref",
        "advisory_trace_refs",
        "das_fact_count",
        "retrieval_corpus_count",
    ):
        if field not in evidence:
            findings.append(_finding("error", "AI_RUNTIME_PROFILE_EVIDENCE_FIELD", field))
    if not isinstance(evidence.get("advisory_trace_refs"), list):
        findings.append(
            _finding("error", "AI_RUNTIME_PROFILE_EVIDENCE_FIELD", "advisory_trace_refs")
        )
    corpus = evidence.get("retrieval_corpus_count")
    if not isinstance(corpus, Mapping):
        findings.append(
            _finding("error", "AI_RUNTIME_PROFILE_EVIDENCE_FIELD", "retrieval_corpus_count")
        )
    return findings


def _profile_requirements(profile: Mapping[str, Any]) -> dict[str, Any]:
    lanes = profile.get("model_lanes")
    lane_requirements: dict[str, Any] = {}
    if isinstance(lanes, Mapping):
        for lane in AI_RUNTIME_PROFILE_REQUIRED_LANES:
            lane_profile = lanes.get(lane)
            if not isinstance(lane_profile, Mapping):
                continue
            lane_requirements[lane] = {
                "model": lane_profile.get("model"),
                "revision": lane_profile.get("revision"),
                "served_model_name": lane_profile.get("served_model_name"),
                "context_budget_tokens": lane_profile.get("context_budget_tokens"),
                "quantization": lane_profile.get("quantization"),
                "gpu_memory_utilization": lane_profile.get("gpu_memory_utilization"),
                "enable_lora": lane_profile.get("enable_lora"),
            }
    context_budget = profile.get("context_budget_tokens")
    return {
        "required_lanes": list(AI_RUNTIME_PROFILE_REQUIRED_LANES),
        "lanes": lane_requirements,
        "context_budget_tokens": dict(context_budget) if isinstance(context_budget, Mapping) else {},
        "adapter_hotset": list(profile.get("adapter_hotset") or [])
        if isinstance(profile.get("adapter_hotset"), list)
        else [],
        "observed_vram_growth_mib": profile.get("observed_vram_growth_mib"),
    }


def _profile_evidence(
    profile: Mapping[str, Any], *, workerbee_status: Mapping[str, Any] | None
) -> dict[str, Any]:
    evidence = profile.get("evidence")
    if not isinstance(evidence, Mapping):
        evidence = {}
    traces = evidence.get("advisory_trace_refs")
    return {
        "runtime_validation_ref": evidence.get("runtime_validation_ref"),
        "workerbee_status_ref": evidence.get("workerbee_status_ref"),
        "workerbee_status_supplied": workerbee_status is not None,
        "workerbee_status_ok": _workerbee_status_ok(workerbee_status),
        "f5_evidence_ref": evidence.get("f5_evidence_ref"),
        "advisor_scenarios_ref": evidence.get("advisor_scenarios_ref"),
        "advisory_trace_count": len(traces) if isinstance(traces, list) else 0,
        "das_fact_count": evidence.get("das_fact_count"),
        "das_f5_evidence_count": evidence.get("das_f5_evidence_count"),
        "retrieval_corpus_count": evidence.get("retrieval_corpus_count"),
    }


def _promotion_findings(
    profile: Mapping[str, Any], *, workerbee_status: Mapping[str, Any] | None
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if str(profile.get("track") or "") == "lora-adapter-smoke":
        findings.append(
            _finding(
                "warning",
                "AI_RUNTIME_PROFILE_ADAPTER_SMOKE_ONLY",
                "LoRA adapter evidence is runtime-smoke-only and not a quality claim",
            )
        )
    adapter_hotset = profile.get("adapter_hotset")
    if isinstance(adapter_hotset, list):
        for adapter in adapter_hotset:
            if isinstance(adapter, Mapping) and adapter.get("claim_scope") == "runtime-smoke-only":
                findings.append(
                    _finding(
                        "warning",
                        "AI_RUNTIME_PROFILE_ADAPTER_SCOPE",
                        "adapter_hotset includes runtime-smoke-only claim scope",
                    )
                )
                break
    if str(profile.get("track") or "") not in {"baseline", "quality"}:
        findings.append(
            _finding(
                "warning",
                "AI_RUNTIME_PROFILE_SOAK_EVIDENCE",
                "baseline or quality soak evidence is not present in this runtime profile",
            )
        )
    if workerbee_status is None:
        findings.append(
            _finding(
                "warning",
                "AI_RUNTIME_PROFILE_WORKERBEE_STATUS_UNVERIFIED",
                "WorkerBee project status was not supplied for promotion evidence",
            )
        )
    elif _workerbee_status_ok(workerbee_status) is not True:
        findings.append(
            _finding(
                "warning",
                "AI_RUNTIME_PROFILE_WORKERBEE_STATUS",
                "WorkerBee project status is missing, placeholder-like, or not ok",
            )
        )
    return findings


def _workerbee_status_ok(workerbee_status: Mapping[str, Any] | None) -> bool | None:
    if workerbee_status is None:
        return None
    if workerbee_status.get("ok") is True:
        return True
    if workerbee_status.get("ok") is False:
        return False
    project_status = workerbee_status.get("project_status")
    if isinstance(project_status, Mapping):
        return _workerbee_project_status_ok(project_status)
    data = workerbee_status.get("data")
    if isinstance(data, Mapping):
        return _workerbee_project_status_ok(data)
    return workerbee_status.get("ok") if workerbee_status.get("ok") is None else False


def _workerbee_project_status_ok(status: Mapping[str, Any]) -> bool:
    app_status = status.get("app_status")
    if not isinstance(app_status, Mapping):
        app_status = {}
    degraded = app_status.get("degraded_workload_count")
    return bool(
        status.get("running")
        and app_status.get("ready")
        and (not isinstance(degraded, int) or degraded == 0)
    )


def _finding(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}
