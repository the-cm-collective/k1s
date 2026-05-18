# Projections

- Source: `runtime/projections.py`
- Last reviewed: 2026-05-13
- Size: 177 lines

## Purpose
Helpers for projecting K8s ConfigMap/Secret volumes into host paths.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| ensure_k8s_volume_projections | 15 | function | Entrypoint/helper without docstring. |
| _find_projection_root | 55 | function | Internal helper. |
| _write_projection_source | 69 | function | Internal helper. |
| _write_projection_file | 131 | function | Internal helper. |
| _parse_mode | 152 | function | Internal helper. |
| _spec_value | 168 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`
- Environment inputs: `AE_PROJECTION_ROOT`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_configs_secrets.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/unit/test_k8s_exporter.py`
- `tests/unit/test_projection.py`
- `tests/unit/test_reconciler.py`
