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

## Multi-node Smoke (Phase 7)
- Bring up the controller with overlay + agent API (`ops/dev/multinode-lab.sh -h` shows the env needed).
- Start at least one worker agent with `--ensure-pod-net` and the matching `AE_AGENT_TOKEN`.
- Apply the multi-node sample:
  ```
  export AE_STATE_DB=/home/ae/state/controller.db  # when running inside the controller VM
  python3 -m ae.cli apply -f specs/examples/echo-multinode.yaml
  python3 -m ae.cli nodes list
  python3 -m ae.cli status echo-mn --watch 5 --timeout 120
```
- Verify Service VIP routing: `python3 -m ae.cli services --json | jq -r '.[0].cluster_ip'` then
  `curl http://<cluster-ip>:8080/healthz` from the controller host.
- Quick regression: `pytest tests/integration/test_multinode_agent_flow.py`.
  - Service VIP sanity: `pytest tests/integration/test_service_vip_routing.py` (requires docker-enabled env).
- QEMU helper shortcut: `RUN_SMOKE=1 AE_TOKEN=dev-token ops/ci/multinode-qemu.sh start` runs apply → watch → VIP curl → kill worker1 agent → re-watch → VIP curl, then leaves the lab up for inspection.

## K8s Export Smoke

Validate the exporter renders portable YAML and passes basic validation without a cluster.

```
# Basic echo export (Deployment, Service, Ingress)
python -m ae.cli export-k8s -f specs/examples/echo.yaml \
  --namespace demo --ingress-class traefik --validate > /tmp/echo-k8s.yaml

# envFrom + projected volumes; emit ConfigMap/Secret objects
python -m ae.cli export-k8s -f specs/examples/envfrom-and-projection.yaml \
  --namespace demo --emit-configs --emit-secrets --validate > /tmp/envfrom-k8s.yaml

# Portability check (strict)
python -m ae.cli k8s-check -f specs/examples/echo.yaml --policy strict
```
