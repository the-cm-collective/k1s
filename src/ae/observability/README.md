# ae.observability

- Source folder: `src/ae/observability`
- Last reviewed: 2026-05-13

## System Summary
Controller HTTP API, dashboard/static assets, Prometheus metrics snapshotting, and logging setup.

## Subsystems
- Controller-native HTTP API and dashboard resource serving.
- Prometheus metrics aggregation from controller state.
- Integrated HA/system/dashboard snapshots and log streaming.

## Package Initializer
Observability exports for metrics and logging. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| http_api.py | [docs/http-api.md](docs/http-api.md) | Lightweight HTTP API for metrics, status, events, and previews. | _ApiHandler |
| logging.py | [docs/logging.md](docs/logging.md) | Simple logging setup helpers. | configure_logging |
| metrics.py | [docs/metrics.md](docs/metrics.md) | Metrics helpers derived from state store snapshots. | MetricsSnapshot, MetricsService |

## Resource And Generated Subtrees
| Folder | Files | Types | Review policy |
| --- | --- | --- | --- |
| static | 3 | .css:1, .js:2 | Generated/vendor/static/resource subtree; summarized at folder level. |

## Environment And Operational Touchpoints
`AE_APISHIM_BASE`, `AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_APISHIM_ENV_FILE`, `AE_APISHIM_PUBLIC_BASE`, `AE_APISHIM_SERVER`, `AE_APISHIM_SESSION_SECRET`, `AE_APISHIM_SESSION_TTL`, `AE_APISHIM_SESSION_TTL_MAX`, `AE_APISHIM_TLS_CA`, `AE_APISHIM_TLS_CA_CERT`, `AE_APISHIM_TOKEN`, `AE_API_ADMIN_SCOPE`, `AE_API_ADMIN_TOKEN`, `AE_API_DEV_EXPORT`, `AE_API_MUTATIONS`, `AE_API_RBAC`, `AE_API_READ_SCOPE`, `AE_API_READ_TOKEN`, `AE_API_SCALER_TOKEN`, `AE_API_TOKEN_WARN_HOURS`, `AE_CONTROLLER_ADVERTISE_ADDR`, `AE_CONTROLLER_ID`, `AE_DASHBOARD_BOOTSTRAP_TOKEN`, `AE_DEMO_MODE`, `AE_DISABLE_INGRESS`, `AE_ENABLE_SERVICE_PROXY`, `AE_ETCD_ENDPOINTS`, `AE_HA_MODE`, `AE_JS_DOMAIN`, `AE_LABS`, `AE_LABS_CORS_ORIGIN`, `AE_LABS_DOCKER`, `AE_LABS_HELM_CHART`, `AE_LABS_HELM_KEEP`, `AE_LABS_HELM_LOG`, `AE_LABS_HELM_NAMESPACE`, `AE_LABS_HELM_PORT`, `AE_LABS_HELM_RUNTIME`, `AE_LABS_HELM_SERVER`, `AE_LABS_HELM_TOKEN`, `AE_LABS_K3D_AUTOCREATE`, `AE_LABS_K3S`, `AE_LABS_RESET_BLOCK_SECONDS`, `AE_LABS_SESSION_HOSTS`, `AE_LABS_STABLE_SERVICE`, `AE_LABS_TOKEN`, `AE_LOG_LEVEL`, `AE_NODE_LABELS`, `AE_NODE_NOTREADY_AFTER`, `AE_NODE_PROFILE`, `AE_PLAYGROUND`, `AE_PROFILE`, `AE_SERVICE_PROVIDER`, `AE_SITE_ID`, `AE_SITE_NOTREADY_AFTER`, `AE_STATE_DB`, `AE_STORAGE_QUOTAS`, `AE_TLS_DIR`, `AE_TRANSPORT_BACKEND`, `DEV_PROFILE_DIR`, `K3D_HTTP`, `K3D_HTTPS`, `K3D_NAME`

## Cross-Package Dependencies
`ae`, `ae.accelerators`, `ae.apishim`, `ae.apishim.store`, `ae.controller.authority`, `ae.controller.spec`, `ae.controller.state`, `ae.ingress.tls_sync`, `ae.k8s.exporter`, `ae.observability.metrics`, `ae.resources`, `ae.storage.config`

## Maintenance Notes
Detailed markers live in the per-module docs; direct module counts:
- `http_api.py`: 6 marker(s)

## Related Tests
- `tests/integration/test_reconcile_flow.py`
- `tests/unit/test_helm_demo_stop.py`
- `tests/unit/test_http_api_apishim_verify.py`
- `tests/unit/test_http_api_rbac.py`
- `tests/unit/test_http_api_status_detail.py`
- `tests/unit/test_http_api_version.py`
- `tests/unit/test_k8s_preview_api.py`
- `tests/unit/test_labs_ingress_check.py`
- `tests/unit/test_labs_reset_apishim.py`
- `tests/unit/test_metrics_per_app.py`
- `tests/unit/test_metrics_volume_health.py`
- `tests/unit/test_system_ha_dashboard.py`
