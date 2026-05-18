# Tokens

- Source: `security/tokens.py`
- Last reviewed: 2026-05-13
- Size: 59 lines

## Purpose
Join-token utilities for agent bootstrap.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _b64 | 22 | function | Internal helper. |
| _db64 | 26 | function | Internal helper. |
| issue_token | 31 | function | Entrypoint/helper without docstring. |
| verify_token | 42 | function | Entrypoint/helper without docstring. |

## Runtime And Data Flow
- Internal dependencies:
- Environment inputs: `AE_AGENT_JOIN_SECRET`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_cli.py`
- `tests/unit/test_docs_command_alignment.py`
