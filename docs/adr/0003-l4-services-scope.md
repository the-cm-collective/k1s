# ADR 0003 — L4 services via external proxy, not built‑in

Date: 2025-11-07
Status: Accepted
Owners: networking/controller

## Context
- Caddy provides L7 HTTP/TLS routing but does not offer general TCP/UDP load‑balancing.
- Building an L4 load‑balancer in the controller would add scope and operational complexity.
- k1s already exposes per‑replica host ports that can be consumed by an external proxy.

## Decision
- Do not implement a built‑in L4 load‑balancer.
- Recommend an external L4 proxy (HAProxy/Traefik TCP/NGINX Stream) for multi‑replica TCP/UDP services.

## Options Considered
1) **Controller‑managed L4 LB**: large scope increase; rejected.
2) **Single‑replica fixed port only**: too limiting; rejected.
3) **External L4 proxy (chosen)**: flexible and keeps core small.

## Consequences
- Docs must include a supported external‑proxy pattern.
- Planner hints should warn about ephemeral host ports for multi‑replica apps without ingress.

## Action Plan
1) Keep the HAProxy example config + update scripts in `scripts/dev/`.
2) Add planner warnings when L4 usage is likely to be unstable.
