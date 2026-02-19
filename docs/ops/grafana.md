# Grafana: AE Controller Dashboard

This folder includes `ae_dashboard.json`, a minimal Grafana dashboard seeded with:
- Apps/replicas ready stats
- Per-app reconcile duration (avg via sum/count)
- Canary weight per app
- Container restart counts
- Transport stats (sites seen/stale, outbox publish OK/fail, JS stream/consumer gauges, gateway NAK/stale, route bundle apply)

Steps
1) Ensure Prometheus scrapes the controller (port 9108 by default):

```
- job_name: 'ae-controller'
  static_configs:
    - targets: ['<host>:9108']
```

2) Import the dashboard:
- Grafana → Dashboards → Import → Upload `docs/grafana/ae_dashboard.json`.

3) Optional variable
- Add a templating variable for app names (`label_values(ae_app_status, app)`) to filter panels.

Notes
- The controller exposes metrics even without ingress. If you run it in Docker, publish `-p 9108:9108`.
- Canary metrics appear when canary strategy with auto progression is active.


One-liner demo stack (controller + Prometheus + Grafana)
```
make docker-build-controller && docker compose -f ops/dev/docker-compose.grafana.yml up -d
```
Then visit http://localhost:3000 (admin/admin), add the dashboard JSON, and browse metrics.


Pre-provisioned dashboards
- The compose stack auto-provisions Prometheus as a datasource.
- Dashboards can be pre-loaded by placing JSON files under `/var/lib/grafana/dashboards`.
- The compose file maps `docs/grafana/ae_dashboard.json` there; you can add `ae_rollout_ops.json` similarly:
  - `- ../../docs/grafana/ae_rollout_ops.json:/var/lib/grafana/dashboards/ae_rollout_ops.json:ro`
