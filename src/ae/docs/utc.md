#  Utc

- Source: `_utc.py`
- Last reviewed: 2026-05-13
- Size: 12 lines

## Purpose
Small UTC datetime compatibility helper used where timezone-aware timestamps are needed.

## Public Surface And Internal Entry Points
No top-level classes or functions are defined; behavior is import/delegation or package setup.

## Runtime And Data Flow
- No obvious external side-effect surface in static review.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 1: `"""UTC compatibility constant for Python versions before 3.11."""`

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
