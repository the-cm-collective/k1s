# ae.ingress

- Source folder: `src/ae/ingress`
- Last reviewed: 2026-05-13

## System Summary
Ingress rendering and synchronization for Caddy, Envoy, edge-local/core-proxy routing, TLS, and tunnel helpers.

## Subsystems
- Caddy site-fragment generation and reload orchestration.
- Envoy control-plane/core-proxy rendering.
- Edge-local routing, core-proxy policy rendering, TLS secret resolution, and Rathole tunnel config.

## Package Initializer
Ingress configuration writers and helpers. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| caddy.py | [docs/caddy.md](docs/caddy.md) | Caddy ingress templating and reload helpers. | CaddyIngressManager |
| edge_core_proxy.py | [docs/edge-core-proxy.md](docs/edge-core-proxy.md) | Core-proxy config renderer for Envoy + Rathole. | EdgeCoreProxyConfig, EdgeCoreProxyRenderer |
| edge_docs.py | [docs/edge-docs.md](docs/edge-docs.md) | Helpers for normalizing edge ingress route/policy documents. | _ensure_metadata, normalize_route_doc, normalize_policy_doc |
| edge_local.py | [docs/edge-local.md](docs/edge-local.md) | Edge-local ingress renderer (Caddyfile) driven by route bundles. | EdgeLocalIngressConfig, EdgeLocalIngressRenderer |
| envoy_control_plane.py | [docs/envoy-control-plane.md](docs/envoy-control-plane.md) | Control-plane Envoy renderer for docs/dashboard browser auth. | ControlPlaneEnvoyConfig |
| envoy_core_proxy.py | [docs/envoy-core-proxy.md](docs/envoy-core-proxy.md) | Envoy core ingress config renderer for edge core-proxy mode. | CoreProxyRoute, CoreProxyCluster, DownstreamTlsCert, EnvoyRenderConfig |
| rathole.py | [docs/rathole.md](docs/rathole.md) | Rathole config renderer (core-proxy tunnel). | RatholeServerService, RatholeServerConfig, RatholeClientService, RatholeClientConfig |
| service.py | [docs/service.md](docs/service.md) | Ingress orchestration service to manage Caddy configs per manifest. | IngressResult, IngressService |
| tls_sync.py | [docs/tls-sync.md](docs/tls-sync.md) | TLS secret sync helper. | TlsSecretResolver |

## Environment And Operational Touchpoints
`AE_CADDY_ACTIVE_HEALTH`, `AE_CADDY_HOST_ALIAS`, `AE_CONTROLPLANE_APISHIM_TLS`, `AE_CONTROLPLANE_APISHIM_UPSTREAM`, `AE_CONTROLPLANE_API_APISHIM_TLS`, `AE_CONTROLPLANE_API_APISHIM_UPSTREAM`, `AE_CONTROLPLANE_API_CONTROLLER_UPSTREAM`, `AE_CONTROLPLANE_API_HOST`, `AE_CONTROLPLANE_CONTROLLER_UPSTREAM`, `AE_CONTROLPLANE_DASH_HOST`, `AE_CONTROLPLANE_DOCS_HOST`, `AE_CONTROLPLANE_PROXY_ADDR`, `AE_CONTROLPLANE_PROXY_PORT`, `AE_CONTROLPLANE_PUBLIC_ENABLE`, `AE_CRI_ENDPOINT`, `AE_DEV_LOCAL`, `AE_EDGE_INGRESS_CONFIG_DIR`, `AE_EDGE_INGRESS_ENVOY_CONFIG`, `AE_EDGE_INGRESS_HTTP_PORT`, `AE_EDGE_INGRESS_LOCAL_ADDR`, `AE_EDGE_INGRESS_RATHOLE_RELOAD`, `AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD`, `AE_EDGE_INGRESS_RELOAD_CMD`, `AE_EDGE_INGRESS_RELOAD_LOCK`, `AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX`, `AE_EDGE_INGRESS_TLS_DEFAULT_SECRET`, `AE_EDGE_INGRESS_TLS_FALLBACK`, `AE_EDGE_INGRESS_TLS_FALLBACK_CN`, `AE_EDGE_INGRESS_TLS_FALLBACK_DAYS`, `AE_EDGE_INGRESS_TLS_PORT`, `AE_EDGE_LOCAL_INGRESS_CONFIG_DIR`, `AE_EDGE_LOCAL_INGRESS_CONFIG_FILE`, `AE_EDGE_LOCAL_INGRESS_RELOAD_CMD`, `AE_EDGE_LOCAL_SERVICE_DOMAIN`, `AE_EDGE_LOCAL_SERVICE_PORT_FALLBACK`, `AE_EDGE_LOCAL_UPSTREAM_MODE`, `AE_INGRESS_RELOAD_DELAY_MS`, `AE_LABS`, `AE_RATHOLE_BIND_ADDR`, `AE_RATHOLE_CLIENT_DIR`, `AE_RATHOLE_DEFAULT_TOKEN`, `AE_RATHOLE_SERVER_ADDR`, `AE_RATHOLE_SERVER_CONFIG`, `AE_RUNTIME_BACKEND`, `AE_TLS_DIR`, `CRICTL_BIN`

## Cross-Package Dependencies
`.caddy`, `.tls_sync`, `ae.controller.spec`, `ae.controller.state`, `ae.ingress.edge_docs`, `ae.ingress.envoy_core_proxy`, `ae.ingress.rathole`, `ae.ingress.tls_sync`, `ae.observability.http_api`, `ae.resources`

## Maintenance Notes
- `caddy.py` line 106: `# CRI fallback`
- `edge_core_proxy.py` line 859: `fallback = _ensure_fallback_tls(`
- `edge_core_proxy.py` line 865: `if fallback:`
- `edge_core_proxy.py` line 866: `crt_path, key_path = fallback`
- `edge_core_proxy.py` line 909: `crt = root / "envoy-fallback.crt"`
- `edge_core_proxy.py` line 910: `key = root / "envoy-fallback.key"`
- `edge_local.py` line 337: `fallback = _dns_upstream_for_service(service_ref, namespace, config)`
- `edge_local.py` line 338: `return [fallback] if fallback else []`
- `service.py` line 37: `# Back-compat in-memory state when no store is available`
- `service.py` line 78: `# Create a temporary copy of the manifest spec with cert/key paths filled`
- `service.py` line 163: `# fallback to in-memory when no store is present`

## Related Tests
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/unit/test_edge_local_ingress.py`
- `tests/unit/test_envoy_control_plane.py`
- `tests/unit/test_envoy_core_local_ingress.py`
- `tests/unit/test_envoy_render_yaml.py`
- `tests/unit/test_ingress.py`
- `tests/unit/test_projection.py`
- `tests/unit/test_rathole_render.py`
- `tests/unit/test_tls_sync.py`
