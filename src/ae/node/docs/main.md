# Main Entrypoint

- Source: `node/__main__.py`
- Last reviewed: 2026-05-13
- Size: 7 lines

## Purpose
Support module within Node agent HTTP server, runtime proxying, heartbeat loop, local network helper, and Rosenpass/WireGuard support.

## Public Surface And Internal Entry Points
No top-level classes or functions are defined; behavior is import/delegation or package setup.

## Runtime And Data Flow
- Internal dependencies: `ae.node.server`

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback markers were found in this module during static review.

## Related Tests And Docs
- No direct test reference found by path/import search; rely on package-level and integration coverage.
