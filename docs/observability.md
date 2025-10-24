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
