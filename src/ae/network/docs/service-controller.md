# Service Controller

- Source: `network/service_controller.py`
- Last reviewed: 2026-05-13
- Size: 206 lines

## Purpose
Service controller that bridges manifests to the network provider.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ServiceController | 15 | Orchestrates Service VIP lifecycle using a NetworkProvider and state store. | public methods: reconcile |

## Runtime And Data Flow
- Internal dependencies: `.provider`, `ae.controller.health`, `ae.controller.spec`, `ae.controller.state`, `ae.runtime`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_reconciler.py`
