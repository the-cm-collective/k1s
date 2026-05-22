# Validate

- Source: `k8s/validate.py`
- Last reviewed: 2026-05-13
- Size: 73 lines

## Purpose
Lightweight structural validator for exported K8s YAML.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| validate_documents | 10 | function | Validate a multi-doc YAML string for basic schema sanity. |

## Runtime And Data Flow
- External libraries: `yaml`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_cli_split_export.py`
- `tests/unit/test_f0n_nvidia_validate_script.py`
- `tests/unit/test_host_a_netfs_lane_script.py`
- `tests/unit/test_k8s_validate_and_preset.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_netfs_validation_scripts.py`
- `tests/unit/test_nightly_runtime_workflow.py`
- `tests/unit/test_placeholder.py`
- `tests/unit/test_security_spec.py`
