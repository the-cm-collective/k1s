K8s Parity and Alignment (MVP)

Status summary (2025-11-12)
- Exporter: Deployment/StatefulSet/Service/Ingress/HPA/PDB/SA/PVC/ConfigMap/Secret/Role/RoleBinding/NetworkPolicy ✓
- Probes: HTTP/TCP/exec + startupProbe; windowing and thresholds ✓
- Security: runAs*, readOnlyRootFilesystem, cap drop, seccomp; AppArmor via annotation ✓
- Services: ClusterIP with multi-port, NodePort support; validation for names/ports/nodePort ✓
- Ingress: v1 with Prefix pathType default; TLS with secretName; ingressClassName passthrough ✓
- Scheduling: affinity/tolerations/topology spread/priorityClassName/nodeSelector ✓
- API writes: CLI + optional HTTP API mutations (token‑gated) ✓

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
  - Readiness/liveness/startup probes present (HTTP/TCP/exec).
  - Resources.requests/limits modeled; strict policy available.
  - Non-root security context encouraged; defaults available.
  - Graceful termination via terminationGracePeriodSeconds and preStop support.
  - Ingress flips only after ≥1 ready replica.
  - Storage: PVCs; stateful demo available.

Verification notes
- Docker and Podman paths validated by unit/integration tests and demos.
- Echo variants exercise readiness, ingress, storage.
- Dashboard provides live logs and status snapshots.

Roadmap (selected)
- RBAC emitters tied to ServiceAccount.
- Job/CronJob exporters for batch.
- TLS Secret helper and Traefik ingress presets.
- `emptyDir` ephemeral volumes.
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

## Compliance Summary (2025-11-11)

- Stable APIs only: apps/v1, v1, networking.k8s.io/v1, policy/v1, autoscaling/v2.
- Deployment and StatefulSet export; Services (multi-port, NodePort/LoadBalancer), Ingress v1 with TLS.
- Probes (HTTP/TCP/exec/startup), resources, securityContext (+seccomp/AppArmor), SA/PDB/HPA/NetworkPolicy supported as described.
- Single-node runtime diverges from ClusterIP semantics; exporter YAML remains upstream-compatible.

## k3s High‑Priority Gaps (Q4 2025)

- PodSecurity labels preset (Namespace): DONE via `--emit-namespace --psa-enforce`.
- NetworkPolicy provider guidance: note enforcement dependency; provide web/backend presets.

## Open Gaps (non‑k3s specific)

- Service healthCheckNodePort.
- Advanced Ingress features (regex, weighted/canary annotations, multiple backends per rule).
- Ephemeral containers.
- RBAC resources for richer roles (ClusterRole/ClusterRoleBinding) and admission webhooks/CRDs (out of scope).

## Compliance Baseline

- Latest report JSON lives at `docs/site/k8s_status.json`.
- Regenerate locally:
  - `python -m ae.cli k8s-report --samples specs/examples/echo.yaml specs/examples/multi-replica-echo.yaml specs/examples/echo-hpa.yaml --run-dry-run -o docs/site/k8s_status.json`
  - `python docs/build_docs.py`
  - Open `docs/site/k8s-parity.html`.
