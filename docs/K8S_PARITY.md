K8s Parity and Alignment (MVP)

Status summary
- Deployment-like rollouts: ready/progressing/degraded with health-gated cutover ✓
- Ingress/TLS (Caddy) with ready-only upstreams ✓
- Probes: HTTP and TCP with initialDelay/timeout; windowing for success/failure thresholds ✓
- Resources: CPU/memory limits mapped to runtime ✓
- Security: runAsUser/runAsGroup/readOnlyRootFilesystem/cap drop ✓
- Services: single replica host port; multi-replica via ingress LB (HTTP) ◻︎
- API writes: CLI-only; HTTP API read-only ◻︎

Checklist
- Use stable APIs and portable features where analogous:
  - Readiness/liveness probes present (HTTP/TCP).
  - Resources.limits present; requests optional.
  - Non-root security context available and wired.
  - Graceful termination exposed via terminationGracePeriodSeconds.
  - Ingress flips only after ≥1 ready replica.
  - Storage: hostPath and PV-lite; stateful demo available.

Verification notes
- Docker and Podman paths validated by unit/integration tests and demos.
- Echo variants exercise readiness, ingress, storage.
- Dashboard provides live logs and status snapshots.

Roadmap
- Add exec probe (optional).
- Token-gated mutate endpoints for remote CLI against controller.
- Document multi-replica non-HTTP “service” patterns.
Examples
- TCP readiness probe (spec):
  - readiness: { tcpSocket: { port: 8080 }, successThreshold: 2, failureThreshold: 2 }
- SecuritySpec:
  - security: { runAsUser: 1000, runAsGroup: 1000, readOnlyRootFilesystem: true, dropCapabilities: ["NET_RAW"] }

Demo manifests
- specs/examples/echo-sec.yaml: non-root + read-only root filesystem + HTTP readiness + ingress.
  - Apply: `python -m ae.cli apply -f specs/examples/echo-sec.yaml`
- specs/examples/echo-tcp.yaml: TCP readiness with thresholds + ingress.
  - Apply: `python -m ae.cli apply -f specs/examples/echo-tcp.yaml`
