# Service

- Source: `gateway/service.py`
- Last reviewed: 2026-05-13
- Size: 1224 lines

## Purpose
Site Gateway skeleton (Phase 2).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| GatewayStats | 48 | No class docstring. |  |
| SiteGateway | 61 | public methods: start | public methods: start |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| render_subjects | 1062 | function | Entrypoint/helper without docstring. |
| _safe_json | 1073 | function | Internal helper. |
| _bundle_latency_seconds | 1082 | function | Internal helper. |
| _gateway_fence_db_path | 1099 | function | Internal helper. |
| _record_fence_decision | 1106 | function | Internal helper. |
| _work_key | 1135 | function | Internal helper. |
| _next_result_retry_delay | 1143 | function | Internal helper. |
| _truthy_env | 1155 | function | Internal helper. |
| _parse_labels | 1160 | function | Internal helper. |
| _merge_node_labels | 1175 | function | Internal helper. |
| _parse_duration_seconds | 1191 | function | Internal helper. |
| _parse_float | 1205 | function | Internal helper. |
| _should_sample | 1214 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae`, `ae.config.transport`, `ae.gateway.spool`, `ae.ha.fencing`, `ae.ingress.edge_local`, `ae.observability.http_api`, `ae.transport`, `ae.transport.nats_client`
- Environment inputs: `AE_GATEWAY_BACKEND`, `AE_GATEWAY_FENCE_DB`, `AE_GATEWAY_LEASE_RETRY_JITTER`, `AE_GATEWAY_LEASE_RETRY_MAX`, `AE_GATEWAY_LEASE_RETRY_MIN`, `AE_GATEWAY_LEASE_TIMEOUT`, `AE_GATEWAY_LOGS_PUBLISH_INTERVAL`, `AE_GATEWAY_LOGS_SAMPLE_RATE`, `AE_GATEWAY_RESULT_RETRY_MAX`, `AE_GATEWAY_RESULT_RETRY_MIN`, `AE_GATEWAY_STATUS_PUBLISH_INTERVAL`, `AE_GATEWAY_STATUS_SAMPLE_RATE`, `AE_GATEWAY_WORK_HEARTBEAT_TIMEOUT`, `AE_GATEWAY_WORK_NAK_DELAY`, `AE_JS_STREAM_NAME`, `AE_NODE_ID`, `AE_NODE_LABELS`, `AE_NODE_PROFILE`, `AE_NODE_ROLE`, `AE_TRANSPORT_BACKEND`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_gateway_service_fencing.py`
