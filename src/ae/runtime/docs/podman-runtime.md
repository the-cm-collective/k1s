# Podman Runtime

- Source: `runtime/podman_runtime.py`
- Last reviewed: 2026-05-13
- Size: 2729 lines

## Purpose
Podman-backed runtime adapter using OCI runtimes via Podman CLI.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| _RunResult | 36 | No class docstring. |  |
| PodmanRuntime | 45 | public methods: ensure_app, read_logs, read_logs_for_container, exec_for_container, exec_attach, exec_resize, exec_exit_code, exec_status, remove_app, remove_old_revisions ... | public methods: ensure_app, read_logs, read_logs_for_container, exec_for_container, exec_attach, exec_resize, exec_exit_code, exec_status, remove_app, remove_old_revisions ... |

## Runtime And Data Flow
- Internal dependencies: `.base`, `.ports`, `ae.apishim.store`, `ae.controller.spec`, `ae.runtime.command_args`, `ae.storage`, `ae.storage.netfs`, `ae.storage.state`
- External libraries: `ctypes`, `fcntl`, `pty`, `shutil`, `struct`, `termios`
- Environment inputs: `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_CRI_SANDBOX_IMAGE`, `AE_ENABLE_NETFS`, `AE_NETWORK_NAME`, `AE_NODE_ADVERTISE_IP`, `AE_OCI_RUNTIME`, `AE_PODMAN_BIN`, `AE_PODMAN_DEBUG`, `AE_PODMAN_ENDPOINT_PREFER_DIRECT`, `AE_PODMAN_NETWORK`, `AE_PODMAN_PORTFORWARD_TIMEOUT`, `AE_PODMAN_RETRY_DELAY`, `AE_PODMAN_RETRY_MAX`, `AE_POD_SANDBOX_IMAGE`, `AE_SERIAL_SERVICE_ROLLOUT`
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
- Line 511: `# Fallback: scan ps JSON and match Config.Labels`
- Line 525: `# Fallback to well-known container name if label lookup fails`
- Line 717: `# Fallback to stdio hijack via 'podman exec --interactive --tty' and a pty.`

## Related Tests And Docs
- `tests/unit/test_containerd_runtime.py`
- `tests/unit/test_runtime_podman.py`
- `tests/unit/test_security_spec.py`
