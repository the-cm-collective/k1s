# Dashboard

- Source: `ha/dashboard.py`
- Last reviewed: 2026-05-13
- Size: 320 lines

## Purpose
Background HA dashboard probes for the integrated Hive dashboard.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| HaDashboardProbeConfig | 61 | public methods: from_env | public methods: from_env |
| HaDashboardProbeCache | 108 | public methods: from_env, start, stop, snapshot, run_once | public methods: from_env, start, stop, snapshot, run_once |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _truthy | 31 | function | Internal helper. |
| _env_float | 35 | function | Internal helper. |
| _parse_targets | 45 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.config.transport`, `ae.ha.ops`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_docs_export_and_links.py`
- `tests/unit/test_dashboard_template_ha.py`
- `tests/unit/test_docs_command_alignment.py`
- `tests/unit/test_envoy_control_plane.py`
- `tests/unit/test_envoy_core_local_ingress.py`
- `tests/unit/test_ha_dashboard.py`
- `tests/unit/test_http_api_rbac.py`
- `tests/unit/test_http_api_status_detail.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_microk8s_helm_contracts.py`
- `tests/unit/test_resources_loader.py`
- `tests/unit/test_system_ha_dashboard.py`
