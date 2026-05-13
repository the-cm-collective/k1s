# Main Entrypoint

- Source: `__main__.py`
- Last reviewed: 2026-05-13
- Size: 6 lines

## Purpose
Top-level package launcher that delegates to the controller daemon entrypoint.

## Public Surface And Internal Entry Points
No top-level classes or functions are defined; behavior is import/delegation or package setup.

## Runtime And Data Flow
- Internal dependencies: `ae.cli.__main__`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
