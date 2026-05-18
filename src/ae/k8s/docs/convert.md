# Convert

- Source: `k8s/convert.py`
- Last reviewed: 2026-05-13
- Size: 490 lines

## Purpose
Kubernetes manifest conversion helpers for k1s.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| app_name_for_k8s | 21 | function | Entrypoint/helper without docstring. |
| _get | 25 | function | Internal helper. |
| _metadata | 31 | function | Internal helper. |
| _spec | 36 | function | Internal helper. |
| _name | 41 | function | Internal helper. |
| _namespace | 46 | function | Internal helper. |
| service_selector | 52 | function | Entrypoint/helper without docstring. |
| pod_template_labels | 74 | function | Entrypoint/helper without docstring. |
| pod_template_ports_by_name | 84 | function | Entrypoint/helper without docstring. |
| selector_matches | 106 | function | Entrypoint/helper without docstring. |
| fallback_service_target | 112 | function | Entrypoint/helper without docstring. |
| resolve_port_value | 125 | function | Entrypoint/helper without docstring. |
| probe_from_k8s | 138 | function | Entrypoint/helper without docstring. |
| _resource_quantity_dict | 170 | function | Internal helper. |
| manifest_from_k8s_workload | 193 | function | Entrypoint/helper without docstring. |
| service_spec_from_k8s | 392 | function | Entrypoint/helper without docstring. |
| ingress_spec_from_k8s | 440 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_cri_bootstrap_scripts.py`
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_k8s_convert_ingress.py`
- `tests/unit/test_k8s_convert_pvc.py`
- `tests/unit/test_k8s_convert_service.py`
- `tests/unit/test_k8s_convert_statefulset.py`
- `tests/unit/test_lab_vm_tools.py`
