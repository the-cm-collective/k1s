## Observability

### Metrics

Prometheus-style text at `/metrics` includes aggregated gauges and labeled series:

- Aggregated:
  - `ae_apps_total`, `ae_apps_ready`, `ae_apps_progressing`, `ae_apps_degraded`
  - `ae_replicas_total`, `ae_replicas_ready`, `ae_replicas_live`
- Nodes/Services:
  - `ae_nodes_total`, `ae_nodes_ready`, `ae_nodes_stale`
  - `ae_services_total` plus per-service `cluster_ip`, `provider`, and port labels
- Per-app:
  - `ae_app_desired_replicas{app="<name>"}`
  - `ae_app_ready_replicas{app="<name>"}`
  - `ae_app_live_replicas{app="<name>"}`
- Per-pod:
  - `ae_pod_ready{app="<name>",pod="<id>"}` 0/1
  - Alias (deprecated): `ae_replica_ready{app="<name>",replica="<id>"}` 0/1
- Probe backoff:
  - `ae_pod_probe_backoff_seconds{app="<name>",pod="<id>",type="<readiness|liveness|startup>"}` (alias: `ae_probe_backoff_seconds{...replica=...}`)

API shim (when enabled)
- `apishim_watchers{group,version,resource,namespace}` — active watch streams
- `apishim_watch_queue_depth{...}` — queue depth per watch key
- `apishim_watch_events_enqueued_total{...}` / `_dropped_total{...}` — backpressure counters
- `apishim_watch_broadcasts_total{...}` — publish attempts per watch key
- `apishim_store_backend_info{backend="sqlite|postgres"}` — storage backend in use
- For HA shims on Postgres, the same metrics surface on each instance (shared store, per-process queues).

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

See `docs/ops/runbook.md` for the “Remote CLI over LAN” section and auth token setup.


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

### Hive Dashboard (/dashboard)

- Adds a System snapshot sourced from `GET /system`:
  - Controller: last reconcile timestamp and duration
  - Nodes: Ready/stale counts plus table of node status/cordon
  - Ingress: configured site blocks and existence flag
  - Services: declared `service.port`/`targetPort` per app and VIP/provider info
  - Storage: container‑engine named volumes created for apps (PV‑lite)
  - RBAC: shows whether mutations are enabled and tokens are configured (never reveals secrets)
- Adds an `HA Control Plane` section sourced from `GET /system.ha`:
  - Authority: local role, visible leader, advertise address, controller epoch, and controller-member freshness from `ha.authority.members`
  - Etcd: configured endpoints, maintenance counters, and optional cached probe health when enabled
  - Transport: JetStream pressure, replay backlog, route acknowledgement age, HA fence activity, and optional hub/edge monitor summaries
  - Edge sites: per-site freshness, replay backlog, route pending, ack age, and gateway build/last-seen rows
  - Issue banner: normalized HA/operator warnings from `system.ha.issues`
- The built-in dashboard stays on `GET /system`; it does not parse `/metrics` in the browser.

- Per‑app details now include:
  - Service mapping, count of secret refs, and declared storage volumes (from the manifest)

### System API

`GET /system` returns a JSON object combining controller stats and optional runtime/ingress snapshots, for example:

```
{
  "controller": { "last_reconcile_timestamp": 1698000000.0, "last_reconcile_duration": 0.245, "apps": { "echo": {"reconciles": 12, "duration_sum": 2.3, "ops": {"created": 1}}}},
  "rbac": { "mutations_enabled": false, "read_token_configured": false, "scaler_token_configured": false, "admin_token_configured": false },
  "ha": {
    "enabled": true,
    "authority": {
      "healthy": true,
      "is_leader": false,
      "controller_id": "core-a",
      "leader_id": "core-b",
      "leader_advertise_addr": "https://core-b.example.net:9108",
      "controller_epoch": 19,
      "member_count": 3,
      "members": [
        { "controller_id": "core-b", "advertise_addr": "https://core-b.example.net:9108", "version": "0.1.3.dev0", "is_leader": true, "is_local": false, "role": "leader", "last_heartbeat_at": "2026-03-24T19:20:05+00:00", "last_heartbeat_age_s": 4.0, "freshness": "fresh", "stale_after_seconds": 10.0 },
        { "controller_id": "core-a", "advertise_addr": "https://core-a.example.net:9108", "version": "0.1.3.dev0", "is_leader": false, "is_local": true, "role": "standby", "last_heartbeat_at": "2026-03-24T19:19:48+00:00", "last_heartbeat_age_s": 21.0, "freshness": "stale", "stale_after_seconds": 10.0 },
        { "controller_id": "core-c", "advertise_addr": "https://core-c.example.net:9108", "version": "0.1.2", "is_leader": false, "is_local": false, "role": "standby", "last_heartbeat_at": null, "last_heartbeat_age_s": null, "freshness": "unknown", "stale_after_seconds": 10.0 }
      ]
    },
    "controller_build": { "version": "0.1.3.dev0", "sha": "abc123", "date": "2026-03-19" },
    "etcd": { "configured_endpoints": ["http://10.0.0.11:2379", "http://10.0.0.12:2379", "http://10.0.0.13:2379"], "maintenance_runs_total": 0.0, "maintenance_triggered_total": 0.0, "healthy_endpoints": 3, "unhealthy_endpoints": 0, "members": [], "last_probe_ts": 1710800000.0, "probes_enabled": true },
    "transport": { "backend": "nats-js", "js_domain": "K1S", "site_summary": { "seen": 2, "stale": 0, "fresh": 2, "last_seen_age_s": 4.2 }, "sites": [], "jetstream": { "stream_count": 1, "consumer_count": 2, "pending": 0.0, "ack_pending": 1.0, "redelivered": 0.0, "waiting": 0.0, "consumers": [], "streams": [] }, "gateway": { "site_count": 2, "result_replay_backlog": 0.0, "sites": [] }, "routes": { "site_count": 2, "pending_sites": 0.0, "max_ack_age_s": 0.0, "sites": [] }, "fence": { "surface_count": 2, "stale_total": 0.0, "duplicate_total": 0.0, "epoch_advance_total": 0.0, "surfaces": [] } },
    "hpa": { "reconcile_total": 0.0, "scale_total": 0.0, "metrics_stale_total": 0.0, "metrics_missing_total": 0.0, "snapshot_age_seconds": 0.0 },
    "issues": []
  },
  "ingress": { "dirty": false, "sites": [{"app": "echo", "host": "echo.local", "path": "/path/to/echo.caddy", "exists": true}] },
  "services": [{ "app": "echo", "port": 8080, "target_port": 8080, "replicas": 1 }],
  "volumes": [{ "name": "ae-echo-data", "labels": {"ae.app": "echo"}, "driver": "local", "mountpoint": "/var/lib/docker/volumes/..." }]
}
```

`system.ha` is the stable dashboard contract for live HA operator state. It includes:

- `authority`: HA role and leader visibility derived from controller authority state, plus additive `members[]` freshness for visible controllers
- `controller_build`: controller build metadata already exported at `/__ae/version` and `/metrics`
- `etcd`: configured endpoints, maintenance counters, and optional cached probe/member results
- `transport`: outbox, JetStream, gateway replay, route convergence, HA fence, and per-site joined rows for dashboard rendering
- `hpa`: shared-metrics HPA reconcile/scale quality counters
- `issues`: UI-facing warnings/errors summarized from the current live snapshot

`system.ha.authority.members[]` includes:

- `controller_id`, `advertise_addr`, `version`, `is_leader`, `is_local`, and `role`
- `last_heartbeat_at`: additive heartbeat timestamp when the controller publishes freshness-capable presence
- `last_heartbeat_age_s`: additive age derived from `last_heartbeat_at`
- `freshness`: one of `fresh`, `stale`, or `unknown`
- `stale_after_seconds`: the current freshness threshold derived from HA keepalive and lease settings
