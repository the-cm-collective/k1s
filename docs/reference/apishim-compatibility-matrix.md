# API Shim Compatibility Matrix (2026-01-16)

Legend: ✅ supported; ⚠️ partial/limited; 🚧 planned.

## Core resources
- Namespaces, ConfigMaps, Secrets, ServiceAccounts: ✅ CRUD, watch, managedFields apply/patch.
- Services: ✅ ClusterIP/NodePort allocation; LB status projection; EndpointSlice + topology hints; svc port-forward.
- Endpoints/EndpointSlice: ✅ virtualized from controller state.
- Ingress (v1): ✅ status.loadBalancer from VIP/clusterIP; annotations passthrough; no canary/regex.

## Workloads
- Deployments, ReplicaSets (virtual), Pods (projected): ✅ status/conditions, logs/exec/port-forward.
- StatefulSet, DaemonSet, Job, CronJob: ⚠️ stored + best-effort status; treated as Deployment-like apps (no PVC templates, no job completion, no CronJob scheduling, no one-per-node scheduling).
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
- OpenAPI: ✅ enriched `/openapi/v2` covering core workloads/services/HPA/ingress; `/openapi/v3` mirrors v2. Helm/kubectl dry-run validated in CI; drift guard compares committed specs.
- Port-forward: ✅ pods and services (SPDY/WebSocket) selecting ready endpoints first.

## Not covered
- Aggregated API servers, metrics.k8s.io, PodSecurity admission, CSI/CNI plugins, cloud LoadBalancers.
- Full conformance test suite (targeting “lite” subset only).
