# Authority

- Source: `controller/authority.py`
- Last reviewed: 2026-05-13
- Size: 612 lines

## Purpose
Provides classes KvClient, LeaseClient, AuthorityConfig, LeaderInfo, AuthorityMember within Core control plane: manifest loading, reconcile loop, state stores, scheduling, HA authority, and workload controllers.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| KvClient | 39 | public methods: put, delete, range, txn | public methods: put, delete, range, txn |
| LeaseClient | 55 | public methods: grant_lease, keepalive, revoke_lease, close | public methods: grant_lease, keepalive, revoke_lease, close |
| AuthorityConfig | 78 | public methods: from_env, leader_key, controllers_prefix, presence_key, presence_stale_after_seconds | public methods: from_env, leader_key, controllers_prefix, presence_key, presence_stale_after_seconds |
| LeaderInfo | 150 | No class docstring. |  |
| AuthorityMember | 160 | No class docstring. |  |
| AuthoritySnapshot | 168 | public methods: controller_epoch | public methods: controller_epoch |
| NotLeaderError | 181 | Raised when a follower receives a leader-only mutation. | public methods: as_payload |
| _LeaseState | 207 | No class docstring. |  |
| ControllerAuthorityService | 213 | public methods: from_env, ready, leader_lost, snapshot, start, stop, run_once, wait_until_ready, list_members | public methods: from_env, ready, leader_lost, snapshot, start, stop, run_once, wait_until_ready, list_members |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _truthy | 17 | function | Internal helper. |
| _now_iso | 21 | function | Internal helper. |
| _dt_from_iso | 25 | function | Internal helper. |
| _b64encode | 34 | function | Internal helper. |
| _prefix_end | 65 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae`, `ae.controller.etcd_lease_client`, `ae.controller.etcd_state`
- Environment inputs: `AE_CONTROLLER_EPOCH`
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_controller_authority.py`
- `tests/unit/test_controller_loop.py`
- `tests/unit/test_cronjob_authority_controller.py`
- `tests/unit/test_cronjob_authority_startup.py`
- `tests/unit/test_dashboard_template_ha.py`
- `tests/unit/test_hpa_authority.py`
- `tests/unit/test_hpa_authority_startup.py`
- `tests/unit/test_http_api_rbac.py`
- `tests/unit/test_netfs_validation_scripts.py`
- `tests/unit/test_remote_runtime_fencing.py`
- `tests/unit/test_storage_authority.py`
- `tests/unit/test_storage_authority_startup.py`
- `tests/unit/test_system_ha_dashboard.py`
- `tests/unit/test_transport_authority.py`
- `tests/unit/test_work_ledger.py`
