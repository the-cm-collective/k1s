Operations Runbook

Purpose
- This runbook captures common operational tasks for the k1s controller: exporting manifests to K8s, validating portability, managing ingress/TLS, rollouts, and API tokens.

Setup
- Install Python deps: `python -m pip install -e .[dev]`
- Dev services (optional): `docker compose -f ops/dev/docker-compose.yaml up -d`
- Controller loop (dev): `python -m ae.controller --loop --interval 5 --specs specs/ --metrics-port 9108`

Export and Validate K8s YAML
- Hardened export with validation:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o specs/examples/echo-k8s.yaml`
- Multi‑replica hardened export:
  - `python -m ae.cli export-k8s -f specs/examples/multi-replica-echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o specs/examples/multi-replica-echo-k8s.yaml`
- CI runs server‑side `kubectl apply --dry-run=server` and `kubeconform -strict` on exported samples.

Ingress and TLS
- Overview: see docs/ingress.md for multi‑path routing and TLS options.
- TLS sync helper:
  - Render PEMs from a Kubernetes Secret file: `python -m ae.cli tls sync --name mycert --input path/to/mycert.yaml --root state/tls`
  - Or place direct PEMs `state/tls/mycert.crt` and `state/tls/mycert.key`.
  - Set only `spec.ingress.tlsSecretName: mycert` in your App manifest; the controller will resolve and wire cert/key for Caddy.
- Environment:
  - AE_TLS_DIR (default: state/tls)
  - AE_CADDY_SITES, AE_CADDY_BIN, AE_CADDY_FILE, AE_CADDY_CONTAINER, AE_CONTAINER_CLI, AE_CADDY_RELOAD_TIMEOUT

Rollouts
- Pause/resume:
  - `python -m ae.cli rollout pause <app>`
  - `python -m ae.cli rollout resume <app>`
- Canary (static weight): set `spec.rollout.strategy: canary` and `spec.rollout.weight` to bias new upstreams.
- Canary (auto), controller‑persisted:
  - `spec.rollout.auto: { start: 1, step: 2, intervalSeconds: 60, max: 10 }`
  - Controller stores weight/next step in SQLite and increments on schedule.

Portability Checks (k8s‑check)
- Baseline: `python -m ae.cli k8s-check -f specs/examples/echo.yaml`
- Strict policy: `python -m ae.cli k8s-check -f specs/examples/echo.yaml --policy strict`
- HPA assumptions (optional validation hints):
  - `--assume-hpa cpu-util`: require CPU requests for utilization metrics
  - `--assume-hpa mem-util`: require Memory requests for utilization metrics
  - `--assume-hpa mem-value=200Mi`: validate AverageValue quantity format

API tokens
- Generate or rotate tokens:
  - `python -m ae.cli api tokens --generate` (use `--rotate` interchangeably)
  - Optional: `-o .env.api` writes export lines to a file.
  - Optional expiry (global or per-role): add `--ttl-hours 24` to emit `AE_API_*_TOKEN_EXPIRES` lines (UTC ISO8601). You can override per role with `--ttl-admin-hours`, `--ttl-scaler-hours`, `--ttl-read-hours`.
- Optional JSON state: add `--state state/api_tokens.json` to write a JSON file with tokens and expiries for rotation tooling.
- Required env when running the HTTP API with mutations:
  - `AE_API_MUTATIONS=1`, `AE_API_ADMIN_TOKEN=...`, `AE_API_SCALER_TOKEN=...`, `AE_API_READ_TOKEN=...`
 - Optional per-role scopes (mutations):
   - `AE_API_ADMIN_SCOPE` and `AE_API_SCALER_SCOPE` accept comma-separated glob patterns restricting which apps a token can mutate. Examples:
     - `AE_API_ADMIN_SCOPE="payments-*"` (admin token can only mutate apps prefixed payments-)
     - `AE_API_SCALER_SCOPE="echo,web-*"` (scaler token can scale only echo and web-* apps)

Observability
- Controller dashboard/API: `http://127.0.0.1:9108` when `--metrics-port` is set.
- Prometheus metrics at `/metrics` (text), recent events via `/events/<app>`.

Tips
- Prefer non‑root containers and set readOnlyRootFilesystem where possible.
- For utilization HPAs, set CPU/Memory requests to avoid HPA errors.
- Use `--policy strict` in CI to keep manifests honest.


Token rotation and cleanup
- Rotate tokens proactively before expiry; consider 24h pre‑expiry for rotation.
- After rotating, remove old `AE_API_*_TOKEN` and `AE_API_*_TOKEN_EXPIRES` values from your environment/secret store.
- HTTP API exports token expiry metrics so you can alert:
  - `ae_api_token_expiry_seconds{role="admin|scaler|read"}` (negative when expired).


Prometheus alert example
```
groups:
- name: ae-tokens
  rules:
  - alert: AETokenExpiringSoon
    expr: ae_api_token_expiry_seconds < 12 * 3600
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "AE API token for {{ $labels.role }} expires soon"
      description: "Token for role {{ $labels.role }} expires in {{ humanizeDuration $value }}"
  - alert: AETokenExpired
    expr: ae_api_token_expiry_seconds < 0
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "AE API token for {{ $labels.role }} expired"
      description: "Token for role {{ $labels.role }} expired {{ humanizeDuration (0 - $value) }} ago"
```


HPA exporter examples
- CPU utilization only:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --hpa-min 1 --hpa-max 3 --hpa-cpu-target 70`
- Memory AverageValue (requires a sensible quantity like 200Mi):
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --hpa-min 1 --hpa-max 3 --hpa-mem-type value --hpa-mem-value 200Mi`
