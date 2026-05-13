# Main Entrypoint

- Source: `cli/__main__.py`
- Last reviewed: 2026-05-13
- Size: 7186 lines

## Purpose
Command-line interface for the ae orchestrator.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| CLIArgumentParser | 62 | public methods: parse_args | public methods: parse_args |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _ha_mode_enabled | 90 | function | Internal helper. |
| build_parser | 94 | function | Entrypoint/helper without docstring. |
| runtime_factory | 1061 | function | Entrypoint/helper without docstring. |
| health_manager_factory | 1084 | function | Entrypoint/helper without docstring. |
| _registry_config_path | 1088 | function | Internal helper. |
| _registry_load | 1095 | function | Internal helper. |
| _registry_save | 1108 | function | Internal helper. |
| handle_registry | 1118 | function | Entrypoint/helper without docstring. |
| ingress_service_factory | 1384 | function | Entrypoint/helper without docstring. |
| secret_manager_factory | 1418 | function | Entrypoint/helper without docstring. |
| config_manager_factory | 1423 | function | Entrypoint/helper without docstring. |
| registry_auth_factory | 1427 | function | Entrypoint/helper without docstring. |
| format_report | 1431 | function | Entrypoint/helper without docstring. |
| format_status | 1439 | function | Entrypoint/helper without docstring. |
| main | 1454 | function | Entrypoint/helper without docstring. |
| handle_apply | 1580 | function | Entrypoint/helper without docstring. |
| handle_config | 1842 | function | Entrypoint/helper without docstring. |
| handle_secret | 1862 | function | Entrypoint/helper without docstring. |
| _fmt_labels | 1927 | function | Internal helper. |
| _fmt_taints | 1933 | function | Internal helper. |
| _node_status_with_staleness | 1949 | function | Internal helper. |
| _node_json_item | 1962 | function | Internal helper. |
| handle_nodes | 1995 | function | Entrypoint/helper without docstring. |
| handle_delete | 2105 | function | Entrypoint/helper without docstring. |
| handle_scale | 2173 | function | Entrypoint/helper without docstring. |
| handle_version | 2237 | function | Entrypoint/helper without docstring. |
| handle_examples | 2243 | function | Entrypoint/helper without docstring. |
| handle_backup | 2272 | function | Entrypoint/helper without docstring. |
| handle_k8s_report | 2350 | function | Entrypoint/helper without docstring. |
| handle_work | 2672 | function | Entrypoint/helper without docstring. |
| _http_timeout | 2740 | function | Internal helper. |
| _http_get_json | 2751 | function | Internal helper. |
| _http_post_json | 2768 | function | Internal helper. |
| _apishim_base_url | 2786 | function | Internal helper. |
| _apishim_ca_bundle_path | 2795 | function | Internal helper. |
| _apishim_requests_verify | 2806 | function | Internal helper. |
| _apishim_ssl_context | 2813 | function | Internal helper. |
| _cli_labs_mint_fallback_enabled | 2827 | function | Internal helper. |
| _controller_api_candidates | 2832 | function | Internal helper. |
| _mint_session_via_labs | 2841 | function | Internal helper. |
| _resolve_labs_stream_token | 2901 | function | Internal helper. |
| _extract_http_status | 2933 | function | Internal helper. |
| _looks_like_connection_refused | 2945 | function | Internal helper. |
| _is_local_apishim_server | 2950 | function | Internal helper. |
| _print_apishim_connection_refused_hint | 2962 | function | Internal helper. |
| _session_token_expiry | 2969 | function | Internal helper. |
| _apishim_session_cache_path | 2988 | function | Internal helper. |
| _load_apishim_session_cache | 2998 | function | Internal helper. |
| _save_apishim_session_cache | 3015 | function | Internal helper. |
| _apishim_cache_key | 3044 | function | Internal helper. |
| _cached_apishim_session_token | 3048 | function | Internal helper. |
| _cache_apishim_session_token | 3071 | function | Internal helper. |
| _mint_apishim_session_token | 3085 | function | Internal helper. |
| _resolve_apishim_stream_token | 3135 | function | Internal helper. |
| _apishim_scope | 3174 | function | Internal helper. |
| _resolve_app_name | 3181 | function | Internal helper. |
| _display_app_name | 3190 | function | Internal helper. |
| handle_status | 3194 | function | Entrypoint/helper without docstring. |
| handle_rollout | 3436 | function | Entrypoint/helper without docstring. |
| handle_api | 3480 | function | Entrypoint/helper without docstring. |
| _read_env_file_var | 3541 | function | Internal helper. |
| _write_export_lines | 3564 | function | Internal helper. |
| _read_proc_env | 3573 | function | Internal helper. |
| _profile_env_from_state_db | 3593 | function | Internal helper. |
| _preferred_profile_apishim_env | 3608 | function | Internal helper. |
| _profile_private_apishim_env | 3619 | function | Internal helper. |
| _profile_controller_env | 3636 | function | Internal helper. |
| _profile_state_db_from_env | 3648 | function | Internal helper. |
| _profile_name_from_env | 3660 | function | Internal helper. |
| _profile_etcd_defaults | 3672 | function | Internal helper. |
| _running_in_container | 3684 | function | Internal helper. |
| _normalize_upstream_server_for_host | 3694 | function | Internal helper. |
| _latest_profile_apishim_env | 3732 | function | Internal helper. |
| _detect_apishim_env | 3757 | function | Internal helper. |
| handle_auth | 3788 | function | Entrypoint/helper without docstring. |
| handle_tls | 4144 | function | Entrypoint/helper without docstring. |
| handle_logs | 4245 | function | Entrypoint/helper without docstring. |
| _resolve_exec_target | 4310 | function | Internal helper. |
| _resolve_pod_via_apishim | 4339 | function | Internal helper. |
| _exec_over_spdy | 4386 | function | Internal helper. |
| _exec_over_ws | 4689 | function | Internal helper. |
| _parse_pf_mapping | 4969 | function | Internal helper. |
| _portforward_over_ws | 4984 | function | Internal helper. |
| handle_exec | 5215 | function | Entrypoint/helper without docstring. |
| handle_exec_remote | 5435 | function | Entrypoint/helper without docstring. |
| handle_shell | 5465 | function | Entrypoint/helper without docstring. |
| handle_port_forward | 5502 | function | Entrypoint/helper without docstring. |
| _status_to_json | 5619 | function | Internal helper. |
| _parse_since_secs | 5669 | function | Internal helper. |
| _parse_rfc3339_to_epoch | 5694 | function | Internal helper. |
| handle_rollback | 5712 | function | Entrypoint/helper without docstring. |
| handle_revisions | 5739 | function | Entrypoint/helper without docstring. |
| handle_metrics | 5756 | function | Entrypoint/helper without docstring. |
| handle_events | 5804 | function | Entrypoint/helper without docstring. |
| handle_cell | 5837 | function | Entrypoint/helper without docstring. |
| handle_cellset | 5963 | function | Entrypoint/helper without docstring. |
| handle_fabric | 6062 | function | Entrypoint/helper without docstring. |
| handle_services | 6108 | function | Entrypoint/helper without docstring. |
| handle_history | 6146 | function | Entrypoint/helper without docstring. |
| handle_volumes | 6293 | function | Entrypoint/helper without docstring. |
| handle_logs_remote | 6327 | function | Entrypoint/helper without docstring. |
| handle_plan | 6377 | function | Entrypoint/helper without docstring. |
| handle_export_k8s | 6822 | function | Entrypoint/helper without docstring. |
| handle_k8s_check | 6989 | function | Entrypoint/helper without docstring. |
| handle_verify_image | 7086 | function | Verify container image signatures using cosign. |
| handle_certs | 7142 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae`, `ae._utc`, `ae.accelerators`, `ae.config.manager`, `ae.controller.health`, `ae.controller.inference_cell`, `ae.controller.reconciler`, `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.ingress`, `ae.ingress.tls_sync`, `ae.k8s`, `ae.k8s.check`, `ae.k8s.exporter`, `ae.k8s.presets`, `ae.k8s.validate`, `ae.observability`, `ae.observability.logging`, `ae.runtime`, ...
- External libraries: `inspect`, `requests`, `shutil`, `tarfile`, `termios`, `tty`, `urllib3`, `yaml`, `zlib`
- Environment inputs: `AE_ALLOW_PLAINTEXT_SECRETS`, `AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_EXEC_TOKEN`, `AE_APISHIM_INSECURE`, `AE_APISHIM_MINT_TOKEN`, `AE_APISHIM_PORTFORWARD_TOKEN`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_SERVER`, `AE_APISHIM_SESSION_CACHE`, `AE_APISHIM_SESSION_SECRET`, `AE_APISHIM_TLS_CA`, `AE_APISHIM_TOKEN`, `AE_API_ADMIN_TOKEN`, `AE_API_READ_TOKEN`, `AE_API_SCALER_TOKEN`, `AE_API_SERVER`, `AE_CADDY_BIN`, `AE_CADDY_CONTAINER`, `AE_CADDY_FILE`, `AE_CADDY_RELOAD_TIMEOUT`, `AE_CADDY_SITES`, `AE_CLI_HTTP_TIMEOUT`, `AE_CLI_LABS_MINT_FALLBACK`, `AE_CLI_SHARED_GROUP`, `AE_CONTAINER_CLI`, `AE_DISABLE_INGRESS`, `AE_DOCKER_NETWORK`, `AE_ETCD_ENDPOINTS`, `AE_ETCD_PREFIX`, ...
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 221: `help="API shim base URL for SPDY exec (defaults to AE_APISHIM_SERVER when set)",`
- Line 224: `"--ws-fallback",`
- Line 239: `help="API shim base URL for SPDY exec (defaults to AE_APISHIM_SERVER when set)",`
- Line 242: `"--ws-fallback",`
- Line 253: `help="Forward a local TCP port to a pod via the API shim (WebSocket)",`
- Line 275: `help="API shim base URL for WebSocket port-forward (defaults to AE_APISHIM_SERVER when set)",`
- Line 429: `help="Deprecated alias for --pod (filter by pod name)",`
- Line 748: `help="Treat warnings as errors (deprecated; use --policy strict)",`
- Line 918: `auth_mint = auth_sub.add_parser("mint", help="Mint short-lived API shim session tokens")`
- Line 934: `help="Optional TTL in seconds (bounded by shim limits)",`
- Line 939: `help="API shim base URL (defaults to AE_APISHIM_SERVER)",`
- Line 2852: `raise RuntimeError("labs session fallback unavailable: AE_LABS_TOKEN is not set")`
- Line 4004: `"warning: no direct apishim stream token resolved; CLI will rely on AE_LABS_TOKEN session fallback on 401",`
- Line 4598: `cols, rows = shutil.get_terminal_size(fallback=(80, 24))`
- Line 4885: `cols, rows = shutil.get_terminal_size(fallback=(80, 24))`
- Line 5252: `f"fallback={'1' if fallback_used else '0'}",`
- Line 5338: `print(f"{kind} exec got 401; trying labs session token fallback...")`
- Line 5368: `f"{kind} exec failed ({exc}); trying {transport_order[idx + 1]} fallback..."`
- Line 5395: `print("warning: --stdin/--tty are only supported against the API shim (SPDY/WebSocket)")`
- Line 5424: `# Fallback: select a pod by name substring`
- Line 5480: `print("shell requires the API shim; set --apishim or AE_APISHIM_SERVER")`
- Line 5518: `print("port-forward requires the API shim; set --apishim or AE_APISHIM_SERVER")`
- Line 5601: `print("port-forward got 401; trying labs session token fallback...")`
- Line 6229: `# Local store fallback`

## Related Tests And Docs
- `tests/unit/test_backup.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_cli_remote.py`
- `tests/unit/test_cli_split_export.py`
- `tests/unit/test_k8s_check_policy.py`
- `tests/unit/test_plan_validation.py`
- `tests/unit/test_registry_kubesecret.py`
- `tests/unit/test_runtime_factory.py`
- `tests/unit/test_version.py`
