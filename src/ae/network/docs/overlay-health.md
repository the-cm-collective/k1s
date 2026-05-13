# Overlay Health

- Source: `network/overlay_health.py`
- Last reviewed: 2026-05-13
- Size: 82 lines

## Purpose
Best-effort overlay/WireGuard health probe.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| wireguard_health | 13 | function | Return peer count, latest handshake age (seconds), and MTU for the iface. |
| _rosenpass_status | 70 | function | Internal helper. |

## Runtime And Data Flow
- External libraries: `shutil`
- Environment inputs: `AE_ROSENPASS_DIR`, `AE_ROSENPASS_STATUS_PATH`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_apishim_hpa.py`
