# Service

- Source: `ingress/service.py`
- Last reviewed: 2026-05-13
- Size: 274 lines

## Purpose
Ingress orchestration service to manage Caddy configs per manifest.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| IngressResult | 20 | No class docstring. |  |
| IngressService | 26 | Coordinates ingress manager operations based on manifest state. | public methods: apply, remove, reload |

## Runtime And Data Flow
- Internal dependencies: `.caddy`, `.tls_sync`, `ae.controller.spec`, `ae.controller.state`, `ae.observability.http_api`
- Environment inputs: `AE_DEV_LOCAL`, `AE_INGRESS_RELOAD_DELAY_MS`, `AE_LABS`, `AE_TLS_DIR`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
- Line 163: `# fallback to in-memory when no store is present`

## Related Tests And Docs
- `tests/integration/test_reconcile_flow.py`
- `tests/unit/test_ingress.py`
- `tests/unit/test_projection.py`
- `tests/unit/test_tls_sync.py`
