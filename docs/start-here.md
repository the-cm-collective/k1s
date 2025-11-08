# Start Here: k1s Onboarding

This single page gets a new contributor or user from a fresh clone to a running demo, with pointers to the most useful docs and commands.

## Prerequisites
- Python 3.11+
- Podman (preferred) or Docker installed and running
- Optional (for ingress/docs via Caddy and Prometheus): `docker compose` or `podman compose`

## Option A — Zero‑to‑Demo (automated)
This script provisions a local demo stack, serves docs, starts the controller API, and applies sample apps.

1) Run the demo initializer (adds hosts; Ctrl‑C safe):
```
./scripts/init_demo.sh --demo-standard -y
```
   - Script reference: `scripts/init_demo.sh:1`
   - Flags map: `docs/demo-modes.md:1`

2) Open endpoints:
- Docs: https://docs.home.arpa:8443/ and http://127.0.0.1:9109/
- API dashboard: https://api.home.arpa:8443/dashboard or http://127.0.0.1:9108/dashboard
- Sample apps: https://blue.home.arpa:8443/ and https://green.home.arpa:8443/

3) Inspect via CLI while the demo runs:
```
python -m ae.cli status --wide
python -m ae.cli logs blue --tail 50
```

4) Tear down when done (stops stack, cleans containers; keeps state):
```
./scripts/init_demo.sh --down -y
```

Tips
- Need only docs + API? Use `./scripts/init_demo.sh --docs-only -y`.
- Prefer Make: `make demo ARGS="--demo-standard -y -d"` and `make demo-down` (see `Makefile:1`).

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

4) Apply and inspect a sample app:
```
python -m ae.cli apply -f specs/examples/echo.yaml
python -m ae.cli status echo --wide --events
python -m ae.cli logs echo --tail 50
```
   - Samples live under: `specs/examples/`

5) Kubectl‑like wrapper (optional):
```
k1s get apps
k1s describe app/echo
k1s logs app/echo --follow --tail 100
```

## Using the `ae` CLI (greatest hits)
- Apply/inspect lifecycle:
```
ae apply -f <manifest.yaml>
ae status [<app>] --wide --events --json
ae logs <app> --follow --tail 100
ae revisions <app>
ae rollback <app> [--to N]
ae delete <app> [--purge]
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
ae tls sync --name mycert --input path/to/k8s-secret.yaml
ae tls verify --name mycert --json
```

## Remote CLI & Tokens (optional)
Generate API tokens and point the CLI at a remote controller.
```
python -m ae.cli api tokens --generate --ttl-hours 24 -o .env.api
source .env.api
ae --server http://<controller-ip>:9108 --token $AE_API_READ_TOKEN status
```
Details: `README.md:67` and token management in `docs/runbook.md:1`.

## Documentation Map (most useful first)
- High‑level overview: `docs/overview.md:1`
- Operations runbook: `docs/runbook.md:1`
- HTTP API reference: `docs/http-api.md:1`
- Ingress & TLS: `docs/ingress.md:1`
- Demo modes: `docs/demo-modes.md:1`
- Examples index: `docs/examples.md:1`
- Architecture deep dive: `docs/architecture.md:1`

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
- To rebuild docs locally: `make docs` (builder at `docs/build_docs.py:1`).
- Teardown and reset demo: `./scripts/init_demo.sh --down -y`.

Happy shipping!

