# Netfs

- Source: `storage/netfs.py`
- Last reviewed: 2026-05-13
- Size: 896 lines

## Purpose
NetFS manager scaffolding for network-backed volumes.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| PvcNotReadyError | 29 | Raised when PVC/PV binding is not ready for mount injection. | 1 internal method(s) |
| StorageDriver | 37 | CSI-aligned storage driver interface (controller + node). | public methods: create_volume, delete_volume, controller_publish, controller_unpublish, node_stage, node_publish, node_unpublish, node_unstage |
| NetFSManager | 61 | Tracks node mounts for network-backed PVCs. | public methods: ensure_mount, release_mount, list_mounts |

## Runtime And Data Flow
- Internal dependencies: `.config`, `.csi`, `.state`, `.types`
- External libraries: `grpc`, `shutil`
- Environment inputs: `AE_CSI_STAGE_ROOT`, `AE_CSI_TIMEOUT_SECONDS`, `AE_NETFS_FS_RESIZE`, `AE_NETFS_MOUNT_TIMEOUT_SECONDS`, `AE_NETFS_ROOT`, `AE_NETFS_SELINUX_RECURSIVE`
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 382: `except AttributeError:  # pragma: no cover - py<3.9 fallback`

## Related Tests And Docs
- `tests/integration/test_agent_pvc_pending.py`
- `tests/unit/test_apishim_remote_pods.py`
- `tests/unit/test_apishim_storage.py`
- `tests/unit/test_host_a_netfs_lane_script.py`
- `tests/unit/test_netfs_csi.py`
- `tests/unit/test_netfs_nfs.py`
- `tests/unit/test_netfs_validation_scripts.py`
- `tests/unit/test_node_server.py`
- `tests/unit/test_storage_controller.py`
