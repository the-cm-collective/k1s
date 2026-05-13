# Cri Runtime

- Source: `runtime/cri_runtime.py`
- Last reviewed: 2026-05-13
- Size: 3403 lines

## Purpose
CRI-backed runtime adapter for managing application pods.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| _StalePodSandboxError | 44 | Raised when CRI reports a dead or missing pod sandbox task. | 1 internal method(s) |
| _ReservedContainerNameError | 52 | Raised when CRI reports a container name still reserved in containerd. | 1 internal method(s) |
| CRIRuntime | 67 | CRI gRPC-backed runtime adapter (containerd/kubelet). | public methods: ensure_app, read_logs, remove_app, remove_old_revisions, remove_replicas, list_containers_info, list_workload_metrics, exec, exec_attach, exec_resize ... |

## Runtime And Data Flow
- Internal dependencies: `.base`, `.registry`, `ae._utc`, `ae.apishim.store`, `ae.controller.spec`, `ae.runtime.cri.api.runtime.v1`, `ae.runtime.ports`, `ae.storage`, `ae.storage.netfs`, `ae.storage.state`
- External libraries: `google`, `grpc`, `pty`, `shutil`
- Environment inputs: `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_CRI_ENDPOINT`, `AE_CRI_SANDBOX_IMAGE`, `AE_CRI_VOLUME_ROOT`, `AE_ENABLE_NETFS`, `AE_HA_MODE`, `AE_NODE_ADVERTISE_IP`, `AE_NODE_ID`, `CRICTL_BIN`
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 1217: `# Fallback: allow simple secrets with username/password/registry keys`
- Line 1605: `"failed to create shim task" in details`
- Line 1615: `"can't find shim for sandbox",`

## Related Tests And Docs
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_cri_runtime_exec_attach.py`
- `tests/unit/test_cri_runtime_mapping.py`
- `tests/unit/test_cri_runtime_recovery.py`
- `tests/unit/test_cri_runtime_workload_metrics.py`
- `tests/unit/test_gpu_guest_passthrough_validate_script.py`
