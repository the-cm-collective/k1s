# Operations Runbook

## Bootstrap Demo Environment
- Run `scripts/init_demo.sh` on Ubuntu (requires sudo). The script installs Python dependencies, starts the Caddy/Prometheus stack, builds the demo Docker images, and applies `blue.home.arpa` / `green.home.arpa` manifests.
- Verify ingress locally over HTTPS: `curl -k https://blue.home.arpa:8443/` and `curl -k https://green.home.arpa:8443/`.

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

## Networking and Ingress

- Shared Docker network: App containers and the dev Caddy container are attached to the same user-defined network (default `dev_default`). This allows Caddy to reach app replicas by container DNS names directly without publishing host ports when running multi-replica apps.
- Service discovery (basic): Each replica connects to the shared network with the following DNS aliases:
  - Container name (unique): `ae-<app>-rev<revision>-<index>`
  - App group alias (round‑robin across replicas): `app-<app>`
  - Revision group alias (round‑robin across replicas of the revision): `app-<app>-rev<revision>`
- Ingress upstreams:
  - Single replica with `spec.service.port`: Caddy proxies to the stable host port (`127.0.0.1:<port>`; rewritten to `host.docker.internal:<port>` inside the container).
  - Multi-replica without `spec.service`: Caddy balances across replica DNS endpoints on the shared network.
- Health checks: If a readiness HTTP probe is defined, Caddy emits active `health_checks` against that path to keep upstreams healthy.

Environment variables:
- `AE_DOCKER_NETWORK` (default `dev_default`) — network name to attach new containers and from which Caddy resolves upstreams.
- `AE_CADDY_SITES` (default `state/caddy`) — directory where dynamic site snippets are written (mounted into `/etc/caddy/dynsites`).

## Configs/Secrets Verification

Projection locations:
- Host: `state/projections/<app>-rev<revision>/{config,secret}/...`
- Container (RO): `/var/run/ae/config/<app>`

Quick checks (for the `echo` example):

```
# List files projected into the container
docker ps --filter "label=ae.app=echo" --format '{{.ID}}' | head -n1 \
  | xargs -I{} docker exec {} sh -lc 'ls -R /var/run/ae/config/echo || true'

# Show selected values
docker ps --filter "label=ae.app=echo" --format '{{.ID}}' | head -n1 \
  | xargs -I{} docker exec {} sh -lc 'echo mode=$(cat /var/run/ae/config/echo/config/mode)'
docker ps --filter "label=ae.app=echo" --format '{{.ID}}' | head -n1 \
  | xargs -I{} docker exec {} sh -lc 'echo token=$(cat /var/run/ae/config/echo/secret/token)'
```

CLI helpers:
- `ae config validate -f configs/app-config.yaml`
- `AE_ALLOW_PLAINTEXT_SECRETS=1 ae secret validate -f specs/examples/demo-secret.sops.yaml`

## HTTP API

When `--metrics-port` is set, a lightweight HTTP API is exposed:

- `GET /metrics` — Prometheus text (app/replica gauges)
- `GET /status` — JSON array of app status objects
- `GET /status/<app>` — JSON status for a single app
- `GET /events/<app>?limit=N` — JSON array of recent events

Example: `curl http://127.0.0.1:9108/status | jq .`

Notes
- When `--watch` is enabled, the controller triggers fast reconciles on YAML changes (debounced via `--debounce-ms`, default 200ms). It still performs periodic full reconciles per `--interval`.

## Remote CLI over LAN

You can point the CLI at a remote controller API running on your LAN.

1) Start the controller with mutations and tokens (on the host running the controller):

```
export AE_API_MUTATIONS=1
export AE_API_READ_TOKEN=readtoken
export AE_API_SCALER_TOKEN=scaletoken
export AE_API_ADMIN_TOKEN=admintoken
python -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch
```

2) From another machine on the LAN, use the CLI:

```
# Status (list + single app)
ae --server http://<controller-ip>:9108 --token readtoken status
ae --server http://<controller-ip>:9108 --token readtoken status echo

# Events (paginated)
ae --server http://<controller-ip>:9108 --token readtoken events echo --limit 20

# Scale (requires scaler/admin token)
ae --server http://<controller-ip>:9108 --token scaletoken scale echo --replicas 2

# Delete (requires admin token)
ae --server http://<controller-ip>:9108 --token admintoken delete echo --purge
```

3) Curls (for troubleshooting):

```
curl -H 'Authorization: Bearer readtoken' http://<ip>:9108/status
curl -H 'Authorization: Bearer readtoken' http://<ip>:9108/status/echo
curl -H 'Authorization: Bearer readtoken' http://<ip>:9108/events/echo?limit=20
curl -X POST -H 'Authorization: Bearer scaletoken' -H 'Content-Type: application/json' \
     -d '{"replicas":2}' http://<ip>:9108/scale/echo
curl -X POST -H 'Authorization: Bearer admintoken' http://<ip>:9108/delete/echo?purge=1
```

Notes
- If no tokens are configured, read-only GETs are open; POST endpoints remain gated by AE_API_MUTATIONS=1.
- /status and /events return paginated JSON with a `next` cursor.

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
  - Adds `docs.home.arpa` hosts mapping and fronts the docs via Caddy at `https://docs.home.arpa:8443` alongside blue/green.
  - Note: Dev proxy serves HTTPS only on host port 8443. Use `-k` with curl to skip local CA trust, or import Caddy's local root cert.
  - Prints convenient URLs for blue/green apps and docs.

### Demo Modes

See `docs/demo-modes.md` for common combinations and flags:

- `--demo-standard` — blue/green demo
- `--demo-echo-mr` — multi-replica echo
- `--demo-configs` — configs & secrets demo (echo)
- `--docs-only` — docs + API only

Environment overrides:
- `DOCS_PORT` to change the docs server port (default 9109)

Tear down the demo:

```
./scripts/init_demo.sh --down
```

You will be prompted to remove the hosts entries for `blue.home.arpa`, `green.home.arpa`, and `docs.home.arpa`.


## Storage Verification

PV-lite volumes are created as Docker named volumes with labels `ae.app=<app>` and `ae.storage=1`.

1) Apply the storage demo:

```
./scripts/init_demo.sh --demo-storage -y
```

2) List volumes:

```
ae volumes list --app echo
```

3) Verify in container:

```
docker ps --filter "label=ae.app=echo" --format '{{.ID}}' | head -n1 \
  | xargs -I{} docker exec {} sh -lc 'ls -R /var/lib/echo || true'
```

4) Purge delete to remove volumes with `retention: Delete`:

```
ae delete echo --purge
```
