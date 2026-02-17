# Changelog

## Unreleased

### Added
- No user-facing additions yet.

### Changed
- No user-facing changes yet.

### Fixed
- No user-facing fixes yet.

## 0.1.3 - 2026-02-14

### Added
- Strict CRI profile orchestration for dev lanes, including `k1s-core-cri`, `k1s-edge-cri`, `k1s-core-edge-cri`, `k1s-edge-core-cri`, and `edge-site-cri`, with registry mode, image policy, and local build fallback handling.
- Ingress capability validation expansion for CRI: single-host and multisite matrix suites, deeper capability probes, and perf-oriented ingress validation harnesses with new ingress matrix scenarios.
- Core-proxy ingress policy controls for load balancing and cookie stickiness.
- Ingress matrix result JSON now includes LB assertion and observability summary fields (`lb_policy_passed`, `lb_strict_proof_passed`, `lb_observability_passed`) plus per-row LB assertion metadata.
- Benchmark tooling now captures CRI (`crictl`) container snapshot/inspect data in matrix/rollout workflows.
- Project metadata now includes the `version_codename` field (`Snow Moon`) in `pyproject.toml`.

### Changed
- Docs server pages and README were refreshed to align CRI guidance around `k1s-*` profile targets, with updated Start Here, Overview, Runtime Profiles, CRI reference, Multi-node lab, and related reference pages.
- Ingress runbooks and examples were tightened around canonical mode validation flows and reproducible single-host strict CRI execution paths.
- Ingress deep validation guidance now treats `core-proxy` as the primary policy/observability lane and keeps strict LB distribution proof in the `edge-local` lane.
- Runtime profile behavior now auto-detects strict CRI core stacks more explicitly and fails fast on incompatible compose profile pairings.
- Benchmark docs/charts were refreshed with CRI scenario coverage annotations.

### Fixed
- Edge-local route bundle publish permissions and preflight handling were hardened, including controller/etcd edge-ingress state alignment and safer route bundle publishing behavior.
- Core-proxy ingress reliability improved across startup/readiness checks, downstream HTTP/2 strictness, path-aware validations, and tunnel target normalization.
- CRI ingress startup sequencing and lab reproducibility issues were fixed for strict CRI multi-site lanes.
- Demo/labs reliability fixes: Caddy dashboard/playground redirects, automatic Caddy reload, CRI cleanup on reset, and stale state cleanup when bench DB paths leak into demo env.
- Playground/dashboard behavior fixes: resilient log/event streaming and re-arming, better ingress status handling, minimal-image shell defaults (`sh`), and improved token selection for exec/port-forward workflows.
- Local auth/export improvements now ensure state DB paths are exported consistently so CLI status matches controller state.
- Bench automation now clears rootful Podman before k1nd and hardens CRI waits/spec isolation to reduce flakiness.

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
