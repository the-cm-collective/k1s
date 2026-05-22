# Main Entrypoint

- Source: `kctl/__main__.py`
- Last reviewed: 2026-05-13
- Size: 587 lines

## Purpose
kubectl-like CLI for working with the ae/k1s engine.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ParsedRef | 40 | No class docstring. |  |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _resolve_app_name | 45 | function | Internal helper. |
| _display_app_name | 50 | function | Internal helper. |
| _ha_mode_enabled | 54 | function | Internal helper. |
| parse_ref | 58 | function | Parse resource reference like "app/echo" or provide NAME + kind default. |
| build_parser | 105 | function | Entrypoint/helper without docstring. |
| _setup | 190 | function | Internal helper. |
| handle_get | 209 | function | Entrypoint/helper without docstring. |
| handle_describe | 354 | function | Entrypoint/helper without docstring. |
| handle_apply | 378 | function | Entrypoint/helper without docstring. |
| handle_rollout | 411 | function | Entrypoint/helper without docstring. |
| handle_logs_k1s | 449 | function | Entrypoint/helper without docstring. |
| handle_events_k1s | 466 | function | Entrypoint/helper without docstring. |
| handle_delete_k1s | 478 | function | Entrypoint/helper without docstring. |
| handle_scale_k1s | 511 | function | Entrypoint/helper without docstring. |
| main | 552 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.cli.__main__`, `ae.controller.__main__`, `ae.controller.reconciler`, `ae.controller.spec`, `ae.controller.state`, `ae.ingress.service`, `ae.observability.logging`
- Environment inputs: `AE_HA_MODE`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_kctl.py`
