# Reconciler

- Source: `controller/reconciler.py`
- Last reviewed: 2026-05-13
- Size: 2289 lines

## Purpose
Reconcile loop coordinating manifests, runtime operations, and health.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ReconcileReport | 57 | Summary of a reconcile run. |  |
| _ObservedRuntimeReplica | 77 | No class docstring. |  |
| Reconciler | 84 | Coordinates manifest application across runtime, health, and state store. | public methods: reconcile_manifest_path, reconcile |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _record_event_metric_safe | 23 | function | Best-effort metric hook used by reconciler events. |
| _truthy_env | 33 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `.state`, `ae.apishim.store`, `ae.config.manager`, `ae.controller.health`, `ae.controller.scheduler`, `ae.controller.spec`, `ae.ingress.service`, `ae.observability.http_api`, `ae.runtime`, `ae.runtime.containerd_runtime`, `ae.secrets`, `ae.storage.config`
- External libraries: `requests`, `yaml`
- Environment inputs: `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_CADDY_PREFER_HOST_PORT_UPSTREAMS`, `AE_CRASHLOOP_TTL`, `AE_NODE_ID`, `AE_NODE_NAME`, `AE_PROJECTION_ROOT`, `AE_RECREATE_COOLDOWN_SEC`, `AE_RESTART_THRESHOLD`, `AE_RESTART_WINDOW_SEC`, `AE_RUNTIME_BACKEND`, `AE_SERIAL_SERVICE_ROLLOUT`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
- Line 1532: `- exec: list[str] executed in the first ready replica (fallback to first replica)`
- Line 1730: `# Fallback: allow loopback endpoints when nothing else is ready`

## Related Tests And Docs
- `tests/integration/test_multinode_agent_flow.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_apishim_workloads.py`
- `tests/unit/test_http_api_status_detail.py`
- `tests/unit/test_init_containers_events.py`
- `tests/unit/test_metrics_per_app.py`
- `tests/unit/test_prestop_exec.py`
- `tests/unit/test_projection.py`
- `tests/unit/test_reconciler.py`
- `tests/unit/test_reconciler_endpoints.py`
- `tests/unit/test_reconciler_netfs.py`
- `tests/unit/test_rollout.py`
- `tests/unit/test_rollout_hooks.py`
