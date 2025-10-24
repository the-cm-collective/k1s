# HTTP API Reference

The controller exposes an HTTP API when started with `--metrics-port PORT`.
By default, endpoints are read-only. Mutating endpoints can be enabled for dev/testing.

Base URL: `http://<host>:<port>`

- GET `/metrics`
  - Content-Type: `text/plain; version=0.0.4`
  - Prometheus text format gauges:
    - `ae_apps_total`, `ae_apps_ready`, `ae_apps_progressing`, `ae_apps_degraded`
    - `ae_replicas_total`, `ae_replicas_ready`, `ae_replicas_live`

- GET `/status`
  - Content-Type: `application/json`
  - 200 OK: JSON array of app status objects
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

- GET `/events/<app>?limit=N`
  - 200 OK: JSON array of recent events for `<app>`
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

Notes
- This API is read-only by design; mutating operations happen via the CLI.
- The API shares the controller’s SQLite store; results are eventually consistent with reconcile intervals.

Extras
- GET `/openapi.json` — Minimal OpenAPI 3 document for the read-only endpoints.
- GET `/docs` — Lightweight HTML that lists available paths by fetching `/openapi.json`.
Mutations (opt-in; dev only)

- Enable by setting env `AE_API_MUTATIONS=1` on the controller process. Optionally set `AE_API_TOKEN` and send `Authorization: Bearer <token>` on requests.

- POST `/scale/<app>`
  - Body: `{ "replicas": <int> }`
  - 200 OK: `{ app, replicas, revision, status, created, updated, removed }`

- POST `/delete/<app>?purge=1`
  - 200 OK: `{ app, removed, purged }`
  - Deletes runtime containers and ingress; `purge=1` also deletes events and revisions for the app.
