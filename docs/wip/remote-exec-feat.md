# Remote Exec + Port-Forward (Option A Draft)

This draft captures the "Option A" path that aligns with:
- SPDY exec/port-forward compatibility for kubectl + k9s over time.
- A browser-friendly Dashboard and Playground experience.
- A future `ae` remote shell command that speaks SPDY.

Status: Option A implemented; conformance-alignment gaps remain.

---

## Goals

- Provide a remote shell and port-forward feature in the Dashboard and Playground.
- Preserve kubectl/k9s compatibility (SPDY exec + port-forward).
- Add `ae` CLI remote shell commands using SPDY protocol.
- Keep security gating explicit and auditable.

## Non-goals

- Fully emulate kubelet exec/attach edge cases on day one.
- Replace kubectl or k9s functionality (we complement them).
- Provide WebRTC or SSH-style tunnels in the browser.

---

## Option A Overview (Recommended)

**Core idea:** apishim remains the SPDY endpoint for kubectl/k9s/`ae` CLI.  
Browsers use WebSocket (not SPDY), and the server adapts WebSocket streams to the runtime exec/port-forward.

### Summary of flows

1) **kubectl/k9s -> apishim (SPDY) -> runtime exec/port-forward**
   - SPDY/3.1 + `X-Stream-Protocol-Version` (v5..v1) for exec.
   - SPDY/3.1 + `portforward.k8s.io` for port-forward.

2) **Dashboard/Playground -> apishim (WebSocket) -> runtime exec/port-forward**
   - WebSocket subprotocol `v5.channel.k8s.io` for exec.
   - WebSocket subprotocol `portforward.k8s.io` for port-forward.
   - Browser does not speak SPDY, so WebSocket is the native transport.

3) **`ae` CLI -> apishim (SPDY) -> runtime exec**
   - `ae exec` / `ae shell` uses SPDY protocol, matching kubectl.
   - Ensures our CLI behaves like kubectl/k9s for compatibility tests.

---

## Dashboard Plan (Option A)

### Features

- **Remote Shell**: modal terminal (xterm.js).
  - Uses WebSocket exec (`v5.channel.k8s.io`) to apishim.
  - Supports stdin/stdout/stderr, terminal resize, exit status.

- **Port-Forward**: per-pod or per-service port actions.
  - Uses WebSocket port-forward (`portforward.k8s.io`).
  - For HTTP targets, optionally expose a proxy URL for a one-click open.

Decision: use xterm.js for the terminal UI (no minimal fallback in v1).

### Required server work

- apishim: add WebSocket exec handling using the same channel mappings as Kubernetes:
  - stdin=0, stdout=1, stderr=2, error=3, resize=4.
  - Accept `v5.channel.k8s.io` (and optionally v4/v3/v2/v1).
  - Runtime wiring via existing `exec_attach`.

- Dashboard UI:
  - Add buttons and modal in `src/ae/observability/http_api.py` HTML/JS.
  - Connect to apishim WS endpoint with bearer token when provided.

---

## Playground Plan (Option A)

### Features

- **Debug Tools** section gated by Labs token:
  - Remote Shell
  - Port-forward (target app/port)

### Gating behavior

- Disabled in read-only mode.
- Enabled only when `AE_LABS=1` and Labs token is valid.
- If disabled, show copyable CLI instructions (kubectl / ae).

### Required work

- Extend `docs/site/static/labs.js` to wire UI controls.
- Add new markup to `docs/guides/playground.md`.

---

## `ae` CLI Plan (SPDY)

### New commands

- `ae exec <app> -- <cmd ...>`
- `ae shell <app> [--container <name>] [--tty]`

### Transport

- SPDY/3.1 with `X-Stream-Protocol-Version` negotiation.
- Stream protocol versions (v5..v1) should match kubectl:
  - v5 supports CLOSE signal (future).
  - v4 supports exit codes on error stream.
  - v3 adds resize stream.
- **Fallback:** add optional WebSocket exec fallback (`v5.channel.k8s.io`)
  - Trigger when SPDY fails with 400/404/426 or the server advertises only WS.
  - Guard with `--ws-fallback` or `AE_EXEC_WS_FALLBACK=1` to avoid silent transport changes.
  - Keeps `ae` usable against apishim in WS-only environments (labs/demo).

### Why SPDY for `ae`

- Aligns with kubectl/k9s expectations and test matrices.
- Provides a single canonical path for remote exec over time.

---

## kubectl/k9s Compatibility Checklist

Exec (SPDY):
- Accept `X-Stream-Protocol-Version` list and negotiate v5..v1.
- Create error, stdout, stderr, stdin, resize streams via SYN_STREAM.
- Send SYN_REPLY correctly (header block + 4 bytes length).
- Send exit status on error stream as JSON (v4+).
- Allow late stream creation after process exit (short grace period).

Port-forward (SPDY):
- Handle `SPDY/3.1+portforward.k8s.io`.
- Parse stream headers: `port` / `streamtype` / `streamname`.
- Ensure data/error stream pairing is stable.

WebSocket (Browser):
- Exec uses `v5.channel.k8s.io` subprotocol with 1-byte stream id prefix.
- Port-forward uses `portforward.k8s.io` with channel prefix frames.

---

## Security and Gating Options (for review)

We should agree on how to gate exec/port-forward across dashboard, playground, and `ae` CLI.

### Option 1: Single token, per-role
- Use apishim bearer token for SPDY + WS.
- Role gating:
  - `read`: view only
  - `exec`: interactive exec/attach
  - `portforward`: port-forward only
  - `admin`: full mutations
- Pros: simple, consistent.
- Cons: requires role expansion in token handling.

### Option 2: Separate exec/port-forward token
- Distinct token for interactive features.
- Pros: limits blast radius if UI token leaks.
- Cons: more UX friction.

### Option 3: Controller proxy
- Dashboard talks to controller; controller proxies to apishim with its own token.
- Pros: no token handling in browser.
- Cons: more server complexity, must prevent CSRF.

### Dashboard token vs controller proxy (detail)

**Embed apishim token in Dashboard**
- Pros: simplest data path; fewer hops; apishim can stream directly.
- Cons: token is visible to browser and JS; higher leak risk.
- Best practices:
  - Use short-lived, scoped tokens (`exec`/`portforward` only).
  - Bind tokens to origin + app scope and enforce TTL (minutes).
  - Prefer one-time session tokens minted by controller.
  - Add audit logs for every exec/port-forward session.

**Proxy via controller**
- Pros: no long-lived token in browser; centralized authz/audit.
- Cons: controller must handle long-lived WS/SPDY streams; higher load.
- Best practices:
  - Require CSRF tokens and origin allowlist.
  - Enforce per-session limits and timeouts.
  - Consider streaming passthrough to apishim using a signed, one-shot token.
  - Log both client identity and backend target (pod/app/port).

Decision: for production dashboards, prefer controller-minted short-lived
session tokens or controller proxying.

### Additional gating controls (baseline)
- Per-app allowlist: `AE_API_EXEC_SCOPE`, `AE_API_PF_SCOPE`.
- Time-bound session tokens for UI.
- Origin allowlist + CSRF token for browser flows.
- Audit logging for every exec/port-forward session.

---

## Security Model Default (RBAC + Tokens)

Decision: single token + RBAC on subresources with RBAC verbs mapped to
`pods/exec`, `pods/attach`, `pods/portforward`, and `pods/log`.
Best practices: keep exec/port-forward in distinct roles and default-deny.

## Labs/Demo Token Handling (Browser)

Decision: dashboards use controller-minted session tokens or controller proxying.
Embedded browser tokens are not the default and require explicit opt-in.

## WebSocket Exec Protocol Versions

Decision: accept `v5.channel.k8s.io` plus v4/v3/v2/v1 with negotiation.
Best practice: implement v5 first, then add older protocols with strict mapping
tests.

## Dashboard Port-Forward UX + HTTP Proxy

Decision: no default in-browser HTTP proxy; use WS port-forward or show CLI
instructions. Any proxy must be explicitly enabled with strict allowlists,
auth, and short-lived URLs.

## Playground Gating Rules

Decision: `AE_LABS=1` + Labs token + read-only off.
Best practice: read-only remains the default unless explicitly disabled.

## CLI Surface Alignment with kubectl

Decision: kubectl-aligned flags for `ae exec` with a `ae shell` wrapper.
- `ae exec <app> -- <cmd>` with `-i/--stdin`, `-t/--tty`, `-c/--container`.
- `ae shell <app>` as a wrapper with auto-tty + resize.

---

## Implementation Order (Option A)

1) apishim: WebSocket exec support (v5 protocol).
2) apishim: tighten RBAC for exec/port-forward + audit logs.
3) Dashboard: shell modal + port-forward UI.
4) Playground: Debug Tools gated by labs token.
5) `ae` CLI: SPDY exec implementation and tests.
6) Compatibility test matrix: kubectl + k9s on docker/podman.

---

## Decisions

- `ae` CLI supports WebSocket exec fallback (guarded by flag/env).
- Introduce separate roles for `exec` and `portforward` (not admin-only).
- apishim remains the primary compatibility track; kube-apiserver/Kine is deferred
  while we align API semantics (see `docs/wip/conformance.md`).

### Default TTY behavior (detail)

**Option: always-on TTY for `ae shell`**
- Pros: best interactive UX; matches `kubectl exec -it` muscle memory.
- Cons: merges stderr into stdout, less script-friendly; broken piping.

**Option: auto-detect**
- If stdin is a TTY, default `--tty` and `--stdin` on.
- If stdin is not a TTY, default `--tty` off (safe for pipes).
- Best practice: `ae shell` uses auto-detect + `--tty/--no-tty` overrides.

**Decision**
- `ae shell`: auto-detect (tty on when interactive) and set `TERM`, enable resize.
- `ae exec`: default `--tty` off; user opts in with `-t` for interactive needs.

---

## API Shim Integration Level (Stack)

Decision: apishim becomes a first-class stack service; source-of-truth is
explicit and double-writers are avoided.

## Kine (k3s datastore shim) and API Shim Path

Kine is an etcd datastore shim that backs a real kube-apiserver with SQL/NATS.
It does not provide API compatibility on its own.

Decision: keep building apishim as the primary path. A kube-apiserver + Kine
track remains an optional future path for full conformance.

## Conformance Alignment Dependencies (Exec/Port-Forward)

Goal: make exec/port-forward behavior align with the conformance-lite semantics
track while apishim remains primary.

- **RBAC subresources**: enforce `pods/exec`, `pods/attach`, `pods/portforward`,
  and `pods/log` verbs with SubjectAccessReview parity and audit records.
- **Pod identity stability**: require pod UID/phase checks before opening
  sessions; close/deny on stale UIDs to match kube-apiserver expectations.
- **Exit/status semantics**: v4+ error-stream JSON exit codes; proper close
  sequencing across stdout/stderr/error streams under churn.
- **Watch consistency**: exec/port-forward should rely on pod status derived
  from list+watch-consistent views (avoid stale cache lookups).
- **Streaming backpressure**: define timeouts, idle close, and max session
  duration to avoid stuck streams and to align with audit expectations.

---

## Implementation Update (2026-01-25)

- **Controller-minted session tokens**: `POST /api/apishim/session` signs short-lived tokens
  for exec/port-forward using `AE_APISHIM_SESSION_SECRET` with TTL controls
  (`AE_APISHIM_SESSION_TTL`, `AE_APISHIM_SESSION_TTL_MAX`).
- **Scope gating**: apishim enforces `AE_API_EXEC_SCOPE` / `AE_API_PF_SCOPE` patterns, and
  respects token-provided scopes when present (both must match).
- **Pod validation**: exec/port-forward checks require a running pod and deny stale/absent
  pods before opening streams (optional controller-state check via `AE_APISHIM_POD_STATE_CHECK`).
- **Pod UID projection + gating**: runtime container UID now surfaces as `metadata.uid` on Pods;
  exec/port-forward enforce `uid`/`podUID` query parameters when supplied (409 on mismatch).
- **Watch cache gating (optional)**: `AE_APISHIM_POD_WATCH_CHECK=1` requires pods to be
  observed via list/watch before exec/port-forward; TTL controlled by
  `AE_APISHIM_POD_WATCH_TTL_SECONDS`.
- **RV pinning (optional)**: exec/port-forward honor `resourceVersion`/`podRV` query params
  when present and require a matching watch-cache entry.
- **Stream limits**: idle + max session timeouts added via `AE_APISHIM_STREAM_IDLE_SECONDS`
  and `AE_APISHIM_STREAM_MAX_SECONDS`, plus total byte caps via `AE_APISHIM_STREAM_MAX_BYTES`.
- **Compatibility smoke**: added `scripts/dev/apishim_spdy_matrix.sh` and a runner
  (`scripts/dev/apishim_spdy_matrix_run.sh`) to spin up apishim with TLS + docker/podman.
- **k9s automation**: added `scripts/dev/apishim_k9s_smoke.sh` and CI matrix
  (`.github/workflows/apishim-spdy-matrix.yml`) to cover docker + podman (podman allow-fail).

---

## Gap Status (2026-01-25)

- **Compatibility matrix**: kubectl exec/port-forward covered by `scripts/dev/apishim_spdy_matrix.sh`
  with a TLS runner; k9s coverage is automated via `scripts/dev/apishim_k9s_smoke.sh`, and
  podman is validated in the matrix (currently allow-fail).
- **Pod identity stability**: Pod `metadata.uid` is now surfaced and enforced when clients provide
  `podUID`/`uid`; phase/rv verification still lacks kube‑apiserver parity.
- **Watch consistency**: optional watch-cache gating exists, but exec/port-forward still
  rely on direct runtime lookups and do not provide full list+watch consistency semantics.
- **Streaming backpressure**: idle/max session limits plus total byte caps are in place, but no
  explicit backpressure/queue semantics or flow control tuning beyond timeouts.
