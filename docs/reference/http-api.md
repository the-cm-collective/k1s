# HTTP API Reference

The controller exposes an HTTP API when started with `--metrics-port PORT`.
By default, endpoints are read-only. Mutating endpoints can be enabled for dev/testing.

Base URL: `http://<host>:<port>`

- GET `/metrics`
  - Content-Type: `text/plain; version=0.0.4`
  - Prometheus text format gauges:
    - Apps/replicas: `ae_apps_total`, `ae_apps_ready`, `ae_apps_progressing`, `ae_apps_degraded`, `ae_replicas_total`, `ae_replicas_ready`, `ae_replicas_live`
    - Nodes: `ae_nodes_total`, `ae_nodes_ready`, `ae_nodes_stale`
    - Services: `ae_services_total` plus per-service labels (ClusterIP, provider)
    - Shim/backends: `apishim_*` metrics (watchers, queue depth, backend) when the API shim is enabled

- GET `/status`
  - Content-Type: `application/json`
  - 200 OK: `{ "items": [ ... ], "next": "cursor" | null }`
  - Query params: `limit`, `cursor`, `app`, `wildcard`
  - Example item:
    ```json
    {
      "app_name": "echo",
      "desired_replicas": 1,
      "ready_replicas": 1,
      "live_replicas": 1,
      "revision": 3,
      "revision_status": "ready",
      "image": "alpine:3.20",
      "ingress_host": "echo.localtest.me",
      "ingress_path": "/"
    }
    ```

- GET `/status/<app>`
  - 200 OK: JSON object (same shape as items in `/status`)
  - 404 Not Found: when no status exists

- GET `/nodes`
  - 200 OK: `{ "nodes": [ { node_id, name, backend, endpoint, labels, taints, pod_cidr, cordoned, status, seen_at, stale }, ... ], "count": N, "stale_after_seconds": 40 }`
  - Requires READ token when auth is configured.

- GET `/events/<app>?limit=N`
  - 200 OK: JSON array of recent events for `<app>` (limit only; cursor is not implemented)
  - Example item:
    ```json
    {
      "app_name": "echo",
      "revision": 3,
      "event_type": "ApplyCompleted",
      "message": "Revision 3 status ready",
      "created_at": "2025-10-23T12:34:56+00:00"
    }
    ```

- GET `/system` and `/dashboard`
  - Aggregate snapshot for the dashboard UI including nodes, services, storage volumes, and token/mutation flags.

Notes
- Read-only by default; mutations require `AE_API_MUTATIONS=1` plus tokens.
- `/plan` is a read-only POST endpoint used by the dashboard/labs UI; `/k8s/preview` requires `AE_API_DEV_EXPORT=1`.
- The API shares the controller’s SQLite/Postgres store; results are eventually consistent with reconcile intervals.
- When any token is set, all GETs require at least the READ token.

Extras
- GET `/openapi.json` — OpenAPI 3 document for controller endpoints.
- GET `/docs` — Lightweight HTML that lists available paths by fetching `/openapi.json`.
- When apishim is enabled, `/openapi/v3` and `/openapi/v2` expose the Kubernetes shim spec.

Mutations (opt-in; dev only)

- Enable by setting `AE_API_MUTATIONS=1` on the controller process.
- Mutations require bearer tokens when auth is configured: `AE_API_SCALER_TOKEN` (scale) and `AE_API_ADMIN_TOKEN` (admin). Send `Authorization: Bearer <token>`.

- POST `/scale/<app>`
  - Body: `{ "replicas": <int> }`
  - 200 OK: `{ app, replicas, revision, status, created, updated, removed }`

- POST `/delete/<app>?purge=1`
  - 200 OK: `{ app, removed, purged }`
  - Deletes runtime containers and ingress; `purge=1` also deletes events and revisions for the app.

- POST `/rollout/pause/<app>` and `/rollout/resume/<app>` (admin)
- POST `/apply` (admin; JSON App manifest)
- POST `/exec/<app>` (admin; remote exec payload)


AuthN/AuthZ (optional)

- Set tokens in the controller environment to enable role-based access:
  - `AE_API_READ_TOKEN` — read-only access
  - `AE_API_SCALER_TOKEN` — can scale
  - `AE_API_ADMIN_TOKEN` — can delete
- When any token is configured, GETs require at least the READ token.
- Mutations require tokens and `AE_API_MUTATIONS=1`.
- Optional scoping: `AE_API_ADMIN_SCOPE` / `AE_API_SCALER_SCOPE` / `AE_API_READ_SCOPE` (comma‑separated glob patterns).
- Optional expiry: `AE_API_*_TOKEN_EXPIRES` (UTC ISO8601).

Endpoints
- GET `/health` — Controller health summary (uptime, last reconcile, app/replica counts)
- GET `/metrics` — Prometheus metrics
- GET `/status` — Paginated list of app statuses
  - Query: `limit`, `cursor`, `app`, `wildcard` (glob)
  - Response: `{ items: [...], next: "cursor" | null }`
- GET `/status/<app>` — JSON status object
- GET `/manifest/<app>` — Latest stored manifest for an app
- GET `/events/<app>?limit=N` — Recent events array
- GET `/history/<app>?limit=N` — Probe history (readiness/liveness)
- GET `/nodes` — Node inventory and staleness info
- GET `/system` and `/dashboard` — Dashboard snapshot
- POST `/plan` — Read-only planner (requires READ token when configured)
- POST `/scale/<app>` — Body: `{ "replicas": <int> }` (requires scaler/admin)
- POST `/delete/<app>?purge=1` — Delete app (requires admin)
- POST `/rollout/pause/<app>` and `/rollout/resume/<app>` — Pause/resume rollouts (admin)
- Logs: GET `/logs/<app>?container=&tail=&since=&follow=` (requires READ when tokens are configured)
  - When `follow=1`, streams plain text lines; otherwise JSON: `{ lines: [...] }`
  - SSE stream: GET `/logs/<app>/stream`
