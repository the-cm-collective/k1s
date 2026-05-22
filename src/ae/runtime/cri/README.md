# ae.runtime.cri

- Source folder: `src/ae/runtime/cri`
- Last reviewed: 2026-05-13

## System Summary
Strict CRI support package containing generated Kubernetes CRI protobuf bindings used by `cri_runtime.py`.

## Subsystems
- Common runtime contract and pod state/result models.
- Docker, Podman, direct containerd, and strict CRI adapters.
- Remote runtime client for node-agent execution.
- Command/args translation, registry auth, port allocation, and projected volume helpers.

## Package Initializer
Container Runtime Interface (CRI) API bindings package.

## Module And Script Map
No direct handwritten Python modules live in this folder; see resource/generated subtree notes below.

## Resource And Generated Subtrees
| Folder | Files | Types | Review policy |
| --- | --- | --- | --- |
| api | 6 | .py:6 | Generated/vendor/static/resource subtree; summarized at folder level. |

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_cri_runtime_exec_attach.py`
- `tests/unit/test_cri_runtime_mapping.py`
- `tests/unit/test_cri_runtime_recovery.py`
- `tests/unit/test_cri_runtime_workload_metrics.py`
