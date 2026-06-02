from __future__ import annotations

from typing import Final

SYMBOLIC_RELATIONSHIP_PREDICATES: Final[tuple[str, ...]] = (
    "owns_service",
    "depends_on",
    "serves_model",
    "requires_resource",
    "produced_artifact",
    "supports_advisory",
)

SYMBOLIC_FACT_NAMESPACE: Final = "runtime"
SYMBOLIC_FACT_SOURCE_WORKERBEE_AI_FABRIC: Final = "workerbee.ai-fabric.runtime-facts/v1"
SYMBOLIC_ADVISORY_DECISION_API_VERSION: Final = (
    "workerbee.ai-fabric.advisory-decision/v1"
)
SYMBOLIC_ADVISORY_DECISION_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "subject",
    "intent",
    "recommended_action",
    "confidence",
    "evidence_refs",
    "risks",
    "blocked_conditions",
    "authoritative",
)
SYMBOLIC_ADVISOR_SCENARIO_EVAL_API_VERSION: Final = (
    "workerbee.ai-fabric.advisor-scenario-eval/v1"
)
SYMBOLIC_ADVISOR_SCENARIO_EVAL_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "api_version",
    "run_id",
    "scenario_count",
    "results",
    "ok",
)
SYMBOLIC_ADVISOR_SCENARIO_RESULT_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "kind",
    "status",
    "risks",
    "blocked_conditions",
    "checks",
    "ok",
)
