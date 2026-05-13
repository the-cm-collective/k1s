# Work Watchdog

- Source: `controller/work_watchdog.py`
- Last reviewed: 2026-05-13
- Size: 86 lines

## Purpose
Work ledger watchdogs for rescheduling stuck dispatches.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| WorkWatchdogConfig | 18 | No class docstring. |  |
| WorkWatchdog | 24 | public methods: start, stop, run_once | public methods: start, stop, run_once |

## Runtime And Data Flow
- Internal dependencies: `ae.controller.state`, `ae.ha.fencing`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_work_ledger.py`
