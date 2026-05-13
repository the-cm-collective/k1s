# Ops

- Source: `ha/ops.py`
- Last reviewed: 2026-05-13
- Size: 1252 lines

## Purpose
Provides classes EtcdLeaderRecord, EtcdMemberAddResult, EtcdRestoreMemberSpec, EtcdRestoreMemberPlan, EtcdQuorumRestorePlan and functions split_csv, ha_core_missing_env, is_loopback_host, parse_nats_url, tcp_connectable within High-availability support code for authority operations, fencing decisions, dashboard probes, and operational helpers.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| EtcdLeaderRecord | 38 | No class docstring. |  |
| EtcdMemberAddResult | 47 | No class docstring. |  |
| EtcdRestoreMemberSpec | 58 | No class docstring. |  |
| EtcdRestoreMemberPlan | 66 | No class docstring. |  |
| EtcdQuorumRestorePlan | 76 | No class docstring. |  |
| BuildInfoRecord | 84 | No class docstring. |  |
| HaCoreNodeTarget | 92 | No class docstring. |  |
| NatsHubNodeTarget | 99 | No class docstring. |  |
| NatsEdgeSiteTarget | 105 | No class docstring. |  |
| NatsHubMonitorRecord | 111 | No class docstring. |  |
| NatsEdgeMonitorRecord | 133 | No class docstring. |  |
| EdgeGatewayStatusRecord | 144 | No class docstring. |  |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| split_csv | 153 | function | Entrypoint/helper without docstring. |
| ha_core_missing_env | 159 | function | Entrypoint/helper without docstring. |
| is_loopback_host | 164 | function | Entrypoint/helper without docstring. |
| parse_nats_url | 170 | function | Entrypoint/helper without docstring. |
| tcp_connectable | 182 | function | Entrypoint/helper without docstring. |
| _http_json | 190 | function | Internal helper. |
| etcd_endpoint_healthy | 216 | function | Entrypoint/helper without docstring. |
| healthy_etcd_endpoints | 230 | function | Entrypoint/helper without docstring. |
| leader_key | 239 | function | Entrypoint/helper without docstring. |
| parse_etcd_leader_response | 244 | function | Entrypoint/helper without docstring. |
| read_etcd_leader | 260 | function | Entrypoint/helper without docstring. |
| parse_prometheus_metric_value | 284 | function | Entrypoint/helper without docstring. |
| collect_prometheus_metric_values | 310 | function | Entrypoint/helper without docstring. |
| _parse_prometheus_labels | 330 | function | Internal helper. |
| _build_local_etcdctl_prefix | 342 | function | Internal helper. |
| build_local_etcdctl_command | 366 | function | Entrypoint/helper without docstring. |
| build_local_etcdctl_recovery_command | 419 | function | Entrypoint/helper without docstring. |
| detect_container_cli | 471 | function | Entrypoint/helper without docstring. |
| resolve_etcdctl_runner | 481 | function | Entrypoint/helper without docstring. |
| detect_container_cli_or_die | 495 | function | Entrypoint/helper without docstring. |
| build_container_etcdctl_command | 502 | function | Entrypoint/helper without docstring. |
| required_parent_mounts | 525 | function | Entrypoint/helper without docstring. |
| parse_etcd_member_add_output | 543 | function | Entrypoint/helper without docstring. |
| derive_client_url | 586 | function | Entrypoint/helper without docstring. |
| build_quorum_restore_plan | 603 | function | Entrypoint/helper without docstring. |
| format_quorum_restore_plan | 655 | function | Entrypoint/helper without docstring. |
| parse_ha_core_node_target | 680 | function | Entrypoint/helper without docstring. |
| parse_nats_hub_node_target | 698 | function | Entrypoint/helper without docstring. |
| parse_nats_edge_site_target | 710 | function | Entrypoint/helper without docstring. |
| fetch_nats_monitor_json | 722 | function | Entrypoint/helper without docstring. |
| build_nats_hub_monitor_record | 731 | function | Entrypoint/helper without docstring. |
| build_nats_edge_monitor_record | 820 | function | Entrypoint/helper without docstring. |
| fetch_nats_hub_monitor_record | 842 | function | Entrypoint/helper without docstring. |
| fetch_nats_edge_monitor_record | 864 | function | Entrypoint/helper without docstring. |
| evaluate_nats_hub_cluster | 880 | function | Entrypoint/helper without docstring. |
| _leader_record_for_stream | 949 | function | Internal helper. |
| _leader_record_for_consumer | 958 | function | Internal helper. |
| _unique_leader_name | 969 | function | Internal helper. |
| _hub_record_by_identity | 976 | function | Internal helper. |
| evaluate_nats_edge_site | 992 | function | Entrypoint/helper without docstring. |
| nats_build_key | 1008 | function | Entrypoint/helper without docstring. |
| collect_site_gateway_status | 1012 | function | Entrypoint/helper without docstring. |
| _clean_str | 1058 | function | Internal helper. |
| _nested_get | 1062 | function | Internal helper. |
| _cluster_name_from_varz | 1071 | function | Internal helper. |
| _jetstream_domain | 1082 | function | Internal helper. |
| _cluster_replica_total | 1093 | function | Internal helper. |
| _cluster_offline_names | 1115 | function | Internal helper. |
| _leaf_count | 1131 | function | Internal helper. |
| _iter_js_streams | 1144 | function | Internal helper. |
| _iter_stream_consumers | 1161 | function | Internal helper. |
| fetch_http_text | 1170 | function | Entrypoint/helper without docstring. |
| fetch_build_info | 1175 | function | Entrypoint/helper without docstring. |
| subprocess_run | 1185 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- External libraries: `shutil`
- Environment inputs: `AE_APISHIM_CA`, `AE_APISHIM_CA_BUNDLE`, `AE_APISHIM_TLS_CA`, `AE_CONTAINER_CLI`
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/unit/test_bench_script_contracts.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_cri_bootstrap_scripts.py`
- `tests/unit/test_ha_dashboard.py`
- `tests/unit/test_ha_ops.py`
- `tests/unit/test_host_a_gpu_guest_script.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_microk8s_helm_contracts.py`
- `tests/unit/test_multinode_qemu_script.py`
- `tests/unit/test_nats_route_bundle_permissions.py`
- `tests/unit/test_nix_dev_env.py`
- `tests/unit/test_nixos_bridge_helper.py`
