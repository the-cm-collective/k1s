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

One-time non-root CLI setup (for `ae shell`/`ae port-forward` without sudo)
```bash
sudo groupadd -f aecli
sudo usermod -aG aecli "$USER"
newgrp aecli
id -nG | tr ' ' '\n' | grep -x aecli
```
Expected
- `aecli` appears in the current shell groups.
- Core startup syncs `state/profiles/<profile>/apishim.cli.env` as `640 root:aecli`.
- `ae auth local --strict` infers the active profile and prefers `apishim.cli.env` automatically.

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
Postgres (apishim) over WG:
- Bind Postgres to the hub WG IP so edge nodes can reach the apishim store:
```
POSTGRES_BIND_IP=<HUB_WG_IP> POSTGRES_PORT=5432 make k1s-core
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
AE_SITE_ID=sea-edge-02 \
python -m ae.worker_stub --log-level debug --node-id edge-1 --nats-url nats://worker:dev@REMOTE_EDGE_NATS:4223
AE_SITE_ID=sea-edge-02 \
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

Node agents for exec/port-forward (required for shell demo)
Single-host (validated command sequence):
1. Allow the controller to read WireGuard handshakes without sudo prompts:
```bash
WG_BIN=$(command -v wg)
echo "$USER ALL=(root) NOPASSWD: ${WG_BIN} show wg-hub dump" | sudo tee /etc/sudoers.d/k1s-wg-dump
sudo chmod 440 /etc/sudoers.d/k1s-wg-dump
```
2. Start the hub controller with overlay dump support:
```bash
WG_BIN=$(command -v wg) \
  AE_DEV_LOCAL=1 \
  AE_WG_INTERFACE=wg-hub \
  AE_WG_DUMP_CMD="sudo -n ${WG_BIN} show {iface} dump" \
  AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
  make k1s-core
```
3. Start the hub node:
```bash
sudo -E \
  AE_NODE_ID=hub-1 \
  AE_NODE_LABELS="role=hub,site=hub,wg_role=hub,wg_psk=rp,wg_endpoint=192.168.29.143:51820" \
  AE_POD_CIDR=10.42.0.0/24 \
  AE_ROSENPASS_ENABLED=1 \
  AE_ROSENPASS_CONFIG=controller \
  AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-hub \
  AE_ROSENPASS_INTERFACE=wg-hub \
  AE_WG_LISTEN_PORT=51820 \
  AE_WG_ADDRESS=10.255.0.1/32 \
  AE_AGENT_TOKEN=devtoken \
  AE_CONTROLLER_URL=http://127.0.0.1:9110 \
  make k1s-core-node
```
4. Register the edge site and start the edge gateway:
```bash
make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224 \
AE_LOG_LEVEL=debug \
make k1s-edge-core
```
5. Start the edge node (single-host routing adjustments):
```bash
sudo -E \
  AE_WG_INTERFACE=wg-edge \
  AE_ROSENPASS_INTERFACE=wg-edge \
  AE_WG_ADDRESS=10.255.0.3/32 \
  AE_WG_TABLE=off \
  AE_WG_LISTEN_PORT=51821 \
  AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-edge \
  AE_NODE_LABELS="site=sea-edge-02,wg_role=spk,wg_psk=rp" \
  AE_AGENT_TOKEN=devtoken \
  AE_CONTROLLER_URL=http://192.168.29.143:9110 \
  make k1s-edge-node
```
Note
- Replace `192.168.29.143` with your hub host LAN IP.
- For split-host setups that require explicit `AE_AGENT_ENDPOINT`, follow `docs/ops/core-edge-wg-psk.md`.

Workload placement smoke tests (manifests)
Use the controller CLI context above (for example: `source <(ae auth local --strict)`).
Node selectors use scoped node ids (`<site_id>--<node_id>`).
Helper manifests:
- `specs/examples/echo-gateway.yaml` targets gateway nodes and spreads across sites.
- `specs/examples/echo-node-sfo-edge-01-edge-1.yaml` pins to `sfo-edge-01--edge-1`.
- `specs/examples/echo-node-sea-edge-02-edge-1.yaml` pins to `sea-edge-02--edge-1`.
- `specs/examples/echo-node-sea-edge-02-edge-2.yaml` pins to `sea-edge-02--edge-2`.
- `specs/examples/echo-node-k1s-core.yaml` targets the core controller node (`role=controller`, `profile=k1s-core`).
- `docs/site/examples/shell-demo-node-k1s-core.yaml` pins the shell demo to the core controller node (only works if the controller host runs a workload-capable node with `role=controller`).
- `docs/site/examples/shell-demo-node-sfo-edge-01-edge-1.yaml` pins the shell demo to `sfo-edge-01--edge-1`.
- `docs/site/examples/shell-demo-node-sea-edge-02-edge-1.yaml` pins the shell demo to `sea-edge-02--edge-1`.
- `docs/site/examples/shell-demo-node-sea-edge-02-edge-2.yaml` pins the shell demo to `sea-edge-02--edge-2`.
- `docs/site/examples/shell-demo-node-hub.yaml` pins the shell demo to the hub node (`role=hub`, `site=hub`).
Blue/green per-node demo manifests:
- `specs/examples/echo-node-k1s-core-blue.yaml`
- `specs/examples/echo-node-k1s-core-green.yaml`
- `specs/examples/echo-node-sfo-edge-01-edge-1-blue.yaml`
- `specs/examples/echo-node-sfo-edge-01-edge-1-green.yaml`
- `specs/examples/echo-node-sea-edge-02-edge-1-blue.yaml`
- `specs/examples/echo-node-sea-edge-02-edge-1-green.yaml`
- `specs/examples/echo-node-sea-edge-02-edge-2-blue.yaml`
- `specs/examples/echo-node-sea-edge-02-edge-2-green.yaml`

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

Port-forward smoke tests (shell demo, one per node):
```
source <(ae auth local --strict)
ae apply -f docs/site/examples/shell-demo-node-sfo-edge-01-edge-1.yaml
ae apply -f docs/site/examples/shell-demo-node-sea-edge-02-edge-1.yaml
ae apply -f docs/site/examples/shell-demo-node-sea-edge-02-edge-2.yaml
ae apply -f docs/site/examples/shell-demo-node-hub.yaml
```
```
ae port-forward shell-demo-node-sfo-edge-01-edge-1 18081:8080
ae port-forward shell-demo-node-sea-edge-02-edge-1 18082:8080
ae port-forward shell-demo-node-sea-edge-02-edge-2 18083:8080
ae port-forward shell-demo-node-hub 18084:8080
```
Notes:
- Port-forward uses the API shim; run `source <(ae auth local --strict)` so CLI exports `AE_APISHIM_SERVER`, `AE_APISHIM_MINT_TOKEN`, and `AE_APISHIM_CA_BUNDLE` before invoking `ae port-forward`.
- If `source <(ae auth local --strict)` reports `permission denied` for `apishim.cli.env`, refresh group membership (`newgrp aecli` or re-login) and re-run.
- If dashboard modal shell works but CLI returns `spdy upgrade failed: 401`, verify:
  - group membership: `id -nG | tr ' ' '\n' | grep -x aecli`;
  - shared env readability: `ls -l state/profiles/k1s-core/apishim.cli.env state/profiles/k1s-core/apishim.ca.crt`;
  - exported mint auth context: `env | grep -E '^AE_APISHIM_(SERVER|MINT_TOKEN|CA_BUNDLE)='`.

Security notes (production hardening)
- Replace dev NATS credentials (`gateway:dev`, `site-<id>-uplink:dev`) with per-site creds (NKeys/JWT or creds files) and lock down subject permissions.
- Keep the node agent API (`AE_AGENT_TOKEN`) reachable only over WG/LAN; do not expose it publicly.
- Keep the API shim (exec/port-forward) behind TLS with scoped tokens; avoid sharing admin tokens across environments.
- Ensure `AE_ROSENPASS_DIR` permissions are restricted (root-owned, 0700/0600) and avoid writing keys under the repo when running nodes with sudo.

Scale replicas (optional):
```
python -m ae.cli -n default scale echo-node-sea-edge-02-edge-1 --replicas 3
python -m ae.cli -n default scale echo-node-sea-edge-02-edge-2 --replicas 3
python -m ae.cli -n default scale echo-node-sfo-edge-01-edge-1 --replicas 4
```

Core-targeted workload:
```
python -m ae.cli apply -f specs/examples/echo-node-k1s-core.yaml
python -m ae.cli status echo-node-k1s-core --wide --events
```

Blue/green per-node demo apps:
```
python -m ae.cli apply -f specs/examples/echo-node-k1s-core-blue.yaml
python -m ae.cli apply -f specs/examples/echo-node-k1s-core-green.yaml
python -m ae.cli apply -f specs/examples/echo-node-sfo-edge-01-edge-1-blue.yaml
python -m ae.cli apply -f specs/examples/echo-node-sfo-edge-01-edge-1-green.yaml
python -m ae.cli apply -f specs/examples/echo-node-sea-edge-02-edge-1-blue.yaml
python -m ae.cli apply -f specs/examples/echo-node-sea-edge-02-edge-1-green.yaml
python -m ae.cli apply -f specs/examples/echo-node-sea-edge-02-edge-2-blue.yaml
python -m ae.cli apply -f specs/examples/echo-node-sea-edge-02-edge-2-green.yaml
```
```
python -m ae.cli status echo-node-k1s-core-blue --wide --events
python -m ae.cli status echo-node-k1s-core-green --wide --events
python -m ae.cli status echo-node-sfo-edge-01-edge-1-blue --wide --events
python -m ae.cli status echo-node-sfo-edge-01-edge-1-green --wide --events
python -m ae.cli status echo-node-sea-edge-02-edge-1-blue --wide --events
python -m ae.cli status echo-node-sea-edge-02-edge-1-green --wide --events
python -m ae.cli status echo-node-sea-edge-02-edge-2-blue --wide --events
python -m ae.cli status echo-node-sea-edge-02-edge-2-green --wide --events
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
python -m ae.cli delete echo-node-k1s-core-blue
python -m ae.cli delete echo-node-k1s-core-green
python -m ae.cli delete echo-node-sfo-edge-01-edge-1-blue
python -m ae.cli delete echo-node-sfo-edge-01-edge-1-green
python -m ae.cli delete echo-node-sea-edge-02-edge-1-blue
python -m ae.cli delete echo-node-sea-edge-02-edge-1-green
python -m ae.cli delete echo-node-sea-edge-02-edge-2-blue
python -m ae.cli delete echo-node-sea-edge-02-edge-2-green
```
- Stop gateways and edge NATS.
- Stop hub services: `make down`
