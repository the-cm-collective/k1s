# Logging

- Source: `observability/logging.py`
- Last reviewed: 2026-05-13
- Size: 30 lines

## Purpose
Simple logging setup helpers.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| configure_logging | 13 | function | Configure root logging with a concise timestamped formatter. |

## Runtime And Data Flow
- Environment inputs: `AE_LOG_LEVEL`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_http_api_rbac.py`
- `tests/unit/test_node_server.py`
