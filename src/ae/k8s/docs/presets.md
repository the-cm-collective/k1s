# Presets

- Source: `k8s/presets.py`
- Last reviewed: 2026-05-13
- Size: 112 lines

## Purpose
Presets for export-k8s to speed up common profiles.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| apply_preset | 14 | function | Merge a preset into options, without clobbering explicitly set values. |
| apply_ingress_preset | 73 | function | Inject opinionated ingress annotations and defaults. |

## Runtime And Data Flow
- Internal dependencies: `ae.k8s.exporter`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_ingress_preset.py`
- `tests/unit/test_k8s_validate_and_preset.py`
