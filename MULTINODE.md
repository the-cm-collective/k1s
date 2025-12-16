# Multi-Node Operation Plan

Status: December 14, 2025 — the engine is single-node only (Podman default, Docker fallback) with Caddy ingress and a local SQLite store. This doc reviews what exists today and lays out a staged path to run workloads across multiple nodes.

---

## Current State (single node)
- Control plane: one `ae.controller` process; no leader election or node inventory. State lives in `state/controller.db` (SQLite) and is mutated only by the local controller.
- Runtime: `PodmanRuntime` (default) and `DockerRuntime` drive containers on the same host. They optionally join a local bridge (`AE_PODMAN_NETWORK` / `AE_DOCKER_NETWORK`) to let Caddy reach replicas by DNS alias. No concept of remote hosts or node-scoped capacity.
- Scheduling/placement: none. Specs carry `affinity`/`tolerations`/`topologySpreadConstraints` only for Kubernetes export; the runtime ignores them.
- Networking & Services:
  - For multi-replica apps, ingress load balances ready endpoints using per-replica host ports or container DNS on the shared bridge.
  - `spec.service.port` is honored only when `replicas == 1`; otherwise host ports are ephemeral and L4 load-balancing is delegated to an external proxy (see `docs/l4-services.md`).
  - No ClusterIP emulation, IPAM, or overlay; everything assumes one host namespace.
- Ingress: single Caddy instance (host or container) on the control node. Upstreams are 127.0.0.1:<hostPort> or container DNS on the shared bridge. No cross-node routing.
- Health: controller-side HTTP/TCP/exec probes against local endpoints. Exec probes rely on local runtime access.
- Storage: per-app Docker/Podman volumes on the host; no remote mounts or placement awareness.
- Observability/API: metrics/events/status served from the controller. CLI can point to a remote controller over HTTP, but nodes are not first-class objects.
- Prior art: `docs/CLUSTERIP.md` defines a phased plan for Service VIPs (Phase 1–3) but is not implemented in code.

---

## Goals for the first multi-node cut
- Run a **single controller node** managing **>1 worker node** (2-node lab minimum).
- Keep the **API/CLI surface stable** for users; multi-node should be mostly transparent to manifests.
- Provide **routable pod/service networking across nodes** with stable Service VIPs (build on `docs/CLUSTERIP.md` Phase 3).
- Add a **node agent** (per worker) that reuses existing runtime logic and surfaces exec/logs for probes and CLI.
- Introduce **basic scheduling/placement** (node registration, labels/taints, sticky storage) and **node health** (heartbeats, drain/cordon).
- Preserve **ingress** with a single entrypoint (Caddy) fronting Service VIPs; allow future per-node ingress.
- Non-goals for this iteration: HA controller/state, dynamic storage replication, full kube-proxy parity, Windows nodes, IPv6.

---

## Proposed architecture (thin controller, per-node agent)
- **Controller (existing process)**: owns desired state, scheduling, and Service/IPAM records. Remains single-instance with SQLite (later pluggable).
- **Node Agent (`ae-node` / `python -m ae.node` daemon)**: runs on each worker, wraps `PodmanRuntime`/`DockerRuntime`, exposes a secure HTTP/gRPC API for ensure/remove/exec/logs, reports capacity and heartbeats.
- **Transport**: mTLS HTTP/gRPC initiated by controller to agents (agent listens) or agent → controller callbacks for heartbeats; reuse existing token model where possible.
- **Networking**:
  - Pod network: per-node Pod CIDR on a WireGuard overlay managed by the agent plus a small privileged per-node helper that owns the `wg` device and routes. Fallback to VXLAN only when WireGuard is unavailable or prohibited.
  - Service network: Service CIDR + provider from `docs/CLUSTERIP.md` Phase 3 (iptables/IPVS or per-node Envoy). Controller allocates VIPs; agents program dataplane on their node.
- **Ingress**: Caddy stays on controller node, upstreaming to Service VIPs. Later, optional per-node ingress tier could register with the same Service VIPs.
- **Health & exec**: controller keeps readiness/liveness orchestration; HTTP/TCP probes hit Service VIPs; exec probes and CLI exec/logs proxy through the agent API.
- **Storage**: mark volumes as local to a node; scheduler pins replicas that declare retained storage to the node that holds it. No cross-node volume moves initially.

### Service VIP path (recommended)
- **Allocation**: controller owns a Service CIDR (configurable; default to align with `docs/CLUSTERIP.md`) and allocates a stable ClusterIP per `spec.service`.
- **Dataplane provider**: per-node iptables/IPVS or Envoy (pluggable). Current impl: HAProxy per-Service VIP on the overlay network (`AE_SERVICE_PROVIDER=overlay`, `AE_OVERLAY_NET`), with a bridge fallback.
- **Overlay dependency**: VIPs ride the WireGuard overlay; no hostPorts required. MTU checks and per-peer keepalives are handled by the net helper. VXLAN remains a fallback if WireGuard modules are missing.
- **Ingress/external**: Caddy (or an external L4 proxy for TCP) points at the Service VIP. External TCP/UDP can be fronted by a small Envoy/HAProxy that targets the VIP, avoiding node-specific hostPorts.
- **Collision avoidance & HA**: removing hostPorts from the critical path eliminates cross-node port conflicts and spreads traffic across ready replicas; planner warns when a hostPort bypasses the VIP path.

---

## Phased plan and deliverables

### Phase 0 — Foundations (prereqs on single node)
- [x] Finish `docs/CLUSTERIP.md` Phase 1–2: Service IPAM, proxy container, provider interface.
- [x] Add Service/endpoint tables to SQLite and reconcile hooks (provider wiring still pending).
- [x] Harden health/ingress to prefer Service VIPs when present (keeps code paths consistent before overlay work).
- [x] Implement Docker bridge provider + HAProxy per-service proxy (behind `AE_ENABLE_SERVICE_PROXY=1`).

Phase 0 note: Service IPAM + provider interface are implemented via `ae.network.service_controller` and the Docker/overlay providers; HAProxy per-service proxies are live behind `AE_ENABLE_SERVICE_PROXY=1`.

### Phase 1 — Node inventory and agent skeleton
- [x] Define `Node` model (id/name, labels/taints, runtime backend, endpoints).
- [x] Extend SQLite with `nodes` + `node_heartbeats` tables and expose read APIs/CLI (`ae nodes list|describe`).
- [x] Ship `ae-node` agent binary/entrypoint (HTTP skeleton wrapping local runtime).
- [x] Wire controller to use a RemoteRuntime with optional agent URL (loopback/local by env).

### Phase 2 — Remote runtime RPC + health plumbing
- [x] Define agent RPCs: ensure_app, remove_app, remove_old_revisions, list_containers_info, read_logs, exec, ensure_storage_volumes, list_storage_volumes.
- [x] Add controller-side “RemoteRuntime” shim that fulfills `RuntimeAdapter` by delegating to agents.
- [x] Proxy exec/logs/probe exec via the agent; keep HTTP/TCP probes hitting Service VIPs directly.
- [x] Implement node heartbeats and basic node conditions (Ready/NotReady), surfaced in CLI/API.

### Phase 3 — Overlay and Service dataplane across nodes
- [x] Allocate per-node Pod CIDRs; controller now assigns from `AE_POD_CIDR_POOL` (`/16` default) with per-node mask `AE_POD_CIDR_MASK` (`/24` default) and returns it in the heartbeat reply. Agent can now request optional pod bridge/WireGuard bring-up (`--ensure-pod-net`).
- [x] Implement an overlay Service provider (`AE_SERVICE_PROVIDER=overlay`) that runs per-Service HAProxy VIPs on the overlay network (`AE_OVERLAY_NET`, default `ae-overlay`) using the Service CIDR. Falls back to the bridge provider when unset.
- [x] Runtime and Service controller prefer pod IP endpoints (container network IP) over host ports; ingress upstream selection already prefers Service VIPs when present.
- [x] Added a minimal net helper for agents to configure pod bridge + WireGuard from env (lab-grade). Conformance/demo script is tracked under `ops/dev/multinode-lab.sh` (scaffold).

### Phase 4 — Scheduling, failover, and lifecycle
- [x] Add a minimal scheduler: defaults to round-robin across Ready nodes, honors `nodeSelector`, and taints/tolerations; respects storage pinning by pinning all replicas to one node when `spec.storage` is set.
- [x] Node drain/cordon commands and controller behavior to reschedule replicas when a node goes NotReady (staleness > `AE_NODE_NOTREADY_AFTER`, default 40s) with create-rate limits inherited from rollout strategy.
- [x] Optional: anti-affinity/topology hints respected when more than one node is available.

### Phase 5 — Ingress and perimeter polish
- [x] Keep single Caddy front door; templates now prefer Service VIPs when ready endpoints exist (tested) and Caddy remains the only external entrypoint.
- [x] Document per-node ingress option (agent-managed Caddy/Envoy) and how it registers back to the controller for certificate management (design only; off by default).
- [x] Add host-level firewall guidance for tunnel ports and ingress.

### Phase 6 — Storage and data locality
- [x] Mark volumes with `node` ownership; scheduler pins pods needing retained volumes to that node.
- [x] Add validation and planner hints when manifests declare storage but the target node lacks the volume.
- [x] Document migration story (manual copy + rebind) and non-goal of distributed block storage.

### Phase 7 — Testing, demos, and release gates
- [ ] Two-node CI job (kind/libvirt or nested VM) running: apply → rollout → failure/recovery → delete.
- [x] Integration tests for agent RPCs and scheduler decisions (remote runtime stub).
- [ ] Service VIP routing integration across nodes (overlay provider end-to-end).
- [x] Demo manifests and walkthrough under `specs/examples/` + `docs/multinode-lab.md`; update `SMOKE.md` and `runbook`.

Phase 7 progress:
- Added multi-node agent integration coverage (`tests/integration/test_multinode_agent_flow.py`) that spins stub agents, validates per-node placement, and proves RemoteRuntime wiring without touching the local runtime.
- New sample manifest for multi-node (`specs/examples/echo-multinode.yaml`) and a hands-on lab guide (`docs/multinode-lab.md`). `SMOKE.md` now lists a minimal multi-node smoke path and the quick regression command.
- Remaining: wire a two-node CI job (KinD fast-path for PRs, QEMU/libvirt canonical on KVM runners) that runs the lab script end-to-end with Service VIPs; add an overlay Service-VIP routing test once the CI substrate is in place. See `MULTINODE-TEST.md` for the QEMU/libvirt blueprint and the helper script `ops/ci/multinode-qemu.sh`; KinD variant stays as the quick gate.

### Observability and dashboards (multinode)
- Gaps (pre-multinode): Prometheus exposed only app/replica/canary counters; Grafana boards lacked node/service/overlay visibility; `/dashboard` rendered a single-host graph.
- Now required: node inventory and heartbeat freshness, cordon state, and service/VIP endpoint readiness surfaced as first-class metrics and panels.
- Implemented: node gauges (`ae_nodes_*`, per-node status/last-seen/cordon), service/VIP metrics, and Grafana controller-health panels for Ready/Total nodes plus service endpoint readiness. (Alerts and overlay/WireGuard health will follow once the net helper emits peer status.)

---

## Risks and open questions
- Overlay choice: WireGuard vs VXLAN vs docker/podman overlay; rootless compatibility and MTU tuning.
- Security: mTLS between controller and agents; certificate/bootstrap flow; token reuse vs new CA.
- State store: SQLite on one controller is acceptable for v1, but recovery story (backup/restore) must be called out; HA is a future concern.
- Exec/probes over the network: ensure timeouts/backoff are sane to avoid blocking reconcile on slow links.
- Node churn: how aggressively to reschedule when a node flaps; need throttling and eviction policies.
- Host port services: keep supporting single-replica hostPort for edge cases; document incompatibility with multi-node routing where applicable.

---

## NSW — risks deep-dive (point-by-point)
- Overlay choice (WireGuard vs VXLAN vs docker/podman overlay)
  - Pros: WireGuard gives encryption and simpler keying; VXLAN is battle-tested and supported by iproute2; built-in docker/podman overlay minimizes new deps.
  - Cons: WireGuard in rootless can be tricky; VXLAN needs MTU tuning and per-node fdb/arp handling; docker/podman overlay may be opaque and hard to debug/control.
  - Notes: start with WireGuard for clear routing control; document MTU/fragmentation checks; keep a VXLAN fallback for environments where kernel modules are constrained.
- Security (mTLS and bootstrap)
  - Pros: mTLS gives mutual auth and protects exec/log traffic; reuse token model simplifies CLI/API alignment.
  - Cons: PKI/bootstrap adds operational steps; rotating certs across agents can be brittle; token reuse without mTLS leaves traffic exposed.
  - Notes: ship a controller-root CA, short-lived agent certs, and auto-rotation hooks; support offline/manual enroll as a fallback.
- State store (single SQLite)
  - Pros: zero extra infra, easy backup/restore; keeps current controller code largely unchanged.
  - Cons: single point of failure; write amplification from node heartbeats; restore is manual.
  - Notes: add periodic sqlite backups plus WAL mode; document recovery playbook; keep interface stable to swap DB later.
- Exec/probes over the network
  - Pros: centralizes logic; keeps health semantics unchanged; leverages existing probe code.
  - Cons: added latency and failure modes; long exec/log calls can tie up controller workers; TCP probes may mislead if tunnel flaps.
  - Notes: enforce per-call timeouts/backoff; mark node/agent errors distinctly in events; prefer Service VIP HTTP/TCP probes so only exec/log flows traverse RPC.
- Node churn and reschedule policy
  - Pros: fast reschedule keeps apps available when a node dies; drain/cordon gives operators control.
  - Cons: aggressive moves can thrash on flapping links; storage pinning may block failover; duplicate creations possible without idempotent runtime calls.
  - Notes: gate reschedule on consecutive missed heartbeats + grace; add jitter; respect storage pinning; emit clear events when skips occur.
- Host port services in multi-node
  - Pros: simple for single-replica edge cases; no dataplane overlay needed; backwards compatible.
  - Cons: not reachable cross-node; port collisions across nodes; ingress VIP path bypassed.
  - Notes: keep supported but warn in planner; for HA recommend Service VIP + overlay; reserve hostPorts only on pinned node when declared.

---

## Ingress and perimeter (Phase 5 outcome)
- **Front door stays single**: Caddy on the controller remains the only external ingress. Upstreams default to Service VIPs once at least one ready endpoint exists, ensuring cross-node routing uses the overlay path instead of host ports (covered by unit test `test_select_upstreams_prefers_service_vip`).
- **Route to Service CIDR**: When `AE_ENABLE_SERVICE_PROXY=1` with `AE_SERVICE_PROVIDER=overlay`, ensure the controller host (or Caddy container) can reach the Service CIDR. Preferred: attach the Caddy container to `AE_OVERLAY_NET` or add a host route to the overlay bridge so VIPs resolve without NAT quirks.
- **Per-node ingress option (documented, disabled)**: Operators may run an agent-managed Caddy/Envoy on a worker. The agent would advertise an ingress endpoint during heartbeat, obtain a node cert signed by the controller CA (via the join token + CSR flow), and register hosts back to the controller. Controller would then render site fragments that target Service VIPs and tag them to the node; certificate renewal and host ownership stay under controller CA. Feature flag to expose later: `AE_ENABLE_NODE_INGRESS=1` plus per-node `AE_NODE_INGRESS_ENDPOINT=https://<node-host>:8443`.
- **Firewall guidance**: open/allow
  - Controller agent API: 9110/TCP (agent → controller heartbeats/Pod CIDR assignment).
  - Agent runtime RPC: 9109/TCP (controller → agent ensure/exec/logs).
  - Overlay tunnel: WireGuard peer UDP (default 51820) or the site-specific WG port in the peer config; allow pod-to-Service CIDR traffic between nodes.
  - Ingress: 80/443 (or custom) on the controller host/Caddy container only. Per-node ingress (if enabled later) would listen on the same ports on each node.
  - Optional provider ports: Service VIP HAProxy containers bind Service ports on `AE_OVERLAY_NET`; no hostPorts required.
- **Verification path**: Caddy → Service VIP (overlay) → per-node HAProxy/iptables → pod IP. Keep hostPort outside the critical path; planner should flag hostPort use when multi-node is active.

---

## Security bootstrap and mTLS
- Decision: use controller-issued mTLS for all controller↔agent RPC (ensure/exec/logs/heartbeats). Tokens remain for user CLI/API auth; mTLS secures node control traffic.
- Options considered
  - Token-only over HTTPS: simplest but no mutual auth; bearer theft compromises all nodes — reject.
  - Reuse ingress/Caddy certs: couples node control plane to public TLS and renew cycles; weak client identity — reject.
  - Controller-root CA with per-agent short-lived certs, CSR authenticated by a join token: mutual auth, offline capable, rotation under our control — chosen default.
  - External CA override (user-provided): enables enterprise PKI/SPKI but raises config surface; keep as optional advanced mode.
- CA and certs (chosen path): controller owns a small CA; agents request short-lived certs (default 24h) via CSR over an authenticated bootstrap channel; automatic renewal before 80% of TTL; expose `ae-node rotate-certs` for manual recovery; support CRL/denylist by serial.
- Bootstrap flow (with privileged net helper present):
  1) Controller issues one-time join token (scoped to node name + expiry) and optional pre-rendered WG peer stanza.
  2) Agent presents join token → controller verifies → returns signed client cert + CA bundle + WG peer config.
  3) Net helper applies WG config (rootful), agent starts listening with mTLS using the new cert.
  4) Join token is single-use; future auth relies on cert renewal. Planner warns when tokens linger unconsumed.
- Rootless stance: full multi-node requires the privileged net helper for WireGuard; pure rootless is unsupported. Planner/CLI surface explicit warnings when AE_RUNTIME_BACKEND is rootless and multi-node is requested.

---

## Probes and exec over the network
- Decision: run probes and exec locally on each node via the agent; controller retains decision logic (thresholds/backoff) and consumes agent-reported outcomes.
- HTTP/TCP probes
  - Controller sends probe spec to agent; agent probes pod IP locally (or Service VIP on the node if needed) and returns success + message.
  - Reduces sensitivity to inter-node/overlay blips; no double-encapsulation of probe traffic.
  - Transport errors are classified separately (`ProbeTransportError`) so health gates don’t flap on transient agent reachability issues.
- Exec probes
  - Agent runs the command against the container runtime and returns exit code/output snippet; controller applies thresholds/backoff.
  - Keeps probe traffic local; avoids streaming IO over WAN.
- CLI exec/logs
  - Controller proxies streams via agent RPC for user-facing exec/logs; probes stay short and bounded.
- Timeouts and deadlines
  - Per-probe RPC deadline slightly above `timeoutSeconds` (HTTP/TCP default 3–5s; exec uses probe timeout).
  - Backoff for transport errors distinct from probe failures; emit structured events with cause (agent unreachable vs probe failed).

---

## Node churn and reschedule policy
- Detection: agent heartbeats every 10s (configurable); controller treats beats older than `AE_NODE_NOTREADY_AFTER` (default 40s) as NotReady/stale for scheduling and emits a `ScheduleWarning` with the skipped node ids.
- Cordon/drain: `ae nodes --cordon <id>` marks a node unschedulable; `ae nodes --drain <id>` cordons and best-effort evicts app containers via the node agent. Cordoned nodes stay in inventory but are skipped by the scheduler until uncordoned.
- Reschedule rules (current impl):
  - Round-robin across Ready, non-cordoned nodes; if `spec.storage` is declared, all replicas pin to the first eligible node to avoid cross-node volume assumptions.
  - Rollout create limits (`maxSurge`/`ordered`) and reconcile cooldowns cap new replica creation across all nodes; remaining replicas are created on subsequent reconciles.
  - NotReady nodes are ignored; replicas are recreated on other nodes with stable replica ids (`<app>-rev<rev>-<idx>`), relying on overlay/VIP routing for continuity.
- Recovery: when heartbeats resume before the grace window expires, nodes automatically become eligible; cordoned nodes require an explicit uncordon.
- Events/observability: schedule warnings are recorded in events; `ae nodes` and `/nodes` report cordon state and staleness.

## Scheduler (Phase 4 current implementation)
- Placement: round-robin across Ready, non-cordoned nodes and honoring `nodeSelector` + taints/tolerations.
- Stability: stable replica ids across nodes (`<app>-rev<rev>-<idx>`) so reschedules replace missing replicas instead of duplicating them.
- Storage-aware: storage volumes are bound to a node (`ae.node` label + DB binding). Scheduler reuses the bound node; if it is NotReady/cordoned, replicas stay unscheduled with a ScheduleWarning instead of failing over.
- Topology spread: when `topologySpreadConstraints` are present and multiple eligible nodes share the constraint’s `topologyKey`, replicas are balanced across the distinct values to minimize skew (best-effort, single constraint honored).
- Controls: `ae nodes --cordon/--uncordon/--drain` manipulate eligibility; drain performs best-effort evictions via the node agent before reschedule.
- Grace: readiness staleness window is `AE_NODE_NOTREADY_AFTER` (default 40s); stale nodes are skipped and listed in reconcile events.

---

## Agent API and heartbeats (Phase 2)
- Controller exposes `/v1/heartbeat` (token-gated via `X-Agent-Token`) when `AE_AGENT_API_PORT` is set; responses are persisted in the node tables and surfaced via `ae nodes` and `/nodes`.
- Agent (`ae-node`) sends Ready heartbeats every `AE_AGENT_HEARTBEAT_SECONDS` (default 10s) to `AE_CONTROLLER_URL`, including node metadata (id/name/backend/endpoint/labels/taints).
- Staleness: CLI/API treat Ready beats older than `AE_NODE_NOTREADY_AFTER` seconds (default 40s) as `NotReady (stale)` to highlight flaps.
- Env knobs: controller `AE_AGENT_API_HOST/PORT/TOKEN`, optional Pod CIDR pool `AE_POD_CIDR_POOL` + `AE_POD_CIDR_MASK`; agent `AE_CONTROLLER_URL`, `AE_AGENT_TOKEN`, `AE_NODE_ID/NAME/LABELS`, `AE_AGENT_ENDPOINT`, `AE_AGENT_HEARTBEAT_SECONDS`, overlay bring-up toggle `AE_AGENT_CONFIGURE_OVERLAY`/`--ensure-pod-net`, `AE_POD_BRIDGE`, `AE_WG_CONFIG`. Rootless nodes can heartbeat but still need the privileged net helper for overlay.

---

## Host port services in multi-node
- Options
  - Keep single-node semantics: allow `spec.service.port` only when replicas==1 and node is pinned; forbid cross-node exposure. Simple, zero new plumbing.
  - Per-node hostPorts: allow hostPort on any node but require `nodeName`/selector; expose via direct node IP. Works for edge cases but not HA; collisions possible across nodes.
  - Force Service VIP + overlay: disallow hostPort in multi-node, require Service abstraction and ingress/VIP for stability. Breaks some legacy TCP use-cases.
- Decision: keep hostPort only for single-replica, node-pinned apps; planner warns (and can fail in strict mode) when hostPort is declared without explicit node selection in multi-node mode. Recommend Service VIP + overlay for HA or multiple replicas.
- Mitigations
  - Detect hostPort conflicts per node; block apply if collision on same node.
  - Document reachability: hostPort only reachable via the hosting node’s IP; not load-balanced across nodes.
  - Provide external L4 proxy pattern (as today) for multi-replica TCP/UDP; point it at Service VIPs or per-replica pod IPs.
- Preferred HA path (Service VIP)
  - Use the Service VIP on the overlay (WireGuard) plus the `KubeProxyLikeProvider` (iptables/IPVS/Envoy) to steer traffic to ready pod IPs across nodes.
  - For external TCP, front the Service VIP with a small L4 proxy (Envoy/HAProxy) on the ingress node; no hostPort collisions and traffic remains balanced.
  - Planner: when multi-node is enabled and `service.port` is set, suggest switching to Service VIP or explicitly mark the app as single-node hostPort with `--strict` failing the apply unless pinned.

---

## Storage locality (Phase 6 outcome)
- **Node-owned volumes**: persistent `spec.storage[*]` volumes are labeled with `ae.node=<node-id>` in the runtime and recorded in the state store. The first successful placement creates the binding; later reconciles reuse it and pin all replicas to that node.
- **Scheduling behavior**: with a binding, the scheduler only targets the bound node. If that node is cordoned or NotReady, placement is skipped and a `ScheduleWarning` is emitted rather than failing over to another node (to avoid silent data loss). Without an existing binding, the first eligible node is chosen and recorded.
- **Planner/validation**: storage + no eligible bound node results in replicas staying unscheduled (status degraded) until the node returns or the binding is migrated. Events surface the reason.
- **Migration story (manual)**: scale app to 0, copy the Docker/Podman volume to the target node (rsync/ssh), update the binding (`sqlite3 state/controller.db "update storage_bindings set node_id='<new>' where app_name='<app>' and volume_name='<vol>'"`), then uncordon and reapply. No automated replication is planned.
- **Non-goal**: distributed block/RWX volumes remain out of scope for this cut.

---

## State store decision and Postgres plan
- Decision: keep SQLite for single-node/dev; add Postgres as the supported multi-node backend. MariaDB/MySQL deferred unless a hard requirement appears.
- Interface: introduce a `StateStore` protocol so controller logic is backend-agnostic. Methods mirror current SQLite behavior plus node/Service/IPAM additions:
  - `record_snapshot(manifest, runtime_result, health_report, revision, revision_status)`
  - `get_status(app)`, `list_status()`, `list_replicas(app)`
  - `prepare_revision(manifest, spec_hash)`, `get_revision_manifest(app, rev)`, `list_revisions(app, limit)`
  - `record_event(app, rev, event_type, message)`, `list_events(app, limit)`, `list_events_paginated(app, limit, offset)`
  - `get_probe_history(app, limit)`
  - `get_canary_state(app)`, `upsert_canary_state(app, weight, next_step_at, step, max_weight)`
  - Node inventory: `upsert_node(node_id, labels, taints, backend, endpoint, pod_cidr, wg_pubkey)`, `record_heartbeat(node_id, status, ts)`, `list_nodes()`, `get_node(node_id)`
  - Service/IPAM: `upsert_service(app, cluster_ip, ports_json)`, `upsert_service_endpoint(app, port, ip, target_port, ready)`, `list_service_endpoints(app)`
  - Storage bindings: `upsert_storage_binding(app, volume, node_id, retention)`, `list_storage_bindings(app)`, `delete_storage_bindings(app)`
- Postgres schema (DDL draft)
```sql
CREATE TABLE app_status (
  app_name TEXT PRIMARY KEY,
  desired_replicas INT NOT NULL,
  ready_replicas INT NOT NULL,
  live_replicas INT NOT NULL,
  revision INT NOT NULL,
  revision_status TEXT NOT NULL,
  image TEXT NOT NULL,
  created INT NOT NULL,
  updated INT NOT NULL,
  removed INT NOT NULL,
  ingress_host TEXT,
  ingress_path TEXT
);

CREATE TABLE replica_status (
  app_name TEXT NOT NULL,
  replica_id TEXT NOT NULL,
  ready BOOLEAN NOT NULL,
  live BOOLEAN NOT NULL,
  status TEXT NOT NULL,
  readiness_message TEXT NOT NULL,
  liveness_message TEXT NOT NULL,
  PRIMARY KEY (app_name, replica_id)
);

CREATE TABLE probe_history (
  id BIGSERIAL PRIMARY KEY,
  app_name TEXT NOT NULL,
  replica_id TEXT NOT NULL,
  check_time TIMESTAMPTZ NOT NULL,
  ready BOOLEAN NOT NULL,
  live BOOLEAN NOT NULL,
  readiness_message TEXT NOT NULL,
  liveness_message TEXT NOT NULL
);

CREATE TABLE app_revisions (
  app_name TEXT NOT NULL,
  revision INT NOT NULL,
  spec_hash TEXT NOT NULL,
  spec_json JSONB NOT NULL,
  image TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY (app_name, revision)
);

CREATE TABLE app_events (
  id BIGSERIAL PRIMARY KEY,
  app_name TEXT NOT NULL,
  revision INT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE rollout_canary (
  app_name TEXT PRIMARY KEY,
  weight DOUBLE PRECISION NOT NULL,
  next_step_at TIMESTAMPTZ NOT NULL,
  step DOUBLE PRECISION NOT NULL,
  max DOUBLE PRECISION NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

-- Node inventory / heartbeats
CREATE TABLE nodes (
  node_id TEXT PRIMARY KEY,
  name TEXT,
  labels JSONB DEFAULT '{}'::jsonb,
  taints JSONB DEFAULT '[]'::jsonb,
  backend TEXT,
  endpoint TEXT,
  pod_cidr TEXT,
  wg_pubkey TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE node_heartbeats (
  node_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  seen_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (node_id)
);

-- Service IPAM
CREATE TABLE services (
  app_name TEXT PRIMARY KEY,
  cluster_ip INET NOT NULL,
  ports JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE service_endpoints (
  app_name TEXT NOT NULL,
  port INT NOT NULL,
  ip INET NOT NULL,
  target_port INT NOT NULL,
  ready BOOLEAN NOT NULL,
  PRIMARY KEY (app_name, port, ip)
);

CREATE TABLE storage_bindings (
  app_name TEXT NOT NULL,
  volume_name TEXT NOT NULL,
  node_id TEXT NOT NULL,
  retention TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (app_name, volume_name)
);
```
- Env/CLI knobs
  - `AE_STATE_BACKEND`: `sqlite` (default) or `postgres`.
  - `AE_STATE_DSN`: e.g., `postgresql://ae:ae@127.0.0.1:5432/ae`.
  - CLI: `ae.controller --state-backend postgres --state-dsn $DSN` (envs as fallback). Planner errors if multi-node is enabled while backend=sqlite.
  - Pool sizing via `AE_STATE_POOL_MIN` / `AE_STATE_POOL_MAX` (default 5/10).
  - Migration helper: `ae state migrate --from sqlite:///state/controller.db --to $DSN` (table-wise copy with upsert; controller stopped during run).

---

## Next actions to start
- Phase 7 CI: add the two-node job (apply → rollout → failover → delete) using the new `specs/examples/echo-multinode.yaml` and `docs/multinode-lab.md` steps; capture artifacts (events, node inventory, service list) on failure.
- Add overlay Service-VIP routing test once CI substrate exists (curl Service VIP from controller and worker, assert endpoints shift on node drain).
