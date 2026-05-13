# Controller Ingress

- Source: `transport/controller_ingress.py`
- Last reviewed: 2026-05-13
- Size: 692 lines

## Purpose
NATS ingress for controller-side lease/result handling (Phase 2).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| LeaseResponse | 24 | public methods: as_dict | public methods: as_dict |
| NatsControllerIngress | 47 | public methods: start, close, sync_authority | public methods: start, close, sync_authority |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _parse_duration_seconds | 655 | function | Internal helper. |
| _site_id_from_subject | 669 | function | Internal helper. |
| _result_matches_ledger | 678 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.config.transport`, `ae.controller.node_identity`, `ae.controller.state`, `ae.ha.fencing`, `ae.observability.http_api`, `ae.transport.nats_client`
- Environment inputs: `AE_CONTROLLER_EPOCH`, `AE_CONTROLLER_INGRESS_AUTHORITY_POLL_SECONDS`, `AE_CORE_PROXY_PORT_MAX`, `AE_CORE_PROXY_PORT_MIN`, `AE_EDGE_INGRESS_CORE_PROXY`, `AE_GATEWAY_JS_ACK_WAIT`, `AE_GATEWAY_JS_MAX_ACK_PENDING`, `AE_GATEWAY_JS_MAX_DELIVER`, `AE_GATEWAY_JS_MAX_WAITING`, `AE_JS_STORAGE`, `AE_JS_STREAM_NAME`, `AE_JS_WORK_SUBJECT`, `AE_LEASE_RENEW_AFTER_MS`, `AE_LEASE_TTL_MS`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_transport_authority.py`
