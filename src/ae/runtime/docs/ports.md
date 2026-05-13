# Ports

- Source: `runtime/ports.py`
- Last reviewed: 2026-05-13
- Size: 75 lines

## Purpose
Helpers for allocating host ports for local runtimes.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _port_candidates | 14 | function | Yield preferred port followed by +/- offsets up to `span`. |
| _port_is_free | 23 | function | Return True if the port can be bound on the host. |
| choose_host_port | 43 | function | Pick an available host port, preferring `preferred` when possible. |
| is_port_free | 73 | function | Expose the low-level check for unit tests. |

## Runtime And Data Flow
- Side-effect surfaces: network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/integration/_profile_smoke.py`
- `tests/integration/test_agent_service_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_overlay_vip.py`
- `tests/integration/test_profile_entrypoints.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/integration/test_strict_cri_profile_smoke.py`
- `tests/unit/test_apishim_ha_store.py`
- `tests/unit/test_apishim_ha_workload_authority.py`
- `tests/unit/test_apishim_patch.py`
- `tests/unit/test_apishim_portforward.py`
- `tests/unit/test_apishim_scopes.py`
- `tests/unit/test_bench_script_contracts.py`
- `tests/unit/test_containerd_runtime.py`
