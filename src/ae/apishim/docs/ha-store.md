# Ha Store

- Source: `apishim/ha_store.py`
- Last reviewed: 2026-05-13
- Size: 1579 lines

## Purpose
HA-mode apishim store routing onto shared controller authority.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| AuthorityMutationError | 82 | Raised when an HA workload-core mutation cannot be mapped safely. | 1 internal method(s) |
| CrdAuthorityCatalog | 649 | State-backed catalog of CRD-served custom-resource GVRs for HA routing. | public methods: refresh, lookup, is_dynamic_resource |
| WorkloadAuthorityStore | 693 | Store adapter exposing converged HA workload resources via controller state. | public methods: close, export_all, render_metrics, get, list, list_all, upsert, upsert_if_not_deleted, delete, watch |
| GenericAuthorityStore | 1216 | Store adapter exposing non-workload HA resources via controller state. | public methods: close, export_all, render_metrics, get, list, list_all, upsert, upsert_if_not_deleted, delete, watch |
| MultiplexApishimStore | 1432 | Route converged HA workload resources to controller state and everything else to legacy store. | public methods: from_state_and_legacy, close, export_all, render_metrics, get, list, list_all, watch, upsert, upsert_if_not_deleted ... |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| is_authority_resource | 98 | function | Entrypoint/helper without docstring. |
| is_workload_authority_resource | 102 | function | Entrypoint/helper without docstring. |
| is_generic_authority_resource | 106 | function | Entrypoint/helper without docstring. |
| is_storage_authority_resource | 110 | function | Entrypoint/helper without docstring. |
| is_controller_owned_storage_authority_resource | 114 | function | Entrypoint/helper without docstring. |
| generic_kind_for_resource | 118 | function | Entrypoint/helper without docstring. |
| _validate_hpa_spec | 152 | function | Internal helper. |
| workload_kind_for_entry | 201 | function | Entrypoint/helper without docstring. |
| workload_resource_for_entry | 212 | function | Entrypoint/helper without docstring. |
| daemonset_manifest_for_entry | 223 | function | Entrypoint/helper without docstring. |
| materialize_registry_manifests | 232 | function | Entrypoint/helper without docstring. |
| _registry_labels | 244 | function | Internal helper. |
| _rv_from_metadata | 261 | function | Internal helper. |
| _service_name | 271 | function | Internal helper. |
| _ingress_name | 278 | function | Internal helper. |
| _service_cluster_ip | 285 | function | Internal helper. |
| _owner_references_for_entry | 296 | function | Internal helper. |
| _owner_label_updates | 315 | function | Internal helper. |
| _workload_doc | 333 | function | Internal helper. |
| _workload_status | 369 | function | Internal helper. |
| _k8s_object_from_doc | 424 | function | Internal helper. |
| _manifest_port_map | 446 | function | Internal helper. |
| _service_spec_from_object | 464 | function | Internal helper. |
| _service_doc | 517 | function | Internal helper. |
| _ingress_doc | 547 | function | Internal helper. |
| _entry_to_object | 582 | function | Internal helper. |
| _authority_object_to_k8s | 601 | function | Internal helper. |
| _crd_served_resources | 620 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.apishim.store`, `ae.controller.spec`, `ae.controller.state`, `ae.k8s`, `ae.k8s.exporter`
- Environment inputs: `AE_APISHIM_HA_CRD_REFRESH_SEC`, `AE_APISHIM_HA_WATCH_POLL_SEC`

## Maintenance Notes
- Line 1438: `"""Route converged HA workload resources to controller state and everything else to legacy store."""`
- Line 1444: `legacy: ObjectStore,`
- Line 1449: `self._legacy = legacy`
- Line 1455: `cls, state: SQLiteStateStore, legacy: ObjectStore`
- Line 1469: `legacy,`

## Related Tests And Docs
- `tests/unit/test_apishim_ha_crd_authority.py`
- `tests/unit/test_apishim_ha_mode.py`
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_apishim_ha_store.py`
- `tests/unit/test_apishim_ha_workload_authority.py`
- `tests/unit/test_cronjob_authority_controller.py`
- `tests/unit/test_storage_controller.py`
