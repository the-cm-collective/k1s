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
- `make demo` now starts the playground labs demo by default. Use `make demo ARGS="..."` for the modes below.

## Demo modes

### Standard Demo (blue/green)

- Apps: blue and green services behind TLS via Caddy.
- Command:
  - `./scripts/init_demo.sh --demo-standard -y`
  - `make demo ARGS="--demo-standard -y -d"`
- Endpoints:
  - `https://blue.home.arpa:8443/`
  - `https://green.home.arpa:8443/`

### Configs & Secrets Demo (echo)

- Shows `configRefs` and `secretRefs` → env and file projections.
- Files: `configs/app-config.yaml`, `specs/examples/demo-secret.sops.yaml`
- Command:
  - `./scripts/init_demo.sh --with-secrets-env --demo-configs -y`
  - `make demo ARGS="--demo-configs -y -d"`

### Multi-Replica Echo (echo-mr)

- Shows Caddy load-balancing across replicas on a shared container network.
- File: `specs/examples/multi-replica-echo.yaml`
- Command:
  - `./scripts/init_demo.sh --demo-echo-mr -y`
  - `make demo ARGS="--demo-echo-mr -y -d"`

### Multi‑Port Echo (echo-multi)

- Demonstrates a Service with multiple ports (HTTP + metrics) and readiness bound to the HTTP port. Ingress routes to the `http` port.
- File: `specs/examples/echo-multiport.yaml`
- Commands:
  - `./scripts/init_demo.sh --demo-echo-multi -y`
  - `make demo ARGS="--demo-echo-multi -y -d"`
- Endpoint: `https://echo-mr.home.arpa:8443/`

### Hardened Echo (echo-hardened)

- Shows a security-hardened manifest with non-root, read-only root filesystem, seccomp RuntimeDefault, startup/liveness probes, topology spread, PDB hint, and a default-deny NetworkPolicy with DNS/HTTP(S) egress.
- File: `specs/examples/echo-hardened.yaml`
- Commands:
  - `./scripts/init_demo.sh --demo-hardened -y`
  - `make demo ARGS="--demo-hardened -y -d"`
- Endpoint: `https://echo-hardened.home.arpa:8443/`

### Rollout Demo

- Ordered rollout for `echo` with default prefer-first routing:
  - `./scripts/init_demo.sh --demo-rollout -y -d`
  - `make demo ARGS="--demo-rollout -y -d"`
  - Optional canary: set `spec.rollout.strategy: canary` with `weight` (and `auto` for ramps).

### Storage (PV-lite)

- Applies an `echo` app with a named volume mounted at `/var/lib/echo`.
- Command:
  - `./scripts/init_demo.sh --demo-storage -y`
  - `make demo ARGS="--demo-storage -y -d"`
- Inspect volumes:
  - `ae volumes list --app echo`
- Delete with purge to remove volumes marked `retention: Delete`:
  - `ae delete echo --purge`

### EmptyDir (ephemeral)

- Example manifest using `spec.emptyDirs`.
- File: `specs/examples/echo-emptydir.yaml`
- Notes:
  - Exported K8s YAML uses `emptyDir`.
  - CRI runtime maps to per‑pod host paths under `AE_CRI_EMPTYDIR_ROOT` (default `/var/lib/ae/emptydirs`).

### Docs Only

- Starts the docs server and API; does not apply any apps.
- Command:
  - `./scripts/init_demo.sh --docs-only -y`
  - `make demo ARGS="--docs-only -y -d"`
- Endpoints:
  - Docs: `https://docs.home.arpa:8443/` and `http://127.0.0.1:9109/`
  - API:  `https://api.home.arpa:8443/swagger` and `http://127.0.0.1:9108/swagger`

### Helpful Flags & Targets

- `-d, --debug` — attach logs for controller, caddy, prometheus, and site changes.
- `--with-secrets-env` — export `AE_ALLOW_PLAINTEXT_SECRETS=1` and `SOPS_AGE_KEY_FILE=~/.config/ae/keys.txt` for the demo session.
- `--down -y` — tear down: `./scripts/init_demo.sh --down -y`
- `make demo-help` — print demo script usage
- `make demo-down` — tear down demo
- `make integ-test` — run integration tests (set `AE_INTEG_RUNTIME=podman` or `docker`)

### Notes

- Caddy HTTP: `:8888`, HTTPS: `:8443`.
- Hosts entries (added with `-y`): `blue|green|echo-mr|docs|api.home.arpa` → `127.0.0.1`.
  - Also: `echo-hardened.home.arpa` when running the hardened demo.
  - Dashboard lives under the API host: `https://api.home.arpa:8443/dashboard` (or `http://127.0.0.1:9108/dashboard` directly).
- Health checks are disabled by default for compatibility; enable with `AE_CADDY_ACTIVE_HEALTH=1` if your Caddy supports the directive.

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
