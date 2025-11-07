# Interactive Demo Labs — Project Proposal

## Context & Goals

- Objective: add “interactive labs” to the docs server so users can (a) walk through hands‑on product demos and (b) explore k1s internals to learn the architecture.
- Current docs stack: lightweight Markdown→HTML builder (`docs/build_docs.py`) generating `docs/site/*.html` with a fixed nav and client‑side Mermaid; API links resolved via `DOCS_API_BASE` to the running controller’s HTTP API (`/status`, `/events`, `/metrics`, `/logs`, `/swagger`, `/redoc`, `/dashboard`).
- Constraints: HTTP API is intentionally read‑mostly; write paths are via CLI and spec files. Docs are static pages served by Caddy in dev (`docs.home.arpa:8443`).
- Design intent: keep the controller/observability leaf modules stable; integrate labs without invasive runtime changes; prefer opt‑in for anything that enables mutations from the browser.

## Three Approaches

1) Guided Read‑Only Labs (Stepper + Verifiers)
- What: Static pages with a client‑side “stepper” UI. Each step describes an action the user runs locally (e.g., `ae apply -f specs/examples/echo.yaml`) and the page verifies success by polling read‑only endpoints (`/status/<app>`, `/events/<app>`, `/metrics`).
- How: A small JS widget embedded by the docs builder. Steps are defined in per‑lab JSON with: instruction, a verification probe (HTTP GET), and a condition (JSONPath or small predicate). Show green checks as conditions pass.
- Pros: Zero backend risk, works with today’s API, easy to ship. Offline‑friendly for air‑gapped demos. Clear upgrade path to richer labs.
- Cons: User must run commands locally; browser cannot “apply” changes itself.

2) Demo Control API (Feature‑Flagged, Dev‑Only)
- What: An opt‑in mini‑API exposing limited mutations for demos only (apply curated examples, scale up/down, trigger canary, rollback).
- How: New module (e.g., `ae.demo.api`) hosting endpoints under `/demo/*`, wired behind `AE_DEMO_API=1` and a random bearer token printed on startup. Implementation reuses existing reconciler/CLI code paths and only accepts whitelisted, bundled specs from `specs/examples/`.
- Pros: One‑click labs from the browser; repeatable and scriptable demos on the docs server.
- Cons: Security surface (even if gated), more moving parts; needs careful scoping and tests.

3) In‑Browser Terminal to a Restricted Runner
- What: Embed a WebTTY/xterm.js UI bound to a sandboxed process/container that can run `ae` commands with limited permissions and a whitelisted working dir.
- How: Add a tiny WebSocket shim alongside Caddy for dev, mount the repo’s `specs/examples/`, expose a “Copy & Run” button per step.
- Pros: Powerful and familiar; doubles as a general teaching console.
- Cons: Highest complexity; sandboxing, lifecycle, and portability across Docker/Podman dev setups.

## Revised Direction — Single Playground Page

We will build one comprehensive “Interactive Lab Playground” page that demonstrates all interactive features in one place. Once approved, we’ll stamp out additional lab pages using the same technical reference.

Key goals for the playground:
- Unified surface to exercise: read‑only verification, controlled write actions (apply/scale/reset), event/log viewers, metrics checks, and ingress tests.
- Pluggable backends selectable via a page toggle:
  1) Local k1s (host): the current dev setup using Docker/Podman and Caddy.
  2) k1s‑in‑Docker (compose): an all‑in‑one stack for self‑contained labs.
  3) k3s: connect to a local k3s (or k3d) for Kubernetes‑oriented exercises.
- Session‑scoped isolation so each browser session has its own namespace/app prefix for safe experimentation.

## Playground Feature Set (v1)

- Mode selector: Read‑Only Verify | Controlled Actions | Terminal (optional).
- Backend selector: k1s‑Host | k1s‑in‑Docker | k3s.
- Session token + isolation: cookie‑backed token; resource prefix `lab-<short-id>` and, for k3s, per‑session namespace.
- Core widgets:
  - Stepper with checks: instructions + live verifiers against `/status`, `/events`, `/metrics`, `/logs`.
  - Apply/Scale/Reset buttons (when “Controlled Actions” enabled): call a gated lab service to apply curated specs from `specs/examples/`.
  - Event & log viewers: auto‑tail recent events and logs for the session’s app(s).
  - Ingress tester: open target host and show quick pass/fail on HTTP 200.
- Accessibility and no‑JS fallback: instructions still visible as plain content.

## Architecture Overview

- Docs remain static HTML via `docs/build_docs.py`. We add a minimal, dev‑only “Lab Orchestrator” microservice co‑located with the controller, reachable at `/labs/*` behind Caddy.
- Browser → Docs (static) for UI; Docs JS → `/labs/*` for controlled actions and session management; Docs JS → controller read‑only endpoints for verification.
- Backends:
  - k1s‑Host: orchestrator shells out to the existing CLI or calls internal adapters to apply curated specs, tagged with the session id.
  - k1s‑in‑Docker: orchestrator targets a controller running in a sibling container; Caddy proxies both controller and orchestrator; specs volume mounted read‑only.
  - k3s: orchestrator holds a kubeconfig (from local dev) and manages a per‑session namespace, applying templated YAML derived from our examples via `kubectl`/client‑libs.

### Verification patterns (examples)
- Status ready: GET `/status/<app>` expect `jsonpath: $.readyReplicas == $.spec.replicas`.
- Event recorded: GET `/events/<app>?limit=10` expect `contains: "Applied manifest"`.
- Metrics present: GET `/metrics` expect `regex: ^ae_ready_replicas{.*app=\"<app>\".*} [0-9]+$`.

- Frontend widget:
  - Vanilla JS (no external deps) rendered after content; progressive enhancement if JS disabled.
  - Backoff strategy: 1s→2s→3s (cap 5s), total timeout default 60s.
  - Compact UI to match existing docs theme.

## Playground Flow (single page)

- Section A: Environment check — detect API base, show which backend is reachable; allow switching backend with a dropdown.
- Section B: Apply Echo — one button to apply example; shows underlying YAML; verifier checks events/status.
- Section C: Scale & Rollout — buttons to scale up/down and trigger a canary manifest; verifiers check events and metrics.
- Section D: Logs & Events — live tails with pause/resume.
- Section E: Ingress Test — open app host, show status and HTTP checks.
- Section F: Reset — purge session resources safely.

## Implementation Plan (Playground v1)

1) Docs Builder + UI
- Add `docs/static/labs.js` and `labs.css`; include via `build_docs.py` template.
- Create `docs/playground.md` → `docs/site/playground.html` with the unified UI and sections A–F.
- Keep the stepper/verify JSON manifest inlined in the page for now.

2) Lab Orchestrator (dev‑only)
- New module `ae.labd` (or within `ae.cli` as `labs_service`) served at `/labs/*`, enabled with `AE_LABS=1`.
- Endpoints (whitelisted actions only):
  - `POST /labs/session` → returns `{session_id, backend}`.
  - `POST /labs/apply` (example id, session)
  - `POST /labs/scale` (app, replicas, session, limits)
  - `POST /labs/reset` (session)
  - `GET /labs/info` (which backends available; versions; health)
- Security: random bearer token printed at startup; loopback binding by default; CORS limited to docs origin; resource caps.

3) Backend Integrations
- k1s‑Host: call internal adapters or shell out to `ae` with a session‑prefixed app name (e.g., `echo-lab-abc`).
- k1s‑in‑Docker: provide `ops/dev/labs-compose.yaml` to run controller+ingress+orchestrator+docs; mount `specs/examples/` read‑only.
- k3s: use an existing local kubeconfig; on session create, generate namespace `lab-<id>`, apply templated examples; require `kubectl`/client‑lib availability and RBAC checks. Optional helper script to bootstrap a `k3d` cluster.

4) Testing
- Unit: predicate evaluator for verifiers; orchestrator request validation and action guards.
- Manual: exercise all three backends on Linux/macOS (Docker and Podman), verify Caddy proxying.

## k1s‑in‑Docker vs Local k1s vs k3s — Recommendation

- Local k1s (Default for dev): simplest path, zero extra infra, uses today’s controller and Caddy. Best for contributors and offline demos.
- k1s‑in‑Docker (Preferred for “clean room” labs): reproducible, OS‑agnostic, easy teardown. We will ship `labs-compose.yaml` and a `make labs` target to launch the full stack in isolation.
- k3s (Optional track): valuable for Kubernetes parity teaching; requires a local cluster (k3s or k3d) and RBAC. We will support it via the orchestrator with per‑session namespaces. Not the default due to heavier prerequisites.

Decision: build the playground to support all three, with k1s‑in‑Docker as the recommended “workshop” default and Local k1s as the contributor default. k3s support is enabled when detected.

### k3d Automation Plan (auto spin-up supported)

- Provisioning strategy: leverage `k3d` to create an ephemeral local k3s cluster when the user selects the “k3s” backend.
- Entry points:
  - `make labs-k3d-up` / `make labs-k3d-down` convenience targets.
  - `scripts/lab_k3d.sh up|down|ensure` handles install, create, merge kubeconfig, and teardown.
  - Orchestrator auto-create: when `AE_LABS_K3D_AUTOCREATE=1`, backend=k3s triggers `ensure` on first use.
- Default cluster profile:
  - Name: `k1s-labs`
  - Ports: map Traefik LB to host without colliding with Caddy: `--port 8081:80@loadbalancer --port 8444:443@loadbalancer`.
  - Ingress: keep Traefik enabled (k3s default) for simplicity.
  - Registry (optional): create `k3d-k1s-registry` bound to `localhost:5000` and configure as mirror for faster pulls of example images.
  - Nodes: 1 server (agents optional).
- Kubeconfig handling: `k3d kubeconfig merge k1s-labs --switch-context`; orchestrator reads from `$KUBECONFIG` or `~/.kube/config`.
- Session isolation: per-session namespace `lab-<id>` with `ResourceQuota` and `LimitRange` applied on session create.
- Teardown and idempotency: `scripts/lab_k3d.sh down` removes cluster and registry; `ensure` is safe to re-run.

Example create (for docs):

```
k3d cluster create k1s-labs \
  --port 8081:80@loadbalancer \
  --port 8444:443@loadbalancer \
  --wait --timeout 120s
```

If the chosen ports are in use, the script will choose alternatives (e.g., 8082/8445) and surface them in the playground UI.

## Kubernetes‑Oriented Lab Track (k3d “proposed case”)

Purpose: teach Kubernetes parity using our exporter and a real k3s API via k3d.

Playground flow (backend=k3s):
- Export: render K8s manifests using `ae.k8s.exporter` for a selected example (e.g., Echo) with `ingressClassName=traefik`.
- Apply: orchestrator applies the YAML to namespace `lab-<id>` using a limited‑scope service account.
- Verify: browser checks rollout status through orchestrator helpers (read‑only) that query Deployment/Service/Ingress objects.
- Scale: orchestrator patches the Deployment replicas; predicates watch `.status.availableReplicas`.
- Ingress: open `https://localhost:8444/` (or the dynamically chosen port) to verify routing via Traefik.
- Reset: orchestrator deletes namespace `lab-<id>`.

Buttons and underlying actions:
- “Export + Apply Echo (K8s)”: run exporter then `kubectl -n lab-<id> apply -f -`.
- “Scale to 3”: `kubectl -n lab-<id> scale deploy/echo --replicas=3`.
- “Check Rollout”: `kubectl -n lab-<id> rollout status deploy/echo`.

Verification predicates (examples):
- Deployment available: `.status.availableReplicas == .spec.replicas`.
- Service endpoints: Endpoints object has at least one address.
- Ingress ready: HTTP 200 through LB port 8081/8444.

Security & limits:
- Namespaced RBAC via a per-session service account.
- ResourceQuota/LimitRange to cap pods/cpu/memory.
- Image allow-list (examples only) enforced in orchestrator; reject unknown images.

UX:
- If k3d is missing and auto-create is off, show a one-click “Install & Start k3d” action with logs inline.
- Backend switcher tears down or preserves session state based on user choice (“clean switch” vs “keep resources”).

## Optional: Embedded Terminal

- Add a restricted terminal panel (xterm.js) bound to the orchestrator for `ae` subcommands and `kubectl` scoped to the session namespace.
- Off by default; enable via `AE_LABS_TERMINAL=1`. Never expose on public sites.

## Security & Operations

- Default posture: browser never mutates state in MVP; all actions are user‑driven via CLI.
- Any future mutation capability must be feature‑flagged, token‑gated, and off by default; whitelist inputs and cap resources.
- No secrets in labs content; only sealed samples committed under `specs/examples/` per repo policy.

## Telemetry & Observability

- Consider a lightweight “labs progress” counter exposed as a metric only in MVP (client‑side increments via an image beacon to `/dashboard` is out‑of‑scope). For now, rely on standard `/events` and `/metrics` for verification.

## Milestones & Estimates

- P1 (1–1.5 days): Playground page + labs.js/css + verifiers + orchestrator skeleton (session, apply, scale, reset) + k1s‑Host backend.
- P2 (0.5–1 day): k1s‑in‑Docker compose stack; docs wiring; “backend selector” detection and UX polish.
- P3 (0.5–1 day): k3s backend with per‑session namespaces; helper script for k3d; guards and RBAC checks.
- P4 (0.5 day, optional): Embedded terminal.

## Files to Touch (Playground)

- `docs/build_docs.py` — include labs assets; add mapping for `playground.md` → `playground.html`.
- `docs/playground.md` — the single interactive lab page.
- `docs/static/labs.js`, `docs/static/labs.css` — UI and verifiers.
- `src/ae/labd/__init__.py` and `src/ae/labd/__main__.py` (or `ae.cli.labs_service`) — orchestrator service (dev‑only).
- `ops/dev/labs-compose.yaml` — containerized stack for k1s‑in‑Docker.
- `scripts/lab_k3s.sh` — optional helper for k3d bootstrap/teardown.

## Open Questions

- Do we want a dedicated “Labs” nav entry or list labs under “Examples” for now?
- Should we add a minimal `/demo/ping` read‑only endpoint to check availability explicitly, or keep using `/health`?
- Any preference for JSONPath subset syntax in predicates (keep minimal vs. bring a tiny dependency)?

---

If this direction looks good, I’ll implement M1 (widget + two labs in JSON + labs page) and thread it into the docs builder with minimal, well‑scoped changes.

## Reference Interactive Lab Playground — Technical Spec

This section is the authoritative reference for building the single interactive playground page. It defines the page structure, client behaviors, orchestrator API, predicates, and sample flows. Subsequent lab pages will reuse these primitives.

### Page & Assets
- Source: `docs/playground.md` → built to `docs/site/playground.html` by `docs/build_docs.py`.
- Assets: `docs/static/labs.js`, `docs/static/labs.css` are included by the template. No external JS deps.
- Nav: add a “Playground” link next to “Demo Modes”.

### UI Structure (Sections A–F)
- A. Environment & Backend Selector (`#env`)
  - Shows detected API base, orchestrator availability, and backend capabilities (k1s‑Host, k1s‑in‑Docker, k3s).
  - Controls: dropdown `backend`, toggle `controlled_actions`, “Start Session” button.
- B. Apply Echo (`#apply`)
  - Button: “Apply Echo Example” → POST `/labs/apply {example:"echo"}`.
  - Readonly view of the YAML used (from `specs/examples/echo.yaml`).
  - Verifier panel: status/event checks.
- C. Scale & Rollout (`#scale`)
  - Buttons: “Scale to 2”, “Scale to 3”, “Canary 10%”.
  - Verifier panel: events and metrics checks.
- D. Logs & Events (`#observe`)
  - Live tail of `/events/<app>` and `/logs/<app>?tail=...` with pause/resume.
- E. Ingress Tester (`#ingress`)
  - Open app host and display HTTP status; hint to add hosts if necessary.
- F. Reset (`#reset`)
  - Destroys session resources; confirms via modal; disables controls until session restarted.

### Session Model
- Browser obtains a session via `POST /labs/session` returning `{session_id, backend, token_hint}`.
- All resources are prefixed with `echo-<session>` (k1s) or created in namespace `lab-<session>` (k3s via k3d).
- Token is stored in memory and sent as `Authorization: Bearer <token>` for `/labs/*` only.

### Orchestrator API Contract (dev‑only)
- `POST /labs/info` → `{ backends: ["k1s-host","k1s-docker","k3s"], api_base, k3d:{present,ports:{http,https}} }`
- `POST /labs/session { backend }` → `{ session_id, backend }`
- `POST /labs/apply { session_id, backend, example:"echo" | "echo-multiport" | "rollout", params? }` → `{ ok:true, app:"echo-<id>", manifest_revision }`
- `POST /labs/scale { session_id, app, replicas:int }` → `{ ok:true }`
- `POST /labs/reset { session_id }` → `{ ok:true }`
- Errors: `{ ok:false, error:{code, message, details?} }` with HTTP 4xx/5xx.
- Security: require bearer token in `Authorization` header; bind service to loopback; CORS origin limited to docs page.

### Verification Predicates (client‑side)
- Types: `httpStatus`, `contains`, `regex`, `jsonpathEq`, `jsonpathGte`, `jsonpathExists`.
- Targets: controller endpoints (`/status/<app>`, `/events/<app>`, `/metrics`, `/logs/<app>`); for k3s, orchestrator exposes a read‑only proxy for select objects: `/labs/k8s/{kind}/{name}?ns=lab-<id>`.
- JSONPath subset:
  - Dot paths and simple arrays: `$.status.readyReplicas`, `$.items[0].message`.
  - No filters; keep implementation tiny and audited.
- Polling: default interval 1000 ms; backoff to 2000/3000 ms; timeout 60 s unless overridden.

### Backend Detection & Routing
- JS detects the controller API base using the same logic as `build_docs.py` (prefers `DOCS_API_BASE` → `api.home.arpa` → `127.0.0.1:9108`).
- JS calls `/labs/info` to discover available backends and k3d status; the backend selector is filtered accordingly.
- For k1s‑in‑Docker, Caddy proxies the orchestrator and controller from the compose network to the docs origin; the playground uses relative paths.

### Example: Echo Flow (k1s‑in‑Docker)
1) Start session (backend `k1s-docker`) → receive `session_id = abc`.
2) Click “Apply Echo Example” → POST `/labs/apply {example:"echo", session_id:"abc"}`.
3) Verifier A (events): GET `/events/echo-abc?limit=10` expect contains “Applied manifest”.
4) Verifier B (status): GET `/status/echo-abc` expect `jsonpathEq($.readyReplicas, $.spec.replicas)`.
5) Verifier C (metrics): GET `/metrics` expect `regex(^ae_ready_replicas\{.*app=\"echo-abc\".*\} 1$)`.
6) Ingress: open `https://echo-abc.home.arpa:8443/` and show HTTP 200.
7) Scale to 3 → POST `/labs/scale` then verify events and metrics reflect 3.
8) Reset → POST `/labs/reset {session_id:"abc"}`.

### Example: Echo Flow (k3d)
1) Start session (backend `k3s`) → orchestrator ensures `k3d` cluster `k1s-labs` exists; creates namespace `lab-abc` and SA + RBAC.
2) “Export + Apply Echo (K8s)” → orchestrator runs exporter then applies to `lab-abc` namespace.
3) Verifier Deployment: GET `/labs/k8s/deploy/echo?ns=lab-abc` expect `jsonpathEq($.status.availableReplicas, $.spec.replicas)`.
4) Verifier Ingress: HTTP 200 at `https://localhost:8444/` for `/echo` path.
5) Scale and verify rollout; Reset deletes namespace.

### Minimal UI API (labs.js)
- `LabClient.init()` → detects API base, fetches `/labs/info`, hydrates UI state.
- `LabClient.startSession(backend)` → POST session; stores `session_id`.
- `LabClient.apply(example)` / `LabClient.scale(app, replicas)` / `LabClient.reset()`.
- `Verifier.run({url, type, expect, headers?})` → Promise that resolves pass/fail with details and elapsed time.
- `EventTail.attach(el, {source:"events"|"logs", app, limit})` → manages polling and pause/resume.

### Accessibility & Progressive Enhancement
- All instructions render as static HTML; controls are `<button>` elements enhanced by JS.
- Verify states use role=“status” with clear text; color is not the only signal.

### Acceptance Criteria (Playground v1)
- From a clean dev machine:
  - k1s‑Host: `make demo` + open `playground.html` → session works; apply/scale/reset succeed; verifiers pass.
  - k1s‑in‑Docker: `docker compose -f ops/dev/labs-compose.yaml up` → same as above inside the compose stack.
  - k3d: `make labs-k3d-up` → playground detects k3s backend; export/apply/scale/reset work in a per‑session namespace; ingress reachable on 8444.
- Security: orchestrator disabled by default; enabled with `AE_LABS=1`; requires bearer token; CORS restricted to docs origin.

### Future Extensions
- Multi‑app scenarios (blue/green) with prefer‑first routing visualized.
- “Explain” panels that link to exact code references (e.g., reconciler paths) and diagrams.

## Tech Stack Review (HTMX + SSE preferred, pragmatic fallback)

- Frontend
  - Use HTMX for progressive enhancement: `hx-get`, `hx-post`, `hx-trigger`, and `hx-vals` for simple interactions. Adopt the HTMX SSE extension for live updates where it simplifies DOM updates.
  - Keep a small vanilla JS layer (`labs.js`) for predicate evaluation, backend detection, session/token handling, copy buttons, and complex step orchestration. HTMX augments, not replaces, this layer.
  - Load HTMX and the SSE extension via CDN on `playground.html` only; no bundler needed. Maintain accessibility by updating `aria-live` regions for dynamic content.

- Streaming model
  - Prefer Server‑Sent Events for logs/events/status tails: simpler than WebSockets, plays well with Caddy, sufficient for one‑way updates. Provide a polling fallback if SSE is not available.
  - Use WebSockets only if the optional terminal is enabled; otherwise avoid the extra surface area.

- Backend
  - Implement the orchestrator with FastAPI/Starlette (consistent with existing API that serves Swagger/ReDoc). Add SSE endpoints as streaming responses.
  - Endpoints: `/labs/sse/events`, `/labs/sse/logs`, `/labs/sse/status` (k1s) and `/labs/sse/k8s` (k3s), alongside existing JSON POST actions.
  - Security: bearer token required; CORS restricted to docs origin; bind to loopback by default; rate‑limit and cap resource sizes.

- Build & Docs integration
  - Extend `docs/build_docs.py` to include HTMX + SSE scripts on the playground page and to ship `labs.js`/`labs.css`.
  - No change to the broader docs pipeline; other pages remain static.

- Rationale
  - HTMX + SSE keeps the UI minimal and declarative for the streaming parts while our focused JS handles verification logic and cross‑backend orchestration.
  - This aligns with the project’s preference for small, explicit components and avoids large front‑end frameworks.
