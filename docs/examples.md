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

- Shows Caddy load-balancing across replicas on a shared Docker network.
- File: `specs/examples/multi-replica-echo.yaml`
- Command:
  - `./scripts/init_demo.sh --demo-echo-mr -y`
  - `make demo ARGS="--demo-echo-mr -y -d"`

### Rollout (ordered)

- Two-step apply for `echo` with rollout `{ strategy: ordered }`.
- Files: `specs/examples/echo.yaml`, `specs/examples/echo-rollout.yaml`
- Command:
  - `./scripts/init_demo.sh --demo-rollout -y`
  - Optional bias: `AE_ROLLOUT_FIRST_WEIGHT=3 ./scripts/init_demo.sh --demo-rollout -y`

### Storage (PV-lite)

- Named Docker volume mounted at `/var/lib/echo`.
- Files: `specs/examples/echo-storage.yaml` and `echo-storage-delete.yaml` (Delete retention)
- Command:
  - `./scripts/init_demo.sh --demo-storage -y`
  - `ae volumes list --app echo` (use `--json` for machine-readable output)
  - `ae delete echo-del --purge` to remove volumes marked `retention: Delete`
