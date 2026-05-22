# Edge Local

- Source: `ingress/edge_local.py`
- Last reviewed: 2026-05-13
- Size: 418 lines

## Purpose
Edge-local ingress renderer (Caddyfile) driven by route bundles.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| EdgeLocalIngressConfig | 18 | No class docstring. |  |
| EdgeLocalIngressRenderer | 27 | public methods: apply_bundle | public methods: apply_bundle |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| build_edge_local_renderer | 65 | function | Entrypoint/helper without docstring. |
| render_edge_local_caddy | 99 | function | Entrypoint/helper without docstring. |
| _render_site_block | 180 | function | Internal helper. |
| _render_route_block | 207 | function | Internal helper. |
| _render_ip_filters | 245 | function | Internal helper. |
| _render_timeouts | 261 | function | Internal helper. |
| _extract_max_body_bytes | 281 | function | Internal helper. |
| _header_kv | 289 | function | Internal helper. |
| _upstreams_for_service | 304 | function | Internal helper. |
| _bundle_upstreams_for_service | 341 | function | Internal helper. |
| _dns_upstream_for_service | 375 | function | Internal helper. |
| _dedupe_preserving_order | 393 | function | Internal helper. |
| _coerce_int | 404 | function | Internal helper. |

## Runtime And Data Flow
- Environment inputs: `AE_EDGE_LOCAL_INGRESS_CONFIG_DIR`, `AE_EDGE_LOCAL_INGRESS_CONFIG_FILE`, `AE_EDGE_LOCAL_INGRESS_RELOAD_CMD`, `AE_EDGE_LOCAL_SERVICE_DOMAIN`, `AE_EDGE_LOCAL_SERVICE_PORT_FALLBACK`, `AE_EDGE_LOCAL_UPSTREAM_MODE`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_edge_local_ingress.py`
