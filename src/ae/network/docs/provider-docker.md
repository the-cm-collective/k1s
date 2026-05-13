# Provider Docker

- Source: `network/provider_docker.py`
- Last reviewed: 2026-05-13
- Size: 231 lines

## Purpose
Docker bridge provider for Service VIPs.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| DockerBridgeProvider | 20 | Ensure a bridge network exists and allocate ClusterIP addresses. | public methods: ensure_network, ensure_service, update_service_endpoints, remove_service |

## Runtime And Data Flow
- Internal dependencies: `.provider`, `ae.controller.state`
- Environment inputs: `AE_DOCKER_BIN`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_network_provider_docker.py`
