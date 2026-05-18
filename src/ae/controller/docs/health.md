# Health

- Source: `controller/health.py`
- Last reviewed: 2026-05-13
- Size: 703 lines

## Purpose
Health probe evaluation utilities.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ProbeOutcome | 17 | Result of a single probe evaluation. |  |
| PodHealth | 25 | Health status for a single pod. | public methods: replica_id, replica_id |
| HealthReport | 49 | Aggregated health across replicas. | public methods: replicas |
| HealthManager | 61 | Evaluates readiness and liveness with thresholds, period, and backoff. | public methods: set_exec_callback, set_portforward_callback, set_event_callback, evaluate |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`, `ae.runtime`
- External libraries: `requests`
- Environment inputs: `AE_PROBE_LOOPBACK_FALLBACK`, `AE_PROBE_MAX_BACKOFF`, `AE_PROBE_TRUST_ENV`, `AE_PROBE_VERBOSE`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/integration/_profile_smoke.py`
- `tests/integration/test_etcd_state_adapter.py`
- `tests/integration/test_multinode_agent_flow.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_apishim_workloads.py`
- `tests/unit/test_containerd_runtime.py`
- `tests/unit/test_cri_runtime_recovery.py`
- `tests/unit/test_dashboard_template_ha.py`
- `tests/unit/test_f0n_nvidia_validate_script.py`
- `tests/unit/test_health.py`
- `tests/unit/test_health_backoff.py`
- `tests/unit/test_health_exec.py`
- `tests/unit/test_health_startup.py`
- `tests/unit/test_health_tcp.py`
