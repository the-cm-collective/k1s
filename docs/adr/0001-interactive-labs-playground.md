# ADR 0001 — Interactive labs consolidation into Playground

Date: 2025-11-07
Status: Accepted
Owners: docs/observability

## Context
- We need interactive docs that teach workflows without exposing unsafe browser-side mutations.
- The initial proposal covered three paths: read-only stepper verification, a gated demo mutation API, and an embedded terminal.

## Decision
- Ship a single Interactive Lab Playground page as the canonical labs surface.
- Default to read-only verification; allow controlled actions only when `AE_LABS=1` and a bearer token is provided.
- Keep the labs orchestrator dev-only and proxied by Caddy; optional k3d helper remains supported.

## Options Considered
1) Read-only stepper only: safest, but limited.
2) Feature-flagged demo control API: chosen for controlled actions when explicitly enabled.
3) Embedded terminal: deferred due to complexity and security surface.

## Consequences
- Canonical docs live in `docs/playground.md`, `docs/static/labs.js`, and `docs/runbook.md`.
- Mutable actions remain opt-in and token-gated; public docs stay read-only by default.
