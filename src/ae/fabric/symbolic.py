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
