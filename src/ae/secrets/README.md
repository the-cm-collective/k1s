# ae.secrets

- Source folder: `src/ae/secrets`
- Last reviewed: 2026-05-13

## System Summary
Secret reference resolution and SOPS/age integration for environment and file projections.

## Package Initializer
Secrets package exports. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| manager.py | [docs/manager.md](docs/manager.md) | Secret management helpers powered by SOPS/age. | SecretManager |

## Environment And Operational Touchpoints
`AE_ALLOW_PLAINTEXT_SECRETS`, `AE_SOPS_BIN`, `SOPS_AGE_KEY_FILE`

## Cross-Package Dependencies
`ae.controller.spec`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/integration/test_reconcile_flow.py`
- `tests/unit/test_rollout_hooks.py`
- `tests/unit/test_secrets.py`
