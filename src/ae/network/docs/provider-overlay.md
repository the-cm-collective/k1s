# Provider Overlay

- Source: `network/provider_overlay.py`
- Last reviewed: 2026-05-13
- Size: 240 lines

## Purpose
Overlay-friendly Service provider.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| OverlayProvider | 28 | public methods: ensure_network, ensure_service, update_service_endpoints, remove_service, overlay_health | public methods: ensure_network, ensure_service, update_service_endpoints, remove_service, overlay_health |

## Runtime And Data Flow
- Internal dependencies: `.provider`, `ae.controller.state`, `ae.network.overlay_health`
- Environment inputs: `AE_DOCKER_BIN`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_overlay_vip.py`
