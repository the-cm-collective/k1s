# ae.network

- Source folder: `src/ae/network`
- Last reviewed: 2026-05-13

## System Summary
Service VIP allocation, pod CIDR allocation, bridge/iptables/overlay network providers, and overlay health checks.

## Subsystems
- Service VIP and endpoint management.
- Pod CIDR allocation and bridge/iptables/overlay provider implementations.
- Overlay/WireGuard health reporting.

## Package Initializer
Network helpers for Service VIP and multi-node plumbing. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| overlay_health.py | [docs/overlay-health.md](docs/overlay-health.md) | Best-effort overlay/WireGuard health probe. | wireguard_health, _rosenpass_status |
| pod_cidr.py | [docs/pod-cidr.md](docs/pod-cidr.md) | Pod CIDR allocator for multi-node overlay networking. | PodCIDRAllocator |
| provider.py | [docs/provider.md](docs/provider.md) | Provider interface for Service/overlay networking. | NetworkProvider, NullProvider |
| provider_docker.py | [docs/provider-docker.md](docs/provider-docker.md) | Docker bridge provider for Service VIPs. | DockerBridgeProvider |
| provider_iptables.py | [docs/provider-iptables.md](docs/provider-iptables.md) | Iptables-based Service VIP provider (single-node, CRI-friendly). | IptablesProvider |
| provider_overlay.py | [docs/provider-overlay.md](docs/provider-overlay.md) | Overlay-friendly Service provider. | OverlayProvider |
| service_controller.py | [docs/service-controller.md](docs/service-controller.md) | Service controller that bridges manifests to the network provider. | ServiceController |

## Environment And Operational Touchpoints
`AE_DOCKER_BIN`, `AE_IPTABLES_BIN`, `AE_POD_CIDR_MASK`, `AE_POD_CIDR_POOL`, `AE_ROSENPASS_DIR`, `AE_ROSENPASS_STATUS_PATH`

## Cross-Package Dependencies
`.provider`, `ae.controller.health`, `ae.controller.spec`, `ae.controller.state`, `ae.network.overlay_health`, `ae.runtime`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/integration/test_overlay_vip.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/unit/test_agent_api.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_network_provider_docker.py`
- `tests/unit/test_pod_cidr_allocator.py`
