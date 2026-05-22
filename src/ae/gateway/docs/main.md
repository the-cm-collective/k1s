# Main Entrypoint

- Source: `gateway/__main__.py`
- Last reviewed: 2026-05-13
- Size: 92 lines

## Purpose
Site Gateway entry point (Phase 2 skeleton).

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| build_parser | 15 | function | Entrypoint/helper without docstring. |
| main | 34 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.config.transport`, `ae.controller.node_identity`, `ae.gateway.service`, `ae.observability.logging`, `ae.transport.nats_client`
- Environment inputs: `AE_GATEWAY_STATUS_INTERVAL`, `AE_NATS_URL`, `AE_NODE_ID`, `AE_SITE_ID`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
