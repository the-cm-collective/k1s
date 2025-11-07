# End-to-End Guide

This guide walks an engineer through full system operation on a single host: boot the controller, apply all example manifests, observe readiness/liveness, ingress, storage, rollouts, run portability checks, export K8s YAML, and clean up. Every command below is runnable as-is on a dev machine.

Prerequisites
- Python 3.11+, Podman (default) or Docker, and optional Caddy for ingress.
- Dev install: `python -m pip install -e .[dev]`
- Optional live file watch: `python -m pip install -e .[watch]`
- Optional fixtures (Caddy+Prometheus): `docker compose -f ops/dev/docker-compose.yaml up -d` (use `podman compose` if you prefer Podman)

Podman Quickstart (recommended)
- Create a shared network for multi-replica + ingress DNS:
  - `podman network create devnet`
- Export environment for the controller and ingress tooling:
  - `export AE_RUNTIME_BACKEND=podman`
  - `export AE_PODMAN_NETWORK=devnet`
  - `export AE_CONTAINER_CLI=podman`  # lets the controller reload Caddy via podman exec
- Start fixtures with Podman if desired:
  - `podman compose -f ops/dev/docker-compose.yaml up -d`
- Run the controller with metrics:
  - `python -m ae.controller --loop --watch --specs specs/ --metrics-port 9108`
- Apply a sample and open the dashboard:
  - `python -m ae.cli apply -f specs/examples/echo.yaml`
  - `http://127.0.0.1:9108/dashboard`

Start Controller
- Polling loop with metrics and watch (if watchdog installed):
  - `python -m ae.controller --loop --watch --specs specs/ --metrics-port 9108`
  - UI: `http://127.0.0.1:9108` → status, docs, metrics.

Conventions
- All sample manifests live in `specs/examples/`.
- Use `python -m ae.cli` (aka `ae`). `k1s` provides kubectl-like aliases.

1) Standard Demo (blue/green)
- Apply blue, verify, then swap to green.
  - `python -m ae.cli apply -f specs/examples/blue.yaml`
  - `python -m ae.cli status blue --wide --events`
  - `python -m ae.cli logs blue --tail 50`
  - Switch color: `python -m ae.cli apply -f specs/examples/green.yaml`
  - Verify zero-downtime: `python -m ae.cli status green --wide --events`

2) Echo (Configs & Secrets)
- Demonstrates `configRefs` and `secretRefs` projected into env and files, plus readiness/liveness.
  - Decrypt sample secret for dev only (requires `sops`):
    - `sops --decrypt specs/examples/demo-secret.sops.yaml > state/demo-secret.yaml` (or set `AE_ALLOW_PLAINTEXT_SECRETS=1` and use the sops file directly)
  - `python -m ae.cli apply -f specs/examples/echo.yaml`
  - Inspect:
    - `python -m ae.cli status echo --wide --events`
    - `python -m ae.cli logs echo --tail 100`
  - Ingress (if Caddy running): open `http://echo.home.arpa/` (map host in `/etc/hosts` to 127.0.0.1 for dev).

3) Echo (Resources and hostPath)
- Demonstrates resource limits and a writable hostPath mount.
  - `python -m ae.cli apply -f specs/examples/echo-resources.yaml`
  - `python -m ae.cli status echo-resources --wide --events`
  - Expect `k8s-check` to warn about hostPath RW when exporting to K8s.

4) Echo (Security Hardening)
- Non-root, read-only root FS, `allowPrivilegeEscalation: false`, drop `ALL` caps.
  - `python -m ae.cli apply -f specs/examples/echo-sec.yaml`
  - `python -m ae.cli status echo-sec --wide`

5) Echo (Probes via Exec & TCP)
- Exec probe: `echo-exec.yaml`
  - `python -m ae.cli apply -f specs/examples/echo-exec.yaml`
  - `python -m ae.cli status echo-exec --events`
- TCP probe: `echo-tcp.yaml`
  - `python -m ae.cli apply -f specs/examples/echo-tcp.yaml`
  - `python -m ae.cli status echo-tcp --events`

6) Storage (PV-lite)
- Retain volume: `echo-stateful.yaml`
  - `python -m ae.cli apply -f specs/examples/echo-stateful.yaml`
  - `python -m ae.cli volumes list --app echo-stateful`
- Delete-on-purge volume: `echo-storage-delete.yaml`
  - `python -m ae.cli apply -f specs/examples/echo-storage-delete.yaml`
  - Purge removes named volumes with `retention: Delete`:
    - `python -m ae.cli delete echo-del --purge`

7) Rollout Policy
- Ordered rolling with explicit surge/unavailable:
  - `python -m ae.cli apply -f specs/examples/echo-rollout.yaml`
  - Pause/resume controls:
    - `python -m ae.cli rollout pause echo-rollout`
    - `python -m ae.cli rollout resume echo-rollout`

8) Multi-Replica
- Native (controller-side) multi-replica demo:
  - `python -m ae.cli apply -f specs/examples/multi-replica-echo.yaml`
  - `python -m ae.cli status echo-mr --wide --events`

9) Observability
- Metrics snapshot: `python -m ae.cli metrics`
- Events stream: `python -m ae.cli events echo --limit 20`
- HTTP API: `curl :9108/status`, `curl :9108/metrics`

10) K8s Portability Check (offline)
- Baseline checklist:
  - `python -m ae.cli k8s-check -f specs/examples/echo.yaml`
- Strict policy (CI-like):
  - `python -m ae.cli k8s-check -f specs/examples/multi-replica-echo.yaml --policy strict`
- HPA pre-req advisory using an HPA-oriented sample:
  - `python -m ae.cli k8s-check -f specs/examples/echo-hpa.yaml --policy strict --assume-hpa cpu-util`

11) Export to Kubernetes YAML
- Hardened single-replica echo:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o specs/examples/echo-k8s.yaml`
- Multi-replica hardened export:
  - `python -m ae.cli export-k8s -f specs/examples/multi-replica-echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o specs/examples/multi-replica-echo-k8s.yaml`
- Validate structure (already done via `--validate`) and run server-side dry-run if a cluster is present:
  - `kubectl apply --dry-run=server -f specs/examples/echo-k8s.yaml -n demo`

12) TLS Options (Ingress)
- BYO TLS via Secret material:
  - `python -m ae.cli tls sync --name mycert --input path/to/mycert.yaml --root state/tls`
  - Set only `spec.ingress.tlsSecretName: mycert` in your App manifest; controller resolves PEMs.

13) Cleanup
- Delete apps (keep history): `python -m ae.cli delete <app>`
- Purge with retained volume deletion where `retention: Delete`: `python -m ae.cli delete <app> --purge`
- Stop fixtures: `docker compose -f ops/dev/docker-compose.yaml down`

CI Conformance (reference)
- GitHub workflow `.github/workflows/k8s-conformance.yaml` bootstraps Kind and k3s, exports hardened samples, `kubectl apply --dry-run=server`, and validates with `kubeconform -strict`.

Where to next
- Runbook: `docs/runbook.md`
- Ingress/TLS: `docs/ingress.md`
- HTTP API: `docs/http-api.md`
- Architecture: `docs/architecture.md`

Quick E2E Target
- Run the multi-port end-to-end smoke test (applies demo, checks status, curls ingress):
  - `make e2e` or `make e2e-multiport`
  - Uses `scripts/e2e/multiport.sh` and defaults to Caddy HTTPS `:8443` unless `CADDY_HTTPS_PORT` is set.
