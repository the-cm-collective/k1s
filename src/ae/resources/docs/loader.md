# Loader

- Source: `resources/loader.py`
- Last reviewed: 2026-05-13
- Size: 23 lines

## Purpose
Resource loaders for packaged text assets.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| load_text | 10 | function | Entrypoint/helper without docstring. |
| render_text | 19 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- External libraries: `importlib`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_bootstrap_inference_model_script.py`
- `tests/unit/test_cri_stack.py`
- `tests/unit/test_f0n_nvidia_validate_script.py`
- `tests/unit/test_gpu_guest_passthrough_validate_script.py`
- `tests/unit/test_ha_core_drills_script.py`
- `tests/unit/test_ha_core_node_smoke_script.py`
- `tests/unit/test_host_a_gpu_guest_script.py`
- `tests/unit/test_host_a_netfs_lane_script.py`
- `tests/unit/test_lab_vm_image_contract.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_rebuild_retained_artifacts.py`
- `tests/unit/test_resources_loader.py`
- `tests/unit/test_smoke_helper.py`
- `tests/unit/test_wait_rollout_steady.py`
