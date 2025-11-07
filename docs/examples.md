## Examples

This page summarizes the example manifests and demo modes available.

### Standard Demo (blue/green)

- Apps: blue and green services behind TLS via Caddy.
- Command:
  - `./scripts/init_demo.sh --demo-standard -y`
  - `make demo ARGS="--demo-standard -y -d"`

### Configs & Secrets (echo)

- Demonstrates `configRefs` and `secretRefs` → env and file projections.
- Files: `configs/app-config.yaml`, `specs/examples/demo-secret.sops.yaml`
- Command:
  - `./scripts/init_demo.sh --demo-configs -y`
  - `make demo` (defaults to `-y --demo-configs`)

### Multi-Replica Echo (echo-mr)

- Shows Caddy load-balancing across replicas on a shared container network.
- File: `specs/examples/multi-replica-echo.yaml`
- Command:
  - `./scripts/init_demo.sh --demo-echo-mr -y`
  - `make demo ARGS="--demo-echo-mr -y -d"`

### Rollout (ordered)

- Two-step apply for `echo` with rollout `{ strategy: ordered }`.
- Files: `specs/examples/echo.yaml`, `specs/examples/echo-rollout.yaml`
- Command:
  - `./scripts/init_demo.sh --demo-rollout -y`
  - Optional canary: set `rollout: { strategy: canary, weight: 3 }` (use `auto` to ramp)

### Storage (PV-lite)

- Named engine volume mounted at `/var/lib/echo`.
- Files: `specs/examples/echo-storage.yaml` and `echo-storage-delete.yaml` (Delete retention)
- Command:
  - `./scripts/init_demo.sh --demo-storage -y`
  - `ae volumes list --app echo` (use `--json` for machine-readable output)
  - `ae delete echo-del --purge` to remove volumes marked `retention: Delete`

### Multi-Port Service (HTTP + Metrics)

- Demonstrates Service with multiple ports and readiness bound to the HTTP port.
- File: `specs/examples/echo-multiport.yaml`
- Notes:
  - Exporter emits multiple Service ports; Ingress routes to the `http` port.
  - Docker/Podman publish all declared `service.ports[]` when `replicas=1`.
  - You can also generate the example via CLI: `ae examples write --type multiport -o specs/examples/echo-multiport.yaml`

### Security Hardening (seccomp + AppArmor)

- Demonstrates runtime hardening with seccomp and AppArmor settings.
- File: `specs/examples/echo-sec-adv.yaml`
- Notes:
  - Default preset uses `seccompProfileType: RuntimeDefault`.
  - For a Localhost profile, place the profile file and set `seccompLocalhostProfile` accordingly.
  - AppArmor annotation is set to `localhost/echo-profile`; adjust based on your cluster policy.
