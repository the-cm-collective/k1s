# k1s Minimal Application Engine

Lightweight multi-node application engine with a Kubernetes-compatible API shim.

- Design and roadmap: see `FEAT.md`.
- Multi-node architecture and lab: `MULTINODE.md`, `docs/multinode-lab.md`.
- API compatibility and shim status: `CONFORMANCE.md`, `docs/apishim-compatibility-matrix.md`.
- Operations runbook: see `docs/runbook.md`.
- Ingress/TLS details: see `docs/ingress.md`.
- End-to-end walkthrough: see `docs/e2e.md`.

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

API endpoints (when started with `--metrics-port`): see `docs/http-api.md`.

Multi-container tips:
- Add sidecars under `spec.containers` and init containers under `spec.initContainers`.
- Use `ae logs <app> --container <name>` and `ae exec <app> --container <name> -- <cmd>` to target a specific container.
- Config/Secret file projections are mounted at `/var/run/ae/config/<app>`; sidecars can add custom `projectionMounts` to bind specific subpaths to custom mount points.

Multi-node lab (controller + agents + overlay Service VIPs):
- Controller: `AE_ENABLE_SERVICE_PROXY=1 AE_SERVICE_PROVIDER=overlay AE_AGENT_API_TOKEN=changeme python -m ae.controller --loop --specs specs/ --metrics-port 9108`
- Worker agent on another host: `AE_CONTROLLER_URL=http://<controller>:9110 AE_AGENT_TOKEN=$AE_AGENT_API_TOKEN python -m ae.node --runtime-backend podman --port 9109 --ensure-pod-net`
- Apply the multi-node sample: `python -m ae.cli apply -f specs/examples/echo-multinode.yaml`
- Inspect nodes/placement: `ae nodes list`, `ae status echo-mn --wide --events`
- Full walkthrough: `docs/multinode-lab.md`

Kubernetes API shim (kubectl/helm):
- Start shim (Postgres or SQLite): `AE_APISHIM_TOKEN=devtoken python -m ae.apishim serve --host 127.0.0.1 --port 8445`
- Point kubectl: `kubectl --server=https://127.0.0.1:8445 --token $AE_APISHIM_TOKEN get pods`
- Port-forward and apply work for Deployments/Services/Ingress/HPA/StatefulSet/DaemonSet/Job/CronJob.
- Compatibility matrix and open gaps: `docs/apishim-compatibility-matrix.md`, `CONFORMANCE.md`.

## Kubernetes Export Quick Start

- Render the echo example to Kubernetes YAML and validate:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --ingress-class traefik --validate > k8s.yaml`
- Include ConfigMap/Secret objects, envFrom, and file projections (mounted at `/var/run/ae/config`):
  - `python -m ae.cli export-k8s -f specs/examples/envfrom-and-projection.yaml --namespace demo --emit-configs --emit-secrets --validate > k8s.yaml`
- Harden NetworkPolicy quickly:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --np-preset web --validate > k8s.yaml`
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --np-preset backend --validate > k8s.yaml`
- See `docs/k8s-export.md` for supported fields: startupProbe, image pull options, env/envFrom, projected volumes, PDB/HPA, pod-level security, and more.

## Documentation

- Start here onboarding: `docs/start-here.md`
- High-level overview and getting started: `docs/overview.md`
- Technical architecture and reference: `docs/architecture.md`
- HTTP API reference and UI docs: `docs/http-api.md`
- Configs & Secrets: `docs/configs-secrets.md`
- Demo Modes (flags for init script and Make): `docs/demo-modes.md`
- End-to-end test process: `docs/e2e.md`
- CI examples: `docs/ci-gh-actions.md`

## Remote CLI (over LAN)

You can point the CLI at a controller running on another machine.

Controller (on the host):

```
export AE_API_MUTATIONS=1
export AE_API_READ_TOKEN=readtoken
export AE_API_SCALER_TOKEN=scaletoken
export AE_API_ADMIN_TOKEN=admintoken
python -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch
```

Client (from another machine):

```
# Reads
ae --server http://<controller-ip>:9108 --token readtoken status
ae --server http://<controller-ip>:9108 --token readtoken events echo --limit 20

# Mutations
ae --server http://<controller-ip>:9108 --token scaletoken scale echo --replicas 2
ae --server http://<controller-ip>:9108 --token admintoken delete echo --purge
```

See `docs/runbook.md` → “Remote CLI over LAN” for details and curl examples.

## License

This project is licensed under the Apache License, Version 2.0. See `LICENSE` for full terms, including the patent grant and redistribution requirements.
