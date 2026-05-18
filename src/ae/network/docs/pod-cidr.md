# Pod Cidr

- Source: `network/pod_cidr.py`
- Last reviewed: 2026-05-13
- Size: 74 lines

## Purpose
Pod CIDR allocator for multi-node overlay networking.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| PodCIDRAllocator | 16 | Stateful allocator backed by the nodes table. | public methods: from_env, allocate |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.state`
- Environment inputs: `AE_POD_CIDR_MASK`, `AE_POD_CIDR_POOL`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/unit/test_agent_api.py`
- `tests/unit/test_apishim_remote_pods.py`
- `tests/unit/test_etcd_state_maintenance.py`
- `tests/unit/test_ha_core_node_smoke_script.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_pod_cidr_allocator.py`
- `tests/unit/test_state_nodes.py`
