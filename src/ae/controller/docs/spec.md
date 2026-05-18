# Spec

- Source: `controller/spec.py`
- Last reviewed: 2026-05-13
- Size: 924 lines

## Purpose
Declarative specification models for the ae application engine.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ManifestError | 16 | Raised when a manifest cannot be parsed. |  |
| Metadata | 20 | Metadata block for top-level resources. | 1 internal method(s) |
| HTTPGetProbe | 36 | HTTP probe configuration. |  |
| TCPSocketProbe | 43 | TCP socket probe configuration. |  |
| ExecProbe | 49 | Exec probe: run a command inside the container. |  |
| ProbeSpec | 55 | Container probe definition. |  |
| LifecycleHandler | 70 | Container lifecycle handler (exec/http/tcp). |  |
| LifecycleSpec | 83 | Container lifecycle hooks. |  |
| PortSpec | 92 | Container port definition. |  |
| HealthSpec | 101 | Readiness and liveness probes. |  |
| IngressSpec | 111 | Ingress configuration targeting Caddy/nginx. |  |
| ServiceSpec | 128 | Service abstraction (single-host) providing a stable published port. |  |
| ResourceQuantities | 195 | CPU and memory quantities. | public methods: quantity_map |
| ResourcesSpec | 237 | Resource requests and limits (limits used for Docker flags). |  |
| SecuritySpec | 244 | Container security context (subset aligned with K8s semantics). |  |
| PodSecuritySpec | 262 | Pod-level security context subset. |  |
| DNSConfigOption | 283 | No class docstring. |  |
| DNSConfig | 288 | No class docstring. |  |
| HostAlias | 294 | No class docstring. |  |
| VolumeSpec | 299 | HostPath volume mapping. |  |
| VolumeDeviceSpec | 309 | Raw device mapping (block volumes). |  |
| PvcMountSpec | 319 | PVC-backed volume mount request (resolved via NetFS). |  |
| StorageRetention | 333 | No class docstring. |  |
| StorageSpec | 338 | Named persistent storage volume (PV-lite). |  |
| EmptyDirSpec | 360 | Ephemeral emptyDir volume mount. |  |
| ExportHints | 375 | Optional exporter hints to suppress certain checks or toggle emissions. |  |
| SecretEnvMapping | 390 | Mapping from decrypted secret key to environment variable. |  |
| SecretRef | 397 | Reference to a sealed secret file decrypted at apply time. |  |
| ConfigEnvMapping | 409 | Mapping from config key to environment variable. |  |
| ConfigRef | 416 | Reference to a config file (YAML/JSON) projected into env vars. |  |
| AppSpec | 430 | Workload specification. |  |
| AppManifest | 547 | Top-level workload manifest (Deployment). | 1 internal method(s) |
| InferenceModelRef | 570 | Model reference for an inference cell. |  |
| InferenceParallelismSpec | 580 | Parallelism settings. |  |
| InferenceRayPorts | 587 | Pinned Ray port profile. |  |
| InferenceExecutorSpec | 605 | Executor selection and options. | 1 internal method(s) |
| InferenceMemberSpec | 634 | Candidate node for stage placement. |  |
| InferencePlacementPolicy | 644 | Placement policy knobs. |  |
| InferenceFabricSpec | 652 | Per-cell fabric policy. |  |
| InferenceRendezvousSpec | 665 | Port ranges and master stage. |  |
| InferenceHealthSpec | 677 | Lifecycle deadlines and restart policy. |  |
| InferenceLinkBudget | 691 | Hard link caps for admission. |  |
| InferencePerfBudget | 701 | Relative latency budget for network overhead. |  |
| LinkMetricSample | 712 | Observed site-to-site metric sample used for admission. |  |
| InferenceCellSpec | 724 | Inference cell desired state. |  |
| InferenceCellManifest | 748 | Top-level inference cell manifest. | 1 internal method(s) |
| InferenceCellSetSpec | 766 | Replica-set style template for inference cells. |  |
| InferenceCellSetManifest | 776 | Top-level inference cellset manifest. | 1 internal method(s) |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| parse_manifest_document | 797 | function | Parse any supported ae.dev/v1alpha1 manifest kind. |
| load_manifest | 815 | function | Load a Deployment manifest from YAML. |
| load_any_manifest | 834 | function | Load any supported ae.dev/v1alpha1 manifest kind from YAML. |
| normalize_namespace | 849 | function | Entrypoint/helper without docstring. |
| app_key | 856 | function | Entrypoint/helper without docstring. |
| split_app_key | 863 | function | Entrypoint/helper without docstring. |
| format_app_ref | 872 | function | Entrypoint/helper without docstring. |
| parse_app_ref | 877 | function | Entrypoint/helper without docstring. |
| app_key_for_manifest | 888 | function | Entrypoint/helper without docstring. |
| k8s_labels_for_manifest | 892 | function | Entrypoint/helper without docstring. |
| runtime_labels_for_manifest | 905 | function | Entrypoint/helper without docstring. |
| all_pvc_mounts | 914 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- External libraries: `pydantic`, `yaml`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/_profile_smoke.py`
- `tests/integration/test_agent_pvc_pending.py`
- `tests/integration/test_agent_streaming_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_cri_runtime_integration.py`
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/integration/test_etcd_state_adapter.py`
- `tests/integration/test_multinode_agent_flow.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_apishim_ha_crd_authority.py`
- `tests/unit/test_apishim_ha_mode.py`
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_apishim_ha_store.py`
- `tests/unit/test_apishim_ha_workload_authority.py`
