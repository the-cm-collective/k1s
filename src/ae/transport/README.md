# ae.transport

- Source folder: `src/ae/transport`
- Last reviewed: 2026-05-13

## System Summary
NATS/JetStream client, subject naming, controller ingress, telemetry, outbox, and route-bundle publishing.

## Subsystems
- NATS subject naming and raw/JetStream client behavior.
- Controller-side ingress for edge work/results/status/logs.
- Outbox and route-bundle publication with retry/backoff and telemetry ingestion.

## Package Initializer
Transport helpers for NATS/JetStream integration (Mode A). Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| controller_ingress.py | [docs/controller-ingress.md](docs/controller-ingress.md) | NATS ingress for controller-side lease/result handling (Phase 2). | LeaseResponse, NatsControllerIngress |
| jetstream_monitor.py | [docs/jetstream-monitor.md](docs/jetstream-monitor.md) | JetStream monitoring poller for Phase 6 operability signals. | JetStreamMonitorConfig, JetStreamMonitor |
| nats_client.py | [docs/nats-client.md](docs/nats-client.md) | Minimal NATS client wrapper for Phase 2 transport wiring. | NatsClientError, NatsMessage, JetStreamMessage, NatsClient |
| outbox_publisher.py | [docs/outbox-publisher.md](docs/outbox-publisher.md) | Outbox publisher loop for JetStream dispatch (Phase 4). | OutboxPublisherConfig, OutboxPublisher |
| route_bundle_publisher.py | [docs/route-bundle-publisher.md](docs/route-bundle-publisher.md) | Route bundle publisher for edge-local mode (stub). | RouteBundlePublisherConfig, _BundleState, RouteBundlePublisher |
| subjects.py | [docs/subjects.md](docs/subjects.md) | Subject helpers for Mode A transport. | local_work_subject, local_result_subject, local_work_progress_subject, local_status_subject, local_logs_subject |
| telemetry_ingress.py | [docs/telemetry-ingress.md](docs/telemetry-ingress.md) | NATS ingress for site telemetry (status/logs/caps). | TelemetryIngress |

## Environment And Operational Touchpoints
`AE_CONTROLLER_EPOCH`, `AE_CONTROLLER_INGRESS_AUTHORITY_POLL_SECONDS`, `AE_CORE_PROXY_PORT_MAX`, `AE_CORE_PROXY_PORT_MIN`, `AE_EDGE_INGRESS_CORE_PROXY`, `AE_GATEWAY_JS_ACK_WAIT`, `AE_GATEWAY_JS_MAX_ACK_PENDING`, `AE_GATEWAY_JS_MAX_DELIVER`, `AE_GATEWAY_JS_MAX_WAITING`, `AE_JS_DOMAIN`, `AE_JS_STORAGE`, `AE_JS_STREAM_NAME`, `AE_JS_WORK_SUBJECT`, `AE_LEASE_RENEW_AFTER_MS`, `AE_LEASE_TTL_MS`

## Cross-Package Dependencies
`ae.config.transport`, `ae.controller.node_identity`, `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.ingress.edge_docs`, `ae.observability.http_api`, `ae.transport.nats_client`, `ae.transport.subjects`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/unit/test_gateway_service_fencing.py`
- `tests/unit/test_route_bundle_sites.py`
- `tests/unit/test_transport_authority.py`
- `tests/unit/test_transport_config.py`
- `tests/unit/test_transport_subjects.py`
