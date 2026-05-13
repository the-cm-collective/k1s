# Edge Core Proxy

- Source: `ingress/edge_core_proxy.py`
- Last reviewed: 2026-05-13
- Size: 1322 lines

## Purpose
Core-proxy config renderer for Envoy + Rathole.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| EdgeCoreProxyConfig | 41 | No class docstring. |  |
| EdgeCoreProxyRenderer | 75 | public methods: render | public methods: render |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| build_core_proxy_config | 158 | function | Entrypoint/helper without docstring. |
| render_core_proxy_bootstrap_from_env | 283 | function | Entrypoint/helper without docstring. |
| _parse_upstream | 296 | function | Internal helper. |
| _core_proxy_services | 313 | function | Internal helper. |
| _reserved_controlplane_hosts | 319 | function | Internal helper. |
| _append_controlplane_public_routes | 333 | function | Internal helper. |
| _build_routes_and_clusters | 463 | function | Internal helper. |
| _route_path_entries | 690 | function | Internal helper. |
| _edge_route_spec | 709 | function | Internal helper. |
| _edge_policy_spec | 715 | function | Internal helper. |
| _route_redirect_https | 724 | function | Internal helper. |
| _core_local_cluster_name | 739 | function | Internal helper. |
| _resolve_core_local_endpoints | 747 | function | Internal helper. |
| _service_port_from_store | 789 | function | Internal helper. |
| _split_host_port | 809 | function | Internal helper. |
| _collect_downstream_tls | 820 | function | Internal helper. |
| _tls_secret_name | 883 | function | Internal helper. |
| _tls_mode | 895 | function | Internal helper. |
| _ensure_fallback_tls | 901 | function | Internal helper. |
| _public_endpoint | 963 | function | Internal helper. |
| _policy_for_route | 997 | function | Internal helper. |
| _policy_route_options | 1014 | function | Internal helper. |
| _normalize_lb_strategy | 1071 | function | Internal helper. |
| _route_lb_policy | 1080 | function | Internal helper. |
| _route_sticky_cookie | 1087 | function | Internal helper. |
| _cluster_name_with_lb_policy | 1097 | function | Internal helper. |
| _select_forward_auth_url | 1107 | function | Internal helper. |
| _policy_forward_auth_url | 1128 | function | Internal helper. |
| _policy_rate_limit | 1140 | function | Internal helper. |
| _normalize_forward_auth_url | 1166 | function | Internal helper. |
| _forward_auth_cluster | 1184 | function | Internal helper. |
| _forward_auth_ext_authz_config | 1200 | function | Internal helper. |
| _header_add | 1232 | function | Internal helper. |
| _header_remove | 1241 | function | Internal helper. |
| _coerce_int | 1250 | function | Internal helper. |
| _coerce_bool | 1259 | function | Internal helper. |
| _run_reload | 1277 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`, `ae.controller.state`, `ae.ingress.edge_docs`, `ae.ingress.envoy_core_proxy`, `ae.ingress.rathole`, `ae.ingress.tls_sync`
- External libraries: `fcntl`, `shutil`
- Environment inputs: `AE_CONTROLPLANE_APISHIM_TLS`, `AE_CONTROLPLANE_APISHIM_UPSTREAM`, `AE_CONTROLPLANE_API_APISHIM_TLS`, `AE_CONTROLPLANE_API_APISHIM_UPSTREAM`, `AE_CONTROLPLANE_API_CONTROLLER_UPSTREAM`, `AE_CONTROLPLANE_API_HOST`, `AE_CONTROLPLANE_CONTROLLER_UPSTREAM`, `AE_CONTROLPLANE_DASH_HOST`, `AE_CONTROLPLANE_DOCS_HOST`, `AE_CONTROLPLANE_PROXY_ADDR`, `AE_CONTROLPLANE_PROXY_PORT`, `AE_CONTROLPLANE_PUBLIC_ENABLE`, `AE_EDGE_INGRESS_CONFIG_DIR`, `AE_EDGE_INGRESS_ENVOY_CONFIG`, `AE_EDGE_INGRESS_HTTP_PORT`, `AE_EDGE_INGRESS_LOCAL_ADDR`, `AE_EDGE_INGRESS_RATHOLE_RELOAD`, `AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD`, `AE_EDGE_INGRESS_RELOAD_CMD`, `AE_EDGE_INGRESS_RELOAD_LOCK`, `AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX`, `AE_EDGE_INGRESS_TLS_DEFAULT_SECRET`, `AE_EDGE_INGRESS_TLS_FALLBACK`, `AE_EDGE_INGRESS_TLS_FALLBACK_CN`, `AE_EDGE_INGRESS_TLS_FALLBACK_DAYS`, `AE_EDGE_INGRESS_TLS_PORT`, `AE_RATHOLE_BIND_ADDR`, `AE_RATHOLE_CLIENT_DIR`, `AE_RATHOLE_DEFAULT_TOKEN`, `AE_RATHOLE_SERVER_ADDR`, ...
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 859: `fallback = _ensure_fallback_tls(`
- Line 865: `if fallback:`
- Line 866: `crt_path, key_path = fallback`
- Line 909: `crt = root / "envoy-fallback.crt"`
- Line 910: `key = root / "envoy-fallback.key"`

## Related Tests And Docs
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/unit/test_envoy_core_local_ingress.py`
