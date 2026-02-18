Operations Runbook

Purpose
- This runbook captures common operational tasks for the k1s controller: exporting manifests to K8s, validating portability, managing ingress/TLS, rollouts, and API tokens.

Setup
- Install Python deps: `python -m pip install -e .[dev]`
- Dev services (optional): `docker compose -f ops/dev/docker-compose.yaml up -d`
- Controller loop (dev):
  - Default specs dir: `python -m ae.controller --loop --interval 10 --specs specs/ --metrics-port 9108`
    - `--interval` defaults to 2s; increase it for ops (for example 10–30s) to reduce log noise.
    - The specs dir is imported into the registry; the reconciler always runs against the registry. An empty specs dir does not clear existing workloads.
  - Curated demo/specs: set `AE_SPECS_DIR` and use Make targets that respect it:
    - `AE_SPECS_DIR=state/profiles/demo/specs make loop` (watches only the curated set)
    - `AE_SPECS_DIR=state/profiles/demo/specs make run` (single reconcile pass)
- Tip: `scripts/init_demo.sh` seeds `state/profiles/demo/specs` and exports `AE_SPECS_DIR` + `AE_DEMO_MODE=1` for demo runs.
  - Demo convenience: `AE_REGISTER_LOCAL_NODE=1` registers a local node when no nodes are present (keeps demo/labs single-node runs working while preserving Kubernetes scheduling semantics by default).
  - Reset state quickly when switching contexts: `./scripts/init_demo.sh --reset` (deletes `state/profiles/demo/controller.db` and `state/profiles/demo/projections/`).
  - Registry cache: `./scripts/init_demo.sh --reset-registry-cache` (clears `state/registry` to force re-pull into the local cache).
- Etcd-backed demo/labs:
  - `AE_STATE_BACKEND=etcd make demo` (auto-starts etcd for the demo controller)
  - `AE_STATE_BACKEND=etcd make labs-aio-up` (labs stack uses an embedded etcd service)
- SOPS/age (secrets):
  - Generate an age identity: `mkdir -p ~/.config/ae && age-keygen -o ~/.config/ae/keys.txt && chmod 600 ~/.config/ae/keys.txt`
  - Point SOPS to it: `export SOPS_AGE_KEY_FILE=~/.config/ae/keys.txt`
  - Seal sample secret: `make secrets-seal-demo` (uses `AE_AGE_RECIPIENT` or your keys.txt)
  - Convenience for demos: run `./scripts/init_demo.sh --with-secrets-env` to export both `AE_ALLOW_PLAINTEXT_SECRETS=1` and `SOPS_AGE_KEY_FILE` automatically.

NATS + etcd dev stack (Mode A)
- Start hub + etcd + edge NATS: `docker compose -f ops/dev/docker-compose.nats-etcd.yaml up -d`
- Stop stack: `docker compose -f ops/dev/docker-compose.nats-etcd.yaml down`
- Configs: `ops/dev/nats-hub.conf`, `ops/dev/nats-edge.conf` (dev-only credentials).
- Manual core+edge test pattern: `docs/ops/core-edge-manual-test.md`
- Gateway env defaults: `ops/dev/site-gateway.env.sample` (Option A ack settings).
- Default dev creds: hub controller `hub-controller/dev`, site uplink `site-sfo-edge-01-uplink/dev`, local `gateway/dev` and `worker/dev` (do not use in prod).
- If docker-compose fails to create `state/etcd`, fix ownership once:
  - `sudo mkdir -p state/etcd`
  - `sudo chown -R $USER:$USER state/etcd`
- Local E2E stub (work.pull path):
  - Start gateway: `AE_TRANSPORT_BACKEND=nats-core AE_SITE_ID=sfo-edge-01 AE_NATS_URL=nats://127.0.0.1:4223 ae-gateway`
  - Start stub worker: `ae-worker-stub --node-id node-01 --nats-url nats://127.0.0.1:4223`
  - Enqueue a work item: `ae work enqueue --site-id sfo-edge-01 --mode queue --op ensure_pod --preferred-node node-01`
- Mode A canary (JetStream path):
  - Hub controller: `AE_TRANSPORT_BACKEND=nats-js AE_SITE_IDS=sfo-edge-01 AE_NATS_URL=nats://127.0.0.1:4222 python -m ae.controller --loop --interval 2 --metrics-port 9108`
  - Gateway: `AE_TRANSPORT_BACKEND=nats-js AE_SITE_ID=sfo-edge-01 AE_NATS_URL=nats://127.0.0.1:4223 ae-gateway`
  - Enqueue: `ae work enqueue --site-id sfo-edge-01 --mode outbox --op ensure_pod --preferred-node node-01`
  - Rollback: stop the gateway and restart the controller with `AE_TRANSPORT_BACKEND=http` (or unset) to return to HTTP dispatch.
- Automated canary + rollback: `scripts/dev/nats_etcd_canary.sh` (uses `.venv` if present; override `METRICS_PORT` if 9108 is in use).

Etcd maintenance (dev/CI)
- Quick status: `scripts/dev/etcd_maintenance.sh status`
- Guard before long ingress lanes: `scripts/dev/validate_ingress_env.sh --lane core-proxy --watchdog`
- Forced reclaim when etcd returns `mvcc: database space exceeded`:
  - `scripts/dev/etcd_maintenance.sh compact-defrag`
- Startup defaults in `k1s-core`/`dev-etcd` profiles:
  - `AE_ETCD_MAINTENANCE_ENABLE=1`
  - `AE_ETCD_MAINTENANCE_THRESHOLD_PCT=80`
- Override/disable behavior when needed:
  - `AE_ETCD_MAINTENANCE_ENABLE=0 make k1s-core-cri`
  - `AE_ETCD_MAINTENANCE_THRESHOLD_PCT=70 make k1s-core-cri`

Rosenpass WireGuard PSK (Option C)
- Requires: WireGuard tools (`wg`, `wg-quick`) and Rosenpass installed on each node host.
- Enable managed Rosenpass: `AE_ROSENPASS_ENABLED=1` on each node.
- Data directory (keys/config/status): `AE_ROSENPASS_DIR=/var/lib/ae/rosenpass` (default).
- Config file: `AE_ROSENPASS_CONFIG=/path/to/rosenpass.yaml` for node-local peers or `AE_ROSENPASS_CONFIG=controller` for controller-managed hub-spoke peers.
- Hub/spoke discovery: hub node labels `role=controller` or `role=hub`; hub site override `AE_OVERLAY_HUB_SITE=<site-id>` (fallback `AE_SITE_ID`); hub WG endpoint label `wg_endpoint=<public-ip:port>` or `AE_OVERLAY_HUB_ENDPOINT`.
- WireGuard interface override: `AE_WG_INTERFACE=wg0` (default).
- Optional Rosenpass command override (if default fails): `AE_ROSENPASS_COMMAND="rosenpass exchange-config {config}"`.
- Peer refresh interval (controller-managed peers): `AE_ROSENPASS_PEER_REFRESH_SEC=30` (set to `0` to disable).
- Status file: `${AE_ROSENPASS_DIR}/rosenpass-status.json` (or `AE_ROSENPASS_STATUS_PATH`).

Manual bringup (hub + edge, controller-managed peers)
- Hub controller (agent API enabled): `AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=... python -m ae.controller --loop`
- Hub node (with labels): `AE_NODE_LABELS="role=hub,site=<hub-site>,wg_endpoint=<public-ip:51820>" AE_ROSENPASS_ENABLED=1 AE_ROSENPASS_CONFIG=controller python -m ae.node --ensure-pod-net`
- Edge node: `AE_NODE_LABELS="site=<edge-site>" AE_ROSENPASS_ENABLED=1 AE_ROSENPASS_CONFIG=controller AE_CONTROLLER_URL=http://<hub>:9110 AE_AGENT_TOKEN=... python -m ae.node --ensure-pod-net`

Quick checks
- Overlay config served: `curl -H "X-Agent-Token: $AE_AGENT_TOKEN" http://<hub>:9110/v1/nodes/<node-id>/overlay`
- Rosenpass running: `cat /var/lib/ae/rosenpass/rosenpass-status.json`
- WireGuard handshakes: `wg show wg0`

CRI nodes (containerd)
- Required env:
  - `AE_RUNTIME_BACKEND=cri`
  - `AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock`
  - `AE_CRI_SANDBOX_IMAGE=registry.k8s.io/pause:3.9`
- Service VIP (optional):
  - `AE_ENABLE_SERVICE_PROXY=1`
  - `AE_SERVICE_PROVIDER=iptables`
  - Run controller as root or with sufficient iptables permissions
- Streaming exec/attach uses `crictl`; ensure it is installed and on PATH (`CRICTL_BIN` overrides).
- CRI port-forward proxy (pods/services): set `AE_APISHIM_CRI_PORTFORWARD=1` (or `AE_APISHIM_CRI_PORTFORWARD_FORCE=1` to always prefer it).
- Service VIP proxy on CRI uses iptables; set `AE_ENABLE_SERVICE_PROXY=1` and run as root.
- CNI dirs (defaults): `/opt/cni/bin` and `/etc/cni/net.d`
- Init CNI configs (bridge + loopback) if missing: `./scripts/cni_init.sh`
- If CNI version mismatches occur, force rewrite with a newer spec and restart containerd:
  - `AE_CNI_FORCE=1 AE_CNI_VERSION=1.0.0 ./scripts/cni_init.sh`
  - `sudo systemctl restart containerd`
- Preflight checks: `./scripts/cri_preflight.sh`
- Smoke check (requires crictl): `./scripts/cri_smoke.sh`
- Optional pull test: `AE_CRI_SMOKE_PULL=1 pytest tests/integration/test_cri_smoke.py -k pull`
- Optional lifecycle test: `AE_CRI_IT=1 pytest tests/integration/test_cri_runtime_integration.py -q`
- CI-style bootstrap (installs containerd/CNI/crictl): `./scripts/cri_ci_setup.sh`

Export and Validate K8s YAML
- Hardened export with validation:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o specs/examples/echo-k8s.yaml`
- Multi‑replica hardened export:
  - `python -m ae.cli export-k8s -f specs/examples/multi-replica-echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o specs/examples/multi-replica-echo-k8s.yaml`
- CI runs server‑side `kubectl apply --dry-run=server` and `kubeconform -strict` on exported samples.

Ingress and TLS
- Overview: see docs/reference/ingress.md for multi‑path routing and TLS options.
- TLS sync helper:
  - Render PEMs from a Kubernetes Secret file: `python -m ae.cli tls sync --name mycert --input path/to/mycert.yaml --root state/tls`
  - Or place direct PEMs `state/tls/mycert.crt` and `state/tls/mycert.key`.
  - Set only `spec.ingress.tlsSecretName: mycert` in your Deployment manifest; the controller will resolve and wire cert/key for Caddy.
- Environment:
  - AE_TLS_DIR (default: state/tls)
  - AE_CADDY_SITES, AE_CADDY_BIN, AE_CADDY_FILE, AE_CADDY_CONTAINER, AE_CONTAINER_CLI, AE_CADDY_RELOAD_TIMEOUT

Ingress validation lanes (CRI, mode-isolated)
- Preflight before long lanes:
  - `sudo -v`
  - `scripts/dev/validate_ingress_env.sh --lane core-proxy --watchdog`
- Optional guided wrapper with lane checkpoints:
  - `scripts/dev/run_ingress_lanes.sh --lanes all` (compat alias: `scripts/dev/run_ingress_mode_lanes.sh`)
- Full tested startup command set (core/core-node/edge-gateway/edge-node by lane mode):
  - `docs/guides/ingress-capability-test-sequence.md` (Step 1a)
- Core-proxy mini sanity lane:
  - `CORE_PROXY_FORCE_RATHOLE_RESTART=0 scripts/dev/test_ingress_matrix_single_host.sh --modes core-proxy --archetypes ws-echo --tier tier2 --validation-profile standard`
- Core-proxy primary deep lane (policy + observability):
  - `CORE_PROXY_FORCE_RATHOLE_RESTART=0 scripts/dev/test_ingress_matrix_single_host.sh --modes core-proxy --archetypes ws-echo,lb-distribution,sticky-cookie --tier tier2 --validation-profile deep+perf --perf-profile sample --lb-proof-scope auto`
- Optional full core-proxy lane:
  - `CORE_PROXY_FORCE_RATHOLE_RESTART=0 scripts/dev/test_ingress_matrix_single_host.sh --modes core-proxy --archetypes http-static,http-path-routing,ws-echo,lb-distribution,sticky-cookie --tier tier2 --validation-profile deep+perf --perf-profile sample --lb-proof-scope auto`
- Core-to-edge-public lane (separate stack start with `EDGE_INGRESS_MODE=core-to-edge-public`):
  - `scripts/dev/test_ingress_matrix_single_host.sh --modes core-to-edge-public --archetypes http-static,http-path-routing --tier tier1 --validation-profile standard`
- Edge-local strict LB proof lane (separate stack start with `EDGE_INGRESS_MODE=edge-local` + `AE_ROUTE_BUNDLE_ENABLED=1`):
  - `scripts/dev/test_ingress_matrix_single_host.sh --modes edge-local --archetypes lb-distribution --tier tier2 --validation-profile deep --lb-proof-scope edge-only --lb-sample-requests 5000 --lb-min-backends 2 --lb-max-skew-ratio 0.35 --edge-local-listener-url https://lb-distribution-edge-local.home.arpa/`
- Security baseline + staged active auth probes (per lane or after full sequence):
  - `scripts/dev/security_baseline_check.sh --fail-on high`
  - `scripts/dev/security_active_tests.sh --fail-on high`
  - wrapper integrated path: `scripts/dev/run_ingress_lanes.sh --lanes all --security-all`
- Keep lanes mode-isolated. Do not run mixed-mode rows on one stack profile.
- Summary interpretation (`state/test-results/ingress-matrix-*.json`):
  - `lb_policy_passed=true`: core-proxy policy lane passed.
  - `lb_observability_passed=true`: core-proxy LB row emitted usable backend-observation evidence.
  - `lb_strict_proof_passed=true`: strict edge-local distribution proof passed.
- Cross-platform parity benchmark: `docs/ops/perf-parity-k1s-vs-k3s.md`

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

Testing notes
- Integration coverage targets and fixture notes live here (formerly `docs/testing.md`). Update when expanding tests or lab fixtures.

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

API shim (kubectl/helm)
- Start shim locally: `AE_APISHIM_ENABLE=1 AE_APISHIM_TOKEN=changeme python -m ae.apishim serve --host 127.0.0.1 --port 8445` (add `--allow-anonymous` only for dev). Postgres backend: set `AE_APISHIM_DSN=postgresql://user:pass@host:5432/dbname`; default is SQLite at `AE_APISHIM_DB` (`state/apishim.db`) unless a DSN is provided.
- Non-root CLI auth (recommended):
  - One-time group setup: `sudo groupadd -f aecli && sudo usermod -aG aecli $USER` (re-login/newgrp required).
  - Start `k1s-core` with sudo if needed; keep root `state/profiles/<profile>/apishim.env` private (`600`).
  - Configure a mint-only credential: `AE_APISHIM_MINT_TOKEN=<long-random-token>` on the shim.
  - Startup now syncs `state/profiles/<profile>/apishim.cli.env` (`640 root:aecli`) with `AE_APISHIM_SERVER`, `AE_APISHIM_MINT_TOKEN`, and `AE_APISHIM_CA_BUNDLE`.
  - Startup also syncs `state/profiles/<profile>/apishim.ca.crt` (`640 root:aecli`) for CA verification.
  - In operator shells run `source <(ae auth local --strict)` (no `--apishim-env` arg required); auth infers the active profile and prefers `apishim.cli.env`.
  - `ae shell` / `ae port-forward` will mint short-lived scoped `sess1.*` tokens through `POST /api/v1/sessiontokens` and automatically refresh once on `401`.
  - If shim token auth returns `401` but `AE_LABS_TOKEN` is present, CLI can fallback to controller-minted session tokens via `POST /api/apishim/session` (dashboard-compatible path).
  - Disable controller fallback by setting `AE_CLI_LABS_MINT_FALLBACK=0` when you want shim-only auth behavior.
  - Keep `AE_APISHIM_TOKEN` (admin) service-only; avoid routine `sudo ae ...`.
- `k1s-core` profile starts Postgres for apishim by default and sets `AE_APISHIM_DSN`:
  - `AE_APISHIM_MODE=host`: uses `127.0.0.1:<port>`.
  - container mode: uses the compose service name `postgres:5432`.
- For multi-site: bind Postgres to the hub WG IP with `POSTGRES_BIND_IP=<hub-wg-ip>` and point edge nodes at `AE_APISHIM_DSN=postgresql://shim:shim@<hub-wg-ip>:5432/shim`.
  - Example: `POSTGRES_BIND_IP=10.255.0.1 make k1s-core` and `AE_APISHIM_DSN=postgresql://shim:shim@10.255.0.1:5432/shim`.
- WS exec/port-forward smoke: `AE_APISHIM_EXEC_TOKEN=exec AE_APISHIM_PORTFORWARD_TOKEN=pf AE_RUNTIME_BACKEND=docker ./scripts/dev/apishim_ws_smoke.sh` (optional: `PF_JS=1` for JS client, `PF_RAW_DUMP=1` to capture raw frames).
- Kubeconfig helper: `python -m ae.apishim kubeconfig --server http://127.0.0.1:8445 --token $AE_APISHIM_TOKEN --insecure-skip-tls-verify > ~/.kube/k1s-apishim.yaml`.
- Storage migration: `python -m ae.apishim migrate --source state/apishim.db --target $AE_APISHIM_DSN` copies objects while preserving resourceVersion between SQLite and Postgres.
- Shim metrics: `/metrics` (token required unless anonymous allowed) exposes `apishim_watch_*` counters/gauges for watch queue depth, enqueued, dropped, streams started, and `apishim_store_backend_info`.
- Helm/kubectl dry-run: shim serves `/openapi/v3` as the primary schema endpoint with `/openapi/v2` as a compatibility mirror; both are exported during release and attached as artifacts.

Controller state store
- Default: SQLite at `state/controller.db`.
- Postgres: set `AE_STATE_DSN=postgresql://user:pass@host:5432/dbname` (shim and controller share the same DSN when present). SQLite path can still be overridden via `AE_STATE_DB` for single-node dev.

Release notes quick links
- Compatibility matrix: `docs/reference/apishim-compatibility-matrix.md` (uploaded with releases)
- OpenAPI artifacts: `/openapi/v2` and `/openapi/v3` are exported during release and attached as `openapi-schemas`.

Observability
- Controller dashboard/API: `http://127.0.0.1:9108` when `--metrics-port` is set.
- Prometheus metrics at `/metrics` (text), recent events via `/events/<app>`.

Dashboard reload vs. restart
- Code/UI changes only (e.g., edits in `src/ae/observability/http_api.py`): `make dashboard-reload`
  - Kills the controller; the supervisor restarts it and picks up code changes.
- Env or port/token changes (anything in `state/env.sh`, `AE_API_*`, `AE_*` flags): `make dashboard-restart`
  - Stops the supervisor, clears any stale lock, then starts fresh so env is re‑sourced.
- Scope of apps shown and reconciled
  - The controller respects `AE_SPECS_DIR` for the active specs root. To avoid reconciling every sample under `specs/`, set `AE_SPECS_DIR` to a curated folder (e.g., `state/profiles/demo/specs`).
  - Updated Make targets and bench scripts auto‑honor `AE_SPECS_DIR`. If unset, they fall back to `specs/`.
  - Dashboard output is unfiltered; scope by curating `AE_SPECS_DIR` and using token scopes if needed.
- Viewing via docs host proxy? If you changed Caddy site files, restart the docs stack:
  - `make dev-down && make dev-up` (or `docker compose -f ops/dev/labs-compose.yaml restart caddy`).
- After any of the above, hard‑refresh the browser (Shift+Reload) to ensure the latest HTML/JS loads.

Tips
- Prefer non‑root containers and set readOnlyRootFilesystem where possible.
- For utilization HPAs, set CPU/Memory requests to avoid HPA errors.
- Use `--policy strict` in CI to keep manifests honest.

NATS edge drills (Mode A)
- Leaf disconnect: stop the edge leader NATS process and confirm `ae_site_stale{site=...}` flips to 1.
- Gateway reconnect: restart gateway and ensure `ae_site_last_seen_seconds` drops back near 0.
- JS consumer lag: enqueue a batch of work and watch `ae_outbox_publish_success_total` advance; use NATS tooling to inspect consumer pending/ack if needed.
- Hub NATS restart: restart the hub NATS process and ensure outbox publishing resumes without manual intervention.
- Site disconnect/reconnect: stop the edge leader + gateway, confirm stale metrics, then restart and confirm the site recovers.
- Worker crash mid-work: kill the worker stub during execution and confirm the gateway stops progress acks; message should be NAKed and redelivered once the gateway sees stale heartbeats.
- etcd leader change: force a leader move and ensure controller reconciliation continues without errors.


Token rotation and cleanup
- Rotate tokens proactively before expiry; consider 24h pre‑expiry for rotation.
- After rotating, remove old `AE_API_*_TOKEN` and `AE_API_*_TOKEN_EXPIRES` values from your environment/secret store.
- HTTP API exports token expiry metrics so you can alert:
  - `ae_api_token_expiry_seconds{role="admin|scaler|read"}` (negative when expired).

Cleanup (stop all dev containers/services)
- Preferred: run as the same user who started the containers.
  - `./scripts/stop_all.sh`
- If containers were started with sudo, run:
  - `sudo ./scripts/stop_all.sh`
- If you’re unsure, the script will attempt to stop both rootless and root containers when invoked with sudo.


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

## TLS and agent certs (multinode)
- Controller agent API can serve HTTPS and require client certs:
  - `AE_AGENT_API_TLS_CERT` / `AE_AGENT_API_TLS_KEY`
  - `AE_AGENT_API_CLIENT_CA` and `AE_AGENT_API_REQUIRE_CLIENT_CERT=1`
- Agents use mTLS to heartbeat to the controller:
  - `AE_CONTROLLER_TLS_CA` (CA bundle), `AE_CONTROLLER_TLS_CERT` / `AE_CONTROLLER_TLS_KEY`
- Join tokens are HMACed with `AE_AGENT_JOIN_SECRET`; bootstrap is single-use and recorded. Issued certs live in `state/tls/issued.json`; revoked serials in `state/tls/revoked.json`.
- Rotate/reissue: `ae-rotate-certs --node-id <node>` writes a new cert/key/CA bundle and a join token; deploy to the node and restart `ae-node`.
- Observe status: `ae certs` lists issued/revoked certs; `/metrics` exposes `ae_agent_cert_expiry_seconds{node=...}`. Revoked certs cause heartbeats to be rejected when mTLS is enabled.
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
  - Dashboard (separate host): https://dash.home.arpa:8443/dashboard
  - Optional token gate:
    - Export `AE_LABS_TOKEN=…` for the controller; paste it into “Labs Token” on the page.

- All‑in‑one compose (controller + docs)
  - Recommended: `make labs-aio-up` (generates shim tokens before compose)
  - Or: `./scripts/ensure_apishim_env.sh && docker compose -f ops/dev/labs-aio.yaml up -d`
  - Open https://localhost:8443/playground.html
  - Dashboard (separate host): https://dash.home.arpa:8443/dashboard
  - API shim starts by default on `127.0.0.1:8445` with per-run tokens stored in `state/profiles/labs/apishim.env`
  - To override tokens, set `AE_APISHIM_TOKEN` / `AE_APISHIM_READ_TOKEN` in `.env` (long values; weak tokens are rejected)
  - To run with a local Postgres backend for controller + shim, set `AE_LABS_USE_POSTGRES=1` before bringing the stack up
  - To print the shim tokens: `make labs-apishim-env`

Tips
- Toggle “Enable Controlled Actions” to activate Apply/Scale/Reset; otherwise the page remains read‑only.
- The page auto‑detects the API base; use the “API Mode” button in the footer to switch proxy vs. direct.
- k3s/k3d: click “Create k3d Cluster” to bootstrap a local k3d for the Kubernetes track; ports default to 8081/8444 and are shown in the banner.
- If ingress uses a custom TLS secret, make sure it’s synced (see “Ingress and TLS” above) so the app hostname resolves under Caddy.
- Gates: set `AE_PLAYGROUND=0` to disable the playground UI and labs endpoints, and `AE_DASHBOARD=0` to disable the dashboard UI.

DNS/hosts for local domains
- Add entries to your hosts file so browsers resolve the dev domains:
  - Linux/macOS: add to `/etc/hosts`
  - Windows: add to `C:\Windows\System32\drivers\etc\hosts`

```
127.0.0.1 docs.home.arpa
127.0.0.1 api.home.arpa
127.0.0.1 dash.home.arpa
```

Caddy site config (dev)
- The repo ships site snippets under `ops/dev/caddy/sites/`:
  - `docs.caddy`: serves the docs at `https://docs.home.arpa:8443/` and proxies API paths (`/health`, `/status`, `/events`, `/logs`, `/metrics`, `/swagger`, `/redoc`, `/labs`, `/system`, `/ui/features`, `/api/apishim/*`) to the controller on `host.docker.internal:9108`.
  - `dash.caddy`: exposes the dashboard UI at `https://dash.home.arpa:8443/dashboard` (controller-backed).
  - `api.caddy`: exposes the API directly at `https://api.home.arpa:8443/` and routes `/api/v1` + `/apis` to the API shim (exec/port-forward).
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

## NetFS Storage Operations

Operational toggles:
- `AE_NETFS_ROOT` controls the node mount root (default `/var/lib/ae/netfs`).
- `AE_NETFS_FS_RESIZE=1` enables best-effort filesystem resize on bound volumes.
- `AE_NETFS_SELINUX_RECURSIVE=1` allows recursive `chcon -R` on RWX/ROX volumes.

PVC cloning (filesystem volumes only):
- Set `spec.dataSourceRef` with kind `PersistentVolumeClaim` on the target PVC.
- Source PVC must be `Bound` and hostPath-backed (NFS/local-path provisioners).
- The target StorageClass must match the source StorageClass.
- Not supported for `volumeMode: Block`.

CSIStorageCapacity overrides:
- StorageClasses can publish static capacity for external CSI drivers via
  `parameters.capacity` (e.g., `5Gi`) or `parameters.capacityBytes` (integer bytes).
- This bypasses hostPath disk probes and only affects the advertised capacity object.

CSI topology + labels (site placement):
- StorageClass must define `topologyKeys: ["site"]` (or your chosen key).
- Nodes must advertise a matching `site=<site-id>` label.
- Confirm the selected node aligns with the intended site before attaching/mounting.

Recovery checklist:
- If PVC binds to the wrong site, update node labels and re-apply the PVC.
- If VolumeAttachment is missing, verify `CSIDriver.attachRequired` and storage class name.
- If NodePublish fails, confirm CSI node endpoint reachability from the edge site.

Smoke checklist:
- `ae nodes` shows `site` labels for all nodes.
- PVC binds on the expected site and VolumeAttachment is present (attachRequired=true).
- Pod mounts successfully on the matching node.

Common failure reasons (PVC events):
- `CloneNotReady` / `CloneNotFound`: source PVC missing or not bound.
- `CloneUnsupported`: block volume or non-hostPath-backed source.
- `CloneInvalid`: mismatched StorageClass or invalid `dataSourceRef`.
- `VolumeNotAttached`: CSI volume lacks a matching VolumeAttachment.
- `SelinuxRelabelFailed`: SELinux relabel attempt failed; check privileges and `chcon`.

Useful checks:
- `ae events <app>` for recent PVC/PV events.
- `ae status --verbose` to verify node mount paths and permissions.
