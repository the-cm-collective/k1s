# Agent Api

- Source: `controller/agent_api.py`
- Last reviewed: 2026-05-13
- Size: 454 lines

## Purpose
Lightweight controller-side API for node agents (heartbeats, node info).

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _json | 47 | function | Internal helper. |
| _serialize_nodes | 56 | function | Internal helper. |
| make_handler | 84 | function | Factory so tests can spin a server without global state. |
| _node_site | 274 | function | Internal helper. |
| _node_is_hub | 284 | function | Internal helper. |
| _wg_role | 297 | function | Internal helper. |
| _peer_endpoint | 306 | function | Internal helper. |
| _build_overlay_payload | 318 | function | Internal helper. |
| start_agent_api | 418 | function | Start the agent API server in a daemon thread. |

## Runtime And Data Flow
- Internal dependencies: `ae.accelerators`, `ae.controller.state`, `ae.network.pod_cidr`, `ae.security.ca`, `ae.security.tokens`
- Environment inputs: `AE_AGENT_TOKEN_EXPIRES`, `AE_OVERLAY_HUB_ENDPOINT`, `AE_OVERLAY_HUB_SITE`, `AE_OVERLAY_PERSISTENT_KEEPALIVE`, `AE_SITE_ID`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
- Line 306: `def _peer_endpoint(labels: dict | None, fallback: str | None) -> str | None:`
- Line 307: `if fallback:`

## Related Tests And Docs
- `tests/unit/test_agent_api.py`
