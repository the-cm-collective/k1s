K8s Parity and Alignment (MVP)

Status summary
- Deployment-like rollouts: ready/progressing/degraded with health-gated cutover ✓
- Ingress/TLS (Caddy) with ready-only upstreams ✓
- Probes: HTTP and TCP with initialDelay/timeout; windowing for success/failure thresholds ✓
- Resources: CPU/memory limits mapped to runtime ✓
- Security: runAsUser/runAsGroup/readOnlyRootFilesystem/cap drop ✓
- Services: single replica host port; multi-replica via ingress LB (HTTP) ◻︎
- API writes: CLI-only; HTTP API read-only ◻︎

Security & Policies
- Seccomp/AppArmor: container `securityContext.seccompProfile` supported (RuntimeDefault/Localhost/Unconfined); AppArmor profile via Pod template annotation `container.apparmor.security.beta.kubernetes.io/<container>`.

Example (security snippet)
```
spec:
  security:
    runAsUser: 1000
    readOnlyRootFilesystem: true
    dropCapabilities: ["NET_RAW"]
    seccompProfileType: RuntimeDefault  # or Localhost
    seccompLocalhostProfile: profiles/echo.json  # when type=Localhost
    apparmorProfile: localhost/echo-profile     # or runtime/default, unconfined
```

Service Types
- NodePort/LoadBalancer: exporter supports `service.type` and per-port `nodePort` (validated in the default range 30000–32767). Out-of-range values raise a validation error.
 - Validation also checks for duplicate `name`, `port`, and `nodePort` entries within a Service and raises a validation error when found.
- externalIPs: exporter passes through `service.externalIPs` for ClusterIP/NodePort Services. Note: reachability depends on your environment (cloud L2/L3 or bare metal with MetalLB/ARP/NDP). This is not a load balancer; traffic routing to those IPs must exist outside the manifest.

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
- Exec readiness probe (spec):
  - readiness: { exec: { command: ["sh", "-c", "true"] }, timeoutSeconds: 1 }

Demo manifests
- specs/examples/echo-sec.yaml: non-root + read-only root filesystem + HTTP readiness + ingress.
  - Apply: `python -m ae.cli apply -f specs/examples/echo-sec.yaml`
- specs/examples/echo-tcp.yaml: TCP readiness with thresholds + ingress.
  - Apply: `python -m ae.cli apply -f specs/examples/echo-tcp.yaml`
- specs/examples/echo-exec.yaml: Exec readiness probe + ingress.
  - Apply: `python -m ae.cli apply -f specs/examples/echo-exec.yaml`

Remote apply
- Enable mutations and token, then:
  - `ae --server https://api.home.arpa:8443 --token <admin> apply -f specs/examples/echo-sec.yaml`

## Compliance Summary (2025-10-31)

- Stable APIs only: apps/v1, v1, networking.k8s.io/v1, policy/v1, autoscaling/v2.
- Deployment and StatefulSet export; Services (multi-port, NodePort/LoadBalancer), Ingress v1 with TLS.
- Probes (HTTP/TCP/exec), resources, securityContext (+seccomp/AppArmor), SA/PDB/HPA/NetworkPolicy supported as described.
- Single-node runtime diverges from ClusterIP semantics; exporter YAML remains upstream-compatible.

## Gaps → Issues Checklist

- [ ] StartupProbe + lifecycle hooks (postStart/preStop).
- [ ] `envFrom` for ConfigMap/Secret.
- [ ] `imagePullSecrets` and `imagePullPolicy` controls.
- [ ] PDB percentage values; validation updates.
- [ ] HPA advanced behaviors (scale policies, stabilization windows).
- [ ] Config/Secret volume mounts with mountPaths.
- [ ] PVC `storageClassName` and accessModes selection.
- [ ] Service sessionAffinity and healthCheckNodePort (flagged).
- [ ] Optional RBAC emission tied to ServiceAccount.
