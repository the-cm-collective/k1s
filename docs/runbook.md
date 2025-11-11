Operations Runbook

Purpose
- This runbook captures common operational tasks for the k1s controller: exporting manifests to K8s, validating portability, managing ingress/TLS, rollouts, and API tokens.

Setup
- Install Python deps: `python -m pip install -e .[dev]`
- Dev services (optional): `docker compose -f ops/dev/docker-compose.yaml up -d`
- Controller loop (dev):
  - Default specs dir: `python -m ae.controller --loop --interval 5 --specs specs/ --metrics-port 9108`
  - Curated demo/specs: set `AE_SPECS_DIR` and use Make targets that respect it:
    - `AE_SPECS_DIR=state/demo-specs make loop` (watches only the curated set)
    - `AE_SPECS_DIR=state/demo-specs make run` (single reconcile pass)
- Tip: `scripts/init_demo.sh` seeds `state/demo-specs` and exports `AE_SPECS_DIR` + `AE_DEMO_MODE=1` so only the selected demo apps are reconciled.
  - Reset state quickly when switching contexts: `./scripts/init_demo.sh --reset` (deletes `state/controller.db` and `state/projections/`).
- SOPS/age (secrets):
  - Generate an age identity: `mkdir -p ~/.config/ae && age-keygen -o ~/.config/ae/keys.txt && chmod 600 ~/.config/ae/keys.txt`
  - Point SOPS to it: `export SOPS_AGE_KEY_FILE=~/.config/ae/keys.txt`
  - Seal sample secret: `make secrets-seal-demo` (uses `AE_AGE_RECIPIENT` or your keys.txt)
  - Convenience for demos: run `./scripts/init_demo.sh --with-secrets-env` to export both `AE_ALLOW_PLAINTEXT_SECRETS=1` and `SOPS_AGE_KEY_FILE` automatically.

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

Dashboard reload vs. restart
- Code/UI changes only (e.g., edits in `src/ae/observability/http_api.py`): `make dashboard-reload`
  - Kills the controller; the supervisor restarts it and picks up code changes.
- Env or port/token changes (anything in `state/env.sh`, `AE_API_*`, `AE_*` flags): `make dashboard-restart`
  - Stops the supervisor, clears any stale lock, then starts fresh so env is re‑sourced.
- Scope of apps shown and reconciled
  - The controller respects `AE_SPECS_DIR` for the active specs root. To avoid reconciling every sample under `specs/`, set `AE_SPECS_DIR` to a curated folder (e.g., `state/demo-specs`).
  - Updated Make targets and bench scripts auto‑honor `AE_SPECS_DIR`. If unset, they fall back to `specs/`.
  - `AE_DEMO_MODE=1` narrows the dashboard to apps discovered under `AE_SPECS_DIR` (plus any Labs‑applied apps), preventing leakage from historical runs.
- Viewing via docs host proxy? If you changed Caddy site files, restart the docs stack:
  - `make dev-down && make dev-up` (or `docker compose -f ops/dev/labs-compose.yaml restart caddy`).
- After any of the above, hard‑refresh the browser (Shift+Reload) to ensure the latest HTML/JS loads.

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

Planning and CI Gating
- Use `ae plan` to dry‑run an apply and surface host port conflicts, TLS resolution status, and best‑practice warnings.
- Use `ae plan --json` for CI gating. The JSON includes a `diagnostics` object with:
  - `service`: `{ type, ports[], duplicates{names|ports|nodePorts}, outOfRangeNodePorts[], hostPortConflicts{port:[containers]} }`
  - `tls`: `{ ingress, secretName, root, resolved, cert?, key?, error? }`

Example:
```
out=$(python -m ae.cli plan -f specs/examples/echo-multiport.yaml --json)
echo "$out" | jq '.'
dup_names=$(echo "$out" | jq -r '.diagnostics.service.duplicates.names | length')
oor=$(echo "$out" | jq -r '.diagnostics.service.outOfRangeNodePorts | length')
if [ $dup_names -gt 0 ] || [ $oor -gt 0 ]; then
  echo "Plan validation failed"; exit 2; fi
```

Tip: Set `AE_TLS_DIR` so `tlsSecretName` can resolve; otherwise plan warns and the controller falls back to Caddy internal TLS.
## Install as a Service (systemd)

Quick install
- Install the package: `python -m pip install -e .[dev]`
- Create a Podman network for multi-replica+ingress DNS (recommended): `podman network create devnet`
- Install and enable the controller service:
  - `make install-systemd`

What it does
- Installs `ops/systemd/ae-controller.service` to `/etc/systemd/system/`.
- Writes env file to `/etc/ae/ae.env` (edit to suit your host).
- Creates `/etc/ae/specs/` and seeds a sample `echo.yaml` if missing.
- Starts the controller (`ae-controller.service`), serving the API on `:9108` by default.

Important env in `/etc/ae/ae.env`
- `AE_RUNTIME_BACKEND=podman` (default)
- `AE_PODMAN_NETWORK=devnet` (set to your network name)
- `AE_SPECS_DIR=/etc/ae/specs`
- `AE_STATE_DB=/var/lib/ae/controller.db` (create the dir and adjust perms if you change this)
- `AE_CADDY_*` to integrate with a Caddy instance

Uninstall
- `make uninstall-systemd`
- Removes the systemd unit; leaves `/etc/ae` for inspection/backups.

Serve Documentation on Boot (optional)
- Build docs: `make docs`
- Install and enable the docs service (serves static HTML):
  - `make install-docs-service`
- By default it serves `/usr/share/ae/docs` on `:9109`.
- Customize in `/etc/ae/ae.env`:
  - `AE_DOCS_DIR=/usr/share/ae/docs`
  - `AE_DOCS_PORT=9109`
- Uninstall:
  - `make uninstall-docs-service`

### Systemd hardening (opt-in)
- Enable hardening drop-ins during install:
  - `AE_SYSTEMD_HARDEN=1 make install-systemd`
- This creates `/etc/systemd/system/ae-controller.service.d/hardening.conf` (and similar drop-ins for docs and caddy units when installed) with:
  - `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `LockPersonality=true`, `RestrictSUIDSGID=true`, `ProtectControlGroups=true`, `ProtectKernelTunables=true`, `ProtectKernelModules=true`, `ProtectClock=yes`, `RestrictRealtime=yes`, `MemoryDenyWriteExecute=true`, `CapabilityBoundingSet=`, `AmbientCapabilities=`, `SystemCallFilter=@system-service`.
- To disable: remove the drop-in and run `systemctl daemon-reload && systemctl restart ae-controller`.

## Container Images and Docker/Podman
Build the controller image
- Docker: `make image-docker IMAGE=ghcr.io/<org>/ae-controller:dev`
- Podman: `make image-podman IMAGE=ghcr.io/<org>/ae-controller:dev`

Push the image
- Docker: `make push-docker IMAGE=ghcr.io/<org>/ae-controller:dev`
- Podman: `make push-podman IMAGE=ghcr.io/<org>/ae-controller:dev`

Tags guidance
- Use `:main` for latest from main branch, `:vX.Y.Z` for releases, and `:sha-<short>` for immutable pins in CI.
- Example:
  - `export ORG=acme && export TAG=v0.1.0`
  - `make image-docker IMAGE=ghcr.io/$ORG/ae-controller:$TAG`
  - `docker tag ghcr.io/$ORG/ae-controller:$TAG ghcr.io/$ORG/ae-controller:main`
  - `make push-docker IMAGE=ghcr.io/$ORG/ae-controller:$TAG && docker push ghcr.io/$ORG/ae-controller:main`

Run locally with host specs/state
- Docker: `docker run --rm -p 9108:9108 -v $PWD/specs:/etc/ae/specs -v $PWD/state:/var/lib/ae ghcr.io/<org>/ae-controller:dev`
- Podman: `podman run --rm -p 9108:9108 -v $PWD/specs:/etc/ae/specs -v $PWD/state:/var/lib/ae ghcr.io/<org>/ae-controller:dev`

Docker users (alternative to Podman)
- Set runtime and shared network in `/etc/ae/ae.env`:
  - `AE_RUNTIME_BACKEND=docker`
  - `AE_DOCKER_NETWORK=dev_default` (or your docker compose network name)
  - `AE_CONTAINER_CLI=docker` (if the Caddy container runs under Docker)
- Start fixtures with Docker Compose instead of Podman Compose:
  - `docker compose -f ops/dev/docker-compose.yaml up -d`
  - Notes:
  - Multi-replica + ingress via container DNS requires a shared Docker network; export `AE_DOCKER_NETWORK=<name>` so app containers join it for Caddy reachability.
  - The controller continues to expose the API on `:9108` by default; metrics and dashboard are identical.

## Interactive Labs

The Interactive Lab Playground is a single HTML page that exercises read‑only verification, controlled actions (apply/scale/reset), live events/logs, and ingress checks.

- Build docs (includes playground): `python docs/build_docs.py`
- Open: `docs/site/playground.html`

Two easy ways to run it:
- Host controller + Caddy (serve docs only)
  - Start controller with labs enabled:
    - `AE_LABS=1 python -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch`
  - Serve docs via compose:
    - `docker compose -f ops/dev/labs-compose.yaml up -d`
  - Open https://localhost:8443/playground.html
  - Optional token gate:
    - Export `AE_LABS_TOKEN=…` for the controller; paste it into “Labs Token” on the page.

- All‑in‑one compose (controller + docs)
  - `docker compose -f ops/dev/labs-aio.yaml up -d`
  - Open https://localhost:8443/playground.html

Tips
- Toggle “Enable Controlled Actions” to activate Apply/Scale/Reset; otherwise the page remains read‑only.
- The page auto‑detects the API base; use the “API Mode” button in the footer to switch proxy vs. direct.
- k3s/k3d: click “Create k3d Cluster” to bootstrap a local k3d for the Kubernetes track; ports default to 8081/8444 and are shown in the banner.
- If ingress uses a custom TLS secret, make sure it’s synced (see “Ingress and TLS” above) so the app hostname resolves under Caddy.

DNS/hosts for local domains
- Add entries to your hosts file so browsers resolve the dev domains:
  - Linux/macOS: add to `/etc/hosts`
  - Windows: add to `C:\Windows\System32\drivers\etc\hosts`

```
127.0.0.1 docs.home.arpa
127.0.0.1 api.home.arpa
```

Caddy site config (dev)
- The repo ships site snippets under `ops/dev/caddy/sites/`:
  - `docs.caddy`: serves the docs at `https://docs.home.arpa:8443/` and proxies API paths (`/health`, `/status`, `/events`, `/logs`, `/metrics`, `/swagger`, `/redoc`, `/dashboard`, `/labs`, `/system`) to the controller on `host.docker.internal:9108`.
  - `api.caddy`: exposes the API directly at `https://api.home.arpa:8443/` (handy for Swagger/ReDoc).
- After updating site files, restart Caddy:
  - `docker compose -f ops/dev/labs-compose.yaml restart caddy` (or bring the stack up again)
## Probe History: Dashboard and CLI

The controller records the result of each readiness/liveness evaluation per replica. You can now surface this in two ways:

- Dashboard
  - Probe History card: shows the most recent evaluations for the selected app (auto‑refreshes every 10s).
  - Replicas table: click a replica row to toggle an inline panel with the last N checks (selector at the top right of the card). Useful for debugging intermittent probe failures or backoff windows.

- CLI
  - List recent checks:
    - `ae history <app> [--limit 20] [--replica <id>] [--json]`
    - Time filters: `--since 30m` (supports `s`, `m`, `h`) or `--since-time 2025-11-10T12:34:00Z`.
  - Remote mode: add `--server http://host:port` and `--token $AE_API_READ_TOKEN` to query a running controller via HTTP.

Notes
- The dashboard caches the most recent history response while browsing replica rows to stay responsive.
- In constrained environments, some tests involving local TCP sockets may be skipped or fail due to sandbox restrictions; this does not affect runtime behavior.

## Dev Exporter Preview (K8s YAML)

For rapid iteration on exporter options, enable a development-only endpoint to render Kubernetes YAML from a posted manifest:

- Set `AE_API_DEV_EXPORT=1` and POST to `/k8s/preview` with JSON payload `{ manifest fields..., "options": { ExportOptions... } }`.
- Response JSON includes a `yaml` field with a multi‑document manifest.

## Ingress Presets and Storage Overrides

- Ingress: use `--ingress-preset nginx-web|traefik-web` to apply common, opt‑in annotations. Combine with `--ingress-class` and custom `--ingress-annotation key=value` for fine‑tuning.
- Storage: `--storage-class-name <name>` and `--pvc-access-modes <mode>` override defaults for generated PVCs and StatefulSet `volumeClaimTemplates`.
