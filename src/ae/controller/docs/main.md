# Main Entrypoint

- Source: `controller/__main__.py`
- Last reviewed: 2026-05-13
- Size: 3164 lines

## Purpose
Controller daemon entry point.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| service_controller_factory | 88 | function | Optional Service VIP controller (disabled by default). |
| _local_node_id | 150 | function | Internal helper. |
| _parse_labels | 154 | function | Internal helper. |
| _register_local_node | 171 | function | Best-effort local node registration for single-controller setups. |
| _truthy_env | 198 | function | Internal helper. |
| _parse_duration_seconds | 203 | function | Internal helper. |
| _parse_site_ids | 217 | function | Internal helper. |
| _bootstrap_jetstream | 224 | function | Internal helper. |
| build_parser | 301 | function | Entrypoint/helper without docstring. |
| _find_manifests | 339 | function | Internal helper. |
| _load_all | 349 | function | Load manifests, preferring one file per app name and returning path for mtime. |
| _import_specs | 369 | function | Internal helper. |
| _reserved_controlplane_hosts_from_env | 382 | function | Internal helper. |
| _iter_yaml_docs | 402 | function | Internal helper. |
| _normalize_metadata | 417 | function | Internal helper. |
| _import_edge_ingress_specs | 431 | function | Internal helper. |
| _store_edge_ingress_route | 451 | function | Internal helper. |
| _store_edge_ingress_policy | 481 | function | Internal helper. |
| _store_site_ingress_endpoint | 504 | function | Internal helper. |
| _reconcile_edge_ingress | 534 | function | Internal helper. |
| _edge_local_policy_unsupported | 628 | function | Internal helper. |
| _edge_ingress_route_spec | 668 | function | Internal helper. |
| _edge_ingress_policy_spec | 675 | function | Internal helper. |
| _lookup_edge_ingress_policy | 684 | function | Internal helper. |
| _collect_core_forward_auth_urls | 702 | function | Internal helper. |
| _core_policy_errors | 729 | function | Internal helper. |
| _policy_auth_mode | 782 | function | Internal helper. |
| _policy_forward_auth_raw | 787 | function | Internal helper. |
| _policy_lb_strategy | 793 | function | Internal helper. |
| _policy_stickiness | 805 | function | Internal helper. |
| _coerce_positive_int | 814 | function | Internal helper. |
| _coerce_bool | 824 | function | Internal helper. |
| _normalize_forward_auth_url | 842 | function | Internal helper. |
| _env_true | 860 | function | Internal helper. |
| _apishim_sot_enabled | 865 | function | Internal helper. |
| _apishim_mirror_enabled | 871 | function | Internal helper. |
| _set_apishim_mirror_mode | 885 | function | Internal helper. |
| _prune_orphan_status | 898 | function | Internal helper. |
| _log_apishim_mirror_stats | 918 | function | Internal helper. |
| _apishim_api_base | 955 | function | Internal helper. |
| _apishim_api_headers | 960 | function | Internal helper. |
| _apishim_api_verify | 972 | function | Internal helper. |
| _apishim_api_get_json | 995 | function | Internal helper. |
| _snapshot_apishim_api_manifests | 1034 | function | Internal helper. |
| _snapshot_apishim_manifests | 1206 | function | Internal helper. |
| _purge_app_from_runtime | 1328 | function | Internal helper. |
| _sync_apishim_registry | 1348 | function | Internal helper. |
| _spec_hash | 1421 | function | Internal helper. |
| _merge_file_and_db_manifests | 1428 | function | Prefer latest DB revision when it differs from on-disk spec, preserving file edits. |
| _make_reconciler | 1493 | function | Internal helper. |
| _make_hpa_sample_reader | 1529 | function | Internal helper. |
| _reconcile_all | 1547 | function | Internal helper. |
| main | 1597 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.apishim.adapter`, `ae.apishim.ha_store`, `ae.apishim.store`, `ae.cli.__main__`, `ae.config.transport`, `ae.controller.agent_api`, `ae.controller.app_ingress`, `ae.controller.authority`, `ae.controller.cronjob_authority`, `ae.controller.hpa_authority`, `ae.controller.reconciler`, `ae.controller.spec`, `ae.controller.state`, `ae.controller.storage_authority`, `ae.controller.work_watchdog`, `ae.ha.dashboard`, `ae.ingress.edge_core_proxy`, `ae.ingress.tls_sync`, `ae.network`, `ae.network.overlay_health`, ...
- External libraries: `errno`, `requests`, `shutil`, `watchdog`, `yaml`
- Environment inputs: `AE_AGENT_API_CLIENT_CA`, `AE_AGENT_API_HOST`, `AE_AGENT_API_PORT`, `AE_AGENT_API_REQUIRE_CLIENT_CERT`, `AE_AGENT_API_TLS_CERT`, `AE_AGENT_API_TLS_KEY`, `AE_AGENT_API_TOKEN`, `AE_AGENT_PORT`, `AE_AGENT_URL`, `AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_APISHIM_MIRROR`, `AE_APISHIM_NAMESPACE`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_SERVER`, `AE_APISHIM_SOT`, `AE_APISHIM_TLS_CA`, `AE_APISHIM_TOKEN`, `AE_APPLY_RECONCILE_BURST`, `AE_APPLY_RECONCILE_DELAY_MS`, `AE_CONTROLPLANE_API_HOST`, `AE_CONTROLPLANE_DASH_HOST`, `AE_CONTROLPLANE_DOCS_HOST`, `AE_CONTROLPLANE_PUBLIC_ENABLE`, `AE_CORE_PROXY_PORT_MAX`, `AE_CORE_PROXY_PORT_MIN`, `AE_CRONJOB_AUTHORITY_INTERVAL_S`, `AE_DOCKER_BIN`, ...
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 1073: `fallback = (`
- Line 1076: `namespaces = [fallback] if fallback else ["demo-helm", "default"]`
- Line 2113: `# Fallback to pod-name matching`
- Line 2178: `# Fallback: run in a matching pod name`
- Line 3031: `observer = None  # fallback to interval polling`

## Related Tests And Docs
- `tests/unit/test_controller_loop.py`
- `tests/unit/test_cronjob_authority_startup.py`
- `tests/unit/test_edge_ingress_validation.py`
- `tests/unit/test_hpa_authority_startup.py`
- `tests/unit/test_storage_authority_startup.py`
- `tests/unit/test_transport_authority.py`
