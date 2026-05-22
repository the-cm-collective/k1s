# Rotate Certs

- Source: `cli/rotate_certs.py`
- Last reviewed: 2026-05-13
- Size: 41 lines

## Purpose
CLI helper to issue node certs and join tokens.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| main | 13 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.security`
- Environment inputs: `AE_AGENT_JOIN_SECRET`, `AE_NODE_ID`, `AE_TLS_DIR`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
