# ae.kctl

- Source folder: `src/ae/kctl`
- Last reviewed: 2026-05-13

## System Summary
Small kubectl-like wrapper around k1s native commands and resource references.

## Package Initializer
kubectl-like CLI wrapper for ae (k1s). Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| __main__.py | [docs/main.md](docs/main.md) | kubectl-like CLI for working with the ae/k1s engine. | ParsedRef |

## Environment And Operational Touchpoints
`AE_HA_MODE`

## Cross-Package Dependencies
`ae.cli.__main__`, `ae.controller.__main__`, `ae.controller.reconciler`, `ae.controller.spec`, `ae.controller.state`, `ae.ingress.service`, `ae.observability.logging`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/unit/test_kctl.py`
