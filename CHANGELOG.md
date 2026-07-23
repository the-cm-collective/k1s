# Changelog

## Unreleased

### Added
- No user-facing changes yet.

### Changed
- No user-facing changes yet.

### Fixed
- No user-facing changes yet.

## 0.1.6 - 2026-07-23

### Added
- Stable release of the 0.1.6 line, including AI Max / Strix Halo edge-cell
  contracts for gateway discovery, reservation policy, boot assurance,
  installer role scaffolding, artifact-signing scaffolds, and edge autonomy
  state.
- Env-scoped edge admission API plus Hive dashboard edge gateway probes, site
  grouping, build visibility, and schedulability display.
- Namespaced RBAC support in Kubernetes apply/API shim paths, including service
  account token projection in the containerd runtime.
- OpenStack Lite public route import and target import dry-run contract
  artifacts for local SaaS integration validation.
- Expanded inference fabric manifest/controller coverage and public reference
  documentation.

### Changed
- Core/edge ingress translation now preserves service target ports, waits for
  translated backends, and syncs routes after API apply.
- Controller reconciliation now orders registry app import, translated ingress,
  API apply, route sync, endpoint recovery, and stale snapshot handling more
  defensibly.
- Runtime endpoint hydration now recovers service endpoints from CRI/container
  runtime state and prefers live pod/container IPs for core-local routes.
- `k1s-core-ha` Helm values expose additional ingress configuration for
  MicroK8s and edge gateway deployments.

### Fixed
- Translated ingress cleanup now follows app deletion and ingress removal more
  reliably.
- Containerd service port conflicts fail closed, stale registry snapshots are
  skipped, and remote runtimes remove deleted apps more consistently.
- Dashboard tests isolate lab edge-gateway config so local
  `/run/k1s-dashboard-edge-gateways.json` does not leak into unit-only layout
  assertions.

## 0.1.6.dev3 - 2026-07-23

### Added
- AI Max / Strix Halo edge-cell contracts for gateway discovery, reservation
  policy, boot assurance, installer role scaffolding, artifact-signing
  scaffolds, and edge autonomy state.
- Env-scoped edge admission API plus Hive dashboard edge gateway probes, site
  grouping, build visibility, and schedulability display.
- Namespaced RBAC support in Kubernetes apply/API shim paths, including service
  account token projection in the containerd runtime.
- OpenStack Lite public route import and target import dry-run contract
  artifacts for local SaaS integration validation.
- Expanded inference fabric manifest/controller coverage and public reference
  documentation.

### Changed
- Core/edge ingress translation now preserves service target ports, waits for
  translated backends, and syncs routes after API apply.
- Controller reconciliation now orders registry app import, translated ingress,
  API apply, route sync, endpoint recovery, and stale snapshot handling more
  defensibly.
- Runtime endpoint hydration now recovers service endpoints from CRI/container
  runtime state and prefers live pod/container IPs for core-local routes.
- `k1s-core-ha` Helm values expose additional ingress configuration for
  MicroK8s and edge gateway deployments.

### Fixed
- Translated ingress cleanup now follows app deletion and ingress removal more
  reliably.
- Containerd service port conflicts fail closed, stale registry snapshots are
  skipped, and remote runtimes remove deleted apps more consistently.
- Dashboard tests isolate lab edge-gateway config so local
  `/run/k1s-dashboard-edge-gateways.json` does not leak into unit-only layout
  assertions.

## 0.1.6.dev2 - 2026-06-19

### Added
- Fabric phase assurance, locality, F4/F5 evidence contracts, symbolic runtime
  facts, and advisory decision contracts for AI fabric runtime review.
- AI runtime profile dry-run admission, soak-evidence gating, advisory state
  persistence, and WorkerBee CLI status intake for runtime profile admission.
- Hive dashboard support for fabric advisory state, trace review, advisory
  providers, review writeback, and advisory proposal dry runs.
- Fabric advisory demo recording plus generated documentation for phase
  assurance and advisory trace review.
- Edge gateway route bundle Helm assets and MicroK8s edge gateway example
  values.
- Rollout restart support for development image refresh.

### Changed
- Runtime and direct-containerd service port handling were tightened for more
  repeatable local validation with WorkerBee and MicroK8s.
- Etcd event maintenance and advisory review storage were hardened for long
  soak and HA validation paths.
- Dashboard and docs output were refreshed for the new AI fabric and advisory
  review surfaces.

### Fixed
- Fixed Podman workload network attachment so workloads join the configured
  network instead of relying on implicit runtime defaults.
- Fixed dashboard JavaScript fallback serving and fabric advisory modal bounds.
- Fixed fabric advisory review persistence, demo recording behavior, and etcd
  read/maintenance edge cases.
- Fixed the etcd-backed inference/advisory test fixture used by release
  validation so it mirrors the production read-timeout path.

## 0.1.5 - 2026-05-22

### Added
- Accelerator-aware scheduling foundations, including typed accelerator node capabilities, local GPU labels, and inference cell examples for single-cell, pipeline-parallel, and Ray-backed layouts.
- Host A and NVIDIA validation tooling for GPU passthrough, vLLM/OpenAI-compatible probes, CUDA/Torch runtime checks, strict CRI retests, and NetFS validation lanes.
- MicroK8s development stack assets, HA ingress/API-auth wiring, and Helm chart skeletons for `k1s-core-ha` and `k1s-node-local`.
- Direct containerd runtime coverage, WorkerBee runtime packaging helpers, containerd insecure registry support, and maintenance regression tooling for common operator failure cases.
- Agent PBX coordination guidance for status reporting, planning options, and release/worktree reporting expectations.

### Changed
- Runtime command handling now maps Kubernetes commands to runtime entrypoints more consistently across Docker, Podman, CRI, direct containerd, and remote execution paths.
- API shim, controller, and observability surfaces gained stronger HA/status detail behavior, safer runtime factory selection, and clearer module-level engineering references.
- Containerd service alias refresh, health probing, runtime naming, CRI image bootstrap, and direct-containerd dashboard paths were tightened for repeatable local and MicroK8s development runs.
- CLI remote transport behavior and request mocks were updated around timeout handling, websocket fallback, CA bundle usage, and deprecated alias warnings.
- Generated documentation, wiki exports, and runbooks were refreshed for inference fabric, Host A GPU workflows, strict CRI retests, MicroK8s operations, observability, runtime profile guidance, and the GitHub-canonical/Codeberg-mirror project links.

### Fixed
- HA core proxy routing and MicroK8s HA ingress/API authentication paths were corrected for the current dev stack.
- Secret injection, unsafe API shim fallbacks, unregistered CSI attachments, and CRI auth codec imports now fail or degrade more predictably.
- Containerd runtime recovery and service alias refreshes now avoid stale dependencies, overlap, and incorrect readiness targets.
- Host A GPU, NetFS, inference API readiness, cloud-init image verification, and VM bootstrap flows were stabilized with stronger probes and retry behavior.
- NixOS containerd CNI environment detection now selects a `tomllib`-capable Python interpreter when `python3` is too old.
- Nightly runtime CI, helm contract tests, maintenance note generation, namespaced dashboard status details, exec transport reporting, and remote pod/log capture paths were hardened.

## 0.1.4 - 2026-04-22

### Added
- High-availability control-plane support across controller authority, mutation fencing/CAS, HA-safe API-shim reads, and shared authority handling for workload-core, CRD, HPA, CronJob, and storage resources.
- HA operator tooling and observability for retained and stage-2 lanes, including public control-plane Envoy exposure, HA dashboard/system surfaces, authority freshness/build recovery metrics, etcd snapshot/recovery helpers, and new HA drill/upgrade/bootstrap scripts.
- HA VM validation automation via `make lab-vm-ha-validation`, with attached-node, retained, drain, stage-2, live-helper, and drill coverage backed by the checked-in HA lab variants and closeout helpers.
- Benchmark rollout policy automation now includes retained dataset rebuilding, rollout-overlap reporting, candidate summarization, and ordered CRI benchmark profile publishing for release-grade comparison runs.

### Changed
- Runbooks, validated procedures, and generated site pages now treat the retained attached-node flow and stage-1/stage-2 HA validation sequence as the canonical operator path.
- This tag's release verification records pooled Debian/NixOS host validation: both hosts run the shared baseline with `AE_USE_REGISTRY_CACHE=0`, Debian owns `make e2e` plus `make strict-cri-smoke`, and NixOS owns `make lab-vm-ha-validation` plus the full benchmark rerun; per-host full-matrix verification starts next release.
- Benchmark runners, retained artifacts, and published docs were hardened for rootless, rootful, k1nd, k3d, and CRI reruns, including isolated bench environments, refreshed comparison outputs, stronger rerun guidance, and refreshed pre-tag benchmark/site artifacts.
- Local dev/operator tooling now includes stronger environment/bootstrap helpers such as `env-doctor`, controller env export helpers, and Nix-based dev shell support.
- CI and release workflows were consolidated into core/docs/nightly lanes and hardened for Gitea-hosted execution, including warning-only Kubernetes export checks and release artifact packaging aligned to the new workflow layout.

### Fixed
- VM golden-image verification and lab overlay guards now size verifier overlays from the backing qcow2 virtual size and reject undersized stale overlays, preventing truncated initramfs/root-device failures in HA validation reruns.
- Demo and premerge smoke workflows were stabilized across playground auth/reset cleanup, helm shim demo behavior, fixed-port rollouts, strict-CRI apishim image builds, and simple dashboard recovery.
- Runtime and benchmark reliability improved across Podman netns socket probing, Docker list races in k1nd, CRI env/bootstrap checks, reserved-name recovery, cleanup boundaries, steady-state attribution, snapshot timing, and k3d comparison/chart capture.
- Published procedures and generated site artifacts now scrub local paths and no longer embed lab tokens in exported output.
- HA VM retained-lane issues were tightened across cloud-init wait behavior, drain/reseed flows, ingress routing/trust setup, SSH dry-run checks, and host mapping restoration during purge/reset.

## 0.1.3 - 2026-02-19

### Added
- Strict CRI profile orchestration for dev lanes, including `k1s-core-cri`, `k1s-edge-cri`, `k1s-core-edge-cri`, `k1s-edge-core-cri`, and `edge-site-cri`, with registry mode, image policy, and local build fallback handling.
- Ingress capability validation expansion for CRI: single-host and multisite matrix suites, deeper capability probes, and perf-oriented ingress validation harnesses with new ingress matrix scenarios.
- Core-proxy ingress policy controls for load balancing and cookie stickiness.
- Ingress matrix result JSON now includes LB assertion and observability summary fields (`lb_policy_passed`, `lb_strict_proof_passed`, `lb_observability_passed`) plus per-row LB assertion metadata.
- Benchmark tooling now captures CRI (`crictl`) container snapshot/inspect data in matrix/rollout workflows.
- Project metadata now includes the `version_codename` field (`Snow Moon`) in `pyproject.toml`.
- Parity benchmark assets for k1s vs k3s were added, including a preflight helper (`scripts/dev/parity_preflight.sh`) and parity fixture manifest (`specs/examples/k3s-ingress-parity.yaml`).
- A containerd trust/bootstrap helper was added for managed registry CA wiring in strict CRI flows (`scripts/containerd_registry_trust.sh`).

### Changed
- Docs server pages and README were refreshed to align CRI guidance around `k1s-*` profile targets, with updated Start Here, Overview, Runtime Profiles, CRI reference, Multi-node lab, and related reference pages.
- Ingress runbooks and examples were tightened around canonical mode validation flows and reproducible single-host strict CRI execution paths.
- Ingress deep validation guidance now treats `core-proxy` as the primary policy/observability lane and keeps strict LB distribution proof in the `edge-local` lane.
- Runtime profile behavior now auto-detects strict CRI core stacks more explicitly and fails fast on incompatible compose profile pairings.
- Benchmark docs/charts were refreshed with CRI scenario coverage annotations.
- Managed registry behavior in CRI profiles now defaults to TLS/trust-first operation, with explicit security-gate guidance for strict runs.
- Ingress lane preflight behavior was softened for setup checks that are not always present in every stage (warn instead of fail for specific public listener checks), while keeping lane gating intact.
- README and Start Here messaging were updated to align with TENETS language and the v0.1.3 production-validation posture.

### Fixed
- Edge-local route bundle publish permissions and preflight handling were hardened, including controller/etcd edge-ingress state alignment and safer route bundle publishing behavior.
- Core-proxy ingress reliability improved across startup/readiness checks, downstream HTTP/2 strictness, path-aware validations, and tunnel target normalization.
- CRI ingress startup sequencing and lab reproducibility issues were fixed for strict CRI multi-site lanes.
- Demo/labs reliability fixes: Caddy dashboard/playground redirects, automatic Caddy reload, CRI cleanup on reset, and stale state cleanup when bench DB paths leak into demo env.
- Playground/dashboard behavior fixes: resilient log/event streaming and re-arming, better ingress status handling, minimal-image shell defaults (`sh`), and improved token selection for exec/port-forward workflows.
- Local auth/export improvements now ensure state DB paths are exported consistently so CLI status matches controller state.
- Bench automation now clears rootful Podman before k1nd and hardens CRI waits/spec isolation to reduce flakiness.
- Security baseline/active probe scripts now handle sudo/proc environment parsing and apishim auth probe evaluation more reliably in strict validation lanes.
- Etcd-backed site ingress endpoint state now preserves public endpoint metadata across lane updates.

## 0.1.2 - 2026-01-30

### Added
- CSI storage API support: snapshots, PVC restore/clone flows, volume expansion, RWOP/multi-attach guardrails, topology constraints, capacity reporting, and volume health metrics/events.
- NetFS/NFS storage plumbing: dynamic NFS provisioning, NetFS mounts with SELinux relabeling/options, fsGroup support, storage quotas, block device mappings, and capacity overrides.
- CRI runtime support with ingress reload fallback and containerd CRI integration workflow coverage.
- Apishim exec/port-forward foundations plus CRI port-forward proxy, richer OpenAPI examples/metadata, and static Swagger export pages.
- CLI namespace targeting, dashboard port-forward preview, and a shell-demo sample.

### Changed
- Dashboard visuals and system graph styling with refreshed background assets.
- Docs and examples refreshed for storage, NetFS, and CRI (ADRs, runbook notes, updated README screenshots, and regenerated site artifacts).

### Fixed
- Local node registration/heartbeat gating and reconciler registration in tests.
- Apishim proxy/routing/TLS hardening and pod-IP preference for port-forward.
- Demo/lab auth bootstrap hardening plus CRI setup/test reliability in CI.
- Benchmarks now enforce container capture with stricter engine isolation and snapshot cleanup to avoid cross-engine contamination.
- Labs AIO now honors `AE_RUNTIME_BACKEND` for runtime selection during dev runs.

## 0.1.1 - 2026-01-22

### Added
- Docs wiki export and concepts-in-practice chapter series.
- Blog: January update post.

### Changed
- Docs site refresh: nav/playground layout, branding assets, README hero, chart styling, chapter navigation, and non-interactive HTML export tooling.
- Doc tooling: Makefile helper now accepts optional parameters for export workflows.

### Fixed
- Doc exports now bundle static assets correctly, hide local-only hero links, and remove footer proxy elements.
- Playground action row layout in exported docs.

## 0.1.0 - 2026-01-18

### Added
- **Node agent + remote runtime:** new `ae-node` HTTP agent and `RemoteRuntime` adapter so the controller can delegate workload lifecycle and log/exec calls to remote nodes; agent sends heartbeats to the controller’s agent API with labels/taints, optional pod CIDR/WireGuard metadata.
- **Controller multi-node plumbing:** scheduler distributes replicas across Ready nodes with nodeSelector/tolerations and storage pinning; state store now records nodes, heartbeats, and storage bindings; controller auto-registers the local node for single-node runs.
- **Service VIP dataplane:** Service controller and Docker/overlay providers allocate ClusterIPs, run per-Service HAProxy sidecars, and program endpoints from health + runtime state (skips loopback, deduplicates targets). Optional overlay provider targets WireGuard-backed networks.
- **Pod networking helpers:** Pod CIDR allocator (env-gated) and node-side bridge/WireGuard helper to plumb pod networks in lab/overlay scenarios.
- **CLI/HTTP API:** `ae services` and `ae nodes` subcommands; status/history JSON/watch tweaks; HTTP API exposes `/nodes` and richer status for dashboards. New console script `ae-node` registered in `pyproject.toml`.
- **Multinode lab & CI assets:** QEMU/libvirt lab scripts (`ops/ci/multinode-qemu.sh`, `ops/dev/multinode-lab.sh`), default test key under `ops/ci/keys/`, overlay-enabled smoke option, and sample `specs/examples/echo-multinode.yaml`. New docs `docs/adr/0006-multinode-ci-qemu-smoke.md`, `docs/guides/multinode-lab.md`, and site rebuild covering the workflow.
- **Test coverage:** Integration suites for agent flow and service VIP routing; unit suites for scheduler, agent API, pod CIDR allocator, docker provider, node state, and reconciler updates.
- **Observability:** Prometheus now exports node inventory/heartbeat and service endpoint readiness metrics; controller-health Grafana board shows Ready/Total nodes, heartbeat age, and service endpoint readiness for multi-node runs.
- **Kubernetes API shim parity:** apishim now exposes pods/logs/exec/port-forward, nodes/endpoints, and HPA; supports RBAC evaluation (Role/RoleBinding + SubjectAccessReview), serviceaccount token minting + projection, JSONPatch/Apply, read/admin tokens, and list/watch continue tokens with resourceVersion handling.
- **Port-forwarding stack:** SPDY/3.1 multi-stream port-forward with flow-control windows, keepalive/ping handling, stderr passthrough, and websocket fallback; kubectl port-forward smoke and multiport e2e coverage.
- **Streaming exec:** apishim streaming exec with TTY resize and CI smoke coverage.
- **OpenAPI tooling:** v2/v3 fixtures, openapi fixture validators, drift guard, and live OpenAPI gate for CI (kind kubeconfig option).
- **Security & TLS:** mTLS bootstrap for agents, join-token verification with single-use + revocation tracking, CA serial persistence, and cert rotation/revocation helpers (`ae rotate-certs`, `ae-rotate-certs`).
- **Multinode observability:** overlay health metrics/alerts plus dashboard node inventory and per-node pod placement.
- **CI workflows:** apishim smoke/HA/postgres/SSA-RBAC workflows, helm shim dry-run/smoke, kubectl exec/port-forward smokes, multinode/port-forward/overlay workflows, podman CI, and expanded release artifacts.
- **Benchmarks & docs:** new memory benchmark pipeline and k3s comparison tooling; docs additions for conformance, multinode, benchmarks, and TLS/mTLS runbook notes (plus ADR `docs/adr/0005-k1nd-memory-benchmark-notes.md`); docs site refresh with nav/layout tweaks, code-block copy pills, and expanded examples.

### Changed
- Docker runtime now prefers host-published endpoints using `AE_NODE_ADVERTISE_IP` when containers run on remote nodes; podman runtime gains parity fixes. Service endpoints are deduplicated to avoid SQLite UNIQUE violations when multiple replicas share targets.
- Reconciler/state tightening: merges file/DB manifests with hash + mtime heuristics, tracks storage bindings, and cleans service records; observer backends handle stale nodes via grace-period NotReady logic.
- Controller/status projection: apishim surfaces deployment/pod status from controller state; service discovery and ingress status use provider VIPs; service proxy cleanup avoids orphaned HAProxy containers.
- OpenAPI validation: v3 fixtures are authoritative for validation; CI adds fixture validation and live OpenAPI checks with optional kind kubeconfig.
- Developer tooling: planner gating skips non-app examples and k3s multi-docs; unit tests run against stub runtime; dev extras include JSON schema validation.
- CI/doc tooling: `scripts/update_docs.sh` now regenerates benchmark charts and snapshot summaries, then refreshes the static site; SMOKE.md references the new multi-node smoke; doc HTML regenerated.

### Fixed
- QEMU CI script waits for cloud-init, retries repo mounts, installs pip before invoking ae binaries, and optionally runs a full smoke (apply → VIP curl → kill worker → reschedule → curl). Helper adds host key support for remote SSH kills and expands overlay disk defaults.
- Port-forward stability: tightened SPDY frame validation, per-stream windows, flow-control resets, websocket cleanup, and clearer upgrade hints; stub port-forward path stabilized.
- Helm shim reliability: resilient helm downloads, numeric `targetPort` in services, templates directory guard, and smoketests that run without auth tokens or in stub runtime mode.
- Apishim workflows: gitea checkout trust/cert handling, local proxy bypass for live OpenAPI checks, PATH preservation for apishim persistence tests, and hardened smoke workflows.
