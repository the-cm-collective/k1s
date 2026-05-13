# Caddy

- Source: `ingress/caddy.py`
- Last reviewed: 2026-05-13
- Size: 306 lines

## Purpose
Caddy ingress templating and reload helpers.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| CaddyIngressManager | 21 | Renders Caddy configuration blocks per application and triggers reloads. | public methods: apply, remove, reload |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`, `ae.resources`
- External libraries: `shutil`
- Environment inputs: `AE_CADDY_ACTIVE_HEALTH`, `AE_CADDY_HOST_ALIAS`, `AE_CRI_ENDPOINT`, `AE_RUNTIME_BACKEND`, `CRICTL_BIN`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 106: `# CRI fallback`

## Related Tests And Docs
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/unit/test_bench_script_contracts.py`
- `tests/unit/test_edge_local_ingress.py`
- `tests/unit/test_ingress.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_mem_aggregate_podman.py`
- `tests/unit/test_nix_dev_env.py`
- `tests/unit/test_projection.py`
- `tests/unit/test_tls_sync.py`
- `tests/unit/test_verify_snapshot.py`
