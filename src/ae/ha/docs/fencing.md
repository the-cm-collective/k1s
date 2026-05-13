# Fencing

- Source: `ha/fencing.py`
- Last reviewed: 2026-05-13
- Size: 317 lines

## Purpose
Shared HA mutation fencing helpers.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ControllerMutationIdentity | 14 | public methods: envelope | public methods: envelope |
| MutationEnvelope | 27 | public methods: as_dict | public methods: as_dict |
| FenceScopeState | 47 | No class docstring. |  |
| FenceDecision | 55 | public methods: accepted, duplicate, stale | public methods: accepted, duplicate, stale |
| SQLiteFenceStore | 73 | Persist accepted controller epochs and operation IDs per executor scope. | public methods: init, current, begin, commit |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| merge_envelope | 40 | function | Entrypoint/helper without docstring. |
| parse_envelope | 220 | function | Entrypoint/helper without docstring. |
| resolve_controller_identity | 238 | function | Entrypoint/helper without docstring. |
| lease_operation | 273 | function | Entrypoint/helper without docstring. |
| work_operation | 277 | function | Entrypoint/helper without docstring. |
| route_operation | 281 | function | Entrypoint/helper without docstring. |
| ensure_operation | 285 | function | Entrypoint/helper without docstring. |
| gc_operation | 289 | function | Entrypoint/helper without docstring. |
| delete_operation | 293 | function | Entrypoint/helper without docstring. |
| fabric_ensure_operation | 297 | function | Entrypoint/helper without docstring. |
| fabric_teardown_operation | 301 | function | Entrypoint/helper without docstring. |
| _scope_state | 305 | function | Internal helper. |
| _now_iso | 316 | function | Internal helper. |

## Runtime And Data Flow
- Environment inputs: `AE_CONTROLLER_EPOCH`, `AE_CONTROLLER_ID`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_gateway_service_fencing.py`
- `tests/unit/test_ha_fencing.py`
- `tests/unit/test_node_server.py`
- `tests/unit/test_node_server_fabric.py`
