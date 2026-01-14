# Start Here: k1s Onboarding

This single page gets a new contributor or user from a fresh clone to a running demo, with pointers to the most useful docs and commands.

## Prerequisites
- Python 3.11+
- Podman (preferred) or Docker installed and running
- Optional (for ingress/docs via Caddy and Prometheus): `docker compose` or `podman compose`
- Optional (for multi-node lab): two Linux hosts/VMs with WireGuard tools and rootful networking

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
- Want to try multi-node? Run `make demo ARGS="--demo-multinode -y"` once you have a second host/VM reachable (see Option C below).

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

Optional: start the Kubernetes API shim for kubectl/helm parity
```
AE_APISHIM_TOKEN=devtoken python -m ae.apishim serve --host 127.0.0.1 --port 8445
kubectl --server=https://127.0.0.1:8445 --token $AE_APISHIM_TOKEN get pods
```
Shim capabilities and gaps live in `docs/apishim-compatibility-matrix.md`.

## Option C — Multi-node Lab (controller + worker)
Use this when you want to validate the overlay Service VIP path and scheduler on two hosts.

1) Controller (host A):
```
AE_ENABLE_SERVICE_PROXY=1 AE_SERVICE_PROVIDER=overlay AE_AGENT_API_TOKEN=REDACTED