# ae.security

- Source folder: `src/ae/security`
- Last reviewed: 2026-05-13

## System Summary
Local CA issuance/revocation helpers and signed token issue/verify helpers.

## Package Initializer
mTLS and join-token helpers for node bootstrap security. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| ca.py | [docs/ca.md](docs/ca.md) | Lightweight CA helper for agent mTLS bootstrap using openssl. | _ensure_root, ensure_ca, issue_cert, _record_issue, record_used_token |
| tokens.py | [docs/tokens.md](docs/tokens.md) | Join-token utilities for agent bootstrap. | _b64, _db64, issue_token, verify_token |

## Environment And Operational Touchpoints
`AE_AGENT_JOIN_SECRET`

## Cross-Package Dependencies


## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in direct modules during static review.

## Related Tests
- No direct package-level test reference found by static search.
