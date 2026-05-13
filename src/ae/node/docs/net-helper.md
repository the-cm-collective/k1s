# Net Helper

- Source: `node/net_helper.py`
- Last reviewed: 2026-05-13
- Size: 216 lines

## Purpose
Best-effort network helper for node agents (bridge/NAT/WireGuard).

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _run | 31 | function | Internal helper. |
| _ensure_iptables_rule | 35 | function | Internal helper. |
| _bridge_addr_for_cidr | 64 | function | Internal helper. |
| _bridge_exists | 73 | function | Internal helper. |
| ensure_pod_bridge | 83 | function | Create and configure a pod bridge with the given CIDR. |
| apply_wireguard | 121 | function | Apply a WireGuard config via wg-quick style stdin. |

## Runtime And Data Flow
- External libraries: `shutil`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_node_net_helper.py`
