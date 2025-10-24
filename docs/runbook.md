# Operations Runbook

## Bootstrap Demo Environment
- Run `scripts/init_demo.sh` on Ubuntu (requires sudo). The script installs Python dependencies, starts the Caddy/Prometheus stack, builds the demo Docker images, and applies `blue.home.arpa` / `green.home.arpa` manifests.
- Verify ingress locally with `curl http://blue.home.arpa/` and `curl http://green.home.arpa/`.

## Deploying a Revision
1. Update the manifest under `specs/` and apply with `python -m ae.cli apply -f <path>`.
2. Watch progress with `python -m ae.cli status <app> --events --history 5`; ready state should show `rev=<n>(ready)` and recent `ApplyCompleted` events.
3. If rollout regresses, execute `python -m ae.cli rollback <app>` to revert to the previous revision or specify `--to <rev>` for an explicit target.

## Secrets and Credentials
- Keep sealed secrets in `specs/<name>-secret.sops.yaml`. Verify decryption locally via `sops --decrypt` before deployment.
- Set `AE_ALLOW_PLAINTEXT_SECRETS=1` only for local smoke tests; production environments must provide an `AE_SOPS_BIN` capable of decrypting.
- Registry credentials live in `~/.config/ae/registries.yaml`. Use `python -m ae.cli registry list` to confirm tokens before a rollout.

## Observability
- Summarize fleet health with `python -m ae.cli metrics`. Use `--json` for dashboards.
- Inspect recent reconciliation events with `python -m ae.cli events <app>`; copy critical findings into an incident document.
- Raw SQLite artifacts reside in `state/controller.db`; back them up along with Caddy config and specs for disaster recovery.

## Troubleshooting
- If Caddy reloads fail, check `/tmp/*.caddy` renderings and rerun `python -m ae.cli apply` after corrections.
- Use `pytest tests/integration/test_reconcile_flow.py -q` to ensure the reconciliation pipeline remains healthy after major changes.
# Operations Runbook

## Controller Loop

- Start once: `python -m ae.controller --once --specs specs/`
- Start loop: `python -m ae.controller --loop --interval 5 --specs specs/ --metrics-port 9108`
- With file watching (if watchdog is installed): `python -m ae.controller --loop --watch --specs specs/`

The loop performs a full reconcile on a fixed interval. It handles SIGINT/SIGTERM for clean shutdown and closes the HTTP API server gracefully.

## HTTP API

When `--metrics-port` is set, a lightweight HTTP API is exposed:

- `GET /metrics` — Prometheus text (app/replica gauges)
- `GET /status` — JSON array of app status objects
- `GET /status/<app>` — JSON status for a single app
- `GET /events/<app>?limit=N` — JSON array of recent events

Example: `curl http://127.0.0.1:9108/status | jq .`

Notes
- When `--watch` is enabled, the controller triggers fast reconciles on YAML changes (debounced via `--debounce-ms`, default 200ms). It still performs periodic full reconciles per `--interval`.

## CLI Cheatsheet

- Apply: `python -m ae.cli apply -f specs/examples/echo.yaml`
- Status (wide): `python -m ae.cli status echo --wide`
- Events: `python -m ae.cli events echo --limit 10`
- Metrics: `python -m ae.cli metrics --json`
- Logs:
  - One-shot: `python -m ae.cli logs echo --tail 100`
  - Stream: `python -m ae.cli logs echo --follow`
  - Select container: `--container 0` or `--container echo-rev3-0`
  - Filter by revision: `--revision 3`
  - Time filters: `--since 5m` or `--since-time 2025-10-23T12:00:00Z`

`k1s` provides kubectl-like aliases: `k1s get apps`, `k1s describe app/echo`, `k1s logs app/echo`.

## Resources and Volumes

Manifest fields:

```
spec:
  resources:
    limits: { cpu: 0.25, memory: 256Mi }
  volumes:
    - hostPath: /data
      mountPath: /mnt/data
      readOnly: false
```

Runtime mapping:
- CPU cores → Docker `nano_cpus`
- Memory → Docker `mem_limit` (supports 256Mi, 1Gi, 512M)
- Volumes → Docker bind mounts (ro/rw)

## Backup and Restore

Create a backup tarball of state DB and specs:

```
python -m ae.cli backup create \
  --output backup.tar.gz \
  --db state/controller.db \
  --specs specs/
```

Restore into a target directory (safe; does not overwrite in-place by default):

```
python -m ae.cli backup restore \
  --input backup.tar.gz \
  --into /tmp/restore-dir
```

After restore, repoint `AE_STATE_DB` and `AE_SPECS_DIR` (or run from the restored tree) and reconcile: `python -m ae.controller --once --specs <restored-specs>`.

## Demo Environment

- Run `scripts/init_demo.sh` on Ubuntu (requires sudo). The script:
  - Installs Python deps in a venv (`.venv-demo`) and brings up Caddy/Prometheus via docker-compose.
  - Builds demo app images and applies the blue/green example manifests.
  - Builds static docs and starts a docs web server at `http://127.0.0.1:9109`.
  - Adds `docs.home.arpa` hosts mapping and fronts the docs via Caddy at `http://docs.home.arpa:8080` alongside blue/green.
  - Prints convenient URLs for blue/green apps and docs.

Environment overrides:
- `DOCS_PORT` to change the docs server port (default 9109)

Tear down the demo:

```
./scripts/init_demo.sh --down
```

You will be prompted to remove the hosts entries for `blue.home.arpa`, `green.home.arpa`, and `docs.home.arpa`.
