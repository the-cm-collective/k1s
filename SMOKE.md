## Smoke Test Process

This checklist validates the current k1s demo stack end-to-end.

Prereqs
- Docker Engine running.
- Python 3.11+ available.
- Fresh baseline (optional):
  - `./scripts/init_demo.sh --down -y`

1) Start standard demo with debug
```
./scripts/init_demo.sh -y -d
```
Expect dev-caddy-1 and dev-prometheus-1 to start; controller supervisor logs show
`http api listening on port 9108`. Sanity checks report OK.

2) Verify endpoints via Caddy
```
curl -k https://blue.home.arpa:8443/
curl -k https://green.home.arpa:8443/
curl -k https://docs.home.arpa:8443/
curl -k https://api.home.arpa:8443/swagger
```

3) Remote CLI (read-only)
```
ae --server http://127.0.0.1:9108 status
ae --server http://127.0.0.1:9108 events echo --limit 10
```
Optionally set READ token on the controller box and pass `--token`.

4) Health & Metrics
```
curl http://127.0.0.1:9108/health
curl http://127.0.0.1:9108/metrics | grep ae_app_desired_replicas
```
Expect both aggregated and labeled metrics.

5) Demo modes (pick any)
- Multi-Replica Echo:
  - `make demo ARGS="--demo-echo-mr -y -d"`
  - `curl -k https://echo-mr.home.arpa:8443/`
- Configs & Secrets:
  - `make demo` (defaults to `-y --demo-configs`)
  - `ae config validate -f configs/app-config.yaml`
- Rollout (ordered):
  - `make demo ARGS="--demo-rollout -y -d"`
- Storage (PV-lite):
  - `make demo ARGS="--demo-storage -y -d"`
  - `ae volumes list --app echo --json`

6) Planner checks
```
ae plan -f specs/examples/echo.yaml
ae plan -f specs/examples/echo.yaml --verbose
ae plan -f specs/examples/echo.yaml --strict
```
Strict mode exits nonzero if warnings are present.

7) Remote logs
```
ae --server http://127.0.0.1:9108 logs echo --tail 20
```
Try `--follow` to stream.

8) Grafana (optional)
- Add Prometheus scrape for `127.0.0.1:9108`.
- Import dashboards:
  - `docs/grafana/controller-health.json`
  - `docs/grafana/apps-overview.json`

Troubleshooting
- Swagger auth: set `AE_API_READ_TOKEN/SCALER_TOKEN/ADMIN_TOKEN` and `AE_API_MUTATIONS=1` on the controller host.
- Caddy reload: check controller logs; you can also run
  `docker exec dev-caddy-1 caddy reload --config /etc/caddy/Caddyfile`.
