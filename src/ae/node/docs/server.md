# Server

- Source: `node/server.py`
- Last reviewed: 2026-05-13
- Size: 1305 lines

## Purpose
HTTP agent exposing runtime operations and optional controller heartbeats.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| AgentHandler | 233 | public methods: log_message, do_POST, do_GET | public methods: log_message, do_POST, do_GET |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _is_client_disconnect | 29 | function | Internal helper. |
| _json_response | 37 | function | Internal helper. |
| _parse_manifest | 57 | function | Internal helper. |
| _pod_state_from_container_record | 61 | function | Internal helper. |
| _duplicate_runtime_result | 91 | function | Internal helper. |
| _ha_mode_enabled | 163 | function | Internal helper. |
| _optional_bool_env | 167 | function | Internal helper. |
| _runtime_prefers_pod_ip_portforward | 179 | function | Internal helper. |
| _agent_fence_db_path | 187 | function | Internal helper. |
| _fence_surface | 194 | function | Internal helper. |
| _build_volume_manager | 200 | function | Internal helper. |
| _result_to_dict | 779 | function | Internal helper. |
| _parse_labels | 810 | function | Internal helper. |
| _start_heartbeat_loop | 825 | function | Internal helper. |
| _start_service_proxy_loop | 883 | function | Internal helper. |
| serve | 1013 | function | Entrypoint/helper without docstring. |
| main | 1126 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.accelerators`, `ae.apishim.store`, `ae.config.transport`, `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.network`, `ae.node.net_helper`, `ae.node.rosenpass`, `ae.observability.http_api`, `ae.runtime`, `ae.storage`, `ae.storage.netfs`, `ae.storage.state`
- External libraries: `requests`
- Environment inputs: `AE_AGENT_CLIENT_CA`, `AE_AGENT_CONFIGURE_OVERLAY`, `AE_AGENT_ENDPOINT`, `AE_AGENT_FENCE_DB`, `AE_AGENT_HEARTBEAT_SECONDS`, `AE_AGENT_REQUIRE_CLIENT_CERT`, `AE_AGENT_SERVICE_PROXY`, `AE_AGENT_SERVICE_PROXY_DB`, `AE_AGENT_SERVICE_PROXY_INTERVAL`, `AE_AGENT_SERVICE_PROXY_TOKEN`, `AE_AGENT_SERVICE_PROXY_URL`, `AE_AGENT_TLS_CERT`, `AE_AGENT_TLS_KEY`, `AE_AGENT_TOKEN`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_API_ADMIN_TOKEN`, `AE_API_READ_TOKEN`, `AE_CONTROLLER_TLS_CA`, `AE_CONTROLLER_TLS_CERT`, `AE_CONTROLLER_TLS_KEY`, `AE_CONTROLLER_URL`, `AE_ENABLE_NETFS`, `AE_FABRIC_WG_ENABLE`, `AE_HA_MODE`, `AE_IPTABLES_BIN`, `AE_NODE_ID`, `AE_NODE_LABELS`, `AE_NODE_NAME`, `AE_NODE_PORT`, ...
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_agent_pvc_pending.py`
- `tests/integration/test_agent_service_proxy.py`
- `tests/integration/test_agent_streaming_proxy.py`
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/unit/test_node_server.py`
- `tests/unit/test_node_server_fabric.py`
