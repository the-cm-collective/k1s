# Edge Docs

- Source: `ingress/edge_docs.py`
- Last reviewed: 2026-05-13
- Size: 61 lines

## Purpose
Helpers for normalizing edge ingress route/policy documents.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _ensure_metadata | 10 | function | Internal helper. |
| normalize_route_doc | 19 | function | Return a full EdgeIngressRoute document with metadata/spec keys. |
| normalize_policy_doc | 40 | function | Return a full EdgeIngressPolicy document with metadata/spec keys. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.state`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
