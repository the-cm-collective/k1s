# TENETS

## One-paragraph pitch

K1S is a small, readable, Kubernetes-like app orchestration engine built for distributed compute across messy reality: NAT/CGNAT, roaming nodes, intermittent links, heterogeneous sites, and federated clouds. The goal is to make secure private clouds and community-owned “crowd compute” feasible without surrendering control to a single vendor or central authority—privacy-first, consent-first, and understandable end-to-end (spec → reconcile → runtime → networking), with a minimal footprint that can run a control plane cheaply while compute lives wherever you can host it.

* * *

## Goals

- **Make distributed compute normal.** Treat geography, churn, and partial connectivity as first-class constraints—not edge cases.
- **Federate clouds without surrendering control.** Stitch together home labs, on-prem, bare metal, VMs, and cloud instances with clear policy and predictable routing.
- **Private clouds that are actually private.** Secure-by-default identity and connectivity so “internal” isn’t “accidentally public.”
- **Crowd-sourced compute with consent.** Enable opt-in, community-owned capacity pools where participants set policy, retain agency, and can leave at any time.
- **Keep it understandable.** If you can’t trace the control flow end-to-end, the system is too big.

* * *

## Tenets

- **Digital rights first.** Privacy isn’t a feature. It’s the baseline.
- **Freedom over convenience.** Run it on your hardware, under your keys, with your rules—without asking permission.
- **Consent is non-negotiable.** Crowd compute must be explicit, transparent, and revocable.
- **Secure by default, not by blog post.** Minimal exposed surface, strong identity, sane defaults.
- **Decentralize power, not just topology.** Federation should reduce technical and political single points of control.
- **Composability over monoculture.** Pluggable runtimes, multiple ingress models, adaptable networking—because the world isn’t one cluster.
- **Small footprint, big reach.** Cheap control plane; distributed compute where it makes sense.
- **No botnets. No stealth. No excuses.** Distributed compute is only legitimate when it’s voluntary, attributable, and governed by participants.

* * *

## Non-goals

- **Not a Kubernetes clone.** K1S borrows the mental model, not the full surface area.
- **Not a managed service.** The project’s priority is self-hosting, portability, and user control.
- **Not “growth at all costs.”** No dark patterns, no telemetry-by-default, no extractive defaults.
- **Not a surveillance platform.** No features that depend on collecting more data than you need to run workloads.
- **Not a vehicle for involuntary compute.** If a design enables stealthy “borrowed compute,” it’s a design bug.
- **Not enterprise compliance theater.** Security and reliability matter; checkbox theater is not the target.

