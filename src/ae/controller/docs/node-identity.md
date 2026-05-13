# Node Identity

- Source: `controller/node_identity.py`
- Last reviewed: 2026-05-13
- Size: 12 lines

## Purpose
Provides functions scoped_node_id within Core control plane: manifest loading, reconcile loop, state stores, scheduling, HA authority, and workload controllers.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| scoped_node_id | 4 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- No obvious external side-effect surface in static review.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
