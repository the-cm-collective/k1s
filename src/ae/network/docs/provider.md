# Provider

- Source: `network/provider.py`
- Last reviewed: 2026-05-13
- Size: 44 lines

## Purpose
Provider interface for Service/overlay networking.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| NetworkProvider | 8 | Abstracts dataplane operations for Service VIP routing. | public methods: ensure_network, ensure_service, update_service_endpoints, remove_service |
| NullProvider | 26 | No-op provider used as a placeholder before real dataplane wiring. | public methods: ensure_network, ensure_service, update_service_endpoints, remove_service |

## Runtime And Data Flow
- No obvious external side-effect surface in static review.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_overlay_vip.py`
- `tests/integration/test_profile_entrypoints.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_network_provider_docker.py`
