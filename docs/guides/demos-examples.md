# Demos & Examples

This guide summarizes the demo modes and example manifests available in `specs/examples/`.

Tip
- When running demos that read secrets, pass `--with-secrets-env` to `init_demo.sh` to export `AE_ALLOW_PLAINTEXT_SECRETS=1` and `SOPS_AGE_KEY_FILE=~/.config/ae/keys.txt` for the session.
- Demo/labs auto-register a local node by default (`AE_REGISTER_LOCAL_NODE=1`) so single-node runs behave like a ready Kubernetes node; unset it to enforce strict “no nodes, no scheduling.”

## Demo script overview

The init script can stand up different demo combinations. Use the flags below
to control which apps are applied. Add `-y` to auto-add hosts and `-d` to attach
logs (Ctrl-C to exit).

Note
- `make demo` is the current seeded default: it runs `dev-min` with blue/green sample apps plus docs/api/dashboard.
- When you need a flag-driven `init_demo.sh` mode, use the script directly or `make demo-legacy ARGS="..."`.

## Dashboard Modes

The built-in dashboard has two intended operator presentations. The layout is chosen by profile/topology, not by browser URL alone.

Simple dashboard
- Use this for single-controller local/dev/demo flows:
  - `make demo`
  - `make dev-min`
  - `make dev-min-caddy`
  - `make dev-etcd`
  - `make dev-etcd-caddy`
  - `make labs`
  - local `make loop` / `make run` flows when there is no HA/site-aware topology
- User-test URLs:
  - `https://dash.home.arpa:8443/dashboard`
  - `https://docs.home.arpa:8443/`
- Expected UI:
  - shared `System Graph` remains visible
  - `HA Control Plane` section is hidden
  - `HA Members` legend key is hidden

Advanced dashboard
- Use this for HA, core, edge, and site-aware flows:
  - `make k1s-core`
  - `make k1s-core-caddy`
  - `make k1s-ha-core`
  - `make k1s-edge`
  - `make k1s-core-node`
  - `make k1s-edge-node`
  - `make edge-site`
  - strict-CRI variants inherit the same advanced layout as their base profile
- Access surfaces depend on how the profile was started:
  - local docs-enabled profiles: `http://127.0.0.1:9109/` and `http://127.0.0.1:9108/dashboard`
  - local profiles with Caddy enabled: `https://docs.home.arpa:8443/` and `https://dash.home.arpa:8443/dashboard`
  - retained HA VM harness: `https://docs.home.arpa:10443/` and `https://dash.home.arpa:10443/dashboard`
- Expected UI:
  - shared `System Graph` remains visible
  - `HA Control Plane` section is visible
  - HA/site-aware topology indicators remain visible

Practical note
- Do not use the presence of `System Graph` by itself to classify the page as advanced. It is shared between both layouts.
- For the exact dashboard user-test command readouts, use [Validated Procedures: simple dashboard](validated-procedures.html#simple-dashboard-user-test) and [Validated Procedures: advanced dashboard](validated-procedures.html#advanced-dashboard-user-test-retained-ha-vm).

## Demo modes

### Standard Demo (blue/green)

- Apps: blue and green services behind TLS via Caddy.
- Command:
  - `make demo`
  - `./scripts/init_demo.sh --demo-standard -y`
  - `make demo-legacy ARGS="--demo-standard -y -d"`
- Endpoints:
  - `https://blue.home.arpa:8443/`
  - `https://green.home.arpa:8443/`
  - `https://docs.home.arpa:8443/`
  - `https://dash.home.arpa:8443/dashboard`
- Dashboard expectation:
  - this is the simple layout, not the HA/site layout

### Configs & Secrets Demo (echo)

- Shows `configRefs` and `secretRefs` → env and file projections.
- Files: `configs/app-config.yaml`, `specs/examples/demo-secret.sops.yaml`
- Command:
  - `./scripts/init_demo.sh --with-secrets-env --demo-configs -y`
  - `make demo-legacy ARGS="--demo-configs -y -d"`

### Multi-Replica Echo (echo-mr)

- Shows Caddy load-balancing across replicas on a shared container network.
- File: `specs/examples/multi-replica-echo.yaml`
- Command:
  - `./scripts/init_demo.sh --demo-echo-mr -y`
  - `make demo-legacy ARGS="--demo-echo-mr -y -d"`

### Multi‑Port Echo (echo-multi)

- Demonstrates a Service with multiple ports (HTTP + metrics) and readiness bound to the HTTP port. Ingress routes to the `http` port.
- File: `specs/examples/echo-multiport.yaml`
- Commands:
  - `./scripts/init_demo.sh --demo-echo-multi -y`
  - `make demo-legacy ARGS="--demo-echo-multi -y -d"`
- Endpoint: `https://echo-multi.home.arpa:8443/`

### Hardened Echo (echo-hardened)

- Shows a security-hardened manifest with non-root, read-only root filesystem, seccomp RuntimeDefault, startup/liveness probes, topology spread, PDB hint, and a default-deny NetworkPolicy with DNS/HTTP(S) egress.
- File: `specs/examples/echo-hardened.yaml`
- Commands:
  - `./scripts/init_demo.sh --demo-hardened -y`
  - `make demo-legacy ARGS="--demo-hardened -y -d"`
- Endpoint: `https://echo-hardened.home.arpa:8443/`

### Rollout Demo

- Ordered rollout for `echo` with default prefer-first routing:
  - `./scripts/init_demo.sh --demo-rollout -y -d`
  - `make demo-legacy ARGS="--demo-rollout -y -d"`
  - Optional canary: set `spec.rollout.strategy: canary` with `weight` (and `auto` for ramps).

### Storage (PV-lite)

- Applies an `echo` app with a named volume mounted at `/var/lib/echo`.
- Command:
  - `./scripts/init_demo.sh --demo-storage -y`
  - `make demo-legacy ARGS="--demo-storage -y -d"`
- Inspect volumes:
  - `ae volumes list --app echo`
- Delete with purge to remove volumes marked `retention: Delete`:
  - `ae delete echo --purge`

### Docs Only

- Starts the docs server and API; does not apply any apps.
- Command:
  - `./scripts/init_demo.sh --docs-only -y`
  - `make demo-legacy ARGS="--docs-only -y -d"`
- Endpoints:
  - Docs: `https://docs.home.arpa:8443/` and `http://127.0.0.1:9109/`
  - API:  `https://api.home.arpa:8443/swagger` and `http://127.0.0.1:9108/swagger`
  - Dashboard: `https://dash.home.arpa:8443/dashboard` and `http://127.0.0.1:9108/dashboard`
- Dashboard expectation:
  - this stays on the simple layout because it is still a local single-controller flow

### Helpful Flags & Targets

- `-d, --debug` — attach logs for controller, caddy, prometheus, and site changes.
- `--with-secrets-env` — export `AE_ALLOW_PLAINTEXT_SECRETS=1` and `SOPS_AGE_KEY_FILE=~/.config/ae/keys.txt` for the demo session.
- `--down -y` — tear down: `./scripts/init_demo.sh --down -y`
- `make demo-legacy` — run flag-driven `init_demo.sh` modes through Make
- `make demo-help` — print demo script usage
- `make demo-down` — tear down demo
- `make integ-test` — run integration tests (set `AE_INTEG_RUNTIME=podman` or `docker`)

### Notes

- Demo apps are copied into `state/profiles/demo/specs` and reconciled from that directory. Changes made via CLI/API (like scaling) are temporary unless you edit those files or rerun the demo after updating `specs/examples/`.
- Caddy HTTP: `:8888`, HTTPS: `:8443` (the demo auto-picks free ports if these are busy).
- Hosts entries (added with `-y`) include: `docs.home.arpa`, `api.home.arpa`, `dash.home.arpa`, `blue.home.arpa`, `green.home.arpa`, plus the selected `echo-*` demo hosts (e.g., `echo-mr`, `echo-multi`, `echo-hardened`, `echo-sec`, `echo-exec`, `echo-tcp`, `echo-storage`).
- Dashboard access: `https://dash.home.arpa:8443/dashboard` (or `http://127.0.0.1:9108/dashboard` directly).
- Health checks are disabled by default for compatibility; enable with `AE_CADDY_ACTIVE_HEALTH=1` if your Caddy supports the directive.
- For the advanced dashboard user-test procedure on the HA VM harness, use [HA Cluster Bring-Up](ha-cluster-bring-up.html).

## Example manifests (standalone)

### Multi-Port Service (HTTP + Metrics)

- File: `specs/examples/echo-multiport.yaml`
- Notes:
  - Exporter emits multiple Service ports; Ingress routes to the `http` port.
  - Docker/Podman publish all declared `service.ports[]` when `replicas=1`.
  - You can also generate the example via CLI: `ae examples write --type multiport -o specs/examples/echo-multiport.yaml`

### Security Hardening (seccomp + AppArmor)

- File: `specs/examples/echo-sec-adv.yaml`
- Notes:
  - Default preset uses `seccompProfileType: RuntimeDefault`.
  - For a Localhost profile, place the profile file and set `seccompLocalhostProfile` accordingly.
  - AppArmor annotation is set to `localhost/echo-profile`; adjust based on your cluster policy.
