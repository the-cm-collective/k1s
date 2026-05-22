# Node Manager

- Source: `storage/node_manager.py`
- Last reviewed: 2026-05-13
- Size: 136 lines

## Purpose
Node-side volume manager for NetFS-backed PVC mounts.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| NodeVolumeManager | 20 | Resolve PVC mounts into hostPath volumes backed by NetFS. | public methods: inject_pvc_mounts |

## Runtime And Data Flow
- Internal dependencies: `.netfs`, `.types`, `ae.controller.spec`
- Environment inputs: `AE_NODE_ID`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_node_volume_manager.py`
