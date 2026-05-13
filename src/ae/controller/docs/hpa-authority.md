# Hpa Authority

- Source: `controller/hpa_authority.py`
- Last reviewed: 2026-05-13
- Size: 695 lines

## Purpose
Leader-gated HPA control loop over shared HA authority state.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| WorkloadMetricsCollectorConfig | 34 | No class docstring. |  |
| HPAAuthorityControllerConfig | 39 | No class docstring. |  |
| WorkloadMetricsCollector | 45 | Poll node agents for workload metrics and persist shared snapshots. | public methods: start, stop, run_once |
| HPAAuthorityController | 197 | Evaluates shared-authority HPA objects and scales converged workloads. | public methods: start, stop, run_once |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _manifest_has_cpu_requests | 540 | function | Internal helper. |
| _manifest_has_memory_requests | 545 | function | Internal helper. |
| _requested_resources_per_pod | 550 | function | Internal helper. |
| _iter_request_specs | 573 | function | Internal helper. |
| _spec_value | 579 | function | Internal helper. |
| _parse_cpu_cores | 587 | function | Internal helper. |
| _parse_quantity_bytes | 599 | function | Internal helper. |
| _fmt_bytes | 636 | function | Internal helper. |
| _condition | 644 | function | Internal helper. |
| _snapshot_is_fresh | 654 | function | Internal helper. |
| _observed_generation | 663 | function | Internal helper. |
| _parse_status_time | 671 | function | Internal helper. |
| _iso_utc | 686 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`, `ae.controller.state`, `ae.observability.http_api`, `ae.runtime`
- External libraries: `types`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_hpa_authority.py`
