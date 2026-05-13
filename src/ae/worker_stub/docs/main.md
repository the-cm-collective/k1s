# Main Entrypoint

- Source: `worker_stub/__main__.py`
- Last reviewed: 2026-05-13
- Size: 203 lines

## Purpose
Stub worker that executes local work and publishes results.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| WorkerStub | 23 | public methods: start, stop | public methods: start, stop |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _safe_json | 139 | function | Internal helper. |
| build_parser | 148 | function | Entrypoint/helper without docstring. |
| main | 172 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.node_identity`, `ae.observability.logging`, `ae.transport`, `ae.transport.nats_client`
- Environment inputs: `AE_NATS_CREDS`, `AE_SITE_ID`, `AE_WORKER_PROGRESS_INTERVAL`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
