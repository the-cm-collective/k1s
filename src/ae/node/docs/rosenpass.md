# Rosenpass

- Source: `node/rosenpass.py`
- Last reviewed: 2026-05-13
- Size: 1034 lines

## Purpose
Managed Rosenpass integration for WireGuard PSK rotation (best-effort).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| PeerConfig | 25 | No class docstring. |  |
| WireGuardConfig | 36 | No class docstring. |  |
| RosenpassConfig | 47 | No class docstring. |  |
| RosenpassNodeConfig | 56 | No class docstring. |  |
| KeyMaterial | 65 | No class docstring. |  |
| RosenpassRuntime | 73 | No class docstring. |  |
| RosenpassPeerRefresher | 596 | public methods: start, stop | public methods: start, stop |
| RosenpassSupervisor | 812 | public methods: start, stop | public methods: start, stop |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _expand_path | 81 | function | Internal helper. |
| _coerce_list | 92 | function | Internal helper. |
| _coerce_int | 102 | function | Internal helper. |
| _safe_role | 111 | function | Internal helper. |
| _safe_interface | 120 | function | Internal helper. |
| _normalize_verbosity | 127 | function | Internal helper. |
| _write_private_file | 143 | function | Internal helper. |
| _read_text | 152 | function | Internal helper. |
| _read_bytes | 159 | function | Internal helper. |
| _b64encode | 167 | function | Internal helper. |
| _decode_b64 | 171 | function | Internal helper. |
| _sanitize_name | 178 | function | Internal helper. |
| _peer_has_rosenpass_keys | 183 | function | Internal helper. |
| _write_status_file | 187 | function | Internal helper. |
| _maybe_dump_wg_config | 195 | function | Internal helper. |
| _derive_address_from_cidr | 206 | function | Internal helper. |
| _normalize_wg_address | 219 | function | Internal helper. |
| load_config | 230 | function | Entrypoint/helper without docstring. |
| load_env_config | 238 | function | Entrypoint/helper without docstring. |
| _parse_config | 262 | function | Internal helper. |
| ensure_wg_keys | 327 | function | Entrypoint/helper without docstring. |
| ensure_rosenpass_keys | 356 | function | Entrypoint/helper without docstring. |
| render_wireguard_config | 443 | function | Entrypoint/helper without docstring. |
| render_rosenpass_stub | 467 | function | Entrypoint/helper without docstring. |
| _materialize_peer_keys | 504 | function | Internal helper. |
| _peer_signature | 572 | function | Internal helper. |
| _format_command | 708 | function | Internal helper. |
| build_command | 723 | function | Entrypoint/helper without docstring. |
| fetch_peers_from_controller | 741 | function | Entrypoint/helper without docstring. |
| _bootstrap_heartbeat | 788 | function | Internal helper. |
| prepare_rosenpass | 912 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.node.net_helper`
- External libraries: `requests`, `shutil`, `yaml`
- Environment inputs: `AE_ROSENPASS_COMMAND`, `AE_ROSENPASS_INTERFACE`, `AE_ROSENPASS_LISTEN`, `AE_ROSENPASS_LOG_LEVEL`, `AE_ROSENPASS_PRIVATE_KEY`, `AE_ROSENPASS_PUBLIC_KEY`, `AE_WG_ADDRESS`, `AE_WG_DEBUG_DUMP`, `AE_WG_INTERFACE`, `AE_WG_LISTEN_PORT`, `AE_WG_MTU`, `AE_WG_PRIVATE_KEY`, `AE_WG_PUBLIC_KEY`, `AE_WG_TABLE`
- Side-effect surfaces: filesystem/state, network/API, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_rosenpass_config.py`
