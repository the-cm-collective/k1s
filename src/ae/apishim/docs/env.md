# Env

- Source: `apishim/env.py`
- Last reviewed: 2026-05-13
- Size: 234 lines

## Purpose
Packaged API shim environment and TLS helper.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| ensure_local_apishim_env | 14 | function | Write API shim token env and best-effort local TLS material. |
| _read_env_file | 89 | function | Internal helper. |
| _read_value | 102 | function | Internal helper. |
| _resolve_secret | 112 | function | Internal helper. |
| _tls_material_missing | 126 | function | Internal helper. |
| _generate_tls | 130 | function | Internal helper. |

## Runtime And Data Flow
- External libraries: `shutil`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 1: `"""Packaged API shim environment and TLS helper."""`
- Line 25: `"""Write API shim token env and best-effort local TLS material.`

## Related Tests And Docs
- `tests/conftest.py`
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/integration/_profile_smoke.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_docs_export_and_links.py`
- `tests/integration/test_etcd_state_adapter.py`
- `tests/integration/test_profile_entrypoints.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/integration/test_service_vip_routing.py`
- `tests/integration/test_strict_cri_profile_smoke.py`
- `tests/test_apishim_env.py`
- `tests/unit/test_apishim_patch.py`
- `tests/unit/test_bench_script_contracts.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_cli_auth.py`
