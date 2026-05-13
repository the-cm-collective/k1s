# Config

- Source: `storage/config.py`
- Last reviewed: 2026-05-13
- Size: 407 lines

## Purpose
Storage configuration helpers for NetFS and provisioner registry.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| StorageConfig | 28 | Resolved storage configuration derived from environment variables. | public methods: from_env |
| StorageClassConfig | 52 | StorageClass definition loaded from configuration. |  |
| StorageProvisionerConfig | 68 | Storage provisioner registry entry (built-in or CSI). |  |
| StorageProvisionerRegistry | 88 | Lookup registry for provisioner entries. | public methods: for_storage_class, for_driver, is_csi |
| StorageQuotaConfig | 119 | Namespace-scoped storage quota. |  |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _env_path | 20 | function | Internal helper. |
| _parse_storage_class | 126 | function | Internal helper. |
| _parse_bool | 178 | function | Internal helper. |
| _normalize_string_list | 186 | function | Internal helper. |
| _parse_provisioner_entry | 194 | function | Internal helper. |
| _storage_class_from_provisioner | 244 | function | Internal helper. |
| _parse_storage_quota | 261 | function | Internal helper. |
| _dedupe_storage_classes | 293 | function | Internal helper. |
| load_storage_registry | 304 | function | Load StorageClasses and provisioner registry from YAML. |
| load_storage_classes | 348 | function | Load StorageClass definitions from YAML. |
| load_storage_quotas | 355 | function | Load namespace storage quotas from YAML. |
| select_default_class | 384 | function | Entrypoint/helper without docstring. |
| load_provisioners | 391 | function | Backward-compatible alias for storage class loading. |
| load_storage_provisioner_registry | 397 | function | Load provisioner registry entries (built-in + CSI). |
| load_storage_provisioners | 404 | function | Load provisioner entries as a list. |

## Runtime And Data Flow
- External libraries: `yaml`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_netfs_csi.py`
- `tests/unit/test_storage_provisioners.py`
