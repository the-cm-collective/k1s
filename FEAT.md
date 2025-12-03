Got it — here’s a pragmatic path to a “minimal app engine” that runs well on a 1‑CPU/2‑GB VPS and gives you: image management, a basic “pod” abstraction, zero‑downtime deploys, health checks, ingress/TLS, logs/metrics, and a tiny CLI. You’ll keep things simple, but leave room to evolve.

# Architecture at a glance

* Runtime: Podman (default) with Docker fallback.
* Controller: single daemon with a reconcile loop + SQLite state.
* Spec: one declarative YAML per app (versioned), your “Deployment”.
* Ingress/TLS: Caddy; controller writes vhost config from specs and can use ACME internal or BYO certs.
* Observability: logs via runtime adapters; Prometheus metrics via HTTP API; events stored in SQLite. Metrics now include system/app/replica gauges, per‑app reconcile duration sum/count, last reconcile ts/duration, rollout ops counters, canary weight and step counters, hook durations, container restarts, and probe backoff seconds.
* Secrets: sealed secrets (age/sops) decrypted at deploy; plaintext allowed only for local dev.
* CLI: `ae` plus a kubectl‑like `k1s` wrapper.
* Packaging & Ops: controller Dockerfile; systemd unit; GitHub Actions release workflow (wheel + image).
* Dev Stack: Docker Compose for controller + Prometheus + Grafana with pre‑provisioned dashboards.

---

## Feature Matrix (as of 2025-11-10)

- Controller daemon
  - `python -m ae.controller --once|--loop --interval N --specs DIR [--watch]`
  - Optional HTTP API and Prometheus metrics: `--metrics-port PORT`
  - File‑watching via watchdog when available; falls back to polling

- Runtime adapters
  - Podman and Docker adapters with image pull, create/start/cleanup, log read, exec, volume helpers
  - Podman default OCI runtime: prefers `crun` (falls back to `runc` when unavailable); honors host/user `containers.conf` and supports explicit override
  - Security mapping: read‑only rootfs, cap drops, seccomp (RuntimeDefault/Unconfined/Localhost), AppArmor profile

- Reconcile features
  - Rolling replace with `rollout.maxSurge` and `rollout.maxUnavailable`
  - Readiness/liveness: HTTP, TCP, and exec probes with backoff/jitter and history
  - Crashloop detection with recreate cooldown and eventing
  - Scale, delete, backup/restore; revision history + rollback

- Ingress/TLS (Caddy)
  - Multi‑path routing; prefer‑first policy for new revision
  - Canary strategy: `rollout.strategy=canary` with `weight` and optional `auto { start, step, intervalSeconds, max }`; state persisted in SQLite
  - TLS: internal ACME by default; BYO via `tlsCertPath/tlsKeyPath` or `tlsSecretName` resolved from `AE_TLS_DIR`

- Spec surface
  - Resources, volumes, env, service (NodePort supported), security, health, rollout, configRefs, secretRefs, storage
  - File projections for configs/secrets into `state/projections/<app>-rev<rev>/`

- CLI
  - Core: `apply`, `status [--events] [--wide] [--json]`, `logs`, `revisions`, `rollback`, `scale`, `delete`, `backup {create|restore|list|verify}`
  - K8s: `export-k8s` (Deployment/StatefulSet/Service/Ingress + optional HPA/PDB/SA/PVC/ConfigMap/Secret; presets; `--validate`), `k8s-check --policy strict`, `k8s-report`
  - Registry and secrets/config helpers; `metrics`, `events`, `plan`, `version`
  - Image supply‑chain: `verify-image` (cosign wrapper)
  - `k1s` kubectl‑like frontend for familiar verbs
  - Remote exec: `ae exec <app> [--container <name>] -- <cmd...>` (local and HTTP API)

- HTTP API (when enabled)
  - Read endpoints: `/metrics`, `/status`, `/status/<app>`, `/events/<app>`, `/logs/<app>`
  - Optional mutations (dev): `/scale/<app>`, `/delete/<app>`, JSON `apply`, and container `exec` via environment‑gated roles
  - Token roles and expiries: `AE_API_{READ,SCALER,ADMIN}_TOKEN[_EXPIRES]`
  - Metrics: app/replica gauges, reconcile sum/count + last, rollout operations, canary weight/steps, hook durations, container restarts, probe backoff.

---

## Podman: Default OCI Runtime = crun (2025-11-10)

Why
- Faster startup, lower RSS, and better cgroup v2 behavior on modern distros.
- Keeps us OCI‑compliant and aligns with common CRI‑O setups.

Behavior
- With the Podman backend (`AE_RUNTIME_BACKEND=podman`), the adapter prefers `crun` by default.
- Fallback: when `crun` is not installed, Podman uses its configured default (typically `runc`).
- Admin intent wins: we honor system/user `containers.conf`; an explicit override can force a runtime when needed.

Operator Controls
- Host config (recommended):
  - System‑wide: `/etc/containers/containers.conf` → `[engine] runtime = "crun"`
  - Rootless: `$HOME/.config/containers/containers.conf` → `[engine] runtime = "crun"`
- AE override (optional):
  - `AE_OCI_RUNTIME=crun|runc` to force the Podman adapter to pass `--runtime=<value>` to `podman run` and sidecar/exec invocations.
  - Unset to let Podman’s configured default apply.

Verification
- Detect effective runtime: `podman info --format '{{ .Host.OCIRuntime.Name }}'`
- Smoke test with adapter: `ae apply -f specs/examples/echo.yaml` then `ae status --verbose`; `podman inspect <id> --format '{{ .OCIRuntime }}'` should report `crun` when available.

Compatibility
- Docker remains `runc` by default. You can register `crun` in `/etc/docker/daemon.json` and run `--runtime=crun` if desired; no change required for k1s.
- Kubernetes alignment: common production stacks are CRI‑O+crun or containerd+runc. Preferring `crun` under Podman keeps parity with the former without impacting the latter.

Testing
- Unit: extend `tests/unit/test_runtime_podman.py` to assert the adapter adds `--runtime crun` when `AE_OCI_RUNTIME=crun` and omits when unset.
- Integration: run existing Podman suites with `AE_OCI_RUNTIME=crun` on a host with `crun`; verify readiness/liveness and logs.
- Bench (optional): compare idle/rollout memory using `scripts/bench/` for Docker vs Podman+crun.

Rollout
- Safe by default: hosts without `crun` keep current behavior.
- Documented here and in `docs/OCI-RUNTIME-FEAT.md`; no migration steps for users preferring `runc`.

---

## Kubernetes Spec Compliance (as of 2025-10-31)

Summary
- Targets only stable APIs: `apps/v1`, `v1`, `networking.k8s.io/v1`, `policy/v1`, `autoscaling/v2`.
- Exporter is conservative: emits a portable subset; optional resources gated by flags.
- Local runtime intentionally diverges from full Kubernetes semantics (single-node, no ClusterIP), but the exporter strives for upstream‑compatible YAML.

Workloads
- Deployment: supported. Pod template maps container `env`, `ports`, `resources`, `securityContext`, `readinessProbe`/`livenessProbe`, `terminationGracePeriodSeconds`, optional `priorityClassName`.
- StatefulSet: supported via `--workload statefulset`. Emits headless Service and `volumeClaimTemplates` when `--emit-storage` is set.
- Not supported: DaemonSet, Job, CronJob.

 PodSpec and Container (current parity)
 - Env: key/value pairs supported; `valueFrom` for key refs (`configMapKeyRef`/`secretKeyRef`) emitted from `configRefs`/`secretRefs`; optional `envFrom` via `configRefs[].envFrom` and `secretRefs[].envFrom`; `fieldRef`/`resourceFieldRef` passed through (local runtime resolves `metadata.{name,namespace}` and basic `resources.{requests,limits}` cpu/memory for convenience).
 - Resources: requests/limits pass‑through. `--require-requests` can enforce both present; checker warns when limits exist without requests.
 - Probes: readiness, liveness, startup (HTTP/TCP/exec). Runtime gates liveness on startup success.
 - SecurityContext: container fields (`runAsUser`, `runAsGroup`, `readOnlyRootFilesystem`, capability drops, `seccompProfile`); Pod‑level `securityContext` (`fsGroup`, `seccompProfile`) supported. AppArmor via annotation. `--default-security` applies conservative defaults.
 - Image pulls: `imagePullPolicy` and `imagePullSecrets`. Helper: `ae registry kubesecret` renders a `dockerconfigjson` Secret from `~/.config/ae/registries.yaml`.
 - Command model: `command` + `args`, `workingDir`, and termination message `{Path,Policy}`.
 - DNS & identity: `dnsPolicy`, `dnsConfig`, `hostname`, `subdomain`; `hostAliases`; `enableServiceLinks`, `shareProcessNamespace`, `hostNetwork`, `hostPID`, `hostIPC`, `nodeSelector`, `setHostnameAsFQDN` pass‑through when set.
 - Volumes: storage mounts from `spec.storage` when `--emit-storage`. When `configRefs[].files[]` or `secretRefs[].files[]` are present, exporter emits a single projected volume with per‑key `items.path` mounted at `/var/run/ae/config`. Lifecycle hooks (postStart/preStop) exported; runtime executes preStop exec and best‑effort HTTP/TCP with timeout.

 Service
 - ClusterIP emitted when ports exist. Multi‑port mapping supported via `spec.service.ports` with validation for duplicate names/ports.
 - Types: `NodePort`/`LoadBalancer` honored; `nodePort` validated in 30000–32767 range; `externalIPs` passthrough.
 - Session Affinity: `service.sessionAffinity: ClientIP|None` and optional `sessionAffinityConfig.clientIP.timeoutSeconds` passed through.
 - healthCheckNodePort not supported.

 Ingress
- `networking.k8s.io/v1` with `PathType=Prefix` by default (Traefik limited to Prefix/ImplementationSpecific). Multiple paths supported. TLS enabled by default; `tls.secretName` set when `tlsSecretName` provided; `ingressClassName` optional. `--ingress-annotation` and `--ingress-preset` provide opt-in controller hints.
- Advanced regex paths, multiple backends per rule, and canary annotations are out of scope.

 Policy, Autoscaling, Accounts
- HPA (`autoscaling/v2`): CPU utilization, memory utilization, or memory AverageValue. Requires requests unless `--allow-hpa-no-requests` set. Exposes `behavior.scaleUp/scaleDown` knobs via CLI JSON.
- PDB (`policy/v1`): emits when `--emit-pdb` and replicas > 1, with either `minAvailable` (default 1) or `maxUnavailable`. Accepts integer or percentage values.
- ServiceAccount: emitted/attached when `--service-account <name>` is provided. RBAC Roles/Bindings are not emitted.
- NetworkPolicy: pass‑through from `spec.networkPolicy`. Exporter can generate default‑deny ingress/egress with optional DNS/HTTP(S) allowances when flags are set; backend preset allows RFC1918 egress on common DB/cache ports; checker advises when missing.

Scheduling
- Pass‑through: `affinity`, `tolerations`, `topologySpreadConstraints`, `priorityClassName`, `nodeSelector`. Optional injection of a host‑level `topologySpreadConstraints` when replicas>1 and none provided.

Validation and Tooling
- `ae export-k8s --validate` performs offline structural checks; `k8s-check --policy strict` applies FEAT checklist; kubeconform integration via `k8s-check --kubeconform`; `export-k8s --split` writes per‑resource YAML.

- Notable Gaps vs. Kubernetes (updated 2025-11-12)
- Ephemeral containers not supported. DaemonSets not emitted.
- Advanced Ingress features (regex paths, canary annotations, multiple backends per rule) remain out of scope.
- Service healthCheckNodePort not supported.
 - Storage: `emptyDir` supported (ephemeral); PVCs supported; local hostPath remains non-portable.
- RBAC (Role/RoleBinding/ClusterRoleBinding) not emitted.
- CRDs and admission webhooks out of scope.

Planned Improvements (refreshed 2025-11-10)

- Multi‑container support (exporter + runtime) and initContainers (exporter + runtime):
  - [x] Sidecars in runtime with health aggregation; container‑targeted logs/exec.
  - [x] InitContainers runtime (sequential with timeouts and events).
  - [x] Exporter emits initContainers/containers and projected config/secret volumes.
  - [x] Export per‑container probes/lifecycle when using `spec.containers[*].health|lifecycle`.
- Config/Secret volumes parity:
  - [x] File projections with per‑container `projectionMounts` in runtime.
  - [x] Exporter: support explicit `items` (ConfigMap/Secret) with mode/path and per‑container mounts.
- Policy & autoscaling:
  - [x] PDB percent values in exporter and CLI.
  - [x] HPA scaleUp/Down behavior (autoscaling/v2) via `--hpa-behavior-{up,down}` JSON.
  - [x] Optional `storageClassName` and PVC `accessModes` selection.
- Service/Ingress:
  - [x] Validation for ingress host/path and service ports.
  - [ ] Extended ingress annotations (nginx/traefik) behind explicit flags.
- Observability:
  - [x] Grafana/Prometheus dev stack with pre‑provisioned dashboards.
  - [ ] Add per‑replica readiness histories endpoint; dashboard panels for histories.
- Multi‑container runtime parity: sidecars and initContainers implemented; ephemeral containers remain.
- Config/Secret volume mounts parity with `items` selection (beyond projected files convenience).
- Extended Service/Ingress knobs behind explicit flags.

---

## Kubernetes Spec Compliance (2025-11-10)

Summary
- Exporter coverage: Deployment, StatefulSet (with headless Service), Service (ClusterIP/NodePort), Ingress (networking.k8s.io/v1), PodDisruptionBudget (policy/v1), HorizontalPodAutoscaler (autoscaling/v2), ServiceAccount, PersistentVolumeClaim, ConfigMap, Secret, NetworkPolicy.
- PodSpec fields: multi‑container and initContainers export; readiness/liveness/startup probes; lifecycle (postStart/preStop); resources (requests/limits); securityContext (seccomp/AppArmor via annotations, cap drops, read‑only root); DNS policy/config; imagePullSecrets; topology spread; affinity/tolerations; host aliases; optional serviceAccountName.
- Runtime parity: single‑node; multi‑container support via sidecars with aggregated readiness and per‑container logs/exec; initContainers sequential with timeouts and events; preStop exec honored with grace period. Ephemeral containers not supported.

What’s out of scope
- DaemonSet, CRDs, admission webhooks; Ingress advanced features (regex, canary annotations); Service healthCheckNodePort.

Validation & Reporting
- Offline checks: `ae export-k8s --validate` and `ae k8s-check --policy strict`.
- Schema: optional kubeconform via `k8s-check --kubeconform`.
- Server‑side dry‑run: `ae k8s-report --run-dry-run` (uses `kubectl` if present).
- Compliance report JSON: `ae k8s-report --samples specs/examples/echo.yaml specs/examples/multi-replica-echo.yaml -o docs/site/k8s_status.json`.

Notes
- PDB percent values supported by both exporter and CLI (integers or percent strings).
- HPA behavior (scaleUp/scaleDown) is emitted when `--hpa-behavior-{up,down}` is provided with autoscaling/v2 JSON.

---

## k3s High‑Priority Gaps (Q4 2025)

Why: We already export portable Kubernetes YAML. These items tighten the experience on k3s (Traefik, servicelb/local-path) and reduce manual cluster prep.

 - RBAC emitters (Role/RoleBinding) — DONE (2025-11-12)
  - Scope: emit minimal Namespaced Role + RoleBinding when a `--service-account` is attached.
  - Acceptance: `kubeconform` clean; permission set documented in docs/k8s-compliance.md.

 - Batch workloads (Job/CronJob) exporters — DONE (2025-11-12)
  - Scope: map a subset of our spec to `batch/v1 {Job,CronJob}` with container spec reuse and optional backoff/ttlSecondsAfterFinished.
  - Acceptance: export-k8s `--workload job|cronjob` generates valid YAML; docs updated with flags.

 - TLS Secret generator (BYO certs) — DONE (2025-11-12)
  - Scope: helper to build `kubernetes.io/tls` Secret from PEM paths or `AE_TLS_DIR` and wire into Ingress `tls.secretName`.
  - Acceptance: docs include k3s/Traefik example; `k8s-report` sample succeeds with precreated Secret.

 - `emptyDir` support — DONE (2025-11-12)
  - Scope: allow ephemeral volumes in spec and export to PodSpec `volumes[].emptyDir` with medium selection.
  - Acceptance: exporter renders; `k8s-check` warns when used for stateful data.

- Ingress presets for Traefik
  - Scope: add opt‑in annotation preset for Traefik (timeouts, proxy headers) behind `--ingress-preset traefik`.
  - Acceptance: preset documented; does not alter defaults unless specified.

---

## Kubernetes API Shim (Helm/kubectl) — MVP

Problem
- Helm and kubectl expect a Kubernetes API server. k1s is not Kubernetes, so charts can’t be installed directly. We need a minimal API shim that accepts core Kubernetes objects and reconciles them into k1s, enabling `helm upgrade --install` and kubectl workflows for simple, non‑CRD charts.

Goals (MVP)
- Serve a small, correct slice of the Kubernetes API so Helm/kubectl work end‑to‑end for basic apps:
  - Discovery: `/version`, `/api`, `/apis`, group/version resource lists.
  - Core kinds (CRUD/list/watch): Namespaces, Secrets, ConfigMaps, ServiceAccounts, Services.
  - apps/v1: Deployments (+ `/status`, `/scale`).
  - networking.k8s.io/v1: Ingress.
  - Helm release storage in `kube-system` via `Secret`/`ConfigMap`.
  - Watches with ADDED/MODIFIED/DELETED notifications.

Non‑Goals (MVP)
- CRDs and admission webhooks, server‑side apply (SSA), full RBAC enforcement, exec/attach/logs/port‑forward, HPA, DaemonSet.

Architecture
- `apishim-server`: HTTPS listener that exposes the endpoints above; discovery + minimal OpenAPI v2; auth via existing k1s token (allow‑all authz for MVP).
- SQLite object store keyed by `(group,version,resource,namespace,name)` with `metadata/spec/status` JSON columns.
- `kube-adapter-controller`: watches stored objects and translates them to k1s `App` + Caddy ingress, then writes back Deployment status/conditions for `helm --wait`.
- Virtual Endpoints/EndpointSlice views derived from k1s readiness to back Services.

K8s → k1s Mapping (happy path)
- Deployment → App: image, command/args, env/envFrom, ports, probes, resources, security, preStop; single‑container only for MVP; name prefixing `<ns>--<name>`.
- Service: ClusterIP emulated locally; NodePort validated (30000–32767) and bound on host; selectors match Deployment template labels.
- Ingress: rules to Caddy; `ingressClassName` passthrough (default `caddy`); TLS via `tls.secretName`.
- Secret/ConfigMap/ServiceAccount: persisted as‑is for Helm and env/envFrom.

Status & Conditions (contract for `--wait`)
- Deployment: set `readyReplicas`, `availableReplicas`, `observedGeneration`; conditions `Available=True` when readyReplicas == spec.replicas, `Progressing` updates during reconcile and on timeout with Reason.

Phases & Acceptance
- Phase 0 — Discovery + Core Storage
  - Endpoints live; CRUD+watch for NS/CM/Secret/SA; Helm can list releases; `kubectl get ns,cm,secret -A` works.
- Phase 1 — Deployment/Service/Ingress + Status/Scale
  - `helm upgrade --install <chart> --wait` completes for a simple, no‑CRD chart; `kubectl rollout status deploy/<name>` reports success; Ingress reachable via Caddy.
- Phase 2 — Batch + Dry‑Run + Polish
  - Add Job/CronJob, `--dry-run=server`, richer OpenAPI, clear 4xx/5xx errors; `helm uninstall` garbage‑collects derived k1s resources.

Operational Notes
- TLS: self‑signed for shim; provide kubeconfig helper.
- SSA: return 415 with guidance (disable SSA) when `apply-patch` content‑type is used.
- CRDs: reject with 403 and actionable message.

Risks
- CRD‑heavy charts are unsupported; document fallback via `helm template` + converter.
- Multi‑container Pods rejected initially (422) with clear message; plan follow‑up for multi‑container support.

Developer Checklist / TODO
- [ ] apishim HTTP skeleton: `/healthz`, `/readyz`, `/version`, discovery trees.
- [ ] SQLite object store + watch pub/sub; JSON merge‑patch support.
- [ ] Namespaces/Secrets/ConfigMaps/ServiceAccounts CRUD + watch.
- [ ] Deployments CRUD + `/status` + `/scale`; k1s App adapter; status propagation.
- [ ] Services translation (ClusterIP/NodePort) + validation; virtual endpoints.
- [ ] Ingress translation to Caddy + TLS secret wiring.
- [ ] kubeconfig helper: `ae apishim kubeconfig`.
- [ ] Helm smoke test: trivial chart install with `--wait`.
- [ ] Error texts for SSA/CRDs/multi‑container.

Getting Started (dev)
```bash
# Start shim (dev): serves on https://127.0.0.1:8445
python -m ae.apishim serve --listen 127.0.0.1:8445 --dev-tls --token $(ae token)

# Generate kubeconfig context
python -m ae.apishim kubeconfig --server https://127.0.0.1:8445 --token $(ae token) > ~/.kube/config

# Sanity
kubectl --context k1s-apishim version && kubectl api-resources
```

References
- Detailed design: `docs/design/api-shim.md`


- PDB percent on CLI — DONE (2025-11-12)
  - Scope: allow percent strings for `--pdb-{min-available,max-unavailable}` to match exporter capability.
  - Acceptance: CLI validates integers or percent; exporter receives value verbatim.

 - PodSecurity labels preset — DONE (2025-11-12)
  - Scope: optional Namespace labels (`pod-security.kubernetes.io/enforce: baseline|restricted`).
  - Acceptance: preset writes a Namespace YAML when requested; documented caveats for existing clusters.

 - NetworkPolicy provider note — DONE (2025-11-12)
  - Scope: call out that enforcement depends on k3s NP backend; provide preset shortcuts for web/backend.
  - Acceptance: FEAT.md and docs/k8s-compliance.md updated with guidance.

Tracking: regenerate docs/site/k8s_status.json after each milestone via `ae k8s-report`.

# Milestones (build order)

# Progress Log (2025-10-30)
* [x] Controller daemon with HTTP API/metrics, file watch, and graceful shutdown.
* [x] Runtime adapters for Podman and Docker with security mapping and volume helpers.
* [x] Health manager with HTTP/TCP/exec probes and backoff reporting; crashloop detection + cooldown.
* [x] Ingress: Caddy templates, multi‑path, BYO TLS and `tlsSecretName` resolver; canary with optional auto progression and persisted state.
* [x] CLI: rich surface including `export-k8s`, `k8s-check`, `k8s-report`, `backup`, `verify-image`, `registry`, `events`, `metrics`.
* [x] K8s export: presets, offline validation, HPA (CPU/memory utilization and AverageValue), PDB, SA, PVC, ConfigMap/Secret emission.
* [x] CI: k8s conformance jobs and kubeconform validation for exported YAML; e2e samples.
* [x] 2025-11-10 – Podman default OCI runtime = crun (safe fallback):
  - Prefer `crun` for Podman backend to improve startup and memory; fall back to `runc` when `crun` is absent.
  - Honor host/user `containers.conf`; optional `AE_OCI_RUNTIME=crun|runc` to force.
  - Added verification guidance (`podman info --format '{{ .Host.OCIRuntime.Name }}'`).
* [x] 2025-11-12 – PDB percent on CLI:
  - CLI now accepts integer or percent strings for `--pdb-min-available`/`--pdb-max-unavailable`.
  - Exporter already maps values verbatim to policy/v1 PDB; validation added with bounds checking (0–100%) for percent forms.
* [x] 2025-11-12 – RBAC emitters tied to ServiceAccount:
  - Exporter emits namespaced Role and RoleBinding when `--service-account` is provided.
  - Role includes conservative read-only rules: pods, pods/log, services, endpoints, events, configmaps.
* [x] 2025-10-29 – Runtime hardening + image verification:
  - Docker/Podman adapters map `spec.security.seccomp*` and `apparmorProfile` to runtime flags (`security_opt`).
  - New CLI: `ae verify-image` (cosign wrapper) for key-based or keyless signature verification; supports `--json`.
  - Planner emits security hardening warnings (read-only root FS, cap drops, seccomp/AppArmor not set).
  - Optional systemd hardening: set `AE_SYSTEMD_HARDEN=1` when running `scripts/install.sh` to install strict drop-ins.

* [x] Phase 3 – K8s export + checks: `ae export-k8s` emits Deployment/Service/Ingress; `ae k8s-check` runs the portability checklist (with `--policy strict`).
* [x] Phase 5 – Policy + rollout safety: PDB/HPA/PVC/ConfigMap/Secret emission; ServiceAccount attach; conservative default securityContext via flags.
* [x] Phase 6 – Operational polish: `export-k8s` presets, `--validate`, and tests.
* [x] Phase 7 – Conformance + Ingress polish: Multi‑path ingress; exporter `tlsSecretName`; CI kind workflow with server‑side dry‑run.
* [x] Phase 8 – Drift guard + richer checks: kubeconform in CI; HPA prerequisites guidance; memory AverageValue targets; configurable PDB.
* [x] Phase 9 – Rollout controls + API tokens: `rollout pause|resume`; ingress canary weighting and auto progression; `ae api tokens --generate`.
* [x] Phase 10 – BYO TLS + strict checks: Ingress BYO cert/key; strict policy in `k8s-check`.

# Progress Log (2025-10-23)

* [x] Phase 0 – Environment & scaffolding complete: Python package layout (`src/ae/...`), tooling (`pytest`, `ruff`, `mypy`, pre-commit), bootstrap script, and dev assets committed.
* [x] Phase 1 – Core spec and reconcile skeleton landed: Pydantic manifest loader, runtime stub, SQLite snapshot store, and CLI commands (`apply`, `status`, `logs`) with unit coverage.
* [x] Phase 2 – Docker runtime + health loops: Real Docker adapter with create/start/cleanup flow, readiness/liveness evaluator with initial-delay semantics, probe history persisted for CLI inspection.
* [x] Phase 3 – Ingress & TLS automation: Added Caddy templating/reload hooks, persisted ingress host metadata, and surfaced replica/ingress status via CLI.
* [x] Phase 4 – Rollouts & rollbacks: Revision-aware reconciler/runtime, revision history + CLI rollback/list commands, and health-gated status tracking. Hooks: optional `rollout.hooks.preSwitch|postSwitch` with `exec` (list) or `tcp.port`, `timeoutSeconds`; preSwitch runs before ingress switch and blocks on failure; postSwitch runs best-effort and records events.
* [x] Phase 5 – Secrets & registry auth: SOPS-backed secret manager with env injection, registry credential loading, and CLI wiring.
* [x] Phase 6 – Observability baseline: Metrics snapshot CLI, event logging in SQLite, and registry/secret guidance updated.

---

## Gap Audit (2025-10-30)

- Networking
  - ClusterIP compatibility and multi-node path — see `docs/CLUSTERIP.md` for the phased plan (Phases 1–3).

- Packaging and distribution
  - Publish a controller container image (GitHub Container Registry) and document image‑based install.
  - Add a release workflow that builds wheels and images; pin toolchain versions in `scripts/bootstrap.sh`.

- Documentation sync
  - Align FEAT.md, runbook, and ingress docs with: canary auto progression, TLS `tlsSecretName` resolver, crashloop cooldown, and HTTP API mutation roles.
  - Add examples for security profiles (seccomp Localhost JSON, AppArmor names) and recommended defaults.

- Policy and validation depth
  - Expand `k8s-check` coverage for service port edge cases (NodePort ranges) and stricter presets for production.
  - Enrich probe/health reporting endpoints (expose recent evaluation windows via API).

- Observability polish
  - Add per‑app reconcile histograms/counters to `/metrics` and document Grafana panels around them.

- Platform scope
  - Multi‑node scheduling/placement: explicitly out of scope (single‑node only).
  - OS support: focus on Linux; call out macOS/WSL2 as best‑effort dev targets.

---

## Last‑Mile Checklist

- [x] Controller canary promotion: ingress bias now ramps via controller state (SQLite) with step/interval/max; UI/API reflect progress.
- [x] Ingress TLS parity: `tlsSecretName` resolves to local PEMs (via `AE_TLS_DIR`) and is wired to Caddy; BYO TLS also supported.
- [x] Requests strictness: `k8s-check --policy strict` escalates missing requests; `export-k8s --require-requests` fails when cpu/memory requests are absent; planner emits warnings.
- [x] Rollout options: `--pdb-max-unavailable` exposed; exporter validates exclusivity with `--pdb-min-available`.
- [x] HPA targets: Memory `AverageValue` supported with validation; utilization modes require requests unless explicitly allowed.
- [x] API tokens: generation/rotation with optional TTLs; docs added (see docs/api-auth.md).
- [x] CI matrix: Kind and k3s jobs run conformance and `k8s-check --policy strict` on sample manifests.
- [x] StartupProbe parity: add `spec.health.startup` → export `startupProbe` and gate runtime liveness until it passes.
- [x] Remote exec (local + HTTP): `ae exec <app> [--container <name>] -- <cmd...>` and POST `/exec/<app>`.
- [x] Multi‑container runtime: sidecars ensured per replica, shared projection mounts, container‑targeted logs/exec, aggregated readiness.
- [x] InitContainers: sequential run with timeouts and events.
- [x] Config/Secret parity: file projections mounted to all containers with optional `projectionMounts`.
- [x] Service/Ingress validations: stricter `k8s-check` for ingress host/path and service port naming/targets.
- [x] NetworkPolicy presets: `--np-preset web|backend` (backend allows RFC1918 egress for common DB/cache ports).
- [x] Metrics: per‑app reconcile sum/count, canary weight + step counters, hook timings, crashloop flags, container restarts.
 - [x] Image pull knobs: support `spec.imagePullPolicy` and `spec.imagePullSecrets` → export to container and pod respectively.
 - [x] Lifecycle parity (export): add `spec.lifecycle.postStart|preStop` with `exec|httpGet|tcpSocket`; exporter emits `container.lifecycle.*`.
 - [x] Service Session Affinity: support `service.sessionAffinity` and `sessionAffinityConfig.clientIP.timeoutSeconds`.
 - [x] DNS & identity: pass through `dnsPolicy`, `dnsConfig`, `hostname`, `subdomain` to PodSpec.
 - [x] Runtime preStop execution (exec): honor `spec.lifecycle.preStop.exec` on container replacement/deletion with timeout; emit events and integrate with grace period.

## Near‑Term Focus (Q4 2025)

1) Releases and packaging
- Ship controller image and PyPI releases; docs for image‑based and systemd installs.

2) Docs and examples
- Update runbook and examples to showcase canary auto, TLS secret resolution, and security profiles.

3) Policy/validation
- Tighten `k8s-check` and exporter validations for prod defaults; add opt‑in strict preset files.

4) Metrics
- [x] Add reconcile duration histogram (sum/count) and canary step counters; document sample Grafana.

5) K8s spec parity (incremental)
- Ship runtime preStop execution semantics and tests.

## Appendix — Spec v0 (reference)

```yaml
apiVersion: v1
kind: App
metadata:
  name: myapp
spec:
  image: ghcr.io/you/myapp:1.0.0
  replicas: 1
  command: null
  env:
    - name: FOO
      value: bar
  ports:
    - name: http
      containerPort: 8080
  health:
    readiness:
      httpGet: { path: /healthz, port: 8080 }
      initialDelay: 5
      timeout: 2
    liveness:
      httpGet: { path: /healthz, port: 8080 }
      initialDelay: 10
      timeout: 2
  resources:
    requests: { cpu: 0.05, memory: 128Mi }
    limits:   { cpu: 0.3,  memory: 256Mi }
  volumes: []        # host paths or named volumes
  ingress:
    host: myapp.example.com
    path: /
    tls: true
  registryAuthRef: default # optional
```

**Controller responsibilities (reference):**

* Parse spec → compute desired container set (replicas, names).
* **Reconcile loop** (every 3–5s or on file watch):

  1. Pull image (with auth if set).
  2. Start new container(s) with generated name/version label.
  3. Health-gate readiness (HTTP/TCP/exec).
  4. On success, stop & remove old version (rolling-replace 1 by 1).
  5. Update ingress (write Caddy config and reload).
  6. Persist status in SQLite (`apps`, `revisions`, `replicas`).

**Data model (SQLite reference):**

* `apps(name TEXT PK, gen INT, desired_replicas INT, image TEXT, …)`
* `revisions(app, rev INT, image, created_at, status, PRIMARY KEY(app,rev))`
* `replicas(app, rev, instance_id, container_id, state, started_at, …)`

**Minimal health checker (reference):**

* Side thread that hits readiness/liveness endpoints; mark instance `Ready` only after success.

## Ingress & TLS (reference)

* Choose **Caddy** for simplest ACME:

  * Controller writes a site block per app:

    ```
    myapp.example.com {
      reverse_proxy 127.0.0.1:<allocated_nodeport_or_host_port>
    }
    ```
  * Reload via `caddy reload`.
* If you prefer nginx, template + `nginx -s reload` (no built-in ACME).

## Zero‑downtime deploy & rollback (reference)

* **Strategy: RollingReplace( maxUnavailable=0, maxSurge=1 )**

  * Create `rev = current+1`; start a new container; wait for readiness; switch ingress; stop previous.
* Keep last N revisions; `ae rollback myapp --to-rev <n>` switches image/env back and reconciles.

## CLI & UX (reference)

Commands (talk to controller over a tiny HTTP API or just call into the db/socket):

```
ae apply -f specs/myapp.yaml
ae get apps
ae status myapp
ae logs myapp [-f] [--container <id>]
ae rollout myapp --image ghcr.io/you/myapp:1.0.1
ae restart myapp
ae rollback myapp --to-rev 3
```

## Secrets & registry auth (reference)

* Store secrets as **sops**-encrypted YAML alongside specs:

  ```yaml
  apiVersion: v1
  kind: Secret
  metadata: { name: myapp-secret }
  data:
    DATABASE_URL: ENC[AES256_GCM,...]
  ```
* Controller decrypts at apply (key from env or age key file), injects env vars or a mounted file.
* Registry creds: `~/.ae/registries.yaml` → Docker login on demand.

## 6) Observability (day 3)

* **Logs**: tail `docker logs` by app/revision; expose `/logs?app=myapp` streaming endpoint; wire `ae logs`.
* **Metrics**:

  * Poll `docker stats` for CPU/mem/net → expose `/metrics` as Prometheus text.
  * Per-app counters: restarts, readiness failures, rollout duration.
* **Events**: append a lightweight event log table for `Applied/Created/Ready/Failed`.

## 7) Resource control & safety (day 3+)

* Translate `resources.limits` to Docker flags (`--memory`, `--cpus`).
* Enforce per-app memory cap to avoid host OOM.
* Optional: **cgroup v2** constraints + oomd.

## 8) Backup & restore (quick)

* Backup: SQLite DB + `/var/lib/ae/volumes/` + specs repo.
* Restore: install runtime → restore DB/volumes → `ae reconcile --all`.

---

# Compatibility story (realistic)

Full `kubectl/k9s` against your daemon requires the Kubernetes API surface — not worth it. Instead:

* **K8s-like spec**: keep keys/shape close to K8s (Deployment/Ingress fields) so you can later write a translator.
* **Translator (optional, later)**: `k2ae` tool that ingests a subset of K8s Deployment/Service/Ingress YAML and emits your `App` spec. That keeps “basic” compatibility without implementing the kube API.

---

# Minimal code skeleton (Python, asyncio + Docker SDK)

```
ae/
  __init__.py
  controller.py        # reconcile loop
  runtime.py           # docker/containerd driver
  ingress.py           # caddy/nginx templating + reload
  health.py            # readiness/liveness probes
  store.py             # sqlite models
  secrets.py           # sops/age integration
  metrics.py           # exporter
  api.py               # tiny HTTP for CLI
  spec.py              # pydantic models, validation
cmd/
  ae                   # click/typer CLI
specs/
  myapp.yaml
```

**Reconcile loop (high-level pseudocode):**

```python
while True:
    desired = load_specs()
    current = store.load_state()
    plan = diff(desired, current)

    for op in plan:
        if op.type == "deploy":
            img = op.spec.image
            runtime.pull(img, auth=auth_for(img))
            rev = store.new_revision(op.app, img)
            inst = runtime.start(op.app, rev, op.spec)     # returns container_id
            if wait_ready(inst, op.spec.health.readiness, timeout=120):
                ingress.upsert(op.app, op.spec.ingress, port=inst.port)
                store.mark_ready(op.app, rev, inst.id)
                runtime.stop_old(op.app, keep=op.spec.replicas)
            else:
                runtime.stop(inst)
                store.mark_failed(op.app, rev)
                events.emit("ReadinessFailed", op.app, rev)

        elif op.type == "scale":
            # add/remove containers and gate readiness
            ...

    metrics.collect()
    sleep(3)
```

---

# Hard edges to skip (on purpose)

* Multi-node scheduling, overlay networking, CNI — you don’t need it.
* Sidecar patterns, Jobs/CronJobs, HPA — defer.
* Complex RBAC — local admin only; CLI can use a shared token.

---

# Footprint on 2 GB VPS (typical)

* Docker daemon: ~100–150 MiB
* Controller + API + exporter: ~30–60 MiB
* Caddy: ~20–40 MiB
* Your 1–3 apps: per limits (e.g., 128–256 MiB each)
  You’ll stay within budget with headroom.

---

# Ready-to-run checklist

1. Pick Docker + Caddy.
2. Implement `spec.py` (validation) and `runtime.py` (run/stop/pull/logs/stats).
3. Write the reconcile loop with **rolling replace** and **readiness gating**.
4. Template Caddy vhosts and implement `reload`.
5. Add CLI (`apply/get/status/logs/rollout/rollback`).
6. Add a Prometheus exporter endpoint and a tiny `/events` feed.

If you want, I can generate:

* a starter repo layout with `typer` CLI, `pydantic` specs, Docker runtime driver, and Caddy integration; and
* a working `myapp.yaml` you can deploy on your VPS immediately.


---


Short version: build it in **Python** first. Your scope (single node, 1–3 apps, rolling deploys, health checks, Caddy/nginx ingress, SQLite state) fits perfectly. Python lets you ship fast with clean async I/O, and you won’t be CPU-bound. If you later want a single static binary and tighter RAM, port the controller to **Go** behind the same spec/driver interfaces.

Here’s the decision in practical terms.

# What you need (and how languages fit)

| Concern                       | Python                                                                  | Go                                                    | Rust                                               |
| ----------------------------- | ----------------------------------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------- |
| **Dev speed**                 | Excellent (asyncio, FastAPI, Typer, Pydantic)                           | Good (chi/gin, cobra, struct tags)                    | Slowest (safety, boilerplate)                      |
| **Runtime footprint**         | Moderate (~30–80 MiB RSS for your controller/API/exporter)              | Low (~10–30 MiB)                                      | Low–moderate                                       |
| **Concurrency/IO**            | Great with `asyncio`/`uvloop`                                           | Great with goroutines                                 | Great, but more work                               |
| **Docker/containerd clients** | Mature: `docker` SDK, `aiohttp` to Engine API                           | Mature: moby client, containerd client                | Usable: `bollard` (Docker), containerd less mature |
| **Packaging/ops**             | Easy; ship venv or PyInstaller/PEX                                      | Easiest; single static binary                         | Single static binary, but build/publish slower     |
| **SQLite**                    | stdlib `sqlite3` / `aiosqlite`                                          | `mattn/go-sqlite3`, `modernc.org/sqlite` (CGO/no-CGO) | `sqlx` + `sqlite` (ffi)                            |
| **Metrics**                   | `prometheus_client`                                                     | `client_golang`                                       | `prometheus` crates                                |
| **File watch**                | `watchdog`                                                              | `fsnotify`                                            | `notify`                                           |
| **cgroups/limits**            | Call Docker with limits; direct cgroup v2 via `/sys/fs/...` if you want | Same; plus solid cgroups libs                         | Same; crates exist but more glue                   |
| **Future: multi-node**        | Possible, but you’ll want to rewrite hot paths                          | Natural                                               | Natural but heavy lift                             |

# Recommendation path

## Phase 1 (ship quickly): Python

* **Why**: fastest build, you already speak it, perfect for single-node reconcile loop + health checks + ingress templating.
* **Stack**:

  * **Runtime/engine**: Docker Engine via `docker` SDK (or raw HTTP API with `httpx`)
  * **Controller/API**: `asyncio` + `uvloop`, `FastAPI` (or `Starlette`)
  * **Spec/validation**: `pydantic` v2
  * **DB**: `sqlite3` or `aiosqlite`
  * **CLI**: `typer`
  * **Metrics**: `prometheus_client` (expose `/metrics`)
  * **Secrets**: `python-gnupg` or `subprocess` to `sops`/`age` (keep it simple)
  * **File watching**: `watchdog` to trigger reconcile on spec changes
* **Service shape**: one `ae-controller.service` (systemd) + Caddy/nginx systemd unit.
* **Packaging**: start with a venv; move to **PEX** or **PyInstaller** when you want a single-file artifact.

## Phase 2 (tighten ops): optional Go port

* **When**: if you want a smaller resident set, faster cold starts for the daemon, or you foresee multi-node/scheduler work.
* **How**: keep your Python spec models as the **contract**. Write a Go controller that honors the same YAML and HTTP API. You can run both during transition.

# Interfaces to design now (so a Go/Rust swap is painless)

1. **Spec** (stable YAML) → internal model

   * `App`, `Revision`, `Probe`, `Ingress`, `Resources`, `Volume`.
2. **Runtime driver**: `pull(image)`, `start(app, rev, spec) -> container_id, host_port`, `stop(container_id)`, `logs(app[, follow])`, `stats(container_id)`.
3. **Ingress driver**: `upsert(app, host, port, tls)`, `remove(app)`, `reload()`.
4. **Store**: `record_revision(app, image, status)`, `track_instance(app, rev, container_id, state)`, `events.emit(type, app, rev, msg)`.
5. **Health**: `wait_ready(target, probe, timeout)` and a periodic liveness checker.

Keep these as thin Python protocols/interfaces so you can reimplement them in Go later without changing the spec or CLI UX.

# Concrete Python module picks

* `fastapi`, `uvicorn[standard]`, `uvloop`
* `typer` (CLI), `rich` (pretty output), `rich-click`
* `pydantic` (v2), `pyyaml`
* `docker` (Engine SDK) or raw Engine API via `httpx`
* `watchdog` (fs events)
* `prometheus_client`
* `aiosqlite` (if you want non-blocking DB)
* `jinja2` (template Caddy/nginx vhosts)
* `tenacity` (retry image pulls, reloads)

# Resource fit (2 GB VPS reality check)

* Python controller + API + exporter: **~40–80 MiB**
* Caddy/nginx: **~20–40 MiB**
* Docker daemon: **~100–150 MiB**
* Leaves **~1.7 GB** for your apps and page cache; fine for 1–3 small services with sane limits.

# A few engineering tips

* **Async everything**: pull images, health probes, and metrics collection concurrently; cap concurrency to avoid bursty CPU.
* **Readiness gate = ingress switch**: only flip Caddy/nginx to the new backend after readiness passes.
* **Crash-only philosophy**: controller should be idempotent; crash and restart cleanly; reconcile builds actual state from Docker + SQLite.
* **Event log first**: a tiny `events` table + `/events` endpoint will save you hours of debugging later.
* **Don’t overfit secrets**: shell out to `sops` initially; swap with a native lib later if needed.
* **Tests**: fake runtime driver (in-memory) + golden YAML specs; integration tests with Docker-in-Docker if you can.

# Final call

Given your comfort in Python and the project’s scope, **Python is the right base**. You’ll move fastest, the ops footprint is acceptable on a 2 GB VPS, and you can keep a clean seam to port the controller to **Go** later if/when you need a single static binary and even smaller memory.

If you want, I can sketch a minimal repo scaffold (folders, `pyproject.toml`, and stubbed interfaces) that you can `git clone` and start filling in.
## Roadmap and Gaps to Close

This document tracks the current capabilities of k1s and outlines gaps to close as we evolve from a dev/demo orchestrator into a small, production‑ready system.

### Implemented Today
- Single‑node controller with file‑based specs and reconcile loop
- Docker runtime adapter; immutable revisions; rolling replace semantics
- Health probes (HTTP), initialDelay, readiness‑gated ingress switch
- Caddy ingress management with dev container reload
- State in SQLite; events and probe history; metrics snapshot
- HTTP API: `/status`, `/status/<app>`, `/events/<app>`, `/metrics`, `/openapi.json`, `/docs`, `/swagger`, `/redoc`
- CLI tools:
  - `ae apply|status|logs|revisions|rollback|events|metrics|backup`
  - `ae delete <app> [--purge]`, `ae scale <app> --replicas N`
  - `k1s` kubectl‑like front: `get`, `describe`, `apply`, `rollout history|undo`, `logs`, `events`, `delete`, `scale`

### High‑Value Next Steps
1) Service model and networking
   - [done: minimal] Add Service spec with stable host port for single‑replica apps; publish fixed port via Docker and use it in ingress.
   - [done] Multi‑replica load balancing via Caddy with per‑replica upstreams on a shared Docker network (no host ports).
   - [done] Active health checks in Caddy using readiness probe path.
   - [done] Pre‑flight port conflict detection for `service.port`.
   - [done] Basic service discovery naming on single host.
2) Multi‑replica rollout controls
   - Parallel vs ordered startup; surge/unavailable knobs; pre/post hooks
3) Secrets and config
   - First‑class Config and Secret resources; SOPS decryption; mount/env wiring; audits
4) Resource enforcement and resiliency
   - Enforce CPU/memory limits; restart policies; backoff; replica restart counters
5) Scheduling and placement (single host first)
   - Soft/Hard affinities; port conflicts detection; stub scheduler groundwork
6) API maturity
   - Mutating API for apply/scale/delete behind an auth gate; pagination; richer OpenAPI schemas
7) Storage
   - Local PV/PVC‑like semantics; named volumes; retention and backup policies
8) Observability
   - Per‑replica logs/events over API; metrics labels for per‑app series; structured event reasons
9) Security hardening
   - TLS everywhere by default via Caddy; token‑based CLI→API auth; least‑privileged Docker access; audit logging
10) Packaging and distribution
   - pip/pipx install; systemd units; dockerized controller; remote CLI mode to talk to controller API

### Current Focus

- Runtime rigor: probe backoff+jitter; restart counters and simple backoff on container crashes; surface in metrics/status.
  - [done: probes] Added exponential backoff with jitter and period enforcement in health checks; optional cap via AE_PROBE_MAX_BACKOFF (default 30s).
- [partial: restarts] Detect crashloop surges (>= AE_RESTART_THRESHOLD within AE_RESTART_WINDOW_SEC); emit CrashLoopDetected events and surface metrics `ae_app_crashloop{app}` plus per-container `ae_container_restart_count`.
  - [partial: recreate cooldown] After crashloop detection, suppress new replica creation for AE_RECREATE_COOLDOWN_SEC (default 30s) and expose `ae_app_recreate_cooldown_seconds{app}`.
- L4 story: document non‑HTTP multi‑replica patterns (external LB), provide examples and planner warnings.
  - [done: docs+planner] Added docs/l4-services.md and planner warnings for multi-replica non-HTTP ports without ingress.
- Packaging: systemd units and optional controller container image; minimal installer and runbook updates.
- Registry ergonomics: `ae registry login` helpers for common providers; short‑lived tokens workflow.

### CLI Installation and Aliases
- `pipx install .` provides `ae` and `k1s` console scripts (see pyproject).
- For quick aliasing in a shell session: `alias k1s='ae kctl'` or use the provided `k1s` entrypoint.
- Option: add a small installer that writes systemd units and exposes the API on LAN with Caddy TLS.

### Remote Use over LAN
- Controller binds `0.0.0.0` on `--metrics-port`; fronted by Caddy at `https://api.home.arpa:8443`.
- From another host on the LAN:
  - Access read‑only API: `curl https://api.home.arpa:8443/status -k` (dev)
  - Future: CLI `k1s --server https://api.home.arpa:8443` for mutating ops via API (not yet implemented).

### Hardening Plan (initial)
- Network/TLS: Issue local CA (step‑ca) and have Caddy terminate TLS with client auth for API UIs.
- AuthZ: Introduce controller token with scoped roles for mutating endpoints (apply/scale/delete).
- Docker: drop broad privileges; restrict to a dedicated user/group; consider rootless Docker.
- Secrets: enforce SOPS in CI; disallow AE_ALLOW_PLAINTEXT_SECRETS outside dev; key rotation.
- Backups: scheduled state DB + specs archives; integrity checks; restore drills.
- Supply chain: pin base images; enable image trust policy; registry credentials scoped and short‑lived.

---

# TODO 

## Task 1 k8s spec

We want to asses our current level of alignment with K8s best practices.  We will use the [K8s App Operator Playbook](https://kubernetes.io/docs/tasks/configure-pod-container/configure-pod-configmap/) as a reference.  See the chat below, our MVP target is 75% feature parity with k3s, eventually exceeding k3s compliance.

## **Chat History for Research:**

# K8s Spec Assesment & Alignement Path Research Chat

Great topic. There isn’t a single “Kubernetes app spec” document to comply with; portability comes from using the stable Kubernetes APIs and avoiding distro/controller-specific features. k3s is a CNCF-conformant Kubernetes, but it ships with lightweight defaults (containerd, flannel, Traefik/ServiceLB, etc.). If you target the common denominator of upstream K8s **stable** APIs and steer clear of optional add-ons, you’ll be >75% portable to k3s out of the box.

Here’s a pragmatic blueprint.

# What’s the *minimum* portable spec for an app?

If you ship only the items below, using **stable** API groups/versions, you’re generally safe across upstream, k3s, MicroK8s, Kind, etc.

**Core resources (all `v1` unless noted):**

* Namespace (optional, but helpful for isolation).
* ServiceAccount (+ minimal RBAC if the app calls the API).
* ConfigMap / Secret for configuration.
* Deployment (`apps/v1`) or StatefulSet if you truly need stable identities.

---

## K8s Export Coverage (current)

This section summarizes what our exporter (`ae cli export-k8s`) can generate today from an App manifest, the key fields supported, and notable gaps. The intent is to keep a concise, testable checklist tied to code under `src/ae/k8s/` and the examples under `specs/examples/`.

Supported resources (stable APIs only)
- Deployment (`apps/v1`): default.
- StatefulSet (`apps/v1`): when `--workload statefulset` (emits headless Service and uses volumeClaimTemplates when `--emit-storage`).
- Service (`v1`): emitted when container `spec.ports` are present.
- Ingress (`networking.k8s.io/v1`): emitted when `spec.ingress` exists; supports multi-path and optional TLS secret name.
- ConfigMap (`v1`): optional via `--emit-configs`; can inline YAML/JSON with `--inline-configs`.
- Secret (`v1` type Opaque): optional via `--emit-secrets`; can inline YAML/JSON with `--inline-secrets` (dev only; prefer SOPS in practice).
- PersistentVolumeClaim (`v1`): optional via `--emit-storage` from `spec.storage[*]` with `--default-pvc-size` fallback.
- ServiceAccount (`v1`): optional via `--service-account <name>` and mounts on Pod.
- PodDisruptionBudget (`policy/v1`): optional via `--emit-pdb` with `--pdb-min-available` or `--pdb-max-unavailable` (mutually exclusive); only when replicas > 1.
- HorizontalPodAutoscaler (`autoscaling/v2`): optional when `--hpa-min`, `--hpa-max` and at least one target is specified: `--hpa-cpu-target`, `--hpa-mem-target`, or `--hpa-mem-type value --hpa-mem-value 200Mi`.

Deployment mapping (from App → Pod/Container)
- Image, command, env list (literal pairs) → container fields.
- `configRefs[*].env` → `env.valueFrom.configMapKeyRef` per key.
- `secretRefs[*].env` → `env.valueFrom.secretKeyRef` per key.
- Ports → `container.ports[*].containerPort` (names preserved).
- Resources → `resources.requests/limits` for `cpu` and `memory` string quantities.
- Security → `securityContext` fields (`runAsUser`, `runAsGroup`, `readOnlyRootFilesystem`, `capabilities.drop`).
- Default security hardening → `--default-security` applies: `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]` when no explicit `security` is present.
- Probes → readiness/liveness translate HTTP/TCP/exec and thresholds/delays.
- ServiceAccount → `spec.template.spec.serviceAccountName`.
- Storage → with `--emit-storage`, PVCs are generated and mounted at each `storage[*].mountPath`.

Service mapping
- `spec.service.port` overrides Service `spec.ports[0].port` (default 80); `targetPort` defaults to first container port or `spec.service.targetPort` if set.

Ingress mapping
- Host and one-or-many paths from `spec.ingress.host` + `spec.ingress.paths` (or single `path`) with `pathType: Prefix`.
- `--ingress-class` sets `spec.ingressClassName`.
- TLS: if `spec.ingress.tls` is truthy, exporter emits `spec.tls[0].hosts` and uses `spec.ingress.tlsSecretName` for `secretName` when provided.

Policy and scaling
- PDB: emitted only when replicas > 1; flags enforce exclusivity between minAvailable/maxUnavailable.
- HPA: CPU `Utilization` percent, Memory `Utilization` percent, or Memory `AverageValue` with a quantity like `200Mi`.

Validation and presets
- Offline structure checks via `--validate` (see `src/ae/k8s/validate.py`).
- Presets fill common flags without clobbering explicit options:
  - `web-basic`: `--default-security`, Service port 80.
  - `web-hardened`: adds PDB, HPA(2→4 @70% CPU), `--emit-configs`, `--service-account app-sa`.
  - `scale-ready`: PDB, HPA(2→10 @70% CPU).

CLI examples (see also docs/runbook.md)
- Single-app hardened export with validation:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o specs/examples/echo-k8s.yaml`
- Multi-replica hardened export:
  - `python -m ae.cli export-k8s -f specs/examples/multi-replica-echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o specs/examples/multi-replica-echo-k8s.yaml`

Portability checks (`ae cli k8s-check`)
- Baseline checklist flags common pitfalls: missing probes, missing `resources.requests`, ingress with no ports, multi-replica without PDB, single-replica with canary rollout, and hostPath RW mounts.
- `--policy strict` escalates key warnings to errors (readiness probe, requests, PDB).
- HPA pre-reqs: `--assume-hpa cpu-util|mem-util|mem-value=200Mi` validates resource requests or quantity formats.

Notable gaps (K8s parity)
- Advanced Ingress features (controller‑specific canary annotations) — out of scope for exporter.
- Secrets: exporter can inline plaintext data for demos; production path remains SOPS‑managed secrets applied by controller.

Tests and CI
- Unit tests cover exporter, checker, and validator under `tests/unit/`.
- CI workflow `.github/workflows/k8s-conformance.yaml` spins up Kind and k3s (via k3d) to dry-run apply exported samples and validates with kubeconform; it also runs `k8s-check --policy strict` on representative manifests.

See also
- Source: `src/ae/k8s/exporter.py`, `src/ae/k8s/check.py`, `src/ae/k8s/validate.py`, `src/ae/k8s/presets.py`.
- Examples: `specs/examples/*k8s*.yaml` and `specs/examples/echo.yaml`, `multi-replica-echo.yaml`, `echo-hpa.yaml`.
  - L4 example: `specs/examples/tcp-echo.yaml` with dev HAProxy at `ops/dev/haproxy/` (see docs/l4-services.md).
* Service (ClusterIP).
* (Optional) Ingress (`networking.k8s.io/v1`) with **basic** rules only.

**Inside the Pod template:**

* One or more containers with:

  * `resources.requests` (CPU/Memory) and (preferably) `limits`.
  * `livenessProbe` and `readinessProbe`.
  * `securityContext` that runs **non-root**, drops `NET_RAW`, and sets `readOnlyRootFilesystem: true` when possible.
  * `terminationGracePeriodSeconds` and proper SIGTERM handling for graceful shutdown.
  * `imagePullPolicy: IfNotPresent` (with immutable tags or digests in production).
  * `envFrom` ConfigMap/Secret or mounted files for config, not baked paths.

**If stateful:**

* `PersistentVolumeClaim` with generic `ReadWriteOnce` (RWO). Don’t assume ReadWriteMany is available.
* Let the default `StorageClass` bind the PVC; avoid hardcoding provisioner-specific parameters.

**Networking/DNS:**

* Talk to peer services using their **ClusterIP** DNS name.
* Assume only basic cluster networking; don’t depend on NetworkPolicy enforcement being present.

**Example “minimal but solid” manifest (portable):**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: demo
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: web
  namespace: demo
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: web-config
  namespace: demo
data:
  APP_ENV: "prod"
---
apiVersion: v1
kind: Secret
metadata:
  name: web-secret
  namespace: demo
type: Opaque
stringData:
  DATABASE_URL: "postgres://user:pass@postgres.demo.svc.cluster.local:5432/app"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: demo
spec:
  replicas: 2
  selector:
    matchLabels: { app: web }
  template:
    metadata:
      labels: { app: web }
    spec:
      serviceAccountName: web
      containers:
      - name: web
        image: ghcr.io/example/web@sha256:...    # use digest for immutability
        ports:
        - containerPort: 8080
        envFrom:
        - configMapRef: { name: web-config }
        - secretRef:    { name: web-secret }
        resources:
          requests: { cpu: "100m", memory: "128Mi" }
          limits:   { cpu: "500m", memory: "512Mi" }
        livenessProbe:
          httpGet: { path: /healthz, port: 8080 }
          initialDelaySeconds: 10
          periodSeconds: 10
        readinessProbe:
          httpGet: { path: /ready, port: 8080 }
          initialDelaySeconds: 3
          periodSeconds: 5
        securityContext:
          allowPrivilegeEscalation: false
          runAsNonRoot: true
          runAsUser: 1000
          readOnlyRootFilesystem: true
          capabilities: { drop: ["ALL"] }
      terminationGracePeriodSeconds: 30
---
apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: demo
spec:
  selector: { app: web }
  ports:
  - name: http
    port: 80
    targetPort: 8080
```

# Hitting ~75% parity with **k3s**

k3s differences to account for (so your app “just works”):

1. **Container runtime**: containerd (no dockershim).

   * Use OCI images; avoid Docker-specific behavior.
   * **Multi-arch** images (amd64/arm64) strongly recommended—k3s often runs on ARM.

2. **CNI default**: flannel.

   * **NetworkPolicy** isn’t enforced with plain flannel. Don’t rely on NetworkPolicies unless you deploy a policy-capable CNI (Calico, Cilium). Your app should run fine with *no* policies.

3. **Ingress**: Traefik often enabled by default.

   * Stick to standard `networking.k8s.io/v1` Ingress with simple rules.
   * Avoid controller-specific annotations (e.g., NGINX-only features) unless you gate them with conditionals.

4. **Service type LoadBalancer**: k3s ServiceLB (klipper-lb) on bare metal.

   * Works, but it’s a thin wrapper around hostPorts; no cloud L4 bells & whistles.
   * Prefer `ClusterIP` + Ingress for HTTP apps; use `NodePort` only when you must.

5. **Storage**: varies by environment.

   * Expect only RWO. If you need RWX, document an external provisioner (NFS, CephFS) rather than assuming it exists.

6. **Control plane/Datastore**: SQLite for single-node; etcd for HA.

   * Don’t assume cloud provider features (no cloud-provider load balancers, instance metadata, etc.).
   * Apps shouldn’t care—but avoid init logic that introspects cloud metadata.

7. **Admission/Pod Security**: PodSecurity admission (baseline/restricted) commonly enabled.

   * Run as non-root, avoid privileged/hostPath/hostNetwork, and keep your filesystem read-only where possible.

8. **Beta/Alpha features**:

   * Don’t use alpha gates. Use stable APIs only:

     * Deployments/DaemonSets/StatefulSets: `apps/v1`
     * Ingress/NetworkPolicy: `networking.k8s.io/v1`
     * Jobs/CronJobs: `batch/v1`
     * PDBs: `policy/v1`

# Things that break portability (avoid by default)

* Controller-specific annotations (e.g., NGINX/Traefik magic) baked into the only manifest.
* Cloud-provider Service annotations (AWS/GCP/Azure) and `LoadBalancer` assumptions.
* Depending on **NetworkPolicy enforcement** being present.
* HostPath volumes, privileged pods, hostNetwork/hostPID.
* Assuming `ReadWriteMany` or storage classes with provider-specific parameters.
* Relying on node OS features or Docker socket mounts.
* Alpha APIs or feature gates.

# Optional (nice-to-have) add-ons that remain portable

* **HorizontalPodAutoscaler** (`autoscaling/v2`) using CPU/Memory metrics.
* **PodDisruptionBudget** (`policy/v1`) for graceful rollouts.
* **TopologySpreadConstraints** for basic HA (still portable; not distro-specific).
* **InitContainers** and **Ephemeral volumes** (projected/emptyDir) for setup.

# A portable Ingress (keeps you k3s-friendly)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  namespace: demo
spec:
  ingressClassName: traefik   # omit if your cluster sets a default class
  rules:
  - host: web.example.test
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: web
            port:
              number: 80
```

If you deploy on clusters with NGINX elsewhere, ship a tiny overlay patch that swaps `ingressClassName` and keeps rules identical.

# Quick self-check checklist

* [ ] Only stable API versions (`apps/v1`, `batch/v1`, `networking.k8s.io/v1`, `policy/v1`).
* [ ] Works with **ClusterIP + Ingress**; does not require `LoadBalancer`.
* [ ] No controller/cloud-specific annotations in the base manifests.
* [ ] No reliance on NetworkPolicy enforcement.
* [ ] Non-root, least-privilege `securityContext`.
* [ ] Probes set (readiness+liveness); graceful shutdown implemented.
* [ ] CPU/Memory requests defined (CPU requests required for HPA utilization targets).
* [ ] PVCs (if any) are generic and RWO.
* [ ] Images available for amd64/arm64.

# How to *prove* portability fast

* Spin up **k3s** (or MicroK8s) locally and apply the same manifests.
* Run e2e smoke tests that only assume ClusterIP + Ingress.
* If you want more rigor, run your app against **Kind** + **k3s** + **MicroK8s** in CI with a shared test suite. If all pass, you’re in excellent shape.

If you want, I can take one of your existing app manifests and “shrink-wrap” it to this baseline, then provide tiny overlays for NGINX/Traefik or for clusters that do offer a real LoadBalancer.

See also:
- Operations Runbook: `docs/runbook.md`
- Ingress Guide: `docs/ingress.md`

---

# Conformance Automation

- Offline validation: `python -m ae.cli export-k8s -f <spec> --preset web-hardened --validate`.
- CI dry-run apply: `.github/workflows/k8s-conformance.yaml` runs server-side `kubectl apply --dry-run=server` on exported samples in a Kind cluster.
- Schema guard: the same workflow validates YAML with `kubeconform -strict`.
- k3s matrix: CI also runs a k3s conformance job (k3d) to server‑dry‑run the exported samples. See `.github/workflows/k8s-conformance.yaml`.

---

# Canary Rollout

- Static weighting: set `spec.rollout.strategy: canary` and `spec.rollout.weight: <int>` to bias first upstream in Caddy.
- Auto progression: optionally configure `spec.rollout.auto`:

  ```yaml
  spec:
    rollout:
      strategy: canary
      weight: 1           # initial bias
      auto:
        start: 1          # starting weight
        step: 2           # weight increment per interval
        intervalSeconds: 60
        max: 10           # cap weight
  ```

  Notes: auto-progression is controller-local and in-memory; it updates on reconcile calls. For single-replica apps, canary has no effect.

---

# BYO TLS (Caddy)

- Provide `spec.ingress.tlsCertPath` and `spec.ingress.tlsKeyPath` to use file-based certificates for the host.
- If unset, the dev default `tls internal` is used.

---

# Exporter Presets and Flags (quick reference)

- Presets: `--preset web-basic | web-hardened | scale-ready` (explicit flags override preset defaults).
- Validation: `--validate` performs offline structural checks.
- Ingress: supports `spec.ingress.paths[]`, `spec.ingress.tlsSecretName`, and `--ingress-class`.
- PDB: `--emit-pdb` plus optional `--pdb-min-available N`.
- HPA: `--hpa-min N --hpa-max M --hpa-cpu-target PCT [--hpa-mem-target PCT]` (requires CPU requests in spec).
- Security: `--default-security` applies conservative container securityContext defaults when none provided.

---


# Scratch

```bash
PYTHONPATH=src AE_RUNTIME_BACKEND=podman AE_COLLECT_ENGINE=podman AE_ALLOW_PLAINTEXT_SECRETS=1 AE_ENGINE_STRICT=1 \
  WAIT_READY_TRIES=120 make bench-mem-e2e-k1s LABEL_SUITE=r20251110+podman+rootless+cg2 APP=specs/examples/blue.yaml APP_NAME=blue \
  REPLICAS=1,5,10 DURATION=30

sudo -E PYTHONPATH=src AE_RUNTIME_BACKEND=podman AE_COLLECT_ENGINE=podman AE_ALLOW_PLAINTEXT_SECRETS=1 AE_ENGINE_STRICT=1 \
  WAIT_READY_TRIES=120 make bench-mem-e2e-k1s LABEL_SUITE=r20251110+podman+priv+cg2 APP=specs/examples/blue.yaml APP_NAME=blue \
  REPLICAS=1,5,10 DURATION=30

PYTHONPATH=src AE_RUNTIME_BACKEND=docker AE_COLLECT_ENGINE=docker AE_ALLOW_PLAINTEXT_SECRETS=1 AE_ENGINE_STRICT=1 \
  WAIT_READY_TRIES=120 make bench-mem-e2e-k1nd LABEL_SUITE=r20251110+docker+k1nd REPLICAS=1,5,10 DURATION=30

AE_ENGINE_STRICT=1 AE_COLLECT_ENGINE=docker WAIT_READY_TRIES=120 make bench-mem-e2e-k3s-sudo LABEL_SUITE=r20251110+k3d \
  MANIFEST=specs/examples/k3s-echo.yaml REPLICAS=1,5,10 DURATION=30
```

> Here’s a clean, repeatable baseline matrix that matches your ask and the repo’s bench scripts. It pins the collector engine, enforces
  strict mode, and uses consistent labels so charts bucket correctly.

  - Environment Prep
      - Stop any demos/labs; if safe, clear engines: sudo make bench-engines-clear CONFIRM=1
      - Common env: export PYTHONPATH=src AE_ALLOW_PLAINTEXT_SECRETS=1 AE_ENGINE_STRICT=1 WAIT_READY_TRIES=120
      - Keep ingress consistently ON across all runs (k1s + Caddy, k3s + Traefik).
  - k1s Rootless (Podman)
      - PYTHONPATH=src AE_RUNTIME_BACKEND=podman AE_COLLECT_ENGINE=podman AE_ALLOW_PLAINTEXT_SECRETS=1 AE_ENGINE_STRICT=1
  WAIT_READY_TRIES=120 make bench-mem-e2e-k1s LABEL_SUITE=r20251110+podman+rootless+cg2 APP=specs/examples/blue.yaml APP_NAME=blue
  REPLICAS=1,5,10 DURATION=30
  - k1s Rootful (Podman, snapshots as root)
      - sudo -E PYTHONPATH=src AE_RUNTIME_BACKEND=podman AE_COLLECT_ENGINE=podman AE_ALLOW_PLAINTEXT_SECRETS=1 AE_ENGINE_STRICT=1
  WAIT_READY_TRIES=120 make bench-mem-e2e-k1s LABEL_SUITE=r20251110+podman+priv+cg2 APP=specs/examples/blue.yaml APP_NAME=blue
  REPLICAS=1,5,10 DURATION=30
  - k1nd (k1s-in-Docker)
      - PYTHONPATH=src AE_RUNTIME_BACKEND=docker AE_COLLECT_ENGINE=docker AE_ALLOW_PLAINTEXT_SECRETS=1 AE_ENGINE_STRICT=1
  WAIT_READY_TRIES=120 make bench-mem-e2e-k1nd LABEL_SUITE=r20251110+docker+k1nd REPLICAS=1,5,10 DURATION=30
  - k3d/k3s (cluster up; sudo for accurate PSS)
      - AE_ENGINE_STRICT=1 AE_COLLECT_ENGINE=docker WAIT_READY_TRIES=120 make bench-mem-e2e-k3s-sudo LABEL_SUITE=r20251110+k3d
  MANIFEST=specs/examples/k3s-echo.yaml REPLICAS=1,5,10 DURATION=30
  - After Each Suite
      - Insert OCI runtime into labels + rebuild charts: make bench-mem-backfill-oci-latest REBUILD_DOCS=1
      - If you ran with sudo, normalize artifacts: sudo make bench-fix-perms
  - Notes to keep the baseline “clean”
      - Pin the collector: AE_COLLECT_ENGINE=podman for k1s; AE_COLLECT_ENGINE=docker for k3s/k1nd. This avoids “missing container metrics”
  bars.
      - Labels matter for chart bucketing: include +rootless+ or +priv+ and engine/runtime in LABEL_SUITE as above.
      - Use the same app across runs. You picked blue.yaml for k1s; for k3s use specs/examples/k3s-echo.yaml (lightweight and equivalent
  footprint). If you prefer strict parity, switch all to echo.
      - Strict mode will fail if foreign ae.app containers are running on the other engine; clear or stop them first.

  If you want, I can generate a tiny wrapper script that runs all four suites back-to-back, stamps the labels, and emits a single combined/
  combined.csv plus charts.

---

## Runtime Backend Options (2025-11-12)

Summary
- Default: Podman adapter. The controller shells to `podman` and can inject `--runtime=<crun|runc>` so the OCI runtime is selected explicitly. Podman handles images, storage, networking, exec, and logs for us.
- Alternatives: a direct OCI path (runc/crun without Podman) or a containerd-based adapter.

Option A — Keep Podman Adapter (status quo)
- What Podman provides
  - Image management: `podman pull`, unqualified name resolution, optional import from `docker-daemon:`.
  - Lifecycle: create/start/stop, restart policies, Podman states (created/configured/paused/running), idempotent naming.
  - Networking and ports: CNI networking, rootless port publishing, `podman port` discovery; shared CNI network via `AE_PODMAN_NETWORK` with DNS aliases.
  - Volumes: named volumes and hostPath mounts with simple flags.
  - Security: seccomp/AppArmor toggles, user namespaces, read-only rootfs, cap drops.
  - Ergonomics: Docker-like CLI semantics our adapter already targets; no resident daemon.
- Pros
  - Minimal adapter complexity; robust features out of the box; strong rootless story.
- Cons
  - Hard dependency on the Podman CLI; small behavioral differences vs Docker to account for.

Option B — Direct OCI Runtime (runc/crun) without Podman
- What we would have to build/own
  - Images: pull/verify (OCI Distribution) and unpack bundles (e.g., `skopeo` + `umoci` or a library), auth and local cache.
  - Bundles/specs: generate `config.json` (namespaces, mounts, env, seccomp, cgroups v2, userns) and rootfs; map our security/resource flags.
  - Supervision/IO: manage lifecycle and logs (a `conmon`-like supervisor) for detached processes and exit status.
  - Exec/attach: namespace entry (`setns`), TTY handling, timeouts.
  - Networking: CNI netns or rootless `slirp4netns`, host port publishing and endpoint discovery.
  - Volumes/storage: named volumes (overlayfs/fuse-overlayfs) and hostPath mounts lifecycle.
  - Introspection: replace `podman ps/inspect/logs/port` with our own inventory and restart counters.
- Pros
  - No Podman dependency; maximum control over runtime behavior.
- Cons/Risks
  - High engineering effort and ongoing maintenance; effectively recreates a slim CRI stack.
  - More surface for security hardening and distro nuances (cgroups/userns/SELinux/AppArmor/slirp4netns).

Option C — containerd-based Adapter
- Shape
  - Use `containerd` for images, snapshots, tasks, exec, and CNI networking; drive via client bindings or `nerdctl/ctr`.
- Pros
  - Removes Podman dependency without re-implementing OCI plumbing; widely supported on Linux; uses `runc/crun` under the hood.
- Cons
  - Requires a resident daemon (`containerd`); rootless support and ops are more involved than Podman’s single-CLI model.
  - New adapter surface and test matrix.

Decision Guidance
- Prefer Podman when targeting single-node, daemonless, rootless-friendly setups and minimizing bespoke runtime code.
- Consider containerd backend when standardizing on a node runtime across environments or where Podman isn’t available.
- Avoid a direct runc/crun path unless there’s a strong requirement to remove intermediaries; expect a multi-phase project.

Environment Controls (current)
- `AE_RUNTIME_BACKEND` = `podman` (default), `docker`, or `stub`.
- `AE_PODMAN_BIN` — path to the Podman binary.
- `AE_OCI_RUNTIME` — forces `--runtime=<crun|runc>` for run/exec when using Podman.
- `AE_PODMAN_NETWORK` — connects new replicas to a shared CNI network and registers DNS aliases.

Next Steps
- Keep: Maintain Podman as default; continue smoothing Docker parity and handling Podman-specific states.
- Explore: Prototype a containerd adapter behind `AE_RUNTIME_BACKEND=containerd` with parity goals: images, run/stop, ports/endpoint discovery, volumes, logs, exec.
- Investigate: For direct OCI, start with a scoped PoC (pull+unpack+run single process via `skopeo+umoci+runc`, no networking) to quantify gaps before a full plan.
