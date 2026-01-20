# Concepts in Practice

Hands-on, chapterized walkthroughs for k1s orchestration concepts, mapped to Kubernetes equivalents.

## Chapters

- [Chapter 01 - Desired State and Reconciliation Loops](concepts-in-practice-01-desired-state-reconciliation.html)
  - Explain how k1s continuously converges actual runtime state to the declared spec, and map that pattern to Kubernetes controllers.
- [Chapter 02 - Declarative Specs and Apply Semantics](concepts-in-practice-02-declarative-apply.html)
  - Show how a declarative spec becomes the single source of truth, and how `apply` merges desired state into the controller's registry.
- [Chapter 03 - Scheduling and Placement (Where Work Runs)](concepts-in-practice-03-scheduling-placement.html)
  - Explain how k1s decides replica placement and how that maps to Kubernetes scheduler behavior.
- [Chapter 04 - Runtime Adapters and Container Execution](concepts-in-practice-04-runtime-adapters.html)
  - Trace how k1s translates a manifest into runtime operations and how adapters make that portable across container engines.
- [Chapter 05 - Ingress and Service Exposure](concepts-in-practice-05-ingress-service-exposure.html)
  - Walk through how k1s exposes services: L4 Service VIPs and L7 ingress via Caddy, then map to k8s Services and Ingress/Gateway.
- [Chapter 06 - Observability: Logs, Metrics, Events](concepts-in-practice-06-observability.html)
  - Teach how to inspect k1s state using metrics snapshots and event streams, and map that to k8s observability patterns.
- [Chapter 07 - Health Probes and Readiness/Liveness](concepts-in-practice-07-health-probes.html)
  - Explain how readiness/liveness/startup probes gate traffic and restarts, and show how k1s evaluates probe state.
- [Chapter 08 - Rollouts, Updates, and Rollbacks](concepts-in-practice-08-rollouts-updates.html)
  - Show how k1s performs controlled updates, tracks revisions, and supports rollbacks, then map to k8s Deployment rollouts.
- [Chapter 09 - Configuration and Secrets Management](concepts-in-practice-09-configuration-secrets.html)
  - Show how k1s loads configs and sealed secrets, projects them into env/files, and maps that to k8s ConfigMaps/Secrets.
- [Chapter 10 - Access and Policy Boundaries](concepts-in-practice-10-access-policy.html)
  - Explain how k1s enforces API access roles, registry credentials, and node join tokens, then map to k8s RBAC and admission controls.
