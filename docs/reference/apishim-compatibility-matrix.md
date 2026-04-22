# API Shim Compatibility Matrix

Legend: ✅ supported; ⚠️ partial/limited; 🚧 planned.

For a human-readable landing page and browser-friendly schema links, see [API Shim](api-shim.html).

For staged implementation work and open gaps, see [API Shim Roadmap](apishim-roadmap.html).

## Core resources
- Namespaces, ConfigMaps, Secrets, ServiceAccounts: ✅ CRUD, watch, managedFields apply/patch.
- Services: ✅ ClusterIP/NodePort allocation; LB status projection; EndpointSlice + topology hints; svc port-forward.
- Endpoints/EndpointSlice: ✅ virtualized from controller state.
- Ingress (v1): ✅ status.loadBalancer from VIP/clusterIP; annotations passthrough; no canary/regex.

## Workloads
- Deployments, ReplicaSets (virtual), Pods (projected): ✅ status/conditions, logs/exec/port-forward.
- StatefulSet, DaemonSet, Job, CronJob: ⚠️ stored + best-effort status; treated as Deployment-like apps (no PVC templates, no strict one-per-node scheduling; Job completion and CronJob scheduling are best-effort).
- HorizontalPodAutoscaler v2: ✅ currentMetrics/status; backs k1s autoscaling.

## AuthN/AuthZ
- Tokens: ✅ bearer tokens required by default; optional AE_APISHIM_ALLOW_ANON=1 for local dev.
- RBAC: ✅ Role/ClusterRole + (Cluster)RoleBinding evaluation; SubjectAccessReview endpoint; static fallback.
- ServiceAccount tokens: ✅ issued and injected into projected pods.

## Mutations & patching
- apply (SSA): ✅ with managedFields tracking and conflict detection.
- mergePatch/jsonPatch: ✅ content-type negotiation enforced.
- Validation: ⚠️ basic DNS-1123 and service port/nodePort checks; no schema admission for custom fields.

## Discovery & tooling
- Discovery endpoints (/api, /apis, preferred versions): ✅
- OpenAPI: ✅ `/openapi/v3` is the primary endpoint; `/openapi/v2` mirrors it for compatibility. Helm/kubectl dry-run validated in CI; drift guard compares committed specs.
- Port-forward: ✅ pods and services (SPDY/WebSocket) selecting ready endpoints first.

Runtime caveats
- CRI/containerd nodes: exec/attach/logs use `crictl` on the node; install it and set `CRICTL_BIN` if needed.
- CRI port-forward proxy is optional: set `AE_APISHIM_CRI_PORTFORWARD=1` (or `..._FORCE=1`) to enable the CRI-native proxy path.

## Not covered
- Aggregated API servers, metrics.k8s.io, PodSecurity admission, CSI/CNI plugins, cloud LoadBalancers.
- Full conformance test suite (targeting “lite” subset only).
