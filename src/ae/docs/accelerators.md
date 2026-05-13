# Accelerators

- Source: `accelerators.py`
- Last reviewed: 2026-05-13
- Size: 309 lines

## Purpose
Normalizes accelerator/GPU inventory and exposes execution labels/capabilities used by scheduling and inference fabric code.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _truthy_env | 16 | function | Internal helper. |
| _normalize_string | 20 | function | Internal helper. |
| _normalize_int | 27 | function | Internal helper. |
| _normalize_string_list | 40 | function | Internal helper. |
| _default_memory_model | 51 | function | Internal helper. |
| _default_partitioning_mode | 59 | function | Internal helper. |
| normalize_accelerator | 65 | function | Entrypoint/helper without docstring. |
| normalize_capabilities | 91 | function | Entrypoint/helper without docstring. |
| has_accelerator_inventory | 107 | function | Entrypoint/helper without docstring. |
| accelerator_inventory | 111 | function | Entrypoint/helper without docstring. |
| execution_accelerators | 118 | function | Entrypoint/helper without docstring. |
| execution_accelerator_count | 134 | function | Entrypoint/helper without docstring. |
| execution_accelerator_models | 144 | function | Entrypoint/helper without docstring. |
| project_gpu_labels | 153 | function | Entrypoint/helper without docstring. |
| merge_projected_gpu_labels | 167 | function | Entrypoint/helper without docstring. |
| gpu_count_from_labels | 177 | function | Entrypoint/helper without docstring. |
| preferred_gpu_count | 191 | function | Entrypoint/helper without docstring. |
| preferred_gpu_models | 197 | function | Entrypoint/helper without docstring. |
| _query_nvidia_smi | 209 | function | Internal helper. |
| _parse_memory_mebibytes | 229 | function | Internal helper. |
| detect_nvidia_accelerator_capabilities | 242 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- External libraries: `csv`, `io`
- Environment inputs: `AE_NVIDIA_SMI_BIN`
- Side-effect surfaces: subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 1: `"""Typed accelerator capability helpers with gpu.* compatibility projection."""`

## Related Tests And Docs
- `tests/integration/test_etcd_state_adapter.py`
- `tests/unit/test_accelerators.py`
- `tests/unit/test_agent_api.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_inference_cell_controller.py`
- `tests/unit/test_state_nodes.py`
