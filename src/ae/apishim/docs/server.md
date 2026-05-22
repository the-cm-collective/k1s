# Server

- Source: `apishim/server.py`
- Last reviewed: 2026-05-13
- Size: 12954 lines

## Purpose
HTTP server implementing a Kubernetes-compatible API for the shim.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| Principal | 2236 | No class docstring. |  |
| ControllerPodRecord | 2245 | No class docstring. |  |
| PodTarget | 2258 | No class docstring. |  |
| ShimHandler | 2271 | public methods: rehydrate_sa_tokens, do_GET, do_POST, do_PUT, do_PATCH, do_DELETE | public methods: rehydrate_sa_tokens, do_GET, do_POST, do_PUT, do_PATCH, do_DELETE |
| ShimServer | 12793 | 2 internal method(s) | 2 internal method(s) |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _json | 94 | function | Internal helper. |
| _spdy_debug_line | 98 | function | Internal helper. |
| _exec_status_obj | 110 | function | Internal helper. |
| _read_json | 132 | function | Internal helper. |
| _ns_name | 152 | function | Internal helper. |
| _app_name | 178 | function | Internal helper. |
| _rule_matches | 184 | function | Internal helper. |
| _json_pointer_tokens | 196 | function | Internal helper. |
| _json_pointer_get | 204 | function | Internal helper. |
| _json_pointer_set | 217 | function | Internal helper. |
| _json_pointer_remove | 246 | function | Internal helper. |
| _apply_json_patch | 264 | function | Internal helper. |
| _list_item_key | 281 | function | Internal helper. |
| _split_selector_terms | 288 | function | Internal helper. |
| _split_selector_values | 312 | function | Internal helper. |
| _label_selector_match | 316 | function | Internal helper. |
| _field_selector_match | 367 | function | Internal helper. |
| _selector_values_from_query | 403 | function | Internal helper. |
| _matches_selectors | 409 | function | Internal helper. |
| _filter_k8s_items | 432 | function | Internal helper. |
| _extract_field_paths | 438 | function | Internal helper. |
| _fieldsV1_to_paths | 459 | function | Internal helper. |
| _paths_to_fieldsV1 | 476 | function | Internal helper. |
| _managed_path_map | 492 | function | Internal helper. |
| _managed_conflict | 505 | function | Internal helper. |
| _update_managed_fields | 520 | function | Internal helper. |
| _ensure_managed_fields_entry | 569 | function | Internal helper. |
| _inject_sa_projection | 595 | function | Internal helper. |
| _swagger_doc | 648 | function | Internal helper. |
| _openapi_v3_stub | 2223 | function | Internal helper. |
| _kind | 11422 | function | Internal helper. |
| _api_version | 11434 | function | Internal helper. |
| _set_secret_type | 11441 | function | Internal helper. |
| _secret_type_from_meta | 11454 | function | Internal helper. |
| _service_account_spec_payload | 11465 | function | Internal helper. |
| _to_obj | 11475 | function | Internal helper. |
| _to_deployment | 11500 | function | Internal helper. |
| _synthesize_deploy_status | 11527 | function | Internal helper. |
| _to_statefulset | 11552 | function | Internal helper. |
| _to_daemonset | 11579 | function | Internal helper. |
| _replicaset_name | 11614 | function | Internal helper. |
| _replicaset_labels | 11622 | function | Internal helper. |
| _replicaset_from_deployment | 11632 | function | Internal helper. |
| _to_replicaset | 11686 | function | Internal helper. |
| _to_job | 11709 | function | Internal helper. |
| _to_cronjob | 11732 | function | Internal helper. |
| _to_scale | 11751 | function | Internal helper. |
| _to_hpa | 11765 | function | Internal helper. |
| _to_event | 11814 | function | Internal helper. |
| _to_stored_event | 11840 | function | Internal helper. |
| _ingress_vip | 11868 | function | Best-effort: use first backend service to derive VIP/clusterIP. |
| _to_ingress | 11898 | function | Internal helper. |
| _list_with_rv | 11927 | function | Internal helper. |
| _to_crd | 11958 | function | Internal helper. |
| _apps_ns_name | 11972 | function | Internal helper. |
| _net_ns_name | 12012 | function | Internal helper. |
| _gv_ns_name | 12022 | function | Internal helper. |
| _batch_ns_name | 12038 | function | Internal helper. |
| _gv_cluster_name | 12060 | function | Internal helper. |
| _to_generic | 12068 | function | Internal helper. |
| _spec_payload | 12106 | function | Internal helper. |
| _render_custom_resource | 12121 | function | Internal helper. |
| _parse_custom_resource_path | 12138 | function | Internal helper. |
| _stable_uid | 12157 | function | Internal helper. |
| _normalize_metadata | 12162 | function | Internal helper. |
| _merge_list | 12173 | function | Strategic-ish merge for lists of maps keyed by a stable identifier. |
| _merge_dict | 12208 | function | Internal helper. |
| _valid_name | 12223 | function | Internal helper. |
| _resolve_create_name | 12231 | function | Internal helper. |
| _service_selector | 12261 | function | Internal helper. |
| _pod_template_labels | 12283 | function | Internal helper. |
| _selector_matches | 12293 | function | Internal helper. |
| _resolve_service_target | 12299 | function | Internal helper. |
| _service_target | 12333 | function | Internal helper. |
| _service_app_name | 12351 | function | Internal helper. |
| _provider_cluster_ip | 12358 | function | Fetch cluster IP allocated by the network provider (if recorded in controller state). |
| _provider_ports | 12372 | function | Fetch provider-recorded port info (including nodePort) for a service, keyed by port name/number. |
| _provider_vip | 12388 | function | Return overlay/proxy VIP if recorded by the network provider. |
| _merge_provider_service | 12405 | function | Augment service spec/status with provider allocations (clusterIP/nodePort). |
| _node_zone_for_ip | 12451 | function | Best-effort mapping from pod IP to node name/zone using node podCIDR labels. |
| _alloc_cluster_ip | 12474 | function | Deterministically allocate a ClusterIP in 10.96.0.0/16 avoiding collisions. |
| _alloc_nodeport | 12491 | function | Internal helper. |
| _service_lb_status | 12502 | function | Ensure loadBalancer status is present for LB/NodePort services. |
| _pick_endpoint_ip | 12539 | function | Choose a ready endpoint IP if available; fall back to first. |
| _endpoints_for_service | 12556 | function | Internal helper. |
| _endpointslice_for_service | 12600 | function | Project a single EndpointSlice per Service using controller endpoints. |
| _node_obj | 12666 | function | Internal helper. |
| _runtime_from_env_base | 12688 | function | Internal helper. |
| _runtime_from_env | 12703 | function | Internal helper. |
| _pod_obj | 12713 | function | Internal helper. |
| _wrap_store_errors | 12891 | function | Internal helper. |
| run_server | 12919 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `.adapter`, `.ha_store`, `.store`, `ae`, `ae.controller.spec`, `ae.controller.state`, `ae.runtime`, `ae.storage.controller`
- External libraries: `io`, `shutil`, `yaml`, `zlib`
- Environment inputs: `AE_AGENT_URL`, `AE_APISHIM_ADAPTER`, `AE_APISHIM_AGENT_URL`, `AE_APISHIM_ALLOW_ANON`, `AE_APISHIM_APP_ADMISSION`, `AE_APISHIM_CRI_PORTFORWARD`, `AE_APISHIM_CRI_PORTFORWARD_FORCE`, `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_APISHIM_ENABLE`, `AE_APISHIM_EXEC_TOKEN`, `AE_APISHIM_HA_CRD_REFRESH_SEC`, `AE_APISHIM_MINT_TOKEN`, `AE_APISHIM_PATCH_DEBUG`, `AE_APISHIM_PF_DEBUG`, `AE_APISHIM_POD_STATE_CHECK`, `AE_APISHIM_POD_WATCH_CHECK`, `AE_APISHIM_POD_WATCH_TTL_SECONDS`, `AE_APISHIM_PORTFORWARD_TOKEN`, `AE_APISHIM_RBAC`, `AE_APISHIM_RBAC_EVAL`, `AE_APISHIM_READ_TOKEN`, `AE_APISHIM_RUNTIME`, `AE_APISHIM_SA_TOKEN_TTL`, `AE_APISHIM_SESSION_SECRET`, `AE_APISHIM_SESSION_TTL`, `AE_APISHIM_SESSION_TTL_MAX`, `AE_APISHIM_SOT`, `AE_APISHIM_SPDY_DEBUG`, `AE_APISHIM_SPDY_LOG`, ...
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
- Line 3674: `# fallback to static if no rules matched`
- Line 7273: `# fallback: single target_ip if map empty`

## Related Tests And Docs
- `tests/integration/test_apishim_agent_streaming.py`
- `tests/unit/test_apishim_hpa.py`
