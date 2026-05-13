# Rathole

- Source: `ingress/rathole.py`
- Last reviewed: 2026-05-13
- Size: 88 lines

## Purpose
Rathole config renderer (core-proxy tunnel).

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| RatholeServerService | 10 | No class docstring. |  |
| RatholeServerConfig | 16 | No class docstring. |  |
| RatholeClientService | 23 | No class docstring. |  |
| RatholeClientConfig | 29 | No class docstring. |  |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| render_rathole_server | 35 | function | Entrypoint/helper without docstring. |
| render_rathole_client | 50 | function | Entrypoint/helper without docstring. |
| write_rathole_server | 65 | function | Entrypoint/helper without docstring. |
| write_rathole_client | 72 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/unit/test_cri_stack.py`
- `tests/unit/test_envoy_core_local_ingress.py`
- `tests/unit/test_microk8s_helm_contracts.py`
- `tests/unit/test_microk8s_stack_bundle.py`
- `tests/unit/test_rathole_render.py`
