# ADR 0010 — HTTP API auth and mutation gating

Date: 2026-01-14
Status: Accepted
Owners: controller/observability

## Context
- The controller exposes an HTTP API for status, metrics, and operational actions.
- Read-only access is useful by default; mutations require explicit opt-in.
- We need simple role‑based access without depending on external identity providers.

## Decision
- Keep read‑only endpoints enabled by default.
- Require an explicit mutation flag (`AE_API_MUTATIONS=1`) for write actions.
- Gate access with bearer tokens scoped to roles: READ, SCALER, ADMIN.

## Options Considered
1) **No auth**: unsafe for any non‑local use; rejected.
2) **Always require auth**: adds friction for local dev; rejected.
3) **Read‑only by default + opt‑in mutation tokens (chosen)**: secure and low friction.

## Consequences
- Operators must manage token distribution for remote access.
- Mutating endpoints remain off unless explicitly enabled.
- The API can stay lightweight without external auth dependencies.

## Action Plan
1) Keep role token environment variables documented and stable.
2) Ensure the CLI supports passing tokens consistently.
3) Recommend TLS/ACLs in production deployments.
