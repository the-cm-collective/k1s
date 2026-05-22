# Command Args

- Source: `runtime/command_args.py`
- Last reviewed: 2026-05-13
- Size: 36 lines

## Purpose
Kubernetes command/args translation helpers for runtime adapters.

## Public Surface And Internal Entry Points
### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| kubernetes_command_parts | 9 | function | Return OCI entrypoint and command arguments for Kubernetes command/args. |
| _items | 28 | function | Internal helper. |

## Runtime And Data Flow
- No obvious external side-effect surface in static review.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_runtime_command_args.py`
