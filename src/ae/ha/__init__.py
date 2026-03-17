"""HA helpers shared across controller and executor surfaces."""

from .fencing import (
    ControllerMutationIdentity,
    delete_operation,
    FenceDecision,
    FenceScopeState,
    MutationEnvelope,
    SQLiteFenceStore,
    ensure_operation,
    fabric_ensure_operation,
    fabric_teardown_operation,
    gc_operation,
    lease_operation,
    merge_envelope,
    parse_envelope,
    resolve_controller_identity,
    route_operation,
    work_operation,
)

__all__ = [
    "ControllerMutationIdentity",
    "delete_operation",
    "FenceDecision",
    "FenceScopeState",
    "MutationEnvelope",
    "SQLiteFenceStore",
    "ensure_operation",
    "fabric_ensure_operation",
    "fabric_teardown_operation",
    "gc_operation",
    "lease_operation",
    "merge_envelope",
    "parse_envelope",
    "resolve_controller_identity",
    "route_operation",
    "work_operation",
]
