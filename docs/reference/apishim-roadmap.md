# API Shim Roadmap

Status: current roadmap and gap tracker for the Kubernetes API shim.

This document captures the staged plan to make k1s surface a Kubernetes‑compatible API for common tooling (kubectl, ingress controllers, service meshes). Each phase is incremental and shippable; later phases build on earlier ones.

## Phase 0 — Document & guardrails
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
(status: functionally complete — svc port-forward + EndpointSlice + clusterIP/nodePort allocation implemented; endpoint selection prefers ready endpoints; zone hints added; LB status honors loadBalancerIP/externalIPs/provider IPs)
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
- RBAC enforcement path: hook Role/ClusterRole + (Cluster)RoleBinding evaluation into the apishim request pipeline and return K8s-style 403s when denied; keep dev token cluster-admin. *(implemented with SubjectAccessReview projection; defaults still allow unauthenticated when no tokens configured.)*
- ServiceAccounts + projected tokens: persist ServiceAccount objects, issue short-lived bearer tokens per SA/namespace, wire token authenticator, and project tokens into rendered Pod specs. *(tokens minted on SA create, auto-projected into workloads/pod projections; TTL/rotation handled in-memory; pod template injection in place.)*
- Patch semantics: add JSONPatch and mergePatch handlers for supported kinds with correct content-type negotiation and status errors. *(json/merge/apply supported; json-patch added with unit coverage.)*
- SSA + managedFields: honor `fieldManager`/`force`, track managedFields per object in shim storage, and surface conflicts on overlapping fields. *(implemented; conflict 409s covered in unit tests.)*
- App CRD admission: validating hook to keep native App schema authoritative; reject by default and allow warn/off via `AE_APISHIM_APP_ADMISSION`. *(implemented; warn/off emit Warning headers instead of rejection.)*
- CLI parity: implement `kubectl auth can-i` via a SubjectAccessReview-equivalent endpoint bound to RBAC evaluation. *(endpoint present; no e2e CI yet.)*
- Tests/CI: unit matrix for RBAC decisions and patch/SSA behavior; add integration smoke that exercises can-i, JSONPatch/mergePatch, and SSA apply flows against the stub runtime. *(covered by `apishim-ssa-rbac` CI workflow.)*

## Current Gaps and Next Steps
- **Phase 6 rollout in progress:** Shim and controller can target Postgres via `AE_APISHIM_DSN`/`AE_STATE_DSN`; migrations preserve resourceVersions; HA shim with shared Postgres is validated in CI as a transitional path. The long-term HA authority target now follows the [HA Control Plane Roadmap](high-availability-control-plane.html): `H4a` routes workload-core HA mutation onto shared controller authority, `H4b1` converges `CronJob` plus passive shim objects (`ConfigMap`, `Secret`, `ServiceAccount`), the later HPA slice waits for a shared metrics source, and `H4b2` finishes storage/RBAC/remaining shim-native convergence onto `etcd`. Remaining: production-grade watch propagation metrics across shim replicas and the storage convergence work needed for full `etcd`-backed HA across the whole shim surface.
- **Phase 7 polish in flight:** Compatibility matrix + kubeconfig/auth docs + helm smoke gate are in place; OpenAPI v2/ v3 and schemas are richer with fixture validation and live gate coverage. Remaining polish: promote the live gate to release-blocking, wire compat/OpenAPI links into docs + release notes, and round out sample coverage.

### Auth defaults (dev toggle)
- Bearer token is now required by default; shim refuses to start without `AE_APISHIM_TOKEN` unless explicitly started with `AE_APISHIM_ALLOW_ANON=1` or `python -m ae.apishim serve --allow-anonymous` for local experiments.
- Requests without a bearer token receive `401 Unauthorized` (or `403` when RBAC blocks a verb). Document this flow in kubeconfig examples and keep dev overrides scoped to local testing.

## Phase 6 — Reliability, storage, and scale
- Move shim object storage off SQLite to the primary HA authority store. Postgres remains a transitional externalized path, but the target HA end state aligns on `etcd` with the core controller; workload-core HA mutation is now on that path, while the remaining shim-native resources still need the `H4b1`, later HPA, and `H4b2` cutovers.
- Improve watch scalability (per‑resource queues, backpressure, timeouts) and add metrics/tracing.
- Conformance subset: target the “Kubernetes API conformance lite” we define; document exclusions.
- Deliverables: soak tests under churn; dashboard panel for shim health; failover of shim without object loss.

## Phase 7 — Polish and ecosystem integration
- Helm friendliness: richer discovery, dry-run/server-side validation support; better error surfaces.
- CRD parity for k1s App (optional) with conversion webhook; docs + samples for migration.
- Backwards compatibility policy and versioning of the shim.
- Deliverables: published compatibility matrix; release note gate that runs kubectl/helm smoke; docs for kubeconfig and auth modes.

### Phase 7 current status (2026-01-14)
- Discovery/OpenAPI: `/openapi/v2` now includes richer shapes (Service ports + external/loadBalancer/ipFamily fields, Deployment/DaemonSet/StatefulSet conditions, Job/CronJob/HPA status); `/openapi/v3` now mirrors `/openapi/v2` and is treated as authoritative. CI guards drift via `scripts/validate-openapi.sh` (helm-dryrun-openapi workflow), Helm/kubectl dry-run is exercised in CI, and OpenAPI artifacts are published. A lightweight fixture check (`scripts/validate-openapi-fixtures.py`) validates the schemas against the shipped sample manifests.
- Compatibility matrix: published at `docs/reference/apishim-compatibility-matrix.md`; release gate runs helm shim smoke and uploads the matrix + OpenAPI artifacts and emits a release-note snippet.
- Live gate: `.github/workflows/apishim-live-openapi.yml` now runs a Postgres-backed shim and exercises kubectl/helm server-side validation plus short get/watch churn, collecting `/openapi/v2` + `/openapi/v3`, fixture validation logs, and object snapshots as artifacts. The script accepts a provided kubeconfig (including kind/dev lab) via `APISHIM_LIVE_KUBECONFIG`/`APISHIM_LIVE_KUBECONFIG_B64` or a kind cluster name via `APISHIM_KIND_CLUSTER`.
- Pending: fold the live gate into the release-blocking checks, wire compatibility matrix/OpenAPI links into website docs and the release-note template, and expand sample coverage used by the live gate.

#### Phase 7.1 — OpenAPI validation hardening (complete)
- Added schemas for the k1s `App` CRD and policy/v1 PodDisruptionBudget so sample manifests validate end-to-end instead of being skipped.
- Wired `scripts/validate-openapi-fixtures.py` into the OpenAPI drift guard and release workflows (runs against freshly generated `/openapi/v2`).
- Release workflow now validates fixtures and performs a kubectl spot-check (apply/get/watch for Deployment/Service/HPA) against the running shim, uploading logs alongside the OpenAPI/compatibility artifacts.
- `/openapi/v3` remains the authoritative mirror of `/openapi/v2` and is called out in release notes and docs; compatibility matrix links are included in the release summary artifacts.

#### Phase 7.2 — Live cluster gate + doc linkage (complete)
- Live gate landed: `scripts/ci/apishim-live-openapi.sh` drives kubectl/helm dry-run plus short watch churn against a Postgres-backed shim, capturing live `/openapi/v2` + `/openapi/v3`, fixture validation logs, and object snapshots. `.github/workflows/apishim-live-openapi.yml` publishes these artifacts on push/PR. The gate can point at a supplied kubeconfig (kind/dev lab) via `APISHIM_LIVE_KUBECONFIG(_B64)` or pull a kind kubeconfig by name with `APISHIM_KIND_CLUSTER`.
- Follow-ups promoted to Phase 7.3: release-blocking promotion, docs/release-note wiring, and expanded sample set validated by the gate.

#### Phase 7.3 — Release gate + site wiring (in progress)
- Promotion: `apishim-live-openapi` now runs on `main`/PRs and release tags; release workflow fails if the live gate fails. Skips are a manual exception reserved for sealed-secret fixtures with explicit justification.
- Coverage: nightly live gate runs both local (Postgres-backed shim) and external kubeconfig targets when configured; artifacts are uploaded per run.
- Samples: live gate now validates the multi-replica/PDB+HPA manifest set; fixtures already include the Deployment/HPA exporter output and PDB-emitting Deployment manifests in `specs/examples/`.
- Docs/release notes: docs nav now links `/openapi/v3`, and release notes call out v3 as primary with v2 as the compatibility mirror.
- Fidelity: live gate includes a non-stub runtime lane (docker) and exercises logs/exec; service status is captured in live artifacts. Postgres storage remains enabled for resourceVersion stability.

## Non‑goals (for now)
- Aggregated API servers, PSP/PodSecurity admission, or CSI/CNI plugins. These would require extra control-plane components, admission/webhook plumbing, and host kernel capabilities (CNI/CSI) that we deliberately avoid to keep the shim lean. PSA alignment and a basic storage story may appear under the conformance-lite track, but full plugin ecosystems stay deferred.
- Cloud load balancer provisioning. We do not run cloud-provider controllers; the networking model centers on Service VIPs/overlay plus Caddy. We project `status.loadBalancer` from our VIP/provider IPs for compatibility, but do not create external L4/L7 balancers; operators can front the shim with their own proxy if needed. A future path is outlined below.

## Success metrics
- `kubectl get/apply/describe/logs/exec` succeeds against multinode lab.
- CI shim job green across node churn and overlay failover.
- Dashboard shows shim health and API errors <1% over soak.

## Conformance outlook (aspirational)
- Status: shim is kubectl/helm-compatible for our targeted subset (core CRUD, workloads, Services/Ingress, HPA, RBAC/SSA, OpenAPI v2/v3) but not yet ready for upstream Kubernetes conformance.
- Major gaps: Pod Security Admission + admission webhooks; NetworkPolicy enforcement; PV/PVC/StorageClass/CSI semantics; Node/Lease objects with kubelet-style heartbeats/evictions; metrics.k8s.io; full exec/log/port-forward parity under churn; webhook/SSA edge cases and managedFields accuracy; aggregated APIs.
- Path to evaluate: run Sonobuoy conformance against kind/dev lab to get a fail list; define and publish a “conformance-lite” profile with documented exclusions while we close highest-value gaps.
- Path to close: implement PSA + webhook plumbing; add NetworkPolicy enforcement; deliver a minimal storage story (static PV/PVC + provisioner stub with reclaim/accessMode/volumeMode fidelity); surface Node/Lease and eviction behaviors; expose metrics-server-compatible endpoints; harden streaming/watch semantics and SSA/managedFields; iterate test→fix with shrinking skip list.

## Cloud load balancer trajectory (OpenStack/GCP/AWS)
- Current stance: k1s projects `status.loadBalancer` from internal VIP/provider IPs for compatibility; Caddy fronts L7 ingress; no external L4/L7 balancers are provisioned.
- Options:
  - Provider interface + MetalLB-style backend (BGP/ARP) for on-prem/OpenStack where we can advertise VIPs; minimal cloud permissions required.
  - Cloud-specific controllers (OpenStack Octavia, GCP TCP/HTTP(S), AWS NLB/ALB/Classic) using per-cloud credentials; map Service annotations to provider features (preserve source ranges, health checks, SSL policies).
  - Gateway/L7: keep Caddy/Envoy as ingress with optional cloud LB in front for static IP; use DNS automation to publish the assigned VIP/hostname.
- Prereqs: credential/config surface (e.g., `~/.config/ae/registries.yaml` analog for cloud creds), Service-class annotations, IPAM alignment between Service CIDR and provider, reconcile + retry loops with rate/backoff per cloud API.
- Suggested next steps:
  1) Define a `LoadBalancerProvider` interface with a no-op/default and a MetalLB/OpenStack Octavia implementation.
  2) Add annotation-driven feature mapping (`service.beta.kubernetes.io/*`) for the first provider.
  3) Dogfood on kind+MetalLB and an OpenStack lab; capture artifacts in CI (opt-in lane) and feed back into `/openapi/v2` status expectations.
  4) Evaluate GCP/AWS controllers after the interface stabilizes; document per-cloud IAM roles and failure modes.

### kind + MetalLB (portable L4 path)
- Topology: kind (Docker) cluster with MetalLB controller + speaker in L2/ARP mode announcing VIPs on the kind bridge.
- Address pool: derive from `docker network inspect kind` (e.g., reserve `172.18.255.200-250` inside the kind subnet) to avoid host collisions on CI runners.
- Flow: install MetalLB, apply IPAddressPool + L2Advertisement, then create a Service `type: LoadBalancer`; MetalLB assigns an IP that is reachable from the runner. Curl the assigned IP:port to verify.
- Integration with shim: add a MetalLB provider to mirror the assigned ingress IP into `status.loadBalancer` (and ingress annotations), while keeping Caddy/Envoy for L7. Use this as the first implementation of the `LoadBalancerProvider` interface.
- CI lane sketch: spin up kind, install MetalLB with generated pool/adverts, run apishim (Postgres/stub) and apply a demo LB Service; assert ingress IP populated and reachable. Publish the assigned IP + logs as artifacts.

## Update (2026-01-24)
- HA watch propagation now fans out across Postgres-backed shim replicas via a watch outbox + poller, and the HA workflow runs a longer churn loop (`v0` → `v25`) plus outbox lag/retention checks. Remaining: longer soak coverage and retention tuning for production.
- Shim server now uses a threaded HTTP server so streaming endpoints no longer block other requests; store access is locked to keep shared DB handles safe. Remaining: consider async I/O to reduce thread load under heavy watch/stream usage.
- ServiceAccount tokens rehydrate from stored annotations at startup and fall back to store lookup when a token is unknown, enabling HA replicas to accept each other’s minted tokens. Remaining: move to durable JWT or DB-backed tokens with rotation.
- CronJob scheduling now includes `croniter` in project dependencies, so cron expressions resolve instead of falling back to 60s/annotation intervals.
- Docs drift fixed: the compatibility matrix now reflects best-effort CronJob scheduling and Job completion.
- Live OpenAPI gate is promoted: it runs on main/PRs plus nightly local/external targets, and release tags now run the live gate alongside OpenAPI exports. Docs/nav and release notes now treat `/openapi/v3` as primary with `/openapi/v2` as the compatibility mirror; the live gate validates the multi-replica/PDB+HPA sample set.
- Live OpenAPI gate now exercises a docker runtime lane with logs/exec smoke checks for non-stub fidelity.
- Focused `kubectl exec` SPDY tests now succeed against both docker and podman runtimes (error/stdout/stderr streams open cleanly); the JSON exec fallback in the live gate can be retired after a full lane rerun.

## Update (2026-01-25)
- Decision: apishim remains the primary Kubernetes-compatibility track; no kube-apiserver/Kine fork in the near term.
- Next focus: align core API semantics toward conformance-lite (resourceVersion ordering, list+watch consistency, bookmarks, compaction/retention, and durable watch history under churn).
- Storage alignment: the public HA target is now `etcd` authority shared with the core controller. Postgres remains supported for externalized and transitional shim deployments, but the convergence target is one `etcd`-backed revision, compaction, and lease model.
- AuthZ alignment: map RBAC verbs to K8s subresources (`pods/exec`, `pods/portforward`, `pods/log`) with explicit audit records per session.
- Testing alignment: run an initial Sonobuoy pass on a dev/kind target to capture a baseline fail list; track the conformance-lite skip set and shrink it as semantics land.
