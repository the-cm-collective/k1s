# Types

- Source: `storage/types.py`
- Last reviewed: 2026-05-13
- Size: 40 lines

## Purpose
Types for storage controller and NetFS plumbing.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| PvcRef | 9 | Reference to a PersistentVolumeClaim. | public methods: key |
| PvRef | 21 | Reference to a PersistentVolume. | public methods: key |
| NetFSMount | 33 | Resolved NetFS mount metadata for a PVC on a node. |  |

## Runtime And Data Flow
- No obvious external side-effect surface in static review.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_agent_pvc_pending.py`
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_netfs_csi.py`
- `tests/unit/test_netfs_nfs.py`
- `tests/unit/test_node_volume_manager.py`
