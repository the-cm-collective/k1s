# Client

- Source: `storage/csi/client.py`
- Last reviewed: 2026-05-13
- Size: 244 lines

## Purpose
CSI gRPC client helpers for controller and node operations.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| CsiVolume | 23 | No class docstring. |  |
| CsiControllerClient | 93 | public methods: endpoint, create_volume, delete_volume, controller_publish, controller_unpublish | public methods: endpoint, create_volume, delete_volume, controller_publish, controller_unpublish |
| CsiNodeClient | 173 | public methods: endpoint, node_stage, node_publish, node_unpublish, node_unstage | public methods: endpoint, node_stage, node_publish, node_unpublish, node_unstage |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| normalize_endpoint | 29 | function | Entrypoint/helper without docstring. |
| build_channel | 45 | function | Entrypoint/helper without docstring. |
| _access_mode_from_modes | 51 | function | Internal helper. |
| build_volume_capability | 64 | function | Entrypoint/helper without docstring. |
| _coalesce_context | 82 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `.api`
- External libraries: `grpc`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/unit/test_cri_stack.py`
- `tests/unit/test_envoy_control_plane.py`
- `tests/unit/test_etcd_http_client.py`
- `tests/unit/test_etcd_lease_client.py`
- `tests/unit/test_etcd_state_maintenance.py`
- `tests/unit/test_nightly_runtime_workflow.py`
- `tests/unit/test_rathole_render.py`
- `tests/unit/test_remote_runtime_fencing.py`
- `tests/unit/test_runtime_docker.py`
