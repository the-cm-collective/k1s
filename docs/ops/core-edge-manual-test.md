Core + Edge Manual Test Runbook (JetStream Hub, Light Edges)

Purpose
- Validate the core hub + edge gateway topology where JetStream runs only on the hub.
- Exercise a local edge on the same LAN as the core and a remote site behind NAT with two gateways.
- Keep edge sites lightweight (core NATS + gateway SQLite spool).

Topology
- Site A (LAN): `k1s-core` + one gateway on `sfo-edge-01`.
- Site B (remote): one edge NATS leader with outbound leaf to the hub, two gateways on `sea-edge-02`.
- Transport: JetStream on hub only; gateways use a local SQLite spool for durability.

Prereqs
- Hub host reachable on TCP `4222` (NATS) and TCP `7422` (leaf node port).
- Remote edge NATS can open outbound TCP to `HUB_PUBLIC:7422`.
- Unique node ids (recommended format: `<site_id>--<node_id>`).

Defaults and Notes
- Transport backend is global per controller. Do not mix JetStream and work.pull in one hub.
- Each gateway connects only to its local edge NATS.
- Only the edge NATS leader maintains the outbound leaf connection to the hub.
- Gateways do not elect a leader; multiple gateways in the same site are peers.

Start with clear state (recommended for repeatable tests)
```
make dev-state-clean CONFIRM=1
```
This wipes `state/` (keeps TLS artifacts) and clears any work queues so you can reuse work ids.
Gateway spools are cleared on exit by default; set `AE_GATEWAY_KEEP_SPOOL=1` to preserve spools for diagnostics.

Step 1: Start the hub (LAN)
```
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core
```

Step 2: Start the local site gateway (same LAN)
```
AE_SITE_ID=sfo-edge-01 \
AE_NODE_ID=edge-1 \
AE_GATEWAY_SPOOL_PATH=state/gw-sfo-edge-01--edge-1.db \
EDGE_WORKER_LOG_LEVEL=debug \
make k1s-edge-core
```
Note: `AE_NODE_ID` is auto-scoped to `sfo-edge-01--edge-1` by the gateway.

Step 3: Register the remote site in hub NATS config
Dev helper (auto-updates hub config and starts a local edge NATS):
```
make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```
Production equivalent:
- Add a `site-<site_id>-uplink` user in `ops/dev/nats-hub.conf`.
- Reload the hub NATS (`nats-server --signal reload`).

Step 4: Edge NATS leader (same-host vs remote)
Same host (dev helper):
- `make edge-site` already starts an edge NATS leader bound to `127.0.0.1:<EDGE_PORT>`.
- Use `AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224` for the gateways.

Remote host (behind NAT):
- Run an edge NATS server with a leaf connection back to the hub.
- Key settings (from `ops/dev/nats-edge.conf`):
  - Replace `sfo-edge-01` with `sea-edge-02`.
  - Set leaf url to `nats://site-sea-edge-02-uplink:dev@HUB_PUBLIC:7422`.
- Ensure `HUB_PUBLIC` resolves and is reachable on TCP `7422`.

Step 5: Start two gateways on the remote site
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
- The spool path must be writable by the gateway process (one DB per gateway).
- Gateways clear their spool DB on exit unless `AE_GATEWAY_KEEP_SPOOL=1` is set.

Optional stub workers (one per gateway node id):
```
python -m ae.worker_stub --log-level debug --node-id edge-1 --nats-url nats://worker:dev@REMOTE_EDGE_NATS:4223
python -m ae.worker_stub --log-level debug --node-id edge-2 --nats-url nats://worker:dev@REMOTE_EDGE_NATS:4223
```
One-shell helpers (gateway + stub worker together):
```
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4223 \
EDGE_WORKER_NATS_URL=nats://worker:dev@REMOTE_EDGE_NATS:4223 \
EDGE_START_NATS=0 \
EDGE_WORKER_LOG_LEVEL=debug \
make k1s-edge-core

AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-2 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4223 \
EDGE_WORKER_NATS_URL=nats://worker:dev@REMOTE_EDGE_NATS:4223 \
EDGE_START_NATS=0 \
EDGE_WORKER_LOG_LEVEL=debug \
make k1s-edge-core
```
Notes:
- Set `EDGE_START_NATS=0` when the edge NATS is already running elsewhere.
- Omit `EDGE_WORKER_LOG_LEVEL` to run the worker at default log level.
Why run stub workers:
- Gateways pull work from the hub and publish it to `k1s.v1.local.work.<node_id>`.
- The stub worker subscribes to that local subject and replies with success, so you can validate the work queue path without running a full runtime on the edge.
- If you want to observe load distribution across gateways, run one stub worker per gateway node id.
Expected debug output (worker):
```
INFO ae.worker_stub: stub worker connected node_id=sea-edge-02--edge-1 nats=nats://worker:dev@127.0.0.1:4224
DEBUG ae.worker_stub: work received node_id=sea-edge-02--edge-1 work_id=test-edge-1 attempt=1 op=noop site_id=sea-edge-02
DEBUG ae.worker_stub: work completed node_id=sea-edge-02--edge-1 work_id=test-edge-1 attempt=1 status=succeeded
```

Validation
Controller CLI context (core host):
```
export AE_STATE_BACKEND=etcd
export AE_ETCD_ENDPOINTS=http://127.0.0.1:2379
export AE_ETCD_PREFIX=k1s/profiles/k1s-core
```
```
ae nodes
python -m ae.cli work enqueue --site-id sfo-edge-01 --mode outbox --preferred-node sfo-edge-01--edge-1 --op noop
python -m ae.cli work enqueue --site-id sea-edge-02 --mode outbox --preferred-node sea-edge-02--edge-2 --op noop
python -m ae.cli work enqueue --site-id sea-edge-02 --mode outbox --preferred-node sea-edge-02--edge-1 --op noop
curl http://HUB_HOST:8222/leafz
```
Expected:
- Nodes show `sfo-edge-01--edge-1`, `sea-edge-02--edge-1`, `sea-edge-02--edge-2`.
- `leafz` shows one or more active leaf connections.
- Gateway logs show a lease acquisition and completed work counts increasing.
- Work ledger transitions to `Succeeded` for the test work items (visible in hub logs via `work_result` entries).

Workload placement smoke tests (manifests)
Use the controller CLI context above.
Node selectors use scoped node ids (`<site_id>--<node_id>`).
Helper manifests:
- `specs/examples/echo-gateway.yaml` targets gateway nodes and spreads across sites.
- `specs/examples/echo-node-sfo-edge-01-edge-1.yaml` pins to `sfo-edge-01--edge-1`.
- `specs/examples/echo-node-sea-edge-02-edge-1.yaml` pins to `sea-edge-02--edge-1`.
- `specs/examples/echo-node-sea-edge-02-edge-2.yaml` pins to `sea-edge-02--edge-2`.
- `specs/examples/echo-node-k1s-core.yaml` targets the core controller node (`role=controller`, `profile=k1s-core`).

Gateway-targeted workload (spread across gateways):
```
python -m ae.cli apply -f specs/examples/echo-gateway.yaml
python -m ae.cli status echo-gateway --wide --events
```

Node-targeted workloads (one per gateway):
```
python -m ae.cli apply -f specs/examples/echo-node-sfo-edge-01-edge-1.yaml
python -m ae.cli apply -f specs/examples/echo-node-sea-edge-02-edge-1.yaml
python -m ae.cli apply -f specs/examples/echo-node-sea-edge-02-edge-2.yaml
python -m ae.cli status echo-node-sfo-edge-01-edge-1 --wide --events
python -m ae.cli status echo-node-sea-edge-02-edge-1 --wide --events
python -m ae.cli status echo-node-sea-edge-02-edge-2 --wide --events
```

Core-targeted workload:
```
python -m ae.cli apply -f specs/examples/echo-node-k1s-core.yaml
python -m ae.cli status echo-node-k1s-core --wide --events
```

Expected:
- Each app reports a `node_id` matching the selector.
- Dashboard workloads appear under the targeted nodes.

Cleanup
Delete test workloads (if applied):
```
python -m ae.cli delete echo-gateway
python -m ae.cli delete echo-node-sfo-edge-01-edge-1
python -m ae.cli delete echo-node-sea-edge-02-edge-1
python -m ae.cli delete echo-node-sea-edge-02-edge-2
python -m ae.cli delete echo-node-k1s-core
```
- Stop gateways and edge NATS.
- Stop hub services: `make down`
