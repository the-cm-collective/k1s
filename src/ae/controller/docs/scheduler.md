# Scheduler

- Source: `controller/scheduler.py`
- Last reviewed: 2026-05-13
- Size: 572 lines

## Purpose
Pod placement planner for multi-node scheduling.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| Placement | 37 | Pod placement target. | public methods: replica_ids, replica_ids |
| Scheduler | 53 | Minimal scheduler that distributes pods across Ready nodes. | public methods: plan |

## Runtime And Data Flow
- Internal dependencies: `ae.apishim.store`, `ae.controller.spec`, `ae.controller.state`, `ae.storage.config`
- Environment inputs: `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_NODE_NOTREADY_AFTER`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_multinode_agent_flow.py`
- `tests/unit/test_scheduler.py`
