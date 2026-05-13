# ae.node

- Source folder: `src/ae/node`
- Last reviewed: 2026-05-13

## System Summary
Node agent HTTP server, runtime proxying, heartbeat loop, local network helper, and Rosenpass/WireGuard support.

## Package Initializer
Node agent package.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| __main__.py | [docs/main.md](docs/main.md) | Support module within Node agent HTTP server, runtime proxying, heartbeat loop, local network helper, and... |  |
| net_helper.py | [docs/net-helper.md](docs/net-helper.md) | Best-effort network helper for node agents (bridge/NAT/WireGuard). | _run, _ensure_iptables_rule, _bridge_addr_for_cidr, _bridge_exists, ensure_pod_bridge |
| rosenpass.py | [docs/rosenpass.md](docs/rosenpass.md) | Managed Rosenpass integration for WireGuard PSK rotation (best-effort). | PeerConfig, WireGuardConfig, RosenpassConfig, RosenpassNodeConfig, KeyMaterial |
| server.py | [docs/server.md](docs/server.md) | HTTP agent exposing runtime operations and optional controller heartbeats. | AgentHandler |

## Environment And Operational Touchpoints
`AE_AGENT_CLIENT_CA`, `AE_AGENT_CONFIGURE_OVERLAY`, `AE_AGENT_ENDPOINT`, `AE_AGENT_FENCE_DB`, `AE_AGENT_HEARTBEAT_SECONDS`, `AE_AGENT_REQUIRE_CLIENT_CERT`, `AE_AGENT_SERVICE_PROXY`, `AE_AGENT_SERVICE_PROXY_DB`, `AE_AGENT_SERVICE_PROXY_INTERVAL`, `AE_AGENT_SERVICE_PROXY_TOKEN`, `AE_AGENT_SERVICE_PROXY_URL`, `AE_AGENT_TLS_CERT`, `AE_AGENT_TLS_KEY`, `AE_AGENT_TOKEN`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_API_ADMIN_TOKEN`, `AE_API_READ_TOKEN`, `AE_CONTROLLER_TLS_CA`, `AE_CONTROLLER_TLS_CERT`, `AE_CONTROLLER_TLS_KEY`, `AE_CONTROLLER_URL`, `AE_ENABLE_NETFS`, `AE_FABRIC_WG_ENABLE`, `AE_HA_MODE`, `AE_IPTABLES_BIN`, `AE_NODE_ID`, `AE_NODE_LABELS`, `AE_NODE_NAME`, `AE_NODE_PORT`, `AE_POD_BRIDGE`, `AE_POD_CIDR`, `AE_ROSENPASS_COMMAND`, `AE_ROSENPASS_CONFIG`, `AE_ROSENPASS_DIR`, `AE_ROSENPASS_ENABLED`, `AE_ROSENPASS_INTERFACE`, `AE_ROSENPASS_LISTEN`, `AE_ROSENPASS_LOG_LEVEL`, `AE_ROSENPASS_PEER_REFRESH_SEC`, `AE_ROSENPASS_PRIVATE_KEY`, `AE_ROSENPASS_PUBKEY`, `AE_ROSENPASS_PUBLIC_KEY`, `AE_RP_PUBKEY`, `AE_RUNTIME_BACKEND`, `AE_SERVICE_IP_POOL`, `AE_WG_ADDRESS`, `AE_WG_CONFIG`, `AE_WG_DEBUG_DUMP`, `AE_WG_INTERFACE`, `AE_WG_LISTEN_PORT`, `AE_WG_MTU`, `AE_WG_PRIVATE_KEY`, `AE_WG_PUBKEY`, `AE_WG_PUBLIC_KEY`, `AE_WG_TABLE`

## Cross-Package Dependencies
`ae.accelerators`, `ae.apishim.store`, `ae.config.transport`, `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.network`, `ae.node.net_helper`, `ae.node.rosenpass`, `ae.node.server`, `ae.observability.http_api`, `ae.runtime`, `ae.storage`, `ae.storage.netfs`, `ae.storage.state`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/integration/test_agent_pvc_pending.py`
- `tests/integration/test_agent_service_proxy.py`
- `tests/integration/test_agent_streaming_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/unit/test_apishim_remote_pods.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_node_net_helper.py`
- `tests/unit/test_node_server.py`
- `tests/unit/test_node_server_fabric.py`
- `tests/unit/test_rosenpass_config.py`
