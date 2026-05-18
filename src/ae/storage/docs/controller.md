# Controller

- Source: `storage/controller.py`
- Last reviewed: 2026-05-13
- Size: 2971 lines

## Purpose
Storage controller for StorageClass seeding and PVC/PV reconciliation.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| StorageController | 85 | Seed StorageClass objects from config and prepare for PVC/PV binding. | public methods: sync, start, stop, reconcile_once |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| seed_storage_classes | 2969 | function | Helper to seed StorageClass definitions from config. |

## Runtime And Data Flow
- Internal dependencies: `.config`, `.csi`, `ae.apishim.store`
- External libraries: `binascii`, `grpc`, `shutil`
- Environment inputs: `AE_CSI_TIMEOUT_SECONDS`, `AE_NETFS_CAPACITY_NAMESPACE`, `AE_NETFS_FS_RESIZE`, `AE_STORAGE_LOCAL_CLASS`, `AE_STORAGE_NFS_CLASS`, `AE_STORAGE_NFS_HOSTPATH`, `AE_STORAGE_NFS_PATH`, `AE_STORAGE_NFS_SERVER`, `AE_STORAGE_ROOT`, `AE_STORAGE_SEED_DEFAULTS`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/integration/_profile_smoke.py`
- `tests/integration/test_agent_pvc_pending.py`
- `tests/integration/test_agent_service_proxy.py`
- `tests/integration/test_agent_streaming_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_cri_runtime_integration.py`
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/integration/test_etcd_state_adapter.py`
- `tests/integration/test_multinode_agent_flow.py`
- `tests/integration/test_overlay_vip.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_agent_api.py`
