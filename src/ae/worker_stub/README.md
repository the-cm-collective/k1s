# ae.worker_stub

- Source folder: `src/ae/worker_stub`
- Last reviewed: 2026-05-13

## System Summary
Development/test worker process for exercising transport and work delivery paths.

## Package Initializer
Stub worker for local NATS work dispatch testing. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| __main__.py | [docs/main.md](docs/main.md) | Stub worker that executes local work and publishes results. | WorkerStub |

## Environment And Operational Touchpoints
`AE_NATS_CREDS`, `AE_SITE_ID`, `AE_WORKER_PROGRESS_INTERVAL`

## Cross-Package Dependencies
`ae.controller.node_identity`, `ae.observability.logging`, `ae.transport`, `ae.transport.nats_client`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
