Got it — here’s a pragmatic path to a “minimal app engine” that runs well on a 1-CPU/2-GB VPS and gives you: image management, a basic “pod” abstraction, zero-downtime deploys, health checks, ingress/TLS, logs/metrics, and a tiny CLI. You’ll keep things simple, but leave room to evolve.

# Architecture at a glance

* **Runtime**: Docker or containerd (pick one; Docker is quickest).
* **Controller**: single daemon (Python or Go) with a reconcile loop + SQLite state.
* **Spec**: one declarative YAML per app (versioned), your “Deployment”.
* **Ingress/TLS**: Caddy (ACME built-in) or nginx; controller writes vhost config from specs.
* **Observability**: logs via `docker logs`/journald; metrics via `docker stats` + a Prometheus exporter.
* **Secrets**: file-based sealed secrets (age/sops) decrypted at deploy.
* **CLI**: `ae` (apply/get/logs/rollout/restart/rollback).

---

# Milestones (build order)

# Progress Log (2025-10-23)

* [x] Phase 0 – Environment & scaffolding complete: Python package layout (`src/ae/...`), tooling (`pytest`, `ruff`, `mypy`, pre-commit), bootstrap script, and dev assets committed.
* [x] Phase 1 – Core spec and reconcile skeleton landed: Pydantic manifest loader, runtime stub, SQLite snapshot store, and CLI commands (`apply`, `status`, `logs`) with unit coverage.
* [x] Phase 2 – Docker runtime + health loops: Real Docker adapter with create/start/cleanup flow, readiness/liveness evaluator with initial-delay semantics, probe history persisted for CLI inspection.
* [x] Phase 3 – Ingress & TLS automation: Added Caddy templating/reload hooks, persisted ingress host metadata, and surfaced replica/ingress status via CLI.
* [x] Phase 4 – Rollouts & rollbacks: Revision-aware reconciler/runtime, revision history + CLI rollback/list commands, and health-gated status tracking.
* [x] Phase 5 – Secrets & registry auth: SOPS-backed secret manager with env injection, registry credential loading, and CLI wiring.
* [x] Phase 6 – Observability baseline: Metrics snapshot CLI, event logging in SQLite, and registry/secret guidance updated.

---

## Known Gaps (not implemented yet)

- HTTP API writes: read-only API (metrics/status/events) only; no mutate endpoints.
- Advanced probes: only HTTP-get; no TCP/exec probes, no per-probe backoff limits.
- Resources/volumes: initial limits and bind mounts only; no requests enforcement or cgroups beyond Docker flags.
- Rollout options: only rolling-replace with implicit surge=1; no pause/resume or canary.
- Ingress extras: no TLS options beyond Caddy defaults; no multi-path or headers.
- Auth ergonomics: no helper to fetch short-lived registry tokens.
- Multi-node: intentionally out of scope.

## Next Steps (near-term plan)

1) Controller daemon + polling watch
- Add `python -m ae.controller` with `--once | --loop --interval N` and `--specs DIR`.
- Poll the specs directory for `*.y(a)ml` and reconcile all apps.

2) Minimal metrics exporter
- Optional `--metrics-port PORT` serving Prometheus text with basic app/replica gauges.

3) Logs implementation (basic)
- `ae logs <app> [-f]` using Docker SDK; default to current revision’s first container.

4) Resource flags
- Map `spec.resources.limits` to Docker `--cpus`/`--memory` on create.

5) Volumes (starter)
- Support simple hostPath → container mount mappings in spec.

6) HTTP API (starter)
- Tiny read-only endpoints: `/status`, `/events`, `/metrics` for CLI parity.

Stretch (after the above)
- TCP/exec probes, pause/resume rollouts, richer ingress, backup/restore command.

## 0) Bootstrap (1–2 hrs)

* Install Docker (or containerd+nerdctl), Caddy, SQLite.
* Create a repo with: `cmd/`, `pkg/` (or `ae/` in Python), `specs/`, `state/`.

## 1) Core “pod” & app spec (day 1)

**Goal:** run one app via a declarative spec and reconcile to desired state.

**Spec v0 (YAML):**

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

**Controller responsibilities:**

* Parse spec → compute desired container set (replicas, names).
* **Reconcile loop** (every 3–5s or on file watch):

  1. Pull image (with auth if set).
  2. Start new container(s) with generated name/version label.
  3. Health-gate readiness (HTTP/TCP/exec).
  4. On success, stop & remove old version (rolling-replace 1 by 1).
  5. Update ingress (write Caddy config and reload).
  6. Persist status in SQLite (`apps`, `revisions`, `replicas`).

**Data model (SQLite):**

* `apps(name TEXT PK, gen INT, desired_replicas INT, image TEXT, …)`
* `revisions(app, rev INT, image, created_at, status, PRIMARY KEY(app,rev))`
* `replicas(app, rev, instance_id, container_id, state, started_at, …)`

**Minimal health checker:**

* Side thread that hits readiness/liveness endpoints; mark instance `Ready` only after success.

## 2) Ingress & TLS (day 1)

* Choose **Caddy** for simplest ACME:

  * Controller writes a site block per app:

    ```
    myapp.example.com {
      reverse_proxy 127.0.0.1:<allocated_nodeport_or_host_port>
    }
    ```
  * Reload via `caddy reload`.
* If you prefer nginx, template + `nginx -s reload` (no built-in ACME).

## 3) Zero-downtime deploy & rollback (day 2)

* **Strategy: RollingReplace( maxUnavailable=0, maxSurge=1 )**

  * Create `rev = current+1`; start a new container; wait for readiness; switch ingress; stop previous.
* Keep last N revisions; `ae rollback myapp --to-rev <n>` switches image/env back and reconciles.

## 4) CLI & UX (day 2)

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

## 5) Secrets & registry auth (day 2–3)

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

- Next up: Secrets and Config (ConfigMap/Secret-like resources)
  - Goals: define resource schemas, implement decryption and mounting/env injections, add CLI commands, update docs and examples.

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
* [ ] Probes set; graceful shutdown implemented.
* [ ] CPU/Memory requests defined.
* [ ] PVCs (if any) are generic and RWO.
* [ ] Images available for amd64/arm64.

# How to *prove* portability fast

* Spin up **k3s** (or MicroK8s) locally and apply the same manifests.
* Run e2e smoke tests that only assume ClusterIP + Ingress.
* If you want more rigor, run your app against **Kind** + **k3s** + **MicroK8s** in CI with a shared test suite. If all pass, you’re in excellent shape.

If you want, I can take one of your existing app manifests and “shrink-wrap” it to this baseline, then provide tiny overlays for NGINX/Traefik or for clusters that do offer a real LoadBalancer.

