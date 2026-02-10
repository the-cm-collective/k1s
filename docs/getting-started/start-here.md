<div class="hero">
  <div class="hero-brand">
    <div class="hero-logo-row">
      <img src="static/k1s-logo-horizontal.svg" alt="k1s logo" class="hero-logo" />
      <span class="hero-pill">Onboarding</span>
    </div>
    <h1>Start Here: k1s Onboarding</h1>
    <p class="hero-tagline">From fresh clone to running demo in minutes. k1s keeps the control plane light, the UI live, and the YAML close to Kubernetes.</p>
    <div class="hero-links hero-links--local">
      <a class="hero-link" href="https://docs.home.arpa:8443/">Docs (TLS)</a>
      <a class="hero-link" href="http://127.0.0.1:9109/">Docs (local)</a>
      <a class="hero-link" href="https://dash.home.arpa:8443/dashboard">Dashboard (TLS)</a>
      <a class="hero-link" href="http://127.0.0.1:9108/dashboard">Dashboard (local)</a>
    </div>
  </div>
  <div class="hero-actions">
    <div class="hero-card">
      <h2>Zero-to-Labs</h2>
      <p>Provision docs, API, dashboard, and sample apps in one shot.</p>
      <pre><code>make demo</code></pre>
    </div>
    <div class="hero-card">
      <h2>Manual Quickstart</h2>
      <p>Install editable, start the controller, then apply echo.</p>
      <pre><code>make install</code></pre>
      <pre><code>make loop</code></pre>
    </div>
    <div class="hero-card">
      <h2>Key CLI Checks</h2>
      <ul>
        <li><code>make apply-sample</code></li>
        <li><code>make status-sample</code></li>
        <li><code>make logs-sample</code></li>
      </ul>
    </div>
  </div>
</div>

This single page gets a new contributor or user from a fresh clone to a running demo, with pointers to the most useful docs and commands.

Note: k1s is under very active development and has not reached a fully stable release. Do not use it in production without thorough security vetting and testing for your environment.

Terminology: k1s "Apps" are Deployment-like workloads; pods are the execution unit, and replicas are the desired pod count (same as Kubernetes); Service VIPs map to Services/ClusterIP.

## Prerequisites
- Python 3.11+
- Podman (preferred) or Docker installed and running
- Optional (CRI/containerd): containerd + CNI + `crictl` (see `docs/ops/runbook.md`)
- Optional (for ingress/docs via Caddy and Prometheus): `docker compose` or `podman compose`
- Optional (for multi-node lab): two Linux hosts/VMs with WireGuard tools and rootful networking

## Option A — Zero‑to‑Labs (automated)
This script provisions a local demo stack, serves docs, starts the controller API, and applies sample workloads (Apps).

1) Run the demo initializer (adds hosts; Ctrl‑C safe):
```
./scripts/init_demo.sh --demo-standard -y -d
# or
make demo ARGS="--demo-standard -y -d"
```
   - Script reference: `scripts/init_demo.sh:1`
   - Flags map: `docs/guides/demos-examples.md:1`

2) Open endpoints:
- Docs: https://docs.home.arpa:8443/ and http://127.0.0.1:9109/
- Dashboard: https://dash.home.arpa:8443/dashboard or http://127.0.0.1:9108/dashboard
- API (Swagger/ReDoc): https://api.home.arpa:8443/swagger and https://api.home.arpa:8443/redoc
- Sample apps: https://blue.home.arpa:8443/ and https://green.home.arpa:8443/

3) Inspect via CLI while the demo runs:
```
python -m ae.cli status --wide
python -m ae.cli logs blue --tail 50
```

4) Tear down when done (stops stack, cleans containers; keeps state):
```
./scripts/init_demo.sh --down -y
# or
make demo-down
```

Tips
- Need only docs + API? Use `./scripts/init_demo.sh --docs-only -y -d` or `make demo ARGS="--docs-only -y -d"`.
- Prefer Make: `make demo ARGS="--demo-standard -y -d"` and `make demo-down` (see `Makefile:1`).
- Want to try multi-node? Follow Option C, then apply `specs/examples/echo-multinode.yaml`.
- Podman registry cache: configure an insecure local registry to avoid HTTPS pull errors and Docker Hub rate limits, or disable the cache.
  ```
  mkdir -p ~/.config/containers/registries.conf.d
  cat > ~/.config/containers/registries.conf.d/local-cache.conf <<'EOF'
  [[registry]]
  location = "localhost:5001"
  insecure = true

  [[registry]]
  location = "localhost:5002"
  insecure = true
  EOF
  ```
  Or run with `AE_USE_REGISTRY_CACHE=0`.

## Option B — Manual Quickstart (hands‑on)
Follow this if you want to see each moving part.

1) Install the package (editable dev mode):
```
python -m pip install -e .[dev]
# Optional: file watching for instant reconciles on spec changes
python -m pip install -e .[watch]
```

2) Start ingress/docs fixtures (optional but recommended):
```
docker compose -f ops/dev/docker-compose.yaml up -d
```
   - Compose file: `ops/dev/docker-compose.yaml:1`

3) Run the controller with API + file watch:
```
python -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch
```
   - For CRI/containerd: `AE_RUNTIME_BACKEND=cri` and follow the CRI section in `docs/ops/runbook.md` for CNI init + smoke checks.

4) Apply and inspect a sample workload (App):
```
python -m ae.cli apply -f specs/examples/echo.yaml
python -m ae.cli status echo --wide --events
python -m ae.cli logs echo --tail 50
```
   - Samples live under: `specs/examples/`

5) Kubectl‑like wrapper (optional; subset of `ae`):
```
k1s get apps
k1s get pods
k1s get services
k1s describe app/echo
k1s logs app/echo --follow --tail 100
```
Note: `k1s` is a thin wrapper over `ae` for common get/describe/logs/rollout/scale/delete flows. For the full surface (plan/export/registry/tls/nodes/volumes/backup), use `ae` directly.

Optional: start the Kubernetes API shim for kubectl/helm parity
```
AE_APISHIM_ENABLE=1 AE_APISHIM_TOKEN=devtoken \
  python -m ae.apishim serve --host 127.0.0.1 --port 8445
kubectl --server=http://127.0.0.1:8445 --token $AE_APISHIM_TOKEN get pods
```
Shim capabilities and gaps live in `docs/reference/apishim-compatibility-matrix.md`.
Note: for TLS, add `--tls`, set `AE_APISHIM_TLS_CERT/KEY`, and use `https://` with `--insecure-skip-tls-verify` or a trusted CA.
Note: dev-only bypass is `AE_APISHIM_ALLOW_ANON=1` (avoid for shared hosts).

## Makefile Helper Commands

Run with `make <target>`. You can override defaults via `VAR=value make <target>`.

Setup and quality
- `make install`: install dev dependencies (`pip -e .[dev]`).
- `make watch`: install file-watching extras (`pip -e .[watch]`).
- `make test`: run unit tests (`pytest -q`).
- `make lint`: run `ruff check` + `mypy src/ae`.
- `make wheel`: build a wheel into `dist/`.

Local dev and samples
- `make dev-up` / `make dev-down`: start/stop dev Docker Compose stack.
- `make down`: stop all dev/demo stacks (best-effort).
- `make loop`: controller reconcile loop (watch mode).
- `make run`: single reconcile pass.
- `make dev-min` / `make dev-etcd` / `make k1s-core` / `make k1s-edge`: runtime profiles with empty specs (no default apps).
- `make dev-min-caddy` / `make dev-etcd-caddy` / `make k1s-core-caddy`: same profiles with TLS hostnames for docs/api/dashboard.
- `make apply-sample`: apply `specs/examples/echo.yaml`.
- `make status-sample`: status for `echo`.
- `make logs-sample`: logs for `echo`.
- `make k8s-smoke`: export + validate sample Kubernetes YAML (no cluster required).
- `make start-here`: build docs and open `docs/site/start-here.html`.
- `make haproxy-update`: regenerate HAProxy config from controller API.
- `make haproxy-watch`: watch/reload HAProxy config from controller API.
- `make install-systemd` / `make uninstall-systemd`: install/remove systemd units.
- `make install-docs-service` / `make uninstall-docs-service`: install/remove docs service.
- `make secrets-seal-demo`: run the sealed-secret demo helper.

Docs, labs, and playground
- `make docs`: combine snapshots (if present), regenerate charts, build docs.
- `make docs-export`: build non-interactive HTML into `docs/export` (override with `DOCS_OUT_DIR=`).
- `make docs-wiki-export`: export wiki-friendly HTML into `docs/wiki` (override with `WIKI_OUT=`).
- `make docs-watch`: rebuild docs when `combined/combined.csv` changes.
- `make labs-up` / `make labs-down`: docs + playground via compose (controller runs on host).
- `make labs-aio-up` / `make labs-aio-down`: all-in-one labs stack (controller + apishim + docs).
- `make labs-k3d-up` / `make labs-k3d-down`: bring up/down local k3d cluster for labs.
- `make labs-apishim-env`: print apishim tokens from `state/profiles/labs/apishim.env`.
- `make apishim-smoke`: quick API shim health check on port 8445.
- `make shim-helm-demo`: run the helm shim demo helper.

Demo workflows
- `make demo`: run the playground labs demo (`--labs --labs-token`; podman backend, plaintext secrets allowed).
- Demo note: `AE_REGISTER_LOCAL_NODE=1` is set by default in demos/labs so the controller registers a local node for scheduling; unset to require explicit node registration.
- `make demo-help`: show demo script help.
- `make demo-down`: tear down demo stacks.
- `make reg-cache-reset`: clear local registry cache used by demos.
- `make demo-hardened`: run hardened demo flow.
- `make demo-reset`: reset demo/labs state and prune volumes.
- `make dashboard-reload`: reload controller under the dashboard supervisor.
- `make dashboard-restart`: restart the supervisor and reload.

Integration and e2e
- `make integ-test`: integration tests (`pytest -q tests/integration/`).
- `make e2e` / `make e2e-multiport`: run the multiport e2e script.

Benchmarks (memory + runtime tooling)
- `make bench-mem-k1s`: snapshot k1s memory.
- `make bench-mem-k3s`: snapshot k3s memory.
- `make bench-mem-debug`: quick sanity-check run with debug artifacts.
- `make bench-mem-agg`: aggregate latest snapshot under a label.
- `make bench-mem-matrix-k1s`: run k1s replica matrix snapshots.
- `make bench-mem-combine`: combine snapshots into `combined/*`.
- `make bench-mem-verify`: verify a snapshot and print per-container split.
- `make bench-k3s-up` / `make bench-k3s-down`: manage a k3s bench cluster.
- `make bench-mem-matrix-k3s`: run k3s replica matrix snapshots.
- `make bench-mem-rollout-k1s`: run k1s rollout snapshots.
- `make bench-mem-rollout-k3s`: run k3s rollout snapshots.
- `make bench-mem-plot`: render benchmark charts.
- `make bench-mem-e2e-k3s-sudo`: full k3s e2e (matrix + rollout + charts) with sudo.
- `make bench-mem-e2e-k1s`: full k1s e2e (matrix + rollout + charts).
- `make bench-mem-e2e-k1s-sudo`: k1s e2e with sudo snapshots.
- `make bench-mem-e2e-k1nd`: k1nd (k1s-in-Docker) e2e.
- `make bench-mem-e2e-k1nd-sudo`: k1nd e2e with sudo snapshots.
- `make bench-mem-e2e-k1nd-quick`: fast k1nd profile.
- `make bench-mem-e2e-k1nd-resume-rollout`: resume only the rollout phase.
- `make bench-mem-e2e-k1nd-down`: k1nd e2e then tear down compose.
- `make bench-mem-e2e-all`: run all baseline suites.
- `make bench-mem-e2e-minimal`: minimal baseline suite.
- `make bench-watch-runtime`: live runtime debug snapshotter.
- `make bench-mem-e2e-baselines`: run baseline suite matrix.
- `make bench-mem-e2e-baselines-sudo`: baseline suite with sudo.
- `make bench-mem-docs`: combine + plot + rebuild docs.
- `make bench-fix-perms`: normalize artifact permissions.
- `make bench-mem-backfill`: backfill missing summary.json + rebuild docs.
- `make bench-engines-clear`: stop/remove all containers (dangerous).
- `make bench-state-clean`: remove benchmark-only state (`state/bench-*`).
- `make dev-state-clean`: wipe full `state/` (requires `CONFIRM=1`).
- `make bench-mem-backfill-oci`: add OCI runtime metadata and recompute charts.
- `make bench-mem-backfill-oci-latest`: backfill OCI metadata for latest label only.
- `make bench-mem-finalize-sudo`: finalize benchmarks and normalize perms (sudo).
- `make bench-mem-e2e-k3s`: full k3s e2e (matrix + rollout + charts).
- `make bench-mem-idle-k1s`: idle baseline snapshot for k1s.
- `make bench-mem-idle-k3s`: idle baseline snapshot for k3s.

Images and containers
- `make image-docker`: build controller image with Dockerfile.
- `make image-podman`: build controller image with Containerfile.
- `make push-docker` / `make push-podman`: push controller image.
- `make docker-build-controller`: build controller image (ops/images/controller.Dockerfile).
- `make docker-run-controller`: run controller container with specs/state mounts.

## Option C — Multi-node Lab (controller + worker)
Use this when you want to validate the overlay Service VIP path and scheduler on two hosts.

1) Controller (host A):
```
AE_ENABLE_SERVICE_PROXY=1 AE_SERVICE_PROVIDER=overlay \
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=changeme \
python -m ae.controller --loop --specs specs/ --metrics-port 9108
```

2) Worker agent (host B):
```
AE_CONTROLLER_URL=http://<controller-host>:9110 AE_AGENT_TOKEN=changeme \
AE_NODE_ID=worker-1 python -m ae.node --runtime-backend podman --port 9109 --ensure-pod-net
```

3) Apply and observe:
```
python -m ae.cli apply -f specs/examples/echo-multinode.yaml
ae nodes list
ae status echo-mn --watch
```

Full walkthrough and WireGuard tips: `docs/guides/multinode-lab.md` or `ops/dev/multinode-lab.sh -h`.

## Using the `ae` CLI (greatest hits)
- Apply/inspect lifecycle:
```
ae apply -f <manifest.yaml>
ae status [<app>] --wide --events --json
ae events <app> --limit 20
ae logs <app> --follow --tail 100
ae revisions <app>
ae rollback <app> [--to N]
ae delete <app> [--purge]
```
- Exec/shell and probe history:
```
ae exec <app> -- sh -c 'id'
ae shell <app>
ae history <app> --limit 20
```
Note: `ae shell` (and `ae exec --tty/--stdin`) requires the API shim; set `AE_APISHIM_SERVER` or pass `--apishim`.
- Inventory and services:
```
ae nodes
ae services --json
ae volumes list
```
- Namespace targeting (optional):
```
ae apply -n demo -f <manifest.yaml>
ae apply -n demo --force-namespace -f <manifest.yaml>
AE_NAMESPACE=demo ae status <app>
```
- Planning and K8s helpers:
```
ae plan -f specs/examples/echo.yaml --verbose --json
ae export-k8s -f specs/examples/echo.yaml --namespace demo --preset web-hardened --validate -o specs/examples/echo-k8s.yaml
ae k8s-check -f specs/examples/echo.yaml --policy strict
```
- Registries and TLS:
```
ae registry login ghcr --username <u> --token <pat>
ae registry list
ae registry refresh
ae tls sync --name mycert --input path/to/k8s-secret.yaml
ae tls verify --name mycert --json
ae tls kubesecret --name mycert --namespace demo -o mycert-secret.yaml
```

## Remote CLI & Tokens (optional)
Generate API tokens and point the CLI at a remote controller.
```
python -m ae.cli api tokens --generate --ttl-hours 24 -o .env.api
source .env.api
ae --server http://<controller-ip>:9108 --token $AE_API_READ_TOKEN status
```
Tip: `ae auth remote -o .env.api` also emits shim tokens + `AE_API_MUTATIONS=1`, and `ae auth local -o .env.local` reuses tokens from `state/*.env` when running demos/labs.
Details: `README.md:67` and token management in `docs/ops/runbook.md:1`.

## Documentation Map (most useful first)
- High‑level overview: `docs/getting-started/overview.md:1`
- Operations runbook: `docs/ops/runbook.md:1`
- HTTP API reference: `docs/reference/http-api.md:1`
- Kubernetes API shim compatibility: `docs/reference/apishim-compatibility-matrix.md:1`
- Ingress & TLS: `docs/reference/ingress.md:1`
- Demos & examples: `docs/guides/demos-examples.md:1`
- Architecture deep dive: `docs/reference/architecture.md:1`

## Where things live in the repo
- Controller daemon entry: `src/ae/controller/__main__.py:1`
- CLIs: `src/ae/cli/__main__.py:1`, `src/ae/kctl/__main__.py:1`
- Ingress (Caddy): `src/ae/ingress/`, site fragments under `ops/dev/caddy/sites/`
- Runtimes: `src/ae/runtime/` (Podman default, Docker optional)
- Observability/API: `src/ae/observability/`
- Specs & samples: `specs/`, `specs/examples/`
- Dev stack compose: `ops/dev/docker-compose.yaml:1`

## Troubleshooting
- If Caddy HTTPS ports 8443/8888 are busy, the demo auto‑picks free ports and prints them.
- Dashboard via Caddy is `https://dash.home.arpa:8443/dashboard`; use `http://127.0.0.1:9108/dashboard` if you’re skipping Caddy.
- To rebuild docs locally: `make docs` (builder at `docs/build_docs.py:1`).
- Teardown demo: `./scripts/init_demo.sh --down -y` or `make demo-down`.
- Reset demo state: `./scripts/init_demo.sh --reset` or `make demo-reset`.

Happy shipping!
Want a stricter baseline? Try the hardened demo (non‑root, read‑only, startup/liveness, PDB, PSA labels, NP default‑deny):
```
./scripts/init_demo.sh --demo-hardened -y -d
# or
make demo-hardened
```
