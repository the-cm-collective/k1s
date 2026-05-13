# Base

- Source: `runtime/base.py`
- Last reviewed: 2026-05-13
- Size: 213 lines

## Purpose
Runtime adapter interfaces for container orchestration.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| PodState | 13 | Status for an individual pod in the runtime. | public methods: replica_id, replica_id |
| RuntimeResult | 43 | Result of reconciling containers for an application. | public methods: replica_states |
| WorkloadMetricSample | 75 | Per-node workload metrics summary for autoscaling. |  |
| RuntimeAdapter | 86 | Adapter that drives container runtime operations. | public methods: ensure_app, read_logs, remove_app, remove_old_revisions, remove_replicas, ensure_storage_volumes, remove_storage_volumes, list_storage_volumes, list_containers_info, list_workload_metrics ... |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_agent_streaming_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_docs_export_and_links.py`
- `tests/integration/test_etcd_state_adapter.py`
- `tests/integration/test_multinode_agent_flow.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/unit/test_bench_script_contracts.py`
- `tests/unit/test_cli_namespace.py`
- `tests/unit/test_controller_authority.py`
- `tests/unit/test_cri_runtime_mapping.py`
- `tests/unit/test_cri_stack.py`
- `tests/unit/test_dashboard_template_ha.py`
- `tests/unit/test_health.py`
- `tests/unit/test_health_backoff.py`
- `tests/unit/test_health_exec.py`
