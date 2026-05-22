# Containerd Runtime

- Source: `runtime/containerd_runtime.py`
- Last reviewed: 2026-05-13
- Size: 865 lines

## Purpose
Direct containerd runtime adapter backed by nerdctl.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ContainerdRuntime | 39 | Containerd-backed runtime adapter using nerdctl. | public methods: ensure_app, list_containers_info, ensure_storage_volumes, list_storage_volumes |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _prefer_direct_endpoint_default | 32 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `.base`, `.podman_runtime`, `.registry`, `ae.controller.spec`, `ae.runtime.command_args`, `ae.runtime.ports`
- External libraries: `decimal`, `shutil`
- Environment inputs: `AE_CONTAINERD_ADDRESS`, `AE_CONTAINERD_CNI_BIN_DIR`, `AE_CONTAINERD_CNI_CONF_DIR`, `AE_CONTAINERD_DATA_ROOT`, `AE_CONTAINERD_ENDPOINT_PREFER_DIRECT`, `AE_CONTAINERD_NAMESPACE`, `AE_CONTAINERD_NETWORK`, `AE_CONTAINERD_NETWORK_SUBNET`, `AE_CRI_ENDPOINT`, `AE_NERDCTL_BIN`, `AE_NETWORK_NAME`, `AE_NETWORK_SUBNET`, `AE_NVIDIA_CONTAINER_CLI_BIN`, `AE_NVIDIA_CONTAINER_RUNTIME_BIN`, `AE_NVIDIA_RUNTIME_CONFIG_DIR`, `AE_NVIDIA_SMI_BIN`, `AE_NVIDIA_TOOLKIT_DIR`, `AE_OCI_RUNTIME`, `AE_SERIAL_SERVICE_ROLLOUT`, `CNI_PATH`, `NETCONFPATH`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_containerd_runtime.py`
- `tests/unit/test_reconciler.py`
