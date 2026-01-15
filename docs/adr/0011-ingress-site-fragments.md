# ADR 0011 — Caddy ingress via per‑app site fragments

Date: 2026-01-14
Status: Accepted
Owners: ingress/controller

## Context
- k1s needs a simple HTTP/TLS ingress path for demos and small deployments.
- We want minimal dependencies and a clear reload model.
- Ingress should prefer stable Service VIPs over per‑replica endpoints.

## Decision
- Render one Caddy site fragment per App with `spec.ingress`.
- Reload Caddy after updates, with optional container exec reload in dev stacks.
- Prefer Service VIP upstreams when available; fall back to hostPorts for single‑node cases.

## Options Considered
1) **Custom proxy**: higher maintenance cost; rejected.
2) **NGINX/Traefik embedding**: more complex configuration surface; rejected.
3) **Caddy site fragments (chosen)**: small config surface and easy TLS in dev.

## Consequences
- Ingress logic stays in the controller; Caddy is treated as a reloadable proxy.
- TLS handling supports direct PEM paths or k8s‑style Secret material.
- Active health checks are optional and gated by Caddy support.

## Action Plan
1) Keep Caddy integration knobs stable (`AE_CADDY_*`).
2) Maintain consistent upstream ordering for rollout biasing.
3) Document TLS resolution and overlay VIP routing clearly.
