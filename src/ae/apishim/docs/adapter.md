# Adapter

- Source: `apishim/adapter.py`
- Last reviewed: 2026-05-13
- Size: 1909 lines

## Purpose
Shim adapter that reconciles Kubernetes objects into k1s runtime state.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| AdapterWorker | 105 | Watches apps/v1 Deployments and reconciles into k1s via Reconciler. | public methods: stop, run |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _app_name | 42 | function | Internal helper. |
| _service_selector | 46 | function | Internal helper. |
| _pod_template_labels | 50 | function | Internal helper. |
| _pod_template_ports_by_name | 54 | function | Internal helper. |
| _selector_matches | 58 | function | Internal helper. |
| _fallback_service_target | 62 | function | Internal helper. |
| _resolve_port_value | 66 | function | Internal helper. |
| _probe_from_k8s | 70 | function | Internal helper. |
| _manifest_from_deployment | 74 | function | Internal helper. |
| _runtime_from_env | 89 | function | Internal helper. |
| build_adapter | 1893 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `.store`, `ae._utc`, `ae.controller.health`, `ae.controller.reconciler`, `ae.controller.spec`, `ae.controller.state`, `ae.k8s`, `ae.runtime`
- External libraries: `croniter`
- Environment inputs: `AE_APISHIM_NODEPORT_MAX`, `AE_APISHIM_NODEPORT_MIN`, `AE_APISHIM_PORT_STATE`, `AE_APISHIM_PVC_REQUEUE_SECONDS`, `AE_APISHIM_PVC_RESCAN_SECONDS`, `AE_APISHIM_RUNTIME`, `AE_HPA_COOLDOWN_SECONDS`, `AE_RUNTIME_BACKEND`, `AE_STATE_DB`, `AE_STATE_DSN`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 2: `"""Shim adapter that reconciles Kubernetes objects into k1s runtime state."""`
- Line 478: `# Desired replicas approximate to number of nodes; fallback to 1`
- Line 753: `# fallback to 60s interval when cron expression invalid or croniter missing`
- Line 808: `# Purge controller state so deleted shim objects don't linger in the dashboard.`
- Line 1680: `# Fallback: when targetPort is a named port (e.g., "http"), just reuse service port`

## Related Tests And Docs
- `tests/integration/test_etcd_state_adapter.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_apishim_runtime_factory.py`
- `tests/unit/test_apishim_statefulset_claims.py`
- `tests/unit/test_apishim_workloads.py`
- `tests/unit/test_controller_apishim_mirror.py`
- `tests/unit/test_runtime_docker.py`
