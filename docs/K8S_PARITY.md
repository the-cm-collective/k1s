K8s Parity and Alignment (MVP)

Status summary (2026-01-14)
- Exporter/shim: Deployments, StatefulSets, DaemonSets, Jobs, CronJobs, Services (ClusterIP/NodePort/LoadBalancer + externalIPs/sessionAffinity), Ingress v1, HPA v2, PDB, ServiceAccount + Role/RoleBinding, ConfigMap/Secret env + envFrom/file projections, imagePullPolicy/imagePullSecrets, startupProbe/lifecycle, seccomp/AppArmor, priorityClassName/affinity/tolerations/topologySpread/nodeSelector, SSA/patch (merge/json/apply) with managedFields.
- Runtime/networking: Service CIDR + overlay provider with ClusterIP allocation and EndpointSlice projection; port-forward for pods/services; exec/logs proxied via agents; scheduler honors nodeSelector/taints/tolerations/topology spread and storage pinning.
- Discovery/OpenAPI: Enriched `/openapi/v2` + `/openapi/v3` published in CI with fixture validation; compatibility matrix lives at `docs/apishim-compatibility-matrix.md`; live gate exercises kubectl/helm apply/watch/port-forward against the shim.

Security & Policies
- Seccomp/AppArmor: container `securityContext.seccompProfile` supported (RuntimeDefault/Localhost/Unconfined); AppArmor via Pod template annotation `container.apparmor.security.beta.kubernetes.io/<container>`.
- PodSecurity labels/presets are emitted by the exporter when requested; PSA admission itself is not enforced by k1s.
- RBAC: shim enforces Role/ClusterRole + (Cluster)RoleBinding; SubjectAccessReview endpoint available. Controller HTTP API uses READ/SCALER/ADMIN tokens.

Service Types and VIPs
- ClusterIP/NodePort/LoadBalancer supported in exporter/shim; nodePort validated.
- Service VIPs allocated from the Service CIDR and backed by the overlay provider; ingress templates prefer VIPs over hostPorts for multi-node routing.
- externalIPs pass-through for ClusterIP/NodePort. Reachability depends on your environment (MetalLB/cloud LB if needed).

Checklist
- Use readiness/liveness/startup probes.
- Provide `resources.requests/limits`; enable `--policy strict` in `ae k8s-check`.
- Add topology spread or anti-affinity for multi-replica apps.
- Keep storage retained volumes single-node or attach PVCs when exporting to Kubernetes.
- Prefer Service VIP + ingress over hostPorts for HA; reserve hostPorts only for single-node edges.

Verification notes
- Exporter validated with `ae export-k8s --validate` + `ae k8s-check --policy strict`.
- Shim gates: kubectl/helm apply/get/watch/port-forward exercised in CI (live gate) with OpenAPI drift guard.
- Multi-node overlay path covered by integration tests and lab script (`docs/multinode-lab.md`).

Compliance Summary (2026-01-14)
- Workloads: Deployment/StatefulSet/DaemonSet/Job/CronJob supported; scale/rollback via shim and exporter.
- Pod/Container: env/envFrom; readiness/liveness/startup probes; lifecycle hooks; resources; securityContext (runAs*/fsGroup/readOnlyRootFilesystem/cap drop/seccomp/AppArmor); priorityClassName; imagePullPolicy/imagePullSecrets.
- Service: ClusterIP/NodePort/LoadBalancer; multi-port mapping; externalIPs; sessionAffinity; EndpointSlice projection with topology hints; service port-forward works for shim/exported Services.
- Ingress: networking.k8s.io/v1 with TLS, ingressClassName, status.loadBalancer from VIP/provider IPs.
- Policy/Autoscaling/Accounts: PDB (int/percent), HPA v2 (CPU/memory value/averageValue), ServiceAccount tokens injected by shim, Role/RoleBinding emit/enforce, NetworkPolicy pass-through.
- Scheduling: affinity/tolerations/topologySpreadConstraints/nodeSelector exported; scheduler respects selectors/tolerations/topology spread and node readiness/cordon.
- Validation/Tooling: `ae export-k8s --validate`, `ae k8s-check --policy strict`, `ae k8s-report`, OpenAPI fixture validation, compatibility matrix.

Current gaps
- NetworkPolicy enforcement depends on your CNI (k1s runtime does not enforce policies).
- PodSecurityAdmission/webhook/OPA policies not implemented.
- PersistentVolume/StorageClass/CSI not managed; use retained named volumes in k1s or PVCs in exported manifests.
- metrics.k8s.io and aggregated API servers are out of scope.

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
