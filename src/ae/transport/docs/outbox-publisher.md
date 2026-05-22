# Outbox Publisher

- Source: `transport/outbox_publisher.py`
- Last reviewed: 2026-05-13
- Size: 132 lines

## Purpose
Outbox publisher loop for JetStream dispatch (Phase 4).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| OutboxPublisherConfig | 18 | No class docstring. |  |
| OutboxPublisher | 23 | public methods: start, stop, run_once | public methods: start, stop, run_once |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.state`, `ae.observability.http_api`, `ae.transport.nats_client`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_transport_authority.py`
