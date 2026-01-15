# ADR 0008 — Gitea Actions runner compatibility (Node 16)

Date: 2026-01-14
Status: Accepted (temporary)
Owners: ci

## Context
- The current Gitea runner image ships Node 16, but upstream GitHub Actions now require Node 20.
- Several workflows broke when actions upgraded their runtime requirements.

## Decision
- Pin `actions/checkout` to v3 and `actions/setup-python` to v4 for Gitea runners until the base image is upgraded.
- Add workflow safeguards: shared concurrency groups for port 8445 usage, non-root tool installs to `$HOME/.local/bin`, and optional Gitea CA trust wiring.

## Consequences
- We must revisit and unpin actions once the runner image moves to Node 20+.
- The temporary pins keep push-triggered workflows green without widening the security surface.
