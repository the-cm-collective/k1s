# ae.ha

- Source folder: `src/ae/ha`
- Last reviewed: 2026-05-13

## System Summary
High-availability support code for authority operations, fencing decisions, dashboard probes, and operational helpers.

## Package Initializer
HA helpers shared across controller and executor surfaces. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| dashboard.py | [docs/dashboard.md](docs/dashboard.md) | Background HA dashboard probes for the integrated Hive dashboard. | HaDashboardProbeConfig, HaDashboardProbeCache |
| fencing.py | [docs/fencing.md](docs/fencing.md) | Shared HA mutation fencing helpers. | ControllerMutationIdentity, MutationEnvelope, FenceScopeState, FenceDecision, SQLiteFenceStore |
| ops.py | [docs/ops.md](docs/ops.md) | Provides classes EtcdLeaderRecord, EtcdMemberAddResult, EtcdRestoreMemberSpec, EtcdRestoreMemberPlan,... | EtcdLeaderRecord, EtcdMemberAddResult, EtcdRestoreMemberSpec, EtcdRestoreMemberPlan, EtcdQuorumRestorePlan |

## Environment And Operational Touchpoints
`AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_TLS_CA`, `AE_CONTAINER_CLI`, `AE_CONTROLLER_EPOCH`, `AE_CONTROLLER_ID`

## Cross-Package Dependencies
`ae.config.transport`, `ae.ha.ops`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/e2e/ha_closeout.py`
- `tests/unit/test_gateway_service_fencing.py`
- `tests/unit/test_ha_dashboard.py`
- `tests/unit/test_ha_fencing.py`
- `tests/unit/test_ha_ops.py`
- `tests/unit/test_node_server.py`
- `tests/unit/test_node_server_fabric.py`
