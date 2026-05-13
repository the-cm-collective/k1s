# Spool

- Source: `gateway/spool.py`
- Last reviewed: 2026-05-13
- Size: 463 lines

## Purpose
SQLite-backed spool for gateway durability (Option A).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| InflightRecord | 15 | No class docstring. |  |
| ResultRecord | 31 | No class docstring. |  |
| GatewaySpool | 47 | public methods: init, record_inflight, update_inflight_state, get_inflight_state, get_inflight_record, record_result, get_result, list_undelivered_results, list_replay_ready_results, count_undelivered_results ... | public methods: init, record_inflight, update_inflight_state, get_inflight_state, get_inflight_record, record_result, get_result, list_undelivered_results, list_replay_ready_results, count_undelivered_results ... |

## Runtime And Data Flow
- Internal dependencies: `ae.ha.fencing`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/unit/test_gateway_service_fencing.py`
- `tests/unit/test_gateway_spool.py`
- `tests/unit/test_transport_config.py`
