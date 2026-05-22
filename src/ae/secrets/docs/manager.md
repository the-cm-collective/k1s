# Manager

- Source: `secrets/manager.py`
- Last reviewed: 2026-05-13
- Size: 125 lines

## Purpose
Secret management helpers powered by SOPS/age.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| SecretManager | 34 | Decrypts sealed secrets and projects them into environment variables. | public methods: load_env |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| resolve_sops_age_key_file | 17 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.spec`
- External libraries: `yaml`
- Environment inputs: `AE_ALLOW_PLAINTEXT_SECRETS`, `AE_SOPS_BIN`, `SOPS_AGE_KEY_FILE`
- Side-effect surfaces: filesystem/state, subprocess/runtime command.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
