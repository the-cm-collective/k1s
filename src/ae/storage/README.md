# ae.storage

- Source folder: `src/ae/storage`
- Last reviewed: 2026-05-13

## System Summary
Storage classes, PVC/PV authority, NetFS/CSI integration, node volume management, and storage state adapters.

## Subsystems
- StorageClass/PVC/PV authority and reconcile behavior.
- NetFS provisioning/mount lifecycle and node-side volume management.
- CSI client integration and storage state adapters for shim-backed reads.

## Package Initializer
Storage primitives and NetFS scaffolding. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| config.py | [docs/config.md](docs/config.md) | Storage configuration helpers for NetFS and provisioner registry. | StorageConfig, StorageClassConfig, StorageProvisionerConfig, StorageProvisionerRegistry, StorageQuotaConfig |
| controller.py | [docs/controller.md](docs/controller.md) | Storage controller for StorageClass seeding and PVC/PV reconciliation. | StorageController |
| netfs.py | [docs/netfs.md](docs/netfs.md) | NetFS manager scaffolding for network-backed volumes. | PvcNotReadyError, StorageDriver, NetFSManager |
| node_manager.py | [docs/node-manager.md](docs/node-manager.md) | Node-side volume manager for NetFS-backed PVC mounts. | NodeVolumeManager |
| state.py | [docs/state.md](docs/state.md) | Storage state interfaces and in-memory implementation. | StorageState, InMemoryStorageState, ApishimStorageState, ApishimHttpStorageState |
| types.py | [docs/types.md](docs/types.md) | Types for storage controller and NetFS plumbing. | PvcRef, PvRef, NetFSMount |

## Resource And Generated Subtrees
| Folder | Files | Types | Review policy |
| --- | --- | --- | --- |
| csi | 8 | .md:2, .proto:1, .py:5 | Generated/vendor/static/resource subtree; summarized at folder level. |

## Environment And Operational Touchpoints
`AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_HTTP_TIMEOUT_S`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_SERVER`, `AE_APISHIM_TLS_CA`, `AE_APISHIM_TOKEN`, `AE_APISHIM_URL`, `AE_CSI_STAGE_ROOT`, `AE_CSI_TIMEOUT_SECONDS`, `AE_NETFS_CAPACITY_NAMESPACE`, `AE_NETFS_FS_RESIZE`, `AE_NETFS_MOUNT_TIMEOUT_SECONDS`, `AE_NETFS_ROOT`, `AE_NETFS_SELINUX_RECURSIVE`, `AE_NODE_ID`, `AE_STORAGE_LOCAL_CLASS`, `AE_STORAGE_NFS_CLASS`, `AE_STORAGE_NFS_HOSTPATH`, `AE_STORAGE_NFS_PATH`, `AE_STORAGE_NFS_SERVER`, `AE_STORAGE_ROOT`, `AE_STORAGE_SEED_DEFAULTS`

## Cross-Package Dependencies
`.config`, `.csi`, `.netfs`, `.state`, `.types`, `ae._utc`, `ae.apishim.store`, `ae.controller.spec`

## Maintenance Notes
- `controller.py` line 1059: `# If no registry entry exists, fall back to legacy marker-only behavior.`
- `controller.py` line 2623: `except AttributeError:  # pragma: no cover - py<3.9 fallback`
- `netfs.py` line 382: `except AttributeError:  # pragma: no cover - py<3.9 fallback`
- `node_manager.py` line 42: `# replica_id is accepted for compatibility with runtime/node call sites.`

## Related Tests
- `tests/integration/test_agent_pvc_pending.py`
- `tests/integration/test_storage.py`
- `tests/unit/test_apishim_ha_mode.py`
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_netfs_csi.py`
- `tests/unit/test_netfs_nfs.py`
- `tests/unit/test_node_server.py`
- `tests/unit/test_node_volume_manager.py`
- `tests/unit/test_storage_controller.py`
- `tests/unit/test_storage_provisioners.py`
- `tests/unit/test_storage_state.py`
