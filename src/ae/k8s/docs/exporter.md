# Exporter

- Source: `k8s/exporter.py`
- Last reviewed: 2026-05-13
- Size: 1810 lines

## Purpose
Kubernetes exporter: convert AppManifest to upstream K8s YAML.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| ExportOptions | 25 | No class docstring. |  |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _resolve_namespace | 92 | function | Internal helper. |
| _resource_labels | 99 | function | Internal helper. |
| _selector_labels | 103 | function | Internal helper. |
| _resource_quantity_dict | 109 | function | Internal helper. |
| _container_from_manifest | 130 | function | Internal helper. |
| _container_from_spec | 289 | function | Build a K8s container dict from a ContainerSpec-like object. |
| _probe_to_k8s | 470 | function | Internal helper. |
| _service_from_manifest | 487 | function | Internal helper. |
| _deployment_from_manifest | 586 | function | Internal helper. |
| _storage_claim_name | 820 | function | Internal helper. |
| _storage_field | 824 | function | Internal helper. |
| _coerce_str_list | 833 | function | Internal helper. |
| _storage_access_modes | 843 | function | Internal helper. |
| _storage_class_name | 850 | function | Internal helper. |
| _storage_volume_mode | 861 | function | Internal helper. |
| _storage_read_only | 868 | function | Internal helper. |
| _headless_service_for_statefulset | 879 | function | Emit a headless Service to back a StatefulSet's stable identities. |
| _statefulset_from_manifest | 907 | function | Internal helper. |
| _ingress_from_manifest | 1137 | function | Internal helper. |
| _configmap_from_ref | 1204 | function | Internal helper. |
| _secret_from_ref | 1234 | function | Internal helper. |
| _projected_volume_from_refs | 1264 | function | Build a single projected volume aggregating config/secret file projections. |
| _explicit_volumes_from_refs | 1298 | function | Emit explicit ConfigMap/Secret volumes with items when files[] present. |
| _pvc_from_storage | 1342 | function | Internal helper. |
| export_k8s_docs | 1369 | function | Produce a list of K8s resource dicts from a manifest. |
| _pod_template_from_manifest | 1643 | function | Return a Deployment-style Pod template for reuse in Job/CronJob. |
| _job_from_manifest | 1649 | function | Internal helper. |
| _cronjob_from_manifest | 1670 | function | Internal helper. |
| _network_policy_from_manifest | 1702 | function | Internal helper. |
| _default_network_policy | 1730 | function | Internal helper. |
| export_k8s_yaml | 1798 | function | Render a multi-document YAML string for the manifest's K8s resources. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`
- External libraries: `yaml`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
- Line 275: `# Fallback to dict keys when using raw dict updates in tests/tools`

## Related Tests And Docs
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_ingress_preset.py`
- `tests/unit/test_k8s_explicit_volumes.py`
- `tests/unit/test_k8s_exporter.py`
- `tests/unit/test_k8s_ingress.py`
- `tests/unit/test_k8s_multi_container.py`
- `tests/unit/test_k8s_statefulset_export.py`
- `tests/unit/test_k8s_validate_and_preset.py`
- `tests/unit/test_microk8s_helm_contracts.py`
- `tests/unit/test_topology_spread.py`
