# Docker Runtime

- Source: `runtime/docker_runtime.py`
- Last reviewed: 2026-05-13
- Size: 2057 lines

## Purpose
Docker-backed runtime adapter for managing application pods.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| DockerRuntime | 33 | Ensures Docker containers match the desired manifest state. | public methods: ensure_app, read_logs, exec, remove_app, remove_old_revisions, remove_replicas, read_logs_for_container, exec_for_container, exec_attach, exec_resize ... |

## Runtime And Data Flow
- Internal dependencies: `.base`, `.registry`, `ae.apishim.store`, `ae.controller.spec`, `ae.runtime.command_args`, `ae.runtime.ports`, `ae.storage`, `ae.storage.netfs`
- External libraries: `docker`
- Environment inputs: `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_CRI_SANDBOX_IMAGE`, `AE_DOCKER_ENDPOINT_PREFER_NETWORK`, `AE_DOCKER_NETWORK`, `AE_ENABLE_NETFS`, `AE_NETWORK_NAME`, `AE_NODE_ADVERTISE_IP`, `AE_POD_SANDBOX_IMAGE`, `AE_SERIAL_SERVICE_ROLLOUT`, `DOCKER_CERT_PATH`, `DOCKER_TLS_CERTDIR`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 1356: `# Fallback for legacy or mismatched labels: scan by name/alternate labels.`
- Line 1619: `# 3) Shared-network fallback using manifest-declared ports (same-host overlay only)`

## Related Tests And Docs
- `tests/unit/test_runtime_docker.py`
