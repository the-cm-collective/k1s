# k1s Kubernetes API Shim — MVP Design

Goal: Allow `kubectl` and `helm` to operate against k1s by exposing a minimal, compatible Kubernetes API surface and controllers that reconcile core objects into the existing k1s runtime.

Date: 2025-11-12
Owner: runtime/controller

## Scope (MVP)
- Discovery and basic auth:
  - `GET /version`, `/api`, `/apis`, `/apis/apps/v1`, resource discovery lists.
  - `GET /healthz`, `/readyz` (always-ok for MVP), `Bearer` token auth (reuse existing k1s token gate).
- Core resources (CRUD + list + watch):
  - core/v1: `Namespace`, `Secret`, `ConfigMap`, `ServiceAccount`, `Service`.
  - apps/v1: `Deployment` (+ `/status`, `/scale`).
  - networking.k8s.io/v1: `Ingress`.
  - batch (phase 2): `Job`, `CronJob`.
- Subresources/semantics required by Helm/kubectl:
  - `status` for Deployment (Available/Progressing) and replica counters.
  - `scale` for Deployment (read/write `.spec.replicas`).
  - `watch` streams (chunked or SSE) for resources Helm/kubectl `--watch` use paths on.
- Storage for Helm releases:
  - Persist Helm release `Secrets`/`ConfigMaps` in the `kube-system` Namespace with labels/annotations intact.
- Explicitly out-of-scope for MVP:
  - CRDs and CR admission; SSA (server‑side apply); full RBAC enforcement; exec/logs/attach/port-forward over the API; EphemeralContainers; advanced Ingress, HPA.

## High-Level Architecture
- `apishim-server` (new module):
  - Serves Kubernetes-compatible endpoints with minimal discovery + OpenAPI v2 stub.
  - Authentication: static Bearer token (reuse existing k1s API token configuration); Authorization: allow‑all for MVP.
  - Storage: SQLite tables keyed by `(group,version,resource,namespace,name)` with JSON `metadata`, `spec`, `status`.
- `kube-adapter-controller` (new controller):
  - Watches stored Kubernetes objects and translates them into k1s `App` specifications + ingress/ports.
  - Writes back status/conditions so `helm --wait` and `kubectl rollout status` converge.
- `service/endpoints view` (virtual):
  - Synthesize Endpoints/EndpointSlice views from k1s runtime readiness; not persisted.

## Resource Mapping (MVP happy path)
Assume single-container Pods; reject or ignore additional containers with a clear Status condition.

- apps/v1 Deployment → k1s App
  - name/namespace → app name (prefix with ns when needed for uniqueness: `<ns>--<name>`).
  - `.spec.replicas` → `spec.replicas`.
  - `.spec.template.spec.containers[0]`
    - `image`, `command/args`, `env`, `envFrom`, `ports[*].containerPort`, `resources`, `securityContext` → mapped to k1s fields supported by exporter.
  - Probes: readiness/liveness/startup → k1s health config.
  - Lifecycle: preStop respected; terminationGracePeriodSeconds respected on rollout.
  - Status backfill: `availableReplicas`, `readyReplicas`, conditions `Available=True`, `Progressing=True/False`.

- core/v1 Service (ClusterIP/NodePort)
  - Selector labels must match Deployment pod template labels; map to exposed host ports.
  - NodePort support: validate range (30000-32767) and bind on host.
  - ClusterIP: emulate via local service registry; clients typically reach via Ingress or NodePort in k1s.

- networking.k8s.io/v1 Ingress
  - `ingressClassName` passthrough; default to `caddy` in shim.
  - Rules/paths → Caddy routes; `tls.secretName` → terminate with provided cert.

- core/v1 Secret/ConfigMap/ServiceAccount
  - Stored exactly as provided for Helm release storage and app env/envFrom; SA is accepted but not enforced.

## Discovery and OpenAPI
- Implement minimal discovery trees for served groups/versions/resources.
- Serve a compact OpenAPI v2 doc with only the schemas above; kubectl/helm use discovery primarily, not full schema validation.

## Watches
- Support `?watch=1` on LIST endpoints with chunked JSON lines (or SSE) carrying `ADDED|MODIFIED|DELETED` + object.
- Backed by a pub/sub on SQLite changefeed (simple trigger + NOTIFY or in-process event bus).

## Namespaces
- Persist Namespace objects; enforcement is logical. k1s runtime is namespace‑agnostic; adapter prefixes names to ensure isolation.
- Label/annotation propagation: preserved in `metadata` for selectors and Helm bookkeeping.

## Patching and Apply
- Support `application/merge-patch+json` and `application/json` (PUT) for Helm upgrades.
- For MVP, return `415` on `application/apply-patch+yaml` (SSA) with clear error text.

## Status & Conditions — Helm --wait contract
- Deployment:
  - Conditions:
    - `Available=True` when `readyReplicas == spec.replicas` for ≥ 1s.
    - `Progressing=True` while reconciling; set to `False` on timeout with a `Reason`.
  - Fields: `observedGeneration`, `replicas`, `updatedReplicas`, `readyReplicas`, `availableReplicas`.
- Service/Ingress:
  - Populate `status.loadBalancer.ingress` with host/IP for UX; not required by Helm.

## Error Handling / Unsupported Features
- Multi-container Pod templates → Condition `ReplicaUnsupported` with message; reject with `422`.
- CRDs:
  - `POST /apis/apiextensions.k8s.io/v1/customresourcedefinitions`: return `403 Forbidden` with actionable message.
  - Any `/apis/<group>/<version>/<plural>` not in MVP: `404` with suggestion.

## Security
- TLS: self-signed certs for the shim HTTPS listener; provide a kubeconfig generator.
- AuthN: static token (reuse existing k1s token).
- AuthZ: allow‑all for MVP; log-only RBAC evaluation later.

## Kubeconfig Recipe
Generate `~/.kube/config` entry pointing to shim:

```bash
ae apishim kubeconfig --server https://127.0.0.1:8445 --token $(ae token) > ~/.kube/config
kubectl --context k1s-apishim version
```

## Phased Plan & Acceptance

Phase 0 — Readiness (discovery + storage)
- Endpoints: `/version`, `/api`, `/apis`, core/apps discovery; Namespaces/Secrets/ConfigMaps CRUD; watch.
- Accept helm release storage; `helm ls` works against empty cluster.
Acceptance:
- `kubectl get ns,cm,secret -A` round-trips; `helm repo add ...` and `helm ls` succeed (no releases).

Phase 1 — Deploy/Service/Ingress
- Implement Deployment create/update/delete + status/scale; Service translator; Ingress→Caddy routes; basic watches.
- Adapter maps Deployment->k1s App and updates status from runtime readiness.
Acceptance:
- `helm upgrade --install hello <simple-chart> --wait` completes; `kubectl rollout status deploy/hello` reports success.

Phase 2 — Batch and polish
- Add Job/CronJob; add `--dry-run=server` support (non-persisting validation), richer OpenAPI, better error text.
Acceptance:
- `kubectl create job ... --dry-run=server -o yaml` returns 200; `helm uninstall` cleans up and shim GC removes derived k1s resources.

## Testing Strategy
- Unit: table-driven mapping tests Deployment→App; Service/Ingress path rendering.
- Integration: bring up shim + k1s locally, run `helm` against a no‑CRD chart (nginx‑like), assert `--wait` and Service reachability via Ingress.
- Conformance: run `kubeconform -strict` on emitted OpenAPI and example manifests (exporter already uses this).

## Risks & Mitigations
- Many charts depend on CRDs: clearly document unsupported; provide `helm template` converter path as alternative.
- SSA adoption in Helm: detect `apply` content‑type and return a precise error recommending `--no-server-side-apply` if needed.
- Name collisions across Namespaces: enforce `<ns>--<name>` prefixing in adapter; keep a reverse index.

## Open Questions
- Do we emulate Pods/ReplicaSets in the API or keep them virtual only? (Leaning virtual for MVP.)
- Should we vend a partial metrics API for `kubectl top`? (Out of MVP scope.)

---
Appendix A — Minimal Discovery Sketch

- `/api` → { versions: ["v1"] }
- `/apis` → groups: apps, networking.k8s.io, batch (phase 2)
- `/apis/apps/v1` → resources: deployments (namespaced), deployments/status, deployments/scale
- `/api/v1` → resources: namespaces, secrets, configmaps, serviceaccounts, services
- `/apis/networking.k8s.io/v1` → resources: ingresses

