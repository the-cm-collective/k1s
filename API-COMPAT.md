# API Compatibility Roadmap (review draft)

This document captures the staged plan to make k1s surface a Kubernetes‑compatible API for common tooling (kubectl, ingress controllers, service meshes). Each phase is incremental and shippable; later phases build on earlier ones.

## Phase 0 — Document & guardrails (current)
- Capture scope, risks, and non‑goals; keep shim behind a feature flag (`AE_APISHIM_ENABLE`).
- Ensure shim runs in dev/CI with bearer token + optional TLS, no cluster impact by default.
- Deliverables: this roadmap; smoke target that starts `ae.apishim serve` and answers `/health`.

## Phase 1 — Kubectl “get/apply” happy path
(status: functionally complete; VIP/loadBalancer status polish remains)
- Harden discovery: `/api`, `/apis`, preferred versions, correct `resourceVersion` on list/watch, bookmarks, list‑continue stubs.
- Auth baseline: bearer token required by default; optional mTLS for kubeconfig; minimal RBAC scaffold (cluster‑admin only).
- Objects: Deployments, Services, Ingresses, Namespaces, Roles/RoleBindings, CRDs (already), plus Nodes and Endpoints/EndpointSlice projected from controller state/runtime.
- Status: real replicas/ready counts from controller state; Service `status.loadBalancer` and ClusterIP/VIP data.
- Deliverables: `kubectl get/describe` works for the above kinds; `kubectl apply -f deployment.yaml` creates/updates an app; `kubectl get endpoints` shows VIP backends; CI job exercising apply+watch.

## Phase 2 — Pods as first‑class citizens
(status: functionally complete)
- Project Pods/ReplicaSets from runtime placements; include conditions, restartCount, containerStatuses.
- Exec/logs/port-forward passthrough to agent runtimes; enforce auth/role gating.
- Support scale‑to‑0 semantics (delete Pods, keep Deployment) and propagate status.
- Deliverables: `kubectl logs/exec/port-forward` on pod names; `kubectl rollout status` on deployments; watch stability under churn.

## Phase 3 — Services fidelity & scheduling hints
(status: in progress — svc port-forward + EndpointSlice + clusterIP/nodePort allocation implemented; endpoint selection prefers ready endpoints; zone hints added; LB status honors loadBalancerIP/externalIPs/provider IPs)
- NodePort/ClusterIP parity: deterministic port allocation, collision handling, VIP + overlay awareness; EndpointSlice as source of truth.
- Respect topology hints when available; expose node labels/zones derived from controller/agent info.
- Ingress status reflects live VIP/host routing; optional external DNS annotations pass‑through.
- Deliverables: `kubectl port-forward svc/...` works; service discovery validated in multinode tests; ingress controllers can watch and reconcile.

Phase 3 action items:
- Service projection: ensure ClusterIP/NodePort allocation table with collision checks; surface `spec.clusterIP`, `spec.ports[].nodePort`, and `status.loadBalancer.ingress`. (clusterIP/nodePort allocation now in apishim; LB status pulls provider clusterIP/loadBalancerIP/externalIPs.)
- EndpointSlice fidelity: generate slices from controller placements; include `hints.forZones` where node labels provide zone info. (now includes nodeName/zone when pod IP matches node podCIDR; basic watch emulation)
- Port-forward svc: reuse SPDY handler to forward to service VIP/backend selection. (Done: selects ready endpoints first; hash-based spread per port list; still single-endpoint target)
- Ingress status: populate `status.loadBalancer.ingress`/`ip` and optional hostnames based on VIP; add annotations passthrough. (VIP propagation via backend service clusterIP/provider IP)
- Tests: multinode service discovery (VIP + nodeport) and `kubectl port-forward svc/...` CI job. (new GH workflow multinode-svc-portforward)

## Phase 4 — Workload breadth and controllers
(status: near complete — adapter reconciles StatefulSets/DaemonSets/Jobs; CronJobs fire with ownerRefs; HPA now drives autoscaling with status/currentMetrics; events include probe/rollout/overlay/job signals)
- Add Jobs/CronJobs, StatefulSets, DaemonSets translations (where feasible) with status/ownerRefs. *(Implemented; unit coverage exercises status + ownerRefs under stub runtime.)*
- HorizontalPodAutoscaler object backed by our autoscaling; emit status to match K8s expectations. *(Implemented with min/max, cooldown, CPU/memory utilization/averageValue, currentMetrics, lastScaleTime.)*
- Events: surface controller/agent events to `/api/v1/events`; emit rollout, probe, and overlay notices. *(Implemented; overlay event hook in ServiceController; probe events emitted on health changes.)*
- Deliverables: basic helm charts that expect these kinds render and run; HPA status visible; events stream consumable by `kubectl get events`. *(Covered by helm-shim-smoke CI workflow exercising Deployment+HPA+StatefulSet+DaemonSet+Job+CronJob on stub runtime.)*

## Phase 5 — AuthZ, SSA, patch semantics
- Implement Role/RoleBinding enforcement; optional ServiceAccounts + projected tokens.
- Support JSONPatch/mergePatch; scoped server‑side apply (SSA) for supported kinds; manage `managedFields`.
- Admission/validation hooks for custom App CRD (optional) to keep native k1s schema authoritative.
- Deliverables: `kubectl auth can-i` works; SSA usable by controllers that expect it (ingress controllers, cert‑manager‑style tools).

Phase 5 action items:
- RBAC enforcement path: hook Role/ClusterRole + (Cluster)RoleBinding evaluation into the apishim request pipeline and return K8s-style 403s when denied; keep dev token cluster-admin.
- ServiceAccounts + projected tokens: persist ServiceAccount objects, issue short-lived bearer tokens per SA/namespace, wire token authenticator, and project tokens into rendered Pod specs. *(tokens now minted on SA create; projection still pending).*
- Patch semantics: add JSONPatch and mergePatch handlers for supported kinds with correct content-type negotiation and status errors.
- SSA + managedFields: honor `fieldManager`/`force`, track managedFields per object in shim storage, and surface conflicts on overlapping fields.
- App CRD admission: validating hook to keep native App schema authoritative; reject or warn on incompatible native objects.
- CLI parity: implement `kubectl auth can-i` via a SubjectAccessReview-equivalent endpoint bound to RBAC evaluation.
- Tests/CI: unit matrix for RBAC decisions and patch/SSA behavior; integration smoke that exercises can-i, JSONPatch/mergePatch, and SSA apply flows against the stub runtime.

## Phase 6 — Reliability, storage, and scale
- Move shim object storage off SQLite to primary state store or Postgres backend to avoid drift and enable HA.
- Improve watch scalability (per‑resource queues, backpressure, timeouts) and add metrics/tracing.
- Conformance subset: target the “Kubernetes API conformance lite” we define; document exclusions.
- Deliverables: soak tests under churn; dashboard panel for shim health; failover of shim without object loss.

## Phase 7 — Polish and ecosystem integration
- Helm friendliness: richer discovery, dry‑run/server‑side validation support; better error surfaces.
- CRD parity for k1s App (optional) with conversion webhook; docs + samples for migration.
- Backwards compatibility policy and versioning of the shim.
- Deliverables: published compatibility matrix; release note gate that runs kubectl/helm smoke; docs for kubeconfig and auth modes.

## Non‑goals (for now)
- Full upstream conformance certification.
- Aggregated API servers, PSP/PodSecurity admission, or CSI/CNI plugins.
- Cloud load balancer provisioning.

## Success metrics
- `kubectl get/apply/describe/logs/exec` succeeds against multinode lab.
- CI shim job green across node churn and overlay failover.
- Dashboard shows shim health and API errors <1% over soak.
