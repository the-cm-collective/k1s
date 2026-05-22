# ae.runtime

- Source folder: `src/ae/runtime`
- Last reviewed: 2026-05-13

## System Summary
Runtime adapter interface and Docker, Podman, direct-containerd, strict CRI, remote, and stub implementations.

## Subsystems
- Common runtime contract and pod state/result models.
- Docker, Podman, direct containerd, and strict CRI adapters.
- Remote runtime client for node-agent execution.
- Command/args translation, registry auth, port allocation, and projected volume helpers.

## Package Initializer
Runtime adapters for container operations. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| base.py | [docs/base.md](docs/base.md) | Runtime adapter interfaces for container orchestration. | PodState, RuntimeResult, WorkloadMetricSample, RuntimeAdapter |
| command_args.py | [docs/command-args.md](docs/command-args.md) | Kubernetes command/args translation helpers for runtime adapters. | kubernetes_command_parts, _items |
| containerd_runtime.py | [docs/containerd-runtime.md](docs/containerd-runtime.md) | Direct containerd runtime adapter backed by nerdctl. | ContainerdRuntime |
| cri_runtime.py | [docs/cri-runtime.md](docs/cri-runtime.md) | CRI-backed runtime adapter for managing application pods. | _StalePodSandboxError, _ReservedContainerNameError, CRIRuntime |
| docker_runtime.py | [docs/docker-runtime.md](docs/docker-runtime.md) | Docker-backed runtime adapter for managing application pods. | DockerRuntime |
| docker_stub.py | [docs/docker-stub.md](docs/docker-stub.md) | Stub runtime adapter for local testing without Docker. | StubRuntime |
| podman_runtime.py | [docs/podman-runtime.md](docs/podman-runtime.md) | Podman-backed runtime adapter using OCI runtimes via Podman CLI. | _RunResult, PodmanRuntime |
| ports.py | [docs/ports.md](docs/ports.md) | Helpers for allocating host ports for local runtimes. | _port_candidates, _port_is_free, choose_host_port, is_port_free |
| projections.py | [docs/projections.md](docs/projections.md) | Helpers for projecting K8s ConfigMap/Secret volumes into host paths. | ensure_k8s_volume_projections, _find_projection_root, _write_projection_source, _write_projection_file, _parse_mode |
| registry.py | [docs/registry.md](docs/registry.md) | Registry authentication helpers. | RegistryAuthProvider |
| remote_runtime.py | [docs/remote-runtime.md](docs/remote-runtime.md) | Remote runtime shim that delegates RuntimeAdapter calls to an HTTP agent. | RemoteRuntime |

## Resource And Generated Subtrees
| Folder | Files | Types | Review policy |
| --- | --- | --- | --- |
| cri | 8 | .md:1, .py:7 | Generated/vendor/static/resource subtree; summarized at folder level. |

## Environment And Operational Touchpoints
`AE_AGENT_CA_FILE`, `AE_AGENT_CERT_FILE`, `AE_AGENT_KEY_FILE`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_CONTAINERD_ADDRESS`, `AE_CONTAINERD_CNI_BIN_DIR`, `AE_CONTAINERD_CNI_CONF_DIR`, `AE_CONTAINERD_DATA_ROOT`, `AE_CONTAINERD_ENDPOINT_PREFER_DIRECT`, `AE_CONTAINERD_NAMESPACE`, `AE_CONTAINERD_NETWORK`, `AE_CONTAINERD_NETWORK_SUBNET`, `AE_CRI_ENDPOINT`, `AE_CRI_SANDBOX_IMAGE`, `AE_CRI_VOLUME_ROOT`, `AE_DOCKER_ENDPOINT_PREFER_NETWORK`, `AE_DOCKER_NETWORK`, `AE_ENABLE_NETFS`, `AE_HA_MODE`, `AE_NERDCTL_BIN`, `AE_NETWORK_NAME`, `AE_NETWORK_SUBNET`, `AE_NODE_ADVERTISE_IP`, `AE_NODE_ID`, `AE_NVIDIA_CONTAINER_CLI_BIN`, `AE_NVIDIA_CONTAINER_RUNTIME_BIN`, `AE_NVIDIA_RUNTIME_CONFIG_DIR`, `AE_NVIDIA_SMI_BIN`, `AE_NVIDIA_TOOLKIT_DIR`, `AE_OCI_RUNTIME`, `AE_PODMAN_BIN`, `AE_PODMAN_DEBUG`, `AE_PODMAN_ENDPOINT_PREFER_DIRECT`, `AE_PODMAN_NETWORK`, `AE_PODMAN_PORTFORWARD_TIMEOUT`, `AE_PODMAN_RETRY_DELAY`, `AE_PODMAN_RETRY_MAX`, `AE_POD_SANDBOX_IMAGE`, `AE_PROJECTION_ROOT`, `AE_REGISTRY_CONFIG`, `AE_REMOTE_RUNTIME_ENSURE_TIMEOUT`, `AE_SERIAL_SERVICE_ROLLOUT`, `AE_STUB_BACKEND_HOST`, `AE_STUB_BACKEND_PORT`, `AE_STUB_NAMESPACE`, `CNI_PATH`, `CRICTL_BIN`, `DOCKER_CERT_PATH`, `DOCKER_TLS_CERTDIR`, `NETCONFPATH`

## Cross-Package Dependencies
`.base`, `.podman_runtime`, `.ports`, `.registry`, `ae.apishim.store`, `ae.controller.spec`, `ae.ha.fencing`, `ae.runtime.command_args`, `ae.runtime.cri.api.runtime.v1`, `ae.runtime.ports`, `ae.storage`, `ae.storage.netfs`, `ae.storage.state`

## Maintenance Notes
Detailed markers live in the per-module docs; direct module counts:
- `cri_runtime.py`: 1 marker(s)
- `docker_runtime.py`: 1 marker(s)
- `podman_runtime.py`: 3 marker(s)

## Related Tests
- `tests/integration/test_agent_pvc_pending.py`
- `tests/integration/test_agent_streaming_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/integration/test_cri_runtime_integration.py`
- `tests/integration/test_etcd_state_adapter.py`
- `tests/integration/test_multinode_agent_flow.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_apishim_runtime_factory.py`
- `tests/unit/test_apishim_statefulset_claims.py`
- `tests/unit/test_apishim_workloads.py`
- `tests/unit/test_containerd_runtime.py`
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_cri_runtime_exec_attach.py`
- `tests/unit/test_cri_runtime_mapping.py`
- `tests/unit/test_cri_runtime_recovery.py`
- `tests/unit/test_cri_runtime_workload_metrics.py`
- `tests/unit/test_health.py`
- `tests/unit/test_health_backoff.py`
- `tests/unit/test_health_exec.py`
- `tests/unit/test_health_startup.py`
- `tests/unit/test_health_tcp.py`
- `tests/unit/test_health_thresholds.py`
