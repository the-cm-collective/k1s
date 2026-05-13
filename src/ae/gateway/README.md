# ae.gateway

- Source folder: `src/ae/gateway`
- Last reviewed: 2026-05-13

## System Summary
Site gateway process for NATS-mediated work delivery, result spooling, and edge/core transport bridging.

## Package Initializer
Site Gateway package. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| __main__.py | [docs/main.md](docs/main.md) | Site Gateway entry point (Phase 2 skeleton). | build_parser, main |
| service.py | [docs/service.md](docs/service.md) | Site Gateway skeleton (Phase 2). | GatewayStats, SiteGateway |
| spool.py | [docs/spool.md](docs/spool.md) | SQLite-backed spool for gateway durability (Option A). | InflightRecord, ResultRecord, GatewaySpool |

## Environment And Operational Touchpoints
`AE_GATEWAY_BACKEND`, `AE_GATEWAY_FENCE_DB`, `AE_GATEWAY_LEASE_RETRY_JITTER`, `AE_GATEWAY_LEASE_RETRY_MAX`, `AE_GATEWAY_LEASE_RETRY_MIN`, `AE_GATEWAY_LEASE_TIMEOUT`, `AE_GATEWAY_LOGS_PUBLISH_INTERVAL`, `AE_GATEWAY_LOGS_SAMPLE_RATE`, `AE_GATEWAY_RESULT_RETRY_MAX`, `AE_GATEWAY_RESULT_RETRY_MIN`, `AE_GATEWAY_STATUS_INTERVAL`, `AE_GATEWAY_STATUS_PUBLISH_INTERVAL`, `AE_GATEWAY_STATUS_SAMPLE_RATE`, `AE_GATEWAY_WORK_HEARTBEAT_TIMEOUT`, `AE_GATEWAY_WORK_NAK_DELAY`, `AE_JS_STREAM_NAME`, `AE_NATS_URL`, `AE_NODE_ID`, `AE_NODE_LABELS`, `AE_NODE_PROFILE`, `AE_NODE_ROLE`, `AE_SITE_ID`, `AE_TRANSPORT_BACKEND`

## Cross-Package Dependencies
`ae`, `ae.config.transport`, `ae.controller.node_identity`, `ae.gateway.service`, `ae.gateway.spool`, `ae.ha.fencing`, `ae.ingress.edge_local`, `ae.observability.http_api`, `ae.observability.logging`, `ae.transport`, `ae.transport.nats_client`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/unit/test_gateway_service_fencing.py`
- `tests/unit/test_gateway_spool.py`
- `tests/unit/test_lab_vm_tools.py`
