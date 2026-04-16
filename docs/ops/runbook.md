# Operations Runbook

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
  - `AE_STATE_BACKEND=etcd make labs-aio-up` (labs host-controller wrapper reuses the `dev-etcd` profile with Caddy/TLS defaults)
- SOPS/age (secrets):
  - Generate an age identity: `mkdir -p ~/.config/ae && age-keygen -o ~/.config/ae/keys.txt && chmod 600 ~/.config/ae/keys.txt`
  - Point SOPS to it: `export SOPS_AGE_KEY_FILE=~/.config/ae/keys.txt`
  - Seal sample secret: `make secrets-seal-demo` (uses `AE_AGE_RECIPIENT` or your keys.txt)
  - Convenience for demos: run `./scripts/init_demo.sh --with-secrets-env` to export both `AE_ALLOW_PLAINTEXT_SECRETS=1` and `SOPS_AGE_KEY_FILE` automatically.

VM GPU Fabric Lab
- Golden image build/verify/transfer: `docs/ops/vm-golden-image-pipeline.md`
- Variant orchestration and bootstrap: [VM Variant Runbook](vm-variant-runbook.html)
- Baseline metrics and throughput gates: `docs/ops/vm-metrics-and-gates.md`
- Remote GPU VM precursor (A+B, libvirt/QEMU): `docs/ops/gpu-vm-remote-host-validation.md`
- Primary smoke entrypoint: `make lab-vm-smoke`
- Lower-level orchestration entrypoint: `scripts/lab/vm/labctl.sh`
- Recommended non-GPU smoke pattern (seeded cache, helper-backed):
  - `AE_CRI_CACHE_SEED_ENGINE=docker AE_CRI_CACHE_SEED_MODE=required AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 make lab-vm-smoke VARIANT=lab/variants/test3-abc-no-gpu.yaml RUN_ID="$RUN_ID" LAB_VM_SMOKE_ARGS="--purge --destroy-network --lanes multi_non_gpu"`
  - This uses the wrapper-backed smoke path, enables `seed_cache` before bootstrap, and auto-tears down on success while keeping failed runs for inspection.

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
  - `AE_ETCD_MAINTENANCE_INTERVAL_SEC=900` (controller loop watchdog cadence)
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
- Peer refresh interval (controller-managed peers): `AE_ROSENPASS_PEER_REFRESH_SEC=60` (set to `0` to disable).
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
- HA passive-resource reads:
  - In HA mode, converged `Secret` and `ServiceAccount` reads come from the shim HTTP authority path rather than the local shim DB.
  - Set `AE_APISHIM_URL` (or `AE_APISHIM_SERVER`) plus `AE_APISHIM_READ_TOKEN` and `AE_APISHIM_CA_BUNDLE`/`AE_APISHIM_CA` when CRI nodes need HA image-pull secret or ServiceAccount lookup.
- Current HA boundary:
  - HA apishim mutation/read/watch now converges on shared authority for workload-core resources, `ConfigMap`, `Secret`, `ServiceAccount`, `CronJob`, `HorizontalPodAutoscaler`, `Namespace`, RBAC resources, `PodDisruptionBudget`, CRDs/custom resources, `StorageClass`, PVC, PV, and the snapshot/CSI resource surface.
  - Controller-owned storage resources remain readable but reject external mutation in HA mode: `VolumeAttachment`, `CSIStorageCapacity`, and `VolumeSnapshotContent`.
  - The elected main controller owns `CronJob`, HPA, and storage reconciliation in HA mode; the apishim-local `StorageController` stays disabled.
  - If controller authority is uncertain or `etcd` quorum is lost, the control plane degrades to read-only.
- Service VIP (optional):
  - `AE_ENABLE_SERVICE_PROXY=1`
  - `AE_SERVICE_PROVIDER=iptables`
  - Run controller as root or with sufficient iptables permissions
- Streaming exec/attach uses `crictl`; ensure it is installed and on PATH (`CRICTL_BIN` overrides).
- CRI port-forward proxy (pods/services): set `AE_APISHIM_CRI_PORTFORWARD=1` (or `AE_APISHIM_CRI_PORTFORWARD_FORCE=1` to always prefer it).
- Service VIP proxy on CRI uses iptables; set `AE_ENABLE_SERVICE_PROXY=1` and run as root.
- CNI dirs (defaults): `/opt/cni/bin` and `/etc/cni/net.d`
- Strict startup now auto-materializes `/opt/cni/bin` when the required plugins are already available in standard source dirs or NixOS PATH-managed plugin locations.
- Init CNI configs (bridge + loopback) if missing: `./scripts/cni_init.sh`
  - Manual plugin bootstrap: `./scripts/cni_bin_bootstrap.sh`
- If CNI version mismatches occur, force rewrite with the compatibility default and restart containerd:
  - `AE_CNI_FORCE=1 AE_CNI_VERSION=0.4.0 ./scripts/cni_init.sh`
  - `sudo systemctl restart containerd`
- Preflight checks: `./scripts/cri_preflight.sh`
- If CRI socket access is root-only, run strict profiles with `sudo -E` or grant temporary access with `./scripts/containerd_socket_access.sh --grant`
- Smoke check (requires crictl): `./scripts/cri_smoke.sh` (pulls the sandbox image and runs a PodSandbox)
- Optional pull test: `AE_CRI_SMOKE_PULL=1 pytest tests/integration/test_cri_smoke.py -k pull`
- Optional lifecycle test: `AE_CRI_IT=1 pytest tests/integration/test_cri_runtime_integration.py -q`
- CI-style bootstrap (installs containerd/CNI/crictl): `./scripts/cri_ci_setup.sh`
- CRI benchmark reruns: prefer `./scripts/bench/run_cri_verify.sh`
  - Smoke lane:
    - `./scripts/bench/bench_env_teardown.sh --env state/bench-cri/env.sh || true`
    - `sudo pkill -f "python .*ae\\.controller.*state/bench-cri/specs" || true`
    - `export BASE="r$(date +%Y%m%d-%H%M)-cri-runc-wrapper-check"`
    - `RUNS="1" ./scripts/bench/run_cri_verify.sh`
  - Full verify:
    - `export BASE="r$(date +%Y%m%d-%H%M)-cri-runc-verify"`
    - `RUNS="1 2 3" ./scripts/bench/run_cri_verify.sh`
  - The wrapper logs to `state/bench-cri-rerun-*.log`, forces a bench-local `runtimeClassName: runc`, rejects `/k8s.io/kata` cgroup paths in `pods-1`, and checks for `8` finalized rows per CRI run.
  - Set `BASE=...` / `RUNS=...` before the script name (or `export` them); do not pass them after `run_cri_verify.sh` as positional args.

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
- Dev-host gate policy and auto recheck: `docs/ops/ingress-gate-policy-devhost.md`

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

Registry credentials
- Registry credentials are stored separately from API tokens at `~/.config/ae/registries.yaml`.
- List configured entries with `ae registry list`.
- Common login flows:
  - Docker Hub: `ae registry login custom --registry docker.io --username <you> --password <token>`
  - GHCR: `ae registry login ghcr --username <you> --token <PAT>` or rely on `gh auth token`
  - GCR / Artifact Registry: `ae registry login gcr --use-gcloud --gcr-host us.gcr.io`
  - ECR: `ae registry login ecr --use-aws --region us-east-1 --account-id 123456789012`
  - Custom: `ae registry login custom --registry registry.example.com --username user --password secret`
- Refresh short-lived provider credentials with `ae registry refresh`, or scope it with `--provider gcr` / `--provider ecr`.
- `ae plan` warns when a private image host appears to need registry credentials but no matching `registries.yaml` entry is configured.

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

HA control-plane mode (`k1s-ha-core`)
- `make k1s-ha-core` is the strict-CRI HA core-node profile. It is intended to be operationally interchangeable with `k1s-core` at the node role level, but it switches the node onto shared-authority HA mode instead of the single-host/dev bootstrap path.
- `k1s-core` remains the single-host and dev-oriented profile. `k1s-ha-core` does not replace it.
- Canonical bootstrap sequence: [HA Cluster Bring-Up: bootstrap sequence](ha-cluster-bring-up.html)
- Exact command readout: [HA Cluster Bring-Up: exact command readout](ha-cluster-bring-up.html#ha-command-readout)
- Required env before startup:
  - `AE_CONTROLLER_ID`
  - `AE_CONTROLLER_ADVERTISE_ADDR`
  - `AE_ETCD_ENDPOINTS`
  - `AE_ETCD_PREFIX`
  - `AE_NATS_URL`
- `k1s-ha-core` defaults:
  - `AE_HA_MODE=1`
  - `AE_STATE_BACKEND=etcd`
  - `AE_TRANSPORT_BACKEND=nats-js`
  - `AE_RUNTIME_BACKEND=cri`
  - `AE_INFRA_BACKEND=cri`
  - `AE_APISHIM_MODE=cri`
  - `AE_ETCD_MAINTENANCE_ENABLE=0`
  - `AE_APISHIM_ETCD_ENDPOINTS` defaults from `AE_ETCD_ENDPOINTS` when unset
- `k1s-ha-core` behavior:
  - does not auto-start local singleton `etcd`, NATS, or Postgres
  - does not start the controller with `--watch`
  - treats shared `etcd` controller state as authority; local `specs/` import is not the HA desired-state path
  - keeps `AE_APISHIM_DB` as compatibility storage only; it is not HA authority
  - still starts the controller, apishim, and the core ingress/core-proxy sidecars expected on a core node
  - allows `AE_DEV_LOCAL=1` for lab convenience, but defaults to operator-safe values with local singleton services and docs extras off
- The new [HA Cluster Bring-Up page](ha-cluster-bring-up.html) owns the numbered 3-controller bootstrap sequence, first validation, and first snapshot checkpoint. This runbook keeps the profile contract, installed-service surface, recovery, and upgrade procedures.

### Exact HA Commands {#ha-exact-commands}

- Full strict-CRI HA command readout: [HA Cluster Bring-Up: full command readout](ha-cluster-bring-up.html#ha-command-readout)
- Reduced one-box regression lane: [HA Cluster Bring-Up: one-box readout](ha-cluster-bring-up.html#ha-command-readout-one-box) and [HA Closeout: reduced-harness evidence](ha-closeout.html)
- Role mapping for the HA lane:
  - controllers use `make k1s-ha-core`
  - hub nodes still use `make k1s-core-node`
  - gateways still use `make k1s-edge-core-cri`
  - edge workers still use `make k1s-edge-node`

- Installed-service surface:
  - `make install-ha-core-systemd` installs the HA node-role unit, env file, and wrapper.
  - `make uninstall-ha-core-systemd` removes the unit and wrapper while leaving `/etc/ae/ha-core.env` in place.
  - The install surface writes:
    - `/etc/systemd/system/ae-ha-core.service`
    - `/etc/ae/ha-core.env`
    - `/usr/local/bin/ae-ha-core-service`
  - The wrapper sources `/etc/ae/ha-core.env`, forces the operator-safe `k1s-ha-core` defaults, and launches the repo's `scripts/dev/run_profile.sh k1s-ha-core` path without enabling controller `--watch`.
  - Under the hood the same actions are available as `bash scripts/install.sh ha-core-install` and `bash scripts/install.sh ha-core-uninstall`.
- HA helpers for this profile:
  - Preflight: `PYTHONPATH=src python scripts/dev/ha_core_preflight.py`
  - Snapshot save: `PYTHONPATH=src python scripts/dev/etcd_snapshot.py --runner auto save --output state/backups/ha-$(date +%Y%m%d-%H%M%S).db`
  - Snapshot status: `PYTHONPATH=src python scripts/dev/etcd_snapshot.py --runner auto status --input state/backups/ha-20260318-120000.db`
  - Snapshot restore: `PYTHONPATH=src python scripts/dev/etcd_snapshot.py --runner auto restore --input state/backups/ha-20260318-120000.db --data-dir state/etcd-restore`
  - Leader failover drill: `PYTHONPATH=src python scripts/dev/ha_core_drills.py leader-failover --command 'systemctl restart ae-controller' --etcd-endpoints http://10.0.0.11:2379,http://10.0.0.12:2379,http://10.0.0.13:2379 --etcd-prefix /k1s/prod --require-controller-change`
  - External-etcd restart drill: `PYTHONPATH=src python scripts/dev/ha_core_drills.py etcd-restart --command 'ssh etcd-a sudo systemctl restart etcd' --metrics-url http://127.0.0.1:9108/metrics --etcd-endpoints http://10.0.0.11:2379,http://10.0.0.12:2379,http://10.0.0.13:2379 --etcd-prefix /k1s/prod`
  - Transport recovery drill: `PYTHONPATH=src python scripts/dev/ha_core_drills.py transport-recovery --command 'systemctl restart ae-gateway' --metrics-url http://127.0.0.1:9108/metrics --etcd-endpoints http://10.0.0.11:2379,http://10.0.0.12:2379,http://10.0.0.13:2379 --etcd-prefix /k1s/prod --site sfo-edge-01`
  - `etcd-restart` and `transport-recovery` prefer the current leader's metrics endpoint when `--etcd-endpoints` and `--etcd-prefix` are supplied; use that leader-aware mode for HA validation after failover.
- Backup boundary:
  - `ae backup` remains the SQLite/specs-oriented single-node backup path.
  - HA etcd backup and restore should use `scripts/dev/etcd_snapshot.py`.
- HA runtime behavior:
  - in HA mode, controllers stop importing local `specs/`, followers reject leader-only mutation with `not_leader`, and only the elected controller reconciles or publishes mutating work
  - apishim remains usable for read/list/watch, exec, port-forward, session token minting, and authorization review endpoints during leader changes
  - if controller authority is uncertain or `etcd` quorum is lost, the control plane degrades to read-only

HA etcd recovery (`H5b1-etcd-recovery`)
- Controller authority metrics:
  - `ae_controller_is_leader` is `1` only on the elected controller.
  - `ae_controller_epoch` exposes the current etcd-issued controller epoch.
  - `ae_controller_authority_healthy` is `1` when authority is visible and `0` when the controller has lost usable HA authority.
- Failed member replacement (default learner workflow):
  - Inspect cluster health first:
    - `PYTHONPATH=src python scripts/dev/etcd_recovery.py endpoint-status`
    - `PYTHONPATH=src python scripts/dev/etcd_recovery.py member-list`
  - Remove the failed member:
    - `PYTHONPATH=src python scripts/dev/etcd_recovery.py member-remove --member-id <member-id>`
  - Add the replacement as a learner:
    - `PYTHONPATH=src python scripts/dev/etcd_recovery.py member-add --name etcd-d --peer-url http://10.0.0.14:2380`
  - Start the replacement node with the printed `ETCD_INITIAL_CLUSTER`, `ETCD_INITIAL_CLUSTER_STATE`, and `ETCD_INITIAL_ADVERTISE_PEER_URLS` values.
  - Promote after catch-up:
    - `PYTHONPATH=src python scripts/dev/etcd_recovery.py member-promote --member-id <learner-member-id>`
  - Verify the control plane returns to healthy authority:
    - `curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_controller_is_leader|ae_controller_epoch|ae_controller_authority_healthy'`
- Quorum-loss restore from snapshot:
  - Use `scripts/dev/etcd_snapshot.py save` while any healthy member remains.
  - Render the restore plan for a fresh 3-member cluster:
    - `PYTHONPATH=src python scripts/dev/etcd_recovery.py quorum-restore-plan --input state/backups/ha-20260318-120000.db --cluster-token k1s-ha-restore --member etcd-a=http://10.0.0.11:2380 --member etcd-b=http://10.0.0.12:2380 --member etcd-c=http://10.0.0.13:2380`
  - Run the printed restore commands on the three replacement members, then start them with the printed `initial-cluster` settings.
  - Repoint `k1s-ha-core` nodes at the restored quorum and confirm `ae_controller_authority_healthy 1` appears again before resuming mutation.
- Stale-leader isolation and safe rejoin:
  - Isolate the suspected stale leader first; do not allow it to keep talking to nodes or gateways while authority is uncertain.
  - Verify follower takeover with the existing drill surface:
    - `PYTHONPATH=src python scripts/dev/ha_core_drills.py leader-failover --command 'systemctl restart ae-controller' --etcd-endpoints http://10.0.0.11:2379,http://10.0.0.12:2379,http://10.0.0.13:2379 --etcd-prefix /k1s/prod --require-controller-change`
  - Confirm the old leader no longer reports `ae_controller_is_leader 1` and does not resume mutation until it rejoins as a follower.
  - Rejoin the isolated node only after etcd membership and controller authority are healthy again.
- Control-plane node role separation:
  - Prefer dedicated control-plane nodes for controllers, shared etcd members, and shared NATS/JetStream members.
  - Avoid placing etcd quorum roles only on ephemeral worker-focused nodes when the deployment has enough dedicated control-plane capacity.
  - For AMD fabric deployments, keep recovery ownership explicit: controller operators own authority recovery, fabric operators consume the resulting health signals instead of improvising their own failover rules.

HA core rolling upgrades (`H5b2a-core-upgrades`)
- Scope and contract:
  - This slice covers systemd-managed `k1s-ha-core` nodes only.
  - Upgrade one core node at a time.
  - Followers first, leader last.
  - The only supported mixed-build steady state is a temporary two-build window: the current build and one target build.
  - There is no explicit leader-transfer command in this slice; the final leader restart relies on the existing etcd lease failover path.
- Build/version visibility:
  - Controller: `curl -fsS http://127.0.0.1:9108/__ae/version`
  - Apishim: `curl -fsSk https://127.0.0.1:8445/__ae/version`
  - Controller build metric: `curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_controller_build_info|ae_controller_authority_healthy'`
  - Kubernetes `/version` stays unchanged; the HA upgrade helper uses `/__ae/version`.
- Upgrade helper surface:
  - Precheck: `PYTHONPATH=src python scripts/dev/ha_core_upgrade.py precheck --metrics-url http://127.0.0.1:9108/metrics`
  - Per-node plan: `PYTHONPATH=src python scripts/dev/ha_core_upgrade.py node-plan --node-name core-a --expected-version 0.1.3.dev0 --expected-sha <target-sha>`
  - Cluster verify: `PYTHONPATH=src python scripts/dev/ha_core_upgrade.py cluster-verify --node core-a=http://10.0.0.11:9108,https://10.0.0.11:8445 --node core-b=http://10.0.0.12:9108,https://10.0.0.12:8445 --node core-c=http://10.0.0.13:9108,https://10.0.0.13:8445 --expected-version 0.1.3.dev0 --expected-sha <target-sha>`
- Rolling procedure:
  - Run `ha_core_upgrade.py precheck` before touching any node. It validates one visible leader, controller authority health, shared etcd/NATS reachability, and transport backlog/route-ack thresholds.
  - For each follower node:
    - Run `ha_core_upgrade.py node-plan --node-name <node> ...` and follow the printed sequence.
    - Install the target build through the node's normal package or image delivery path.
    - Restart the service: `sudo systemctl restart ae-ha-core.service`
    - Verify controller and apishim `/__ae/version`, then verify `ae_controller_authority_healthy` and `ae_controller_build_info`.
    - If core ingress sidecars are enabled, confirm listeners are back before treating the node as upgraded.
    - Run `ha_core_upgrade.py cluster-verify ...` and confirm the cluster is still within the two-build window.
  - Restart the elected leader last, using the same service restart flow and allowing lease failover to choose the next leader.
  - After the final node restart, run `ha_core_upgrade.py cluster-verify ... --require-converged` and confirm every core node reports the same target build.
- Non-goals in this slice:
  - No remote SSH or multi-node orchestration is provided.
  - No NATS/JetStream member replacement or transport-cluster upgrade sequencing is covered here; that is deferred to the later `H5b2b-hub-transport-upgrades` slice.
  - No single-host 3x HA harness is introduced here.

HA hub transport upgrades (`H5b2b-hub-transport-upgrades`)
- Scope and contract:
  - This slice covers the shared hub NATS/JetStream cluster that `k1s-ha-core` depends on.
  - Upgrade or replace one hub NATS node at a time.
  - Non-meta-leader JetStream nodes first, JetStream meta leader last.
  - The only supported mixed-build steady state is a temporary two-build window: the current hub build and one target build.
  - Edge-site NATS leader upgrades and replacement choreography are not in scope here.
  - The repo does not install or manage the shared NATS service surface in this slice.
- Monitoring and validation surfaces:
  - Per-node NATS monitoring endpoints:
    - `/varz`
    - `/routez`
    - `/jsz?streams=true&consumers=true&config=true`
    - optional `/leafz` when edge leaves are present
  - Controller transport metrics:
    - `ae_gateway_result_replay_backlog`
    - `ae_route_bundle_ack_age_seconds`
    - `ae_site_stale`
    - `ae_js_stream_*`
    - `ae_js_consumer_*`
- Helper surface:
  - Precheck:
    - `PYTHONPATH=src python scripts/dev/ha_transport_upgrade.py precheck --node hub-a=http://10.0.0.21:8222 --node hub-b=http://10.0.0.22:8222 --node hub-c=http://10.0.0.23:8222 --controller-metrics-url http://10.0.0.11:9108/metrics`
  - Per-node plan:
    - `PYTHONPATH=src python scripts/dev/ha_transport_upgrade.py node-plan --node-name hub-a --monitor-url http://10.0.0.21:8222`
  - Cluster verify:
    - `PYTHONPATH=src python scripts/dev/ha_transport_upgrade.py cluster-verify --node hub-a=http://10.0.0.21:8222 --node hub-b=http://10.0.0.22:8222 --node hub-c=http://10.0.0.23:8222 --controller-metrics-url http://10.0.0.11:9108/metrics --expected-version <target-version> --expected-commit <target-commit>`
  - Member replacement plan:
    - `PYTHONPATH=src python scripts/dev/ha_transport_upgrade.py member-replace-plan --failed-node hub-b --replacement-node hub-d --replacement-monitor-url http://10.0.0.24:8222 --controller-metrics-url http://10.0.0.11:9108/metrics`
- Rolling upgrade procedure:
  - Run `ha_transport_upgrade.py precheck` before touching any hub node. It validates route mesh health, JetStream domain and replica posture, and controller-observed transport health.
  - Restart one non-meta-leader hub node at a time, using the operator-managed NATS delivery path for that host.
  - After each restart, confirm `/varz`, `/routez`, and `/jsz?streams=true&consumers=true&config=true` on the restarted node, then run `ha_transport_upgrade.py cluster-verify ...`.
  - Keep controller transport metrics inside thresholds between node restarts; do not proceed while replay backlog, route-ack age, or site-stale signals remain out of bounds.
  - Restart the JetStream meta leader last, using the same verification path.
  - After the final node restart, run `ha_transport_upgrade.py cluster-verify ... --require-converged` and confirm the hub cluster is back to one target build.
- Member replacement procedure:
  - Use `ha_transport_upgrade.py member-replace-plan` to print the fixed checklist for a failed hub member.
  - NATS cluster config, JWT/operator auth, and service restart mechanics remain operator-managed outside the repo.
  - Validate the replacement through NATS monitoring endpoints and the controller transport metrics before treating the hub as healthy again.
- Non-goals in this slice:
  - No edge-site NATS leader upgrades or replacement choreography are included here; those are deferred to the later `H5b2c-edge-transport-upgrades` slice.
  - No JWT/operator auth rotation or `nsc` workflow changes are introduced here.
  - No remote SSH or repo-managed NATS install surface is added here.

HA edge transport upgrades (`H5b2c-edge-transport-upgrades`)
- Scope and contract:
  - This slice covers edge-site gateway restart sequencing plus edge NATS leader restart and replacement choreography after the shared hub transport posture is already healthy.
  - The milestone-defining HA lane is `k1s-edge-core` / `k1s-edge-core-cri`; `k1s-edge` / `k1s-core-edge` remain secondary compatibility rather than exit criteria.
  - Restart gateways one at a time first, then restart the edge NATS leader last.
  - Temporary site degradation during the edge leader restart is acceptable, but bounded recovery is required before moving on.
  - The repo does not install or manage an edge service surface in this slice.
- Monitoring and validation surfaces:
  - Edge NATS monitoring endpoints:
    - `/varz`
    - `/leafz`
  - Controller transport metrics:
    - `ae_site_stale`
    - `ae_gateway_result_replay_backlog`
    - `ae_route_bundle_ack_age_seconds`
    - `ae_site_gateway_last_seen_seconds`
    - `ae_site_gateway_build_info`
- Helper surface:
  - Precheck:
    - `PYTHONPATH=src python scripts/dev/ha_edge_transport.py precheck --site sea=http://10.0.1.21:8223 --controller-metrics-url http://10.0.0.11:9108/metrics --expected-gateway edge-a --expected-gateway edge-b`
  - Per-gateway plan:
    - `PYTHONPATH=src python scripts/dev/ha_edge_transport.py gateway-plan --site-id sea --gateway-node edge-a --controller-metrics-url http://10.0.0.11:9108/metrics --expected-version 0.1.3.dev0 --expected-sha <target-sha>`
  - Site verify:
    - `PYTHONPATH=src python scripts/dev/ha_edge_transport.py site-verify --site sea=http://10.0.1.21:8223 --controller-metrics-url http://10.0.0.11:9108/metrics --expected-gateway edge-a --expected-gateway edge-b --expected-edge-version <target-version> --expected-edge-commit <target-commit> --expected-gateway-version 0.1.3.dev0 --expected-gateway-sha <target-sha> --require-gateway-converged`
  - Leader plan:
    - `PYTHONPATH=src python scripts/dev/ha_edge_transport.py leader-plan --site-id sea --monitor-url http://10.0.1.21:8223 --controller-metrics-url http://10.0.0.11:9108/metrics --expected-version <target-version> --expected-commit <target-commit>`
  - Leader replacement plan:
    - `PYTHONPATH=src python scripts/dev/ha_edge_transport.py leader-replace-plan --site-id sea --failed-node edge-nats-a --replacement-node edge-nats-b --replacement-monitor-url http://10.0.1.22:8223 --controller-metrics-url http://10.0.0.11:9108/metrics --expected-gateway edge-a --expected-gateway edge-b`
- Rolling procedure:
  - Run the shared hub precheck first. Do not start edge-site work while hub route mesh, JetStream replication, or controller-observed transport metrics remain out of bounds.
  - Run `ha_edge_transport.py precheck` for the target site before touching any gateway or the edge leader.
  - For each gateway in the site:
    - Run `ha_edge_transport.py gateway-plan ...` and follow the printed sequence.
    - Restart the operator-managed gateway through the node's existing edge profile/service path.
    - Run `ha_edge_transport.py site-verify ...` and confirm the restarted gateway reports the expected build through `ae_site_gateway_build_info`, its `ae_site_gateway_last_seen_seconds` value is fresh, and site-wide replay plus route convergence metrics are back inside thresholds.
  - After the gateways converge, run `ha_edge_transport.py leader-plan ...` and restart the edge NATS leader last.
  - Run `ha_edge_transport.py site-verify ...` again and confirm `/leafz` reconnect, `ae_site_stale{site=...}` clears, replay backlog drains, and expected gateway builds remain visible before treating the site as healthy.
- Leader replacement procedure:
  - Use `ha_edge_transport.py leader-replace-plan` to print the fixed checklist for a failed edge NATS leader host.
  - Bring up the replacement with the existing operator-managed edge NATS config and service delivery path; this slice does not generate NATS configs or install services.
  - Verify the replacement through `/varz`, `/leafz`, per-gateway telemetry, and controller transport metrics before returning the site to service.
- Non-goals in this slice:
  - No installed edge service surface is added here.
  - No repo-managed NATS config generation or auth rotation workflow is introduced here.
  - No remote SSH or multi-node orchestration is provided.
  - `k1s-edge` / `k1s-core-edge` are not the milestone-defining HA exit lane for this slice.

HA closeout (`H5c-ha-closeout`)
- Canonical audit artifact: [HA Closeout: audit artifact](ha-closeout.html)
- Primary evidence lane:
  - `make lab-vm-ha-validation` is now the preferred umbrella rerun for the checked-in HA validation flow; it wraps `scripts/lab/vm/run_ha_validation.sh`.
  - The default stage set is `stage1`, `retained`, `drain`, `stage2`, `stage2-live`, and `drills`.
  - VM/lab variants can now declare explicit `k1s-ha-core` hosts plus a `ha_control_plane` smoke lane.
  - The checked-in HA closeout topology is `lab/variants/ha-control-plane-core.yaml`.
  - For deeper disruptive validation, use `lab/variants/ha-control-plane-core-drills.yaml`; it enables the optional leader-failover, etcd-restart, and transport-recovery drill hooks through `scripts/lab/vm/ha_drill_actions.sh`.
  - `scripts/lab/vm/smoke_helper.py` remains the lower-level one-shot operator entrypoint for `stage1`, `stage2`, and `drills`. `make lab-vm-smoke` is a thin wrapper around it, and the helper in turn wraps `smoke_v2.py`.
  - `smoke_helper.py` prints live phase/check status from the `runs/<RUN_ID>/...` artifacts and can auto-run `variant_down.sh` on success.
  - Helper-owned wrapper flags are `--teardown on-success|always|never`, `--purge`, `--destroy-network`, and `--console`; pass any remaining `smoke_v2.py` flags after the helper flags.
  - When the variant points HA endpoints at the three HA core VM IPs, `smoke_v2` runs a `ha_shared_infra` phase that boots shared `etcd` plus shared hub NATS/JetStream on those VMs before `k1s-ha-core` bootstrap.
  - The one-shot and drill lanes write `runs/<RUN_ID>/ha_summary.json` as the machine-readable HA evidence artifacts; the supplemental drain stage writes `runs/<RUN_ID>/summary.json`.
  - The `retained` stage and the helper portion of `stage2-live` are wrapper-level stage checks from `run_ha_validation.sh`; do not treat them as standalone `ha_summary.json` artifacts.
  - The lane now assumes prereq-ready qcow2 images. Rebuild and re-verify the images after bootstrap/image changes before retrying first-pass VM failures:
    - `scripts/lab/vm/labctl.sh image build --variant all`
    - `scripts/lab/vm/labctl.sh image verify --variant all`
  - `image verify` now sizes its ephemeral verifier overlay from the backing qcow2 virtual size and rejects undersized requested or stale overlays before boot, instead of letting a truncated first-boot verifier fall through to initramfs/root-device failures.
  - Troubleshooting split:
    - if a fresh VM dies before SSH or in initramfs/root-device discovery, treat it as an image contract problem first and rerun `image build` plus `image verify`
    - if `variant up` fails in guest `cloud-init` with `Could not get APT lock`, tear the run down and rerun once before treating it as a product regression
  - `AE_VM_BOOTSTRAP_AUTOFIX=1` can temporarily re-enable guest-side repair for manual debugging, but it is not the default HA evidence path.
  - The lane reuses the existing HA helper family instead of inventing a second operator contract:
    - `ha_core_preflight.py`
    - `ha_core_upgrade.py`
    - `ha_transport_upgrade.py`
    - `ha_edge_transport.py`
    - optional `ha_core_drills.py` commands when the variant supplies drill commands
- Secondary evidence lane:
  - `make ha-closeout-e2e`
  - `scripts/dev/ha_closeout_e2e.sh` is the supported wrapper for the reduced harness. It prefers the repo venv, exports the Nix `libstdc++` runtime path when available, preflights `import grpc`, and then runs `tests/integration/test_ha_closeout_e2e.py`.
  - This reduced harness is intended for nightly/manual regression, not as the milestone-defining HA lane.
  - It forces `AE_JS_REPLICAS=1`, so it is useful for failover and replay regression but not as transport-fidelity evidence for the shared hub cluster.
- Current release policy (2026-04-15 tag):
  - Treat Debian and NixOS as pooled cross-host verification inputs rather than requiring both hosts to pass the full release matrix independently.
  - Standardize both hosts on `AE_USE_REGISTRY_CACHE=0` for release verification.
  - Require the shared baseline on both hosts: `make env-doctor`, `AE_CRI_REQUIRE_RUNTIME_READY=1 ./scripts/cri_preflight.sh`, `python -m pytest --maxfail=1 --disable-warnings -q`, `make docs-verify`, `make profile-smoke`, and `make ha-closeout-e2e`.
  - Debian owns the authoritative `make e2e` and `make strict-cri-smoke` lanes for this tag.
  - NixOS owns the authoritative `make lab-vm-ha-validation` and full benchmark rerun lanes for this tag.
  - Per-host full-matrix verification becomes the default starting with the next release.
- Closeout rule:
  - The 2026-03-19 HA closeout checkpoint satisfies the original evidence rule: [HA Closeout: current audit status](ha-closeout.html) shows zero `must_fix_before_closeout` gaps, the primary VM/lab lane is green, and the wrapper-backed reduced harness is green.
  - The 2026-04-07 maintenance rerun keeps that claim current: image verification hardening closed the verifier overlay/backing-image mismatch, `make lab-vm-ha-validation` passed with green `stage1`, `retained`, `drain`, `stage2`, `stage2-live`, and `drills` results, and `make ha-closeout-e2e` also passed.
  - Reopen `H5c-ha-closeout` if either evidence lane regresses or [HA Closeout: must-fix status](ha-closeout.html) regains a must-fix gap.
  - On passworded-sudo hosts, run `sudo -v` before invoking `smoke_helper.py`; the helper does not prompt for a password and fails fast if `sudo -n true` is not already warm.

Release notes quick links
- Compatibility matrix: [API Shim Compatibility Matrix](apishim-compatibility-matrix.html) (uploaded with releases)
- OpenAPI artifacts: `/openapi/v2` and `/openapi/v3` are exported during release and attached as `openapi-schemas`.

Observability
- Controller dashboard/API: `http://127.0.0.1:9108` when `--metrics-port` is set.
- Prometheus metrics at `/metrics` (text), recent events via `/events/<app>`.
- HA authority metrics:
  - `ae_controller_is_leader`
  - `ae_controller_epoch`
  - `ae_controller_authority_healthy`
- Integrated HA dashboard:
  - `/dashboard` now includes a dedicated `HA Control Plane` section sourced from `GET /system.ha`.
  - Treat the HA section as the live operator snapshot for authority health, controller-member freshness, etcd reachability summary, transport pressure, site freshness, route acknowledgement age, and HA fence activity.
  - Treat Grafana/Prometheus as the history surface; the built-in dashboard does not fetch or parse `/metrics` directly.
  - `system.ha.issues` drives the dashboard issue banner; investigate those warnings before moving on to disruptive HA operations.
- Optional member-level dashboard probes:
  - `AE_HA_DASHBOARD_PROBES=1` enables background cached probes for the built-in dashboard.
  - `AE_HA_DASHBOARD_PROBE_TIMEOUT_S=2` sets per-probe timeout.
  - `AE_HA_DASHBOARD_ETCD_PROBE_INTERVAL_S=30` sets the background probe interval.
  - `AE_HA_DASHBOARD_HUB_MONITORS=hub-a=http://10.0.0.21:8222,hub-b=http://10.0.0.22:8222,hub-c=http://10.0.0.23:8222` enables hub NATS/JetStream member probing.
  - `AE_HA_DASHBOARD_EDGE_MONITORS=sea=http://10.0.1.21:8223,sfo=http://10.0.2.21:8223` enables edge-site NATS monitor probing.
  - If those env vars are unset, the dashboard still renders controller-known HA state; only the optional member-level etcd/NATS probe rows remain absent.

Dashboard reload vs. restart
- Code/UI changes only (e.g., edits in `src/ae/observability/http_api.py`): `make dashboard-reload`
  - Kills the controller; the supervisor restarts it and picks up code changes.
- Env or port/token changes (anything in `state/env.sh`, `AE_API_*`, `AE_*` flags): `make dashboard-restart`
  - Stops the supervisor, clears any stale lock, then starts fresh so env is re‑sourced.
- Retained HA VM attached-node lane:
  - Bring the retained stack up: `make lab-vm-ha-attached-node-up`
  - On NixOS, this helper path applies the local DNS/TLS bridge automatically.
  - Print public Envoy URLs, per-core ingress smoke, direct diagnostics, and auth hints: `make lab-vm-ha-attached-node-status`
  - After `up`, `getent hosts dash.home.arpa docs.home.arpa api.home.arpa` should resolve to the retained HA ingress IP instead of the local `127.0.0.1` dev mapping.
  - `make lab-vm-ha-attached-node-purge` and `make lab-vm-ha-attached-node-reset` restore the prior localhost-oriented mapping on purge/reset when one was already present; if no prior snapshot exists they remove the retained managed mapping instead.
  - For the exact retained operator flow, live stage-2 helper, auth bootstrap, and cleanup semantics, use [Validated Procedures: retained operator readout](validated-procedures.html#advanced-dashboard-user-test-retained-ha-vm) and [HA Cluster Bring-Up: retained operator context](ha-cluster-bring-up.html).
  - Quick references:
    - full checked-in HA validation: `make lab-vm-ha-validation`
    - retained stage-1 smoke: `make lab-vm-ha-attached-node-workload-smoke`
    - retained-VM "rebuild and restart all" path: `make lab-vm-ha-attached-node-refresh-all`
    - retained stop for later restart: `make lab-vm-ha-attached-node-down`
    - live stage-2 helper: `RUN_ID=<live-ha-core-run> make lab-vm-ha-core-workload-smoke`
    - retained cleanup: `make lab-vm-ha-attached-node-purge`
    - retained reset: `make lab-vm-ha-attached-node-reset`
    - retained hard reset with bridge teardown: `make lab-vm-ha-attached-node-reset LAB_VM_HA_ATTACHED_NODE_ARGS="--rebuild-images --destroy-network"`
  - Persistent retained ingress checks use `ha-web-smoke.home.arpa`, including host-side probes such as `curl --resolve ha-web-smoke.home.arpa:10443:192.168.155.10 ...`.
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
- Gateway replay health: during hub loss or restart, watch `ae_gateway_result_replay_backlog` grow, then confirm it drains and `ae_gateway_result_replay_fail_total` stops increasing after reconnect.
- Route convergence: after a gateway reconnect, confirm `ae_route_bundle_pending` returns to `0` and `ae_route_bundle_ack_age_seconds` falls back near `0`.
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

- Host-controller labs wrapper (current default)
  - Recommended: `make labs-aio-up` (runs the `dev-etcd` profile with Caddy/TLS defaults and writes shim tokens before startup)
  - CLI/API-only variant: `make labs-up`
  - Legacy compose-only path: `./scripts/ensure_apishim_env.sh && docker compose -f ops/dev/labs-aio.yaml up -d`
  - Open https://localhost:8443/playground.html
  - Dashboard (separate host): https://dash.home.arpa:8443/dashboard
  - API shim starts by default on `127.0.0.1:8445` with per-run tokens stored in `state/profiles/dev-etcd/apishim.env` unless `PROFILE_DIR=` overrides the profile path
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
