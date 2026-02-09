# Multi-node Edge Gateway Manual Test

This walkthrough is a manual test pattern for the core hub plus edge gateways. It focuses on a JetStream hub with lightweight edge sites and multiple gateways per site. For the ops-focused runbook, see `docs/ops/core-edge-manual-test.md`.

## Topology
- Site A (LAN): `k1s-core` + one gateway on `sfo-edge-01`.
- Site B (remote): one edge NATS leader with outbound leaf to the hub, two gateways on `sea-edge-02`.
- Transport: JetStream on hub only; gateways use a local SQLite spool for durability.

## Prereqs
- Hub host reachable on TCP `4222` (NATS) and TCP `7422` (leaf node port).
- Remote edge NATS can open outbound TCP to `HUB_PUBLIC:7422`.
- Unique node ids (recommended format: `<site_id>--<node_id>`).

## Start with clear state (recommended)
```
make dev-state-clean CONFIRM=1
```
This wipes `state/` (keeps TLS artifacts) and clears any work queues so you can reuse work ids.
Gateway spools are cleared on exit by default; set `AE_GATEWAY_KEEP_SPOOL=1` to preserve spools for diagnostics.

## Step 1: Start the hub (LAN)
```
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core
```

## Step 2: Start the local site gateway (same LAN)
```
AE_SITE_ID=sfo-edge-01 \
AE_NODE_ID=edge-1 \
AE_GATEWAY_SPOOL_PATH=state/gw-sfo-edge-01--edge-1.db \
make k1s-edge-core
```
Note: `AE_NODE_ID` is auto-scoped to `sfo-edge-01--edge-1` by the gateway.

## Step 3: Register the remote site in hub NATS config
Dev helper (auto-updates hub config and starts a local edge NATS):
```
make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```
Production equivalent:
- Add a `site-<site_id>-uplink` user in `ops/dev/nats-hub.conf`.
- Reload the hub NATS (`nats-server --signal reload`).

## Step 4: Edge NATS leader (same-host vs remote)
Same host (dev helper):
- `make edge-site` already starts an edge NATS leader bound to `127.0.0.1:<EDGE_PORT>`.
- Use `AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224` for the gateways.

Remote host (behind NAT):
- Run an edge NATS server with a leaf connection back to the hub.
- Key settings (from `ops/dev/nats-edge.conf`):
  - Replace `sfo-edge-01` with `sea-edge-02`.
  - Set leaf url to `nats://site-sea-edge-02-uplink:dev@HUB_PUBLIC:7422`.
- Ensure `HUB_PUBLIC` resolves and is reachable on TCP `7422`.

## Step 5: Start two gateways on the remote site
Gateway 1:
```
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4223 \
AE_TRANSPORT_BACKEND=nats-js \
AE_JS_DOMAIN=K1S \
AE_GATEWAY_SPOOL_PATH=$HOME/.local/share/ae/gateway-sea-edge-02--edge-1.db \
python -m ae.gateway
```

Gateway 2:
```
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-2 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4223 \
AE_TRANSPORT_BACKEND=nats-js \
AE_JS_DOMAIN=K1S \
AE_GATEWAY_SPOOL_PATH=$HOME/.local/share/ae/gateway-sea-edge-02--edge-2.db \
python -m ae.gateway
```
Notes:
- Same-host dev: replace `REMOTE_EDGE_NATS:4223` with `127.0.0.1:4224`.
- Remote host: set `REMOTE_EDGE_NATS` to the edge NATS LAN IP/hostname.
- Gateways clear their spool DB on exit unless `AE_GATEWAY_KEEP_SPOOL=1` is set.

Optional stub workers (one per gateway node id):
```
python -m ae.worker_stub --log-level debug --node-id edge-1 --nats-url nats://worker:dev@REMOTE_EDGE_NATS:4223
python -m ae.worker_stub --log-level debug --node-id edge-2 --nats-url nats://worker:dev@REMOTE_EDGE_NATS:4223
```
One-shell helper (gateway + stub worker together):
```
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4223 \
EDGE_START_NATS=0 \
EDGE_WORKER_LOG_LEVEL=debug \
make k1s-edge-core
```
Notes:
- Set `EDGE_START_NATS=0` when the edge NATS is already running elsewhere.
- Omit `EDGE_WORKER_LOG_LEVEL` to run the worker at default log level.
Expected debug output (worker):
```
INFO ae.worker_stub: stub worker connected node_id=sea-edge-02--edge-1 nats=nats://worker:dev@127.0.0.1:4224
DEBUG ae.worker_stub: work received node_id=sea-edge-02--edge-1 work_id=test-edge-1 attempt=1 op=noop site_id=sea-edge-02
DEBUG ae.worker_stub: work completed node_id=sea-edge-02--edge-1 work_id=test-edge-1 attempt=1 status=succeeded
```

## Remote Host Runbook (Site B behind NAT/CGNAT)
This runbook adds the overlay + node agent steps so apishim can exec/port-forward
to pods running on remote hosts. NATS remains control-plane only; exec/port-forward
streams are apishim → node agent.

Prereqs:
- Hub public hostname/IP reachable from Site B on TCP `7422` (NATS leaf).
- Hub WireGuard UDP port reachable from Site B for outbound-only NAT traversal.
- Each host that runs pods must also run `ae.node` with a reachable `AE_AGENT_ENDPOINT`.

### Host A (core/hub)
1. Start the hub:
```
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core
```
2. Ensure the controller agent API is reachable:
```
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=changeme \
python -m ae.controller --loop --specs specs/ --metrics-port 9108
```
3. Configure WireGuard on the hub and bring the interface up.
4. Add the remote edge site in the hub NATS config:
```
make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```

### Host B (remote site)
1. Configure WireGuard with `PersistentKeepalive=25` and AllowedIPs that include:
the WG subnet, the pod CIDR pool, and the service CIDR (if used).
2. Start the edge NATS leader with a leaf connection back to the hub.
3. Start the gateway leader:
```
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4223 \
AE_TRANSPORT_BACKEND=nats-js \
AE_JS_DOMAIN=K1S \
AE_GATEWAY_SPOOL_PATH=$HOME/.local/share/ae/gateway-sea-edge-02--edge-1.db \
python -m ae.gateway
```
4. Start the node agent on each host that runs pods and advertise the WG endpoint:
```
AE_CONTROLLER_URL=http://<core-wg-ip>:9110 AE_AGENT_TOKEN=changeme \
AE_NODE_ID=sea-edge-02--edge-1 \
AE_AGENT_ENDPOINT=http://<siteb-wg-ip>:9109 \
AE_POD_CIDR=10.42.2.0/24 \
python -m ae.node --port 9109 --ensure-pod-net
```
Repeat for additional nodes with unique `AE_NODE_ID` and `AE_POD_CIDR`.

### Validation
1. On the hub, verify WG is up:
```
wg show wg0
```
2. Confirm nodes are registered with endpoints and pod CIDRs:
```
ae nodes --json
```
3. Confirm the hub can reach each node agent:
```
curl http://<siteb-wg-ip>:9109/readyz
```
4. Test exec and port-forward against pods pinned to Site B.

## Validation
```
ae nodes
python -m ae.cli work enqueue --site-id sfo-edge-01 --mode outbox --preferred-node sfo-edge-01--edge-1 --op noop
python -m ae.cli work enqueue --site-id sea-edge-02 --mode outbox --op noop
python -m ae.cli work enqueue --site-id sea-edge-02 --mode outbox --preferred-node sea-edge-02--edge-1 --op noop
curl http://HUB_HOST:8222/leafz
```
Expected:
- Nodes show `sfo-edge-01--edge-1`, `sea-edge-02--edge-1`, `sea-edge-02--edge-2`.
- `leafz` shows one or more active leaf connections.
- Work ledger transitions to `Succeeded` for the test work items.

## Cleanup
- Stop gateways and edge NATS.
- Stop hub services: `make down`

<details>
<summary><strong>k1s-* make helpers</strong></summary>

Quick guidance on the `k1s-*` make helpers and when to use them.

| Target | Transport | Starts | Use case | Pair with |
| --- | --- | --- | --- | --- |
| `k1s-core` | NATS + JetStream | etcd + hub NATS + controller (plus ingress) | Durable hub for edge sites | `k1s-edge-core` |
| `k1s-core-edge` | NATS Core | etcd + hub NATS + controller | Lightweight hub using work.pull | `k1s-edge` |
| `k1s-edge` | NATS Core | edge NATS + gateway + stub worker | Site gateway using work.pull | `k1s-core-edge` |
| `k1s-edge-core` | NATS + JetStream | edge NATS + gateway + stub worker | Site gateway for JetStream hub | `k1s-core` |
| `k1s-core-caddy` | NATS + JetStream | same as `k1s-core` with Caddy enabled | TLS hostnames for docs/api/dashboard | `k1s-edge-core` |

Common options:
- `AE_SITE_ID`, `AE_NODE_ID` set site/node identity. Gateways auto-scope node id with site id.
- `EDGE_INGRESS_MODE` selects `core-proxy`, `core-to-edge-public`, or `edge-local`.
- `EDGE_START_NATS=0` skips starting the local edge NATS container when pointing to a remote edge NATS.
- `EDGE_START_WORKER=0` disables the stub worker.
- `EDGE_WORKER_LOG_LEVEL=debug` enables verbose stub worker logs.
- `AE_GATEWAY_SPOOL_PATH` sets the gateway SQLite spool path (one per gateway).
- `AE_GATEWAY_KEEP_SPOOL=1` preserves the gateway spool DB on exit for diagnostics.
- `AE_NATS_URL` overrides the edge NATS endpoint for gateways.
- `AE_TRANSPORT_BACKEND` is global per controller (`nats-js` or `nats-core`).

Related helper:
- `make edge-site SITE_ID=<site> EDGE_PORT=<port> EDGE_HTTP_PORT=<port>` adds a new edge NATS site in dev.

</details>
