# k1s Minimal Application Engine

Lightweight multi-node application engine with a Kubernetes-compatible API shim.

- Design and roadmap: see `FEAT.md`.
- Multi-node architecture and lab: `docs/adr/0007-multinode-architecture-scope.md`, `docs/guides/multinode-lab.md`.
- API compatibility and shim status: `CONFORMANCE.md`, `docs/reference/apishim-compatibility-matrix.md`.
- Operations runbook: see `docs/ops/runbook.md`.
- Ingress/TLS details: see `docs/reference/ingress.md`.
- End-to-end walkthrough: see `docs/guides/e2e.md`.

Quick token generation with expiry
- Generate API tokens that expire in 24 hours and write them to a file of exports you can `source`:
  - `python -m ae.cli api tokens --generate --ttl-hours 24 -o .env.api`

## Quickstart

1) Install (editable for dev):

```
python -m pip install -e .[dev]
```

Optional: add file-watching support for instant reconciles on spec changes:

```
python -m pip install -e .[watch]
```

2) Run dev fixtures (optional):

```
docker compose -f ops/dev/docker-compose.yaml up -d
```

3) Start the controller loop:

```
python -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch
```

4) Apply a sample app and inspect:

```
python -m ae.cli apply -f specs/examples/echo.yaml
python -m ae.cli status echo --wide --events
python -m ae.cli logs echo --tail 50
python -m ae.cli exec echo -- -- sh -c 'echo hello from main'
```

Kubectl-like aliases via `k1s`:

```
k1s get apps
k1s describe app/echo
k1s logs app/echo --follow --tail 100
```

API endpoints (when started with `--metrics-port`): see `docs/reference/http-api.md`.

Multi-container tips:
- Add sidecars under `spec.containers` and init containers under `spec.initContainers`.
- Use `ae logs <app> --container <name>` and `ae exec <app> --container <name> -- <cmd>` to target a specific container.
- Config/Secret file projections are mounted at `/var/run/ae/config/<app>`; sidecars can add custom `projectionMounts` to bind specific subpaths to custom mount points.

Multi-node lab (controller + agents + overlay Service VIPs):
- Controller: `AE_ENABLE_SERVICE_PROXY=1 AE_SERVICE_PROVIDER=overlay AE_AGENT_API_TOKEN=REDACTED