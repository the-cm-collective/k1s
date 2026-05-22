# State

- Source: `storage/state.py`
- Last reviewed: 2026-05-13
- Size: 440 lines

## Purpose
Storage state interfaces and in-memory implementation.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| StorageState | 33 | Backend for tracking PVC/PV bindings and node mount records. | public methods: get_pv_for_pvc, get_pv, get_storage_class, record_pvc_event, bind_pvc, unbind_pvc, get_mount, upsert_mount, delete_mount, list_mounts ... |
| InMemoryStorageState | 65 | Simple in-memory storage state for early NetFS scaffolding. | public methods: get_pv_for_pvc, get_pv, get_storage_class, record_pvc_event, bind_pvc, unbind_pvc, get_mount, upsert_mount, delete_mount, list_mounts ... |
| ApishimStorageState | 139 | Storage state backed by the apishim object store for PVC/PV lookups. | public methods: get_pv_for_pvc, get_pv, get_storage_class, get_volume_attachment, get_csi_driver, get_secret, get_service_account, record_pvc_event |
| ApishimHttpStorageState | 285 | Storage/passive-resource reads backed by the apishim HTTP API. | public methods: from_env, get_pv_for_pvc, get_pv, get_storage_class, get_volume_attachment, get_csi_driver, get_secret, get_service_account |

## Runtime And Data Flow
- Internal dependencies: `.types`
- External libraries: `binascii`, `requests`
- Environment inputs: `AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_HTTP_TIMEOUT_S`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_SERVER`, `AE_APISHIM_TLS_CA`, `AE_APISHIM_TOKEN`, `AE_APISHIM_URL`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_netfs_csi.py`
- `tests/unit/test_netfs_nfs.py`
- `tests/unit/test_node_server.py`
- `tests/unit/test_storage_state.py`
