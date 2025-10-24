# k1s Minimal Application Engine

Work-in-progress controller and CLI for a lightweight single-node deployment engine. See `FEAT.md` and `docs/` for design and operations.

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
```

Kubectl-like aliases via `k1s`:

```
k1s get apps
k1s describe app/echo
k1s logs app/echo --follow --tail 100
```

API endpoints (when started with `--metrics-port`): see `docs/http-api.md`.

## Documentation

- High-level overview and getting started: `docs/overview.md`
- Technical architecture and reference: `docs/architecture.md`
- HTTP API reference and UI docs: `docs/http-api.md`
- Configs & Secrets: `docs/configs-secrets.md`
- Demo Modes (flags for init script and Make): `docs/demo-modes.md`

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
