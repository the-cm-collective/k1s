# Http Api

- Source: `observability/http_api.py`
- Last reviewed: 2026-05-13
- Size: 5919 lines

## Purpose
Lightweight HTTP API for metrics, status, events, and previews.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| _ApiHandler | 1721 | public methods: send_response, handle_one_request, end_headers, do_OPTIONS, do_GET, do_POST, log_message | public methods: send_response, handle_one_request, end_headers, do_OPTIONS, do_GET, do_POST, log_message |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _read_env_file_var | 84 | function | Internal helper. |
| _resolve_apishim_env_file | 103 | function | Internal helper. |
| _resolve_apishim_verify | 123 | function | Internal helper. |
| _resolve_apishim_server | 155 | function | Internal helper. |
| _resolve_apishim_admin_token | 172 | function | Internal helper. |
| _resolve_apishim_store_config | 185 | function | Internal helper. |
| _describe_apishim_target | 202 | function | Internal helper. |
| record_outbox_publish | 207 | function | Entrypoint/helper without docstring. |
| record_site_seen | 215 | function | Entrypoint/helper without docstring. |
| record_gateway_identity | 225 | function | Entrypoint/helper without docstring. |
| record_gateway_metrics | 246 | function | Entrypoint/helper without docstring. |
| record_route_bundle_apply | 274 | function | Entrypoint/helper without docstring. |
| record_route_bundle_publish_state | 297 | function | Entrypoint/helper without docstring. |
| record_ha_fence_event | 333 | function | Entrypoint/helper without docstring. |
| record_hpa_activity | 367 | function | Entrypoint/helper without docstring. |
| record_heartbeat_write | 397 | function | Entrypoint/helper without docstring. |
| record_etcd_maintenance_run | 404 | function | Entrypoint/helper without docstring. |
| record_js_stream_stats | 411 | function | Entrypoint/helper without docstring. |
| record_js_consumer_stats | 427 | function | Entrypoint/helper without docstring. |
| _port_available | 464 | function | Internal helper. |
| _pick_free_port | 479 | function | Internal helper. |
| _labs_block_app | 487 | function | Internal helper. |
| _labs_unblock_app | 497 | function | Internal helper. |
| _labs_is_blocked | 503 | function | Internal helper. |
| _helm_demo_status | 518 | function | Internal helper. |
| _helm_demo_start | 551 | function | Internal helper. |
| _session_pids | 676 | function | Internal helper. |
| _descendant_pids | 711 | function | Internal helper. |
| _pid_exists | 770 | function | Internal helper. |
| _wait_pids_exit | 790 | function | Internal helper. |
| _signal_session | 801 | function | Internal helper. |
| _wait_session_exit | 818 | function | Internal helper. |
| _helm_demo_stop | 834 | function | Internal helper. |
| set_reconcile_metrics | 899 | function | Entrypoint/helper without docstring. |
| record_app_reconcile | 905 | function | Record per-app reconcile duration and rollout operation counters. |
| set_app_crashloop | 917 | function | Mark an app as in crashloop for a short TTL so metrics/UI can reflect it. |
| record_hook_observation | 927 | function | Entrypoint/helper without docstring. |
| record_canary_weight | 936 | function | Entrypoint/helper without docstring. |
| increment_canary_step | 943 | function | Entrypoint/helper without docstring. |
| record_probe_backoff | 950 | function | Entrypoint/helper without docstring. |
| _truthy_flag | 957 | function | Internal helper. |
| _split_csv | 961 | function | Internal helper. |
| _as_float | 965 | function | Internal helper. |
| _as_int | 974 | function | Internal helper. |
| _as_datetime | 981 | function | Internal helper. |
| _as_iso8601 | 1003 | function | Internal helper. |
| _authority_presence_stale_after_seconds | 1010 | function | Internal helper. |
| _transport_site_join_key | 1017 | function | Internal helper. |
| _build_transport_snapshot | 1021 | function | Internal helper. |
| _build_authority_snapshot | 1294 | function | Internal helper. |
| _build_ha_snapshot | 1392 | function | Internal helper. |
| _dashboard_node_site_id | 1611 | function | Internal helper. |
| _dashboard_node_value | 1628 | function | Internal helper. |
| _dashboard_layout_mode | 1640 | function | Internal helper. |
| _dashboard_bootstrap_token | 1678 | function | Return a read-capable token for simple local demo/dev dashboards. |
| start_http_api | 5848 | function | Start the HTTP API on the given port. |
| _prom_escape_label_value | 5915 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae`, `ae.accelerators`, `ae.apishim`, `ae.apishim.store`, `ae.controller.authority`, `ae.controller.spec`, `ae.controller.state`, `ae.ingress.tls_sync`, `ae.k8s.exporter`, `ae.observability.metrics`, `ae.resources`
- External libraries: `errno`, `requests`, `shutil`, `socketserver`, `yaml`
- Environment inputs: `AE_APISHIM_BASE`, `AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_APISHIM_ENV_FILE`, `AE_APISHIM_PUBLIC_BASE`, `AE_APISHIM_SERVER`, `AE_APISHIM_SESSION_SECRET`, `AE_APISHIM_SESSION_TTL`, `AE_APISHIM_SESSION_TTL_MAX`, `AE_APISHIM_TLS_CA`, `AE_APISHIM_TLS_CA_CERT`, `AE_APISHIM_TOKEN`, `AE_API_ADMIN_SCOPE`, `AE_API_ADMIN_TOKEN`, `AE_API_DEV_EXPORT`, `AE_API_MUTATIONS`, `AE_API_RBAC`, `AE_API_READ_SCOPE`, `AE_API_READ_TOKEN`, `AE_API_SCALER_TOKEN`, `AE_API_TOKEN_WARN_HOURS`, `AE_CONTROLLER_ADVERTISE_ADDR`, `AE_CONTROLLER_ID`, `AE_DASHBOARD_BOOTSTRAP_TOKEN`, `AE_DEMO_MODE`, `AE_DISABLE_INGRESS`, `AE_ENABLE_SERVICE_PROXY`, `AE_ETCD_ENDPOINTS`, ...
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
- Line 1874: `"apply handler failed source=%s app=%s via legacy fallback",`
- Line 3169: `# Prefer tracked labs apps that match the session suffix; fallback to echo-<sess>`
- Line 3187: `# Final fallback: delete base echo if present (covers non-session applies)`
- Line 3581: `# Fallback to direct URL if all overrides fail`
- Line 3622: `# Set canary weight on existing deployment manifest when possible; fallback to curated example`
- Line 3680: `# Fallback to curated example`

## Related Tests And Docs
- `tests/unit/test_helm_demo_stop.py`
- `tests/unit/test_http_api_apishim_verify.py`
- `tests/unit/test_http_api_rbac.py`
- `tests/unit/test_http_api_status_detail.py`
- `tests/unit/test_http_api_version.py`
- `tests/unit/test_k8s_preview_api.py`
- `tests/unit/test_labs_ingress_check.py`
- `tests/unit/test_labs_reset_apishim.py`
- `tests/unit/test_metrics_per_app.py`
- `tests/unit/test_system_ha_dashboard.py`
