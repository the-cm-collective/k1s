# Nats Client

- Source: `transport/nats_client.py`
- Last reviewed: 2026-05-13
- Size: 734 lines

## Purpose
Minimal NATS client wrapper for Phase 2 transport wiring.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| NatsClientError | 39 | No class docstring. |  |
| NatsMessage | 44 | public methods: json | public methods: json |
| JetStreamMessage | 54 | public methods: ack, ack_sync, in_progress, nak | public methods: ack, ack_sync, in_progress, nak |
| NatsClient | 79 | public methods: connect, close, publish, publish_json, publish_js, publish_js_json, request, request_json, subscribe, unsubscribe ... | public methods: connect, close, publish, publish_json, publish_js, publish_js_json, request, request_json, subscribe, unsubscribe ... |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| connect_once | 574 | function | Entrypoint/helper without docstring. |
| _storage_type | 586 | function | Internal helper. |
| _retention_policy | 595 | function | Internal helper. |
| _read | 606 | function | Internal helper. |
| _enum_text | 618 | function | Internal helper. |
| _normalize_token | 635 | function | Internal helper. |
| _seconds_value | 639 | function | Internal helper. |
| _stream_config_drift | 658 | function | Internal helper. |
| _consumer_config_drift | 683 | function | Internal helper. |
| _ack_wait_value | 718 | function | Internal helper. |

## Runtime And Data Flow
- External libraries: `nats`
- Environment inputs: `AE_JS_DOMAIN`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_gateway_service_fencing.py`
- `tests/unit/test_route_bundle_sites.py`
- `tests/unit/test_transport_authority.py`
- `tests/unit/test_transport_config.py`
