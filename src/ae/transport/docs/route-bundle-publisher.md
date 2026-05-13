# Route Bundle Publisher

- Source: `transport/route_bundle_publisher.py`
- Last reviewed: 2026-05-13
- Size: 454 lines

## Purpose
Route bundle publisher for edge-local mode (stub).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| RouteBundlePublisherConfig | 32 | No class docstring. |  |
| _BundleState | 37 | No class docstring. |  |
| RouteBundlePublisher | 49 | public methods: start, stop, run_once | public methods: start, stop, run_once |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _build_bundle | 219 | function | Internal helper. |
| _collect_bundle_payload | 250 | function | Internal helper. |
| _route_ack_matches_state | 281 | function | Internal helper. |
| _route_is_edge_local | 298 | function | Internal helper. |
| _sorted_docs | 305 | function | Internal helper. |
| _bundle_hash | 312 | function | Internal helper. |
| _route_service_refs | 332 | function | Internal helper. |
| _service_ref_pair | 356 | function | Internal helper. |
| _collect_service_endpoints | 365 | function | Internal helper. |
| _coerce_int | 413 | function | Internal helper. |
| _next_backoff | 422 | function | Internal helper. |
| _safe_json | 436 | function | Internal helper. |
| _site_id_from_subject | 445 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.ingress.edge_docs`, `ae.observability.http_api`, `ae.transport.nats_client`, `ae.transport.subjects`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_route_bundle_sites.py`
- `tests/unit/test_transport_authority.py`
