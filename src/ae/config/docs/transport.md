# Transport

- Source: `config/transport.py`
- Last reviewed: 2026-05-13
- Size: 153 lines

## Purpose
Transport feature flags and NATS/gateway configuration.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| TransportConfig | 89 | Top-level transport feature flags. | public methods: from_env |
| GatewayJetStreamConfig | 114 | Site Gateway JetStream pull/ack settings (Option A defaults). | public methods: from_env |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _env_int | 23 | function | Internal helper. |
| _normalize_nats_url | 33 | function | Internal helper. |
| ha_mode_enabled | 39 | function | Entrypoint/helper without docstring. |
| desired_js_replicas | 49 | function | Entrypoint/helper without docstring. |
| _parse_nats_endpoint | 55 | function | Internal helper. |
| parse_nats_explicit_port | 65 | function | Return the explicit port from a NATS URL, or None when omitted. |
| check_nats_connectivity | 76 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_cri_runtime_recovery.py`
- `tests/unit/test_dashboard_template_ha.py`
- `tests/unit/test_gateway_service_fencing.py`
- `tests/unit/test_ha_core_drills_script.py`
- `tests/unit/test_ha_edge_transport_script.py`
- `tests/unit/test_ha_transport_upgrade_script.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_netfs_validation_scripts.py`
- `tests/unit/test_route_bundle_sites.py`
- `tests/unit/test_system_ha_dashboard.py`
- `tests/unit/test_transport_authority.py`
- `tests/unit/test_transport_config.py`
- `tests/unit/test_transport_subjects.py`
