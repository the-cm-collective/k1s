## Observability

### Metrics

Prometheus-style text at `/metrics` includes aggregated gauges and labeled series:

- Aggregated:
  - `ae_apps_total`, `ae_apps_ready`, `ae_apps_progressing`, `ae_apps_degraded`
  - `ae_replicas_total`, `ae_replicas_ready`, `ae_replicas_live`
- Per-app:
  - `ae_app_desired_replicas{app="<name>"}`
  - `ae_app_ready_replicas{app="<name>"}`
  - `ae_app_live_replicas{app="<name>"}`
- Per-replica:
  - `ae_replica_ready{app="<name>",replica="<id>"}` 0/1

### Logs Endpoint

Tail logs over HTTP (READ role if tokens configured):

```
GET /logs/<app>?container=<id|idx>&tail=N&since=<seconds>&follow=1
```

- When `follow=1`, the response streams plain text lines.
- Without follow, the response is JSON: `{ "lines": [ ... ] }`.

Remote CLI example:

```
ae --server http://<ip>:9108 --token readtoken logs echo --tail 100
```

See `docs/runbook.md` for the “Remote CLI over LAN” section and auth token setup.


### Grafana Examples

- Import the example dashboards/panels using a Prometheus data source (uid `PROM` in examples):
  - `docs/grafana/controller-health.json` — stat + timeseries for controller health
  - `docs/grafana/apps-overview.json` — per-app readiness time series and status table

You can also embed a single panel JSON (e.g., stat showing ready apps):

```
{
  "type": "stat",
  "title": "Ready Apps",
  "datasource": { "type": "prometheus", "uid": "PROM" },
  "targets": [ { "expr": "ae_apps_ready", "refId": "A" } ],
  "options": {
    "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
    "colorMode": "value",
    "graphMode": "none"
  },
  "gridPos": { "h": 4, "w": 6, "x": 0, "y": 0 }
}
```

### Demo Dashboard (/dashboard)

- Adds a System snapshot sourced from `GET /system`:
  - Controller: last reconcile timestamp and duration
  - Ingress: configured site blocks and existence flag
  - Services: declared `service.port`/`targetPort` per app
  - Storage: container‑engine named volumes created for apps (PV‑lite)
  - RBAC: shows whether mutations are enabled and tokens are configured (never reveals secrets)

- Per‑app details now include:
  - Service mapping, count of secret refs, and declared storage volumes (from the manifest)

### System API

`GET /system` returns a JSON object combining controller stats and optional runtime/ingress snapshots, for example:

```
{
  "controller": { "last_reconcile_timestamp": 1698000000.0, "last_reconcile_duration": 0.245, "apps": { "echo": {"reconciles": 12, "duration_sum": 2.3, "ops": {"created": 1}}}},
  "rbac": { "mutations_enabled": false, "read_token_configured": false, "scaler_token_configured": false, "admin_token_configured": false },
  "ingress": { "dirty": false, "sites": [{"app": "echo", "host": "echo.local", "path": "/path/to/echo.caddy", "exists": true}] },
  "services": [{ "app": "echo", "port": 8080, "target_port": 8080, "replicas": 1 }],
  "volumes": [{ "name": "ae-echo-data", "labels": {"ae.app": "echo"}, "driver": "local", "mountpoint": "/var/lib/docker/volumes/..." }]
}
```
