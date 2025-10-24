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
