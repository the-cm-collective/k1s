# Multi-node Edge Gateway Manual Test

This walkthrough is a manual test pattern for the core hub plus edge gateways. It focuses on a JetStream hub with lightweight edge sites and multiple gateways per site. For the ops-focused runbook, see `docs/ops/core-edge-manual-test.md`.

## Option A — LAN-only multi-node (no WireGuard)

Use this path when all nodes are on the same LAN and you want typical k8s-style
cluster semantics without a WireGuard overlay.

Prereqs:
- All nodes can reach the controller host on `AE_AGENT_API_PORT` (default `9110`).
- Each worker exposes an `AE_AGENT_ENDPOINT` reachable by the controller.

Step 1: Start the controller (core host)
```
AE_ENABLE_SERVICE_PROXY=1 \
AE_SERVICE_PROVIDER=overlay \
AE_OVERLAY_NET=ae-overlay \
AE_SERVICE_IP_POOL=10.241.0.0/16 \
AE_POD_CIDR_POOL=10.42.0.0/16 \
AE_POD_CIDR_MASK=24 \
AE_AGENT_API_PORT=9110 \
AE_AGENT_API_TOKEN=devtoken \
python -m ae.controller --loop --interval 10 --specs .local/spec/ --metrics-port 9108
```
Notes:
- Ensure the overlay network exists (`AE_OVERLAY_NET`) before starting the controller.
- Optional ingress DNS reachability: set `AE_PODMAN_NETWORK=<net>` (Podman) or `AE_DOCKER_NETWORK=<net>` (Docker) so multi-replica ingress can target container DNS.
- Prefer a dedicated specs directory (for example `.local/spec/`) to avoid reconciling every manifest under `specs/examples/`.
- `--interval` controls the reconcile loop cadence (default is 2s). For ops, set a higher value (for example 10–30s) to reduce noisy logs and churn.
- The controller imports manifests from `--specs` into the registry but always reconciles from the registry. An empty specs dir does not clear existing workloads; use `ae delete <app>` or clean the state DB to start fresh.
- Keep `AE_AGENT_API_TOKEN` private; nodes need it for registration.

Step 2: Start each worker node (same LAN)
```
AE_CONTROLLER_URL=http://<core-host>:9110 \
AE_AGENT_TOKEN=devtoken \
AE_NODE_ID=lan-worker-1 \
AE_NODE_LABELS="role=worker,site=lan" \
AE_AGENT_ENDPOINT=http://<worker-host>:9109 \
AE_AGENT_HEARTBEAT_SECONDS=10 \
AE_POD_CIDR= \
python -m ae.node --port 9109 --ensure-pod-net
```
Notes:
- Leave `AE_POD_CIDR` empty for auto-assignment on first heartbeat.
- Repeat for each node with a unique `AE_NODE_ID` and `AE_AGENT_ENDPOINT`.

Step 3: Validate scheduling
```
ae nodes
ae plan -f specs/examples/echo-multinode.yaml --verbose
python -m ae.cli apply -f specs/examples/echo-multinode.yaml
```

## Option B — Core + edge gateways (WireGuard + NATS)

### Topology
- Site A (LAN): `k1s-core` + one gateway on `sfo-edge-01`.
- Site B (remote): one edge NATS leader with outbound leaf to the hub, two gateways on `sea-edge-02`.
- Transport: JetStream on hub only; gateways use a local SQLite spool for durability.

### Prereqs
- Hub host reachable on TCP `4222` (NATS) and TCP `7422` (leaf node port).
- Remote edge NATS can open outbound TCP to `HUB_PUBLIC:7422`.
- Unique node ids (recommended format: `<site_id>--<node_id>`).

### Start with clear state (recommended)
```
make dev-state-clean CONFIRM=1
```
This wipes `state/` (keeps TLS artifacts) and clears any work queues so you can reuse work ids.
Gateway spools are cleared on exit by default; set `AE_GATEWAY_KEEP_SPOOL=1` to preserve spools for diagnostics.

### Step 1: Start the hub (LAN)
```
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core
```

### Step 2: Start the local site gateway (same LAN)
```
AE_SITE_ID=sfo-edge-01 \
AE_NODE_ID=edge-1 \
AE_GATEWAY_SPOOL_PATH=state/gw-sfo-edge-01--edge-1.db \
make k1s-edge-core
```
Note: `AE_NODE_ID` is auto-scoped to `sfo-edge-01--edge-1` by the gateway.

### Step 3: Register the remote site in hub NATS config
Dev helper (auto-updates hub config and starts a local edge NATS):
```
make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```
Production equivalent:
- Add a `site-<site_id>-uplink` user in `ops/dev/nats-hub.conf`.
- Reload the hub NATS (`nats-server --signal reload`).

### Step 4: Edge NATS leader (same-host vs remote)
Same host (dev helper):
- `make edge-site` already starts an edge NATS leader bound to `127.0.0.1:<EDGE_PORT>`.
- Use `AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224` for the gateways.

Remote host (behind NAT):
- Run an edge NATS server with a leaf connection back to the hub.
- Key settings (from `ops/dev/nats-edge.conf`):
  - Replace `sfo-edge-01` with `sea-edge-02`.
  - Set leaf url to `nats://site-sea-edge-02-uplink:dev@HUB_PUBLIC:7422`.
- Ensure `HUB_PUBLIC` resolves and is reachable on TCP `7422`.

### Step 5: Start two gateways on the remote site
Gateway 1:
```
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4224 \
AE_TRANSPORT_BACKEND=nats-js \
AE_JS_DOMAIN=K1S \
AE_GATEWAY_SPOOL_PATH=$HOME/.local/share/ae/gateway-sea-edge-02--edge-1.db \
python -m ae.gateway
```

Gateway 2:
```
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-2 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4224 \
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
AE_SITE_ID=sea-edge-02 \
python -m ae.worker_stub --log-level debug --node-id edge-1 --nats-url nats://worker:dev@REMOTE_EDGE_NATS:4223
AE_SITE_ID=sea-edge-02 \
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

### Remote Host Runbook (Site B behind NAT/CGNAT)
This runbook adds the overlay + node agent steps so apishim can exec/port-forward
to pods running on remote hosts. NATS remains control-plane only; exec/port-forward
streams are apishim → node agent.

Prereqs:
- Hub public hostname/IP reachable from Site B on TCP `7422` (NATS leaf).
- Hub WireGuard UDP port reachable from Site B for outbound-only NAT traversal.
- Each host that runs pods must also run `ae.node` with a reachable `AE_AGENT_ENDPOINT`. For WG exec/port-forward, use the node WG IP (for example `10.255.0.1:9111` and `10.255.0.2:9112`) so apishim can reach it.

### Host A (core/hub)
1. Start the hub:
```
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy \
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
make k1s-core
```
2. Start the hub node (WireGuard + Rosenpass):
```
AE_WG_ENDPOINT=<PUBLIC_IP>:51820 \
AE_NODE_LABELS="role=hub,site=hub,wg_role=hub,wg_psk=rp" \
AE_ROSENPASS_INTERFACE=wg-hub \
AE_WG_LISTEN_PORT=51820 \
AE_WG_ADDRESS=10.255.0.1/32 \
AE_LOG_LEVEL=debug \
AE_ROSENPASS_LOG_LEVEL=verbose \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://127.0.0.1:9110 \
AE_AGENT_ENDPOINT=http://10.255.0.1:9111 \
AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-hub \
make k1s-core-node
```
3. Configure WireGuard on the hub and bring the interface up.
4. Allow the rootless controller to read WireGuard handshakes (for `/system` + UI overlay status):
```
WG_BIN=$(command -v wg)
echo "$USER ALL=(root) NOPASSWD: ${WG_BIN} show wg-hub dump" | sudo tee /etc/sudoers.d/k1s-wg-dump
sudo chmod 440 /etc/sudoers.d/k1s-wg-dump
```
Ensure the controller can read Rosenpass status by setting:
`AE_ROSENPASS_DIR=/var/lib/ae/rosenpass` (or wherever `rosenpass-status.json` is written).
5. Add the remote edge site in the hub NATS config:
```
make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```
Note
- The agent API must be reachable on `AE_AGENT_API_PORT` (default `9110`). If nodes report “connection refused”, the controller is not listening or the port is blocked.
- If the UI does not show handshakes or Rosenpass state, verify the `wg dump` sudoers rule and `AE_ROSENPASS_DIR` permissions.
- For SPDY exec/port-forward over WG, keep `AE_AGENT_ENDPOINT` on the WG IP, not `127.0.0.1` or a host-only name.

### Host B (remote site)
1. Configure WireGuard with `PersistentKeepalive=25` and AllowedIPs that include:
the WG subnet, the pod CIDR pool, and the service CIDR (if used).
2. Start the edge NATS leader with a leaf connection back to the hub (see `docs/ops/core-edge-wg-psk.md` for the full config steps).
3. Start the gateway leader:
```
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4224 \
AE_LOG_LEVEL=debug \
make k1s-edge-core
```
4. Start the edge node (WG + Rosenpass) on each host that runs pods:
```
sudo -E AE_NODE_ID=edge-1 \
AE_NODE_LABELS="site=sea-edge-02,wg_role=spk,wg_psk=rp" \
AE_ROSENPASS_INTERFACE=wg-edge \
AE_WG_LISTEN_PORT=51821 \
AE_WG_ADDRESS=10.255.0.2/32 \
AE_WG_TABLE=off \
AE_LOG_LEVEL=debug \
AE_ROSENPASS_LOG_LEVEL=verbose \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
AE_AGENT_ENDPOINT=http://10.255.0.2:9112 \
AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-edge \
make k1s-edge-node
```
Repeat for additional nodes with unique `AE_NODE_ID` (and override `AE_POD_CIDR` if needed).
If `ae shell` / `ae port-forward` fails with `Connection refused`, ensure:
- `AE_APISHIM_SERVER` points at the hub apishim (not localhost on the edge host).
- The node advertises a reachable agent endpoint (`AE_AGENT_ENDPOINT` or `--advertise-endpoint`) from the apishim container/host.
Note
- `host.containers.internal` works when apishim runs in a container (podman default). If apishim runs on the host (`AE_APISHIM_MODE=host`), you can use `127.0.0.1` instead.
- `ae auth local` sets `AE_APISHIM_SERVER` to `https://127.0.0.1:8445`. On a remote host, override it to the hub, e.g. `export AE_APISHIM_SERVER=https://<HUB_IP>:8445` (keep the same `AE_APISHIM_TOKEN` from `ae auth local`).

### Same-Host Variant (hub + edge on one box)
Use this when you want to simulate the remote site on the same host as the hub
(like the workflows in `docs/ops/core-edge-manual-test.md` and
`docs/ops/core-edge-wg-psk.md`). The key differences are:
- Keep separate Rosenpass dirs for hub vs edge so WireGuard keys do not collide.

1. Allow the rootless controller to read WireGuard handshakes (for `/system` + UI overlay status):
```
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

Note
- Keep Rosenpass dirs under `/var/lib/ae/` (or another non-repo path) when running nodes with `sudo` to avoid root-owned files under `state/`.

3. Start the hub node:
```
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
3a. Verify the WG interfaces have the expected IPs:
```bash
ip -brief addr show wg-hub wg-edge
```
Expected (example):
- `wg-hub` has `10.255.0.1/32`
- `wg-edge` has `10.255.0.3/32`

4. Register the edge site and start the edge gateway:
```
make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224

AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224 \
AE_LOG_LEVEL=debug \
make k1s-edge-core
```

5. Start the edge node (single-host routing adjustments):
```
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

#### Validation
1. On the hub, verify WG is up:
```
sudo wg show wg-hub
sudo wg show wg-edge
```
2. Confirm nodes are registered with endpoints and pod CIDRs:
```
ae nodes
```
3. Confirm the hub can reach each node agent:
```
curl http://<siteb-wg-ip>:9109/readyz
```
4. Test exec and port-forward against pods pinned to Site B.
```
source <(ae auth local)
ae apply -f docs/site/examples/shell-demo-node-sea-edge-02-edge-1.yaml
ae status shell-demo-node-sea-edge-02-edge-1 --wide --events
ae shell shell-demo-node-sea-edge-02-edge-1 -- /bin/sh
ae port-forward shell-demo-node-sea-edge-02-edge-1 18082:8080
```
Note
- `/system` overlay and Rosenpass indicators are best-effort; missing status usually means the controller cannot read `wg dump` or `rosenpass-status.json`.

#### Troubleshooting exec/port-forward
If `spdy exec failed: [Errno 111] Connection refused` persists:
1. Confirm the node endpoints are reachable from apishim.
```
ae nodes
```
If endpoints show `http://127.0.0.1:9111`/`9112` while apishim is in a container, restart the nodes with `AE_AGENT_ENDPOINT=http://10.255.0.1:9111` and `AE_AGENT_ENDPOINT=http://10.255.0.2:9112` (or `http://host.containers.internal:9111`/`9112` for same-host labs).
2. From the apishim container, probe the node agent:
```
podman exec -it dev-apishim-1 curl -sSf http://host.containers.internal:9112/readyz
```
If apishim runs on the host, use:
```
curl -sSf http://127.0.0.1:9112/readyz
```
3. Verify your CLI points to the correct apishim server:
```
echo "$AE_APISHIM_SERVER"
```
On a remote host, override with `export AE_APISHIM_SERVER=https://<HUB_IP>:8445` and keep the token from `ae auth local`.

### Validation
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

## Option C — Site-to-site NetFS storage over WireGuard/Rosenpass

Use this option when you want persistent PVC-backed storage to work across sites
over the WG/Rosenpass overlay from Option B.

Production default in this guide:
- Primary lane: CSI (`cephfs-rwx`) for Kubernetes-aligned controller/node flows.
- Fallback lane: NFS (`k1s-nfs`) for simpler bring-up and recovery drills.

Related references:
- `docs/reference/storage.md` (NetFS behavior and event reasons)
- `docs/wip/site-to-site-storage.md` (CSI-oriented architecture and phases)
- `docs/ops/core-edge-wg-psk.md` (overlay and Rosenpass runbook)

### Production Pattern (split hosts, hybrid storage lanes)

Prereqs:
- Complete Option B host setup first (hub + remote site + node agents).
- Run node agents with explicit `site` labels (for example `site=hub`, `site=sea-edge-02`).
- Make the same storage registry file available on hub and edge hosts.
  Example path used below: `/etc/ae/storage-provisioners.yaml`.
- Enable NetFS on any node that may mount PVCs: `AE_ENABLE_NETFS=1`.
- Use a shared apishim store reachable from remote sites (Postgres over WG/LAN).

Step 1: Start hub services with shared apishim state and storage registry
```bash
AE_DEV_LOCAL=1 \
EDGE_INGRESS_MODE=core-proxy \
POSTGRES_BIND_IP=<HUB_DB_BIND_IP> \
POSTGRES_PORT=5432 \
AE_STORAGE_PROVISIONERS=/etc/ae/storage-provisioners.yaml \
make k1s-core
```
Notes:
- `POSTGRES_BIND_IP` can be the hub WG IP or another reachable hub IP.
- If binding to a WG IP, ensure the interface/address exists before (re)starting Postgres.

Step 2: Start hub node with WG/Rosenpass + NetFS
```bash
sudo -E \
AE_NODE_ID=hub-1 \
AE_NODE_LABELS="role=hub,site=hub,wg_role=hub,wg_psk=rp,wg_endpoint=<HUB_PUBLIC_IP>:51820" \
AE_ROSENPASS_ENABLED=1 \
AE_ROSENPASS_CONFIG=controller \
AE_ROSENPASS_INTERFACE=wg-hub \
AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-hub \
AE_WG_LISTEN_PORT=51820 \
AE_WG_ADDRESS=10.255.0.1/32 \
AE_ENABLE_NETFS=1 \
AE_APISHIM_DSN=postgresql://shim:shim@<HUB_DB_BIND_IP>:5432/shim \
AE_STORAGE_PROVISIONERS=/etc/ae/storage-provisioners.yaml \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://127.0.0.1:9110 \
AE_AGENT_ENDPOINT=http://10.255.0.1:9111 \
make k1s-core-node
```

Step 3: Start remote edge node with WG/Rosenpass + NetFS
```bash
sudo -E \
AE_NODE_ID=edge-1 \
AE_NODE_LABELS="role=worker,site=sea-edge-02,wg_role=spk,wg_psk=rp" \
AE_ROSENPASS_ENABLED=1 \
AE_ROSENPASS_CONFIG=controller \
AE_ROSENPASS_INTERFACE=wg-edge \
AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-edge \
AE_WG_LISTEN_PORT=51821 \
AE_WG_ADDRESS=10.255.0.2/32 \
AE_WG_TABLE=off \
AE_ENABLE_NETFS=1 \
AE_APISHIM_DSN=postgresql://shim:shim@<HUB_DB_BIND_IP>:5432/shim \
AE_STORAGE_PROVISIONERS=/etc/ae/storage-provisioners.yaml \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
AE_AGENT_ENDPOINT=http://10.255.0.2:9112 \
make k1s-edge-node
```

Step 3a: Prepare kubectl against apishim (for PVC objects)
```bash
source <(ae auth local)
KCTL="kubectl --server=${AE_APISHIM_SERVER} --token=${AE_APISHIM_TOKEN} --insecure-skip-tls-verify"
```

Step 4A: Primary lane (CSI CephFS)
- Ensure `cephfs-rwx` exists in `/etc/ae/storage-provisioners.yaml` with:
  - `type: csi`
  - valid `controllerEndpoint` and `nodeEndpoint`
  - `topologyKeys: ["site"]`
- Apply CSI workload:
```bash
$KCTL apply -f specs/examples/netfs-csi-sea-edge-02-pvc.yaml
python -m ae.cli apply -f specs/examples/netfs-csi-sea-edge-02-edge-1.yaml
```

Step 4B: Fallback lane (NFS)
- Ensure `k1s-nfs` exists in `/etc/ae/storage-provisioners.yaml` or seed it via
  `AE_STORAGE_NFS_SERVER` + `AE_STORAGE_NFS_PATH`.
- Apply NFS workload:
```bash
$KCTL apply -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml
python -m ae.cli apply -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml
python -m ae.cli apply -f specs/examples/netfs-nfs-hub-reader.yaml
```
If `kubectl` is not configured against apishim, use the REST pattern in
`scripts/netfs_smoke.sh` (`PUT /api/v1/namespaces/<ns>/persistentvolumeclaims/<name>`).

Step 5: Validate PVC, attachments, and workload health
```bash
source <(ae auth local)
ae nodes
ae status netfs-nfs-sea-edge-02-edge-1 --wide --events
ae status netfs-nfs-hub-reader --wide --events
ae shell netfs-nfs-sea-edge-02-edge-1 -- cat /data/hello.txt
ae shell netfs-nfs-hub-reader -- cat /data/hello.txt
```

Optional API checks for PVC/PV/VolumeAttachment:
```bash
curl -sk -H "Authorization: Bearer ${AE_APISHIM_TOKEN}" \
  "${AE_APISHIM_SERVER}/api/v1/namespaces/default/persistentvolumeclaims/sea-netfs-pvc"
curl -sk -H "Authorization: Bearer ${AE_APISHIM_TOKEN}" \
  "${AE_APISHIM_SERVER}/apis/storage.k8s.io/v1/volumeattachments"
```
Expected:
- PVC reaches `Bound`.
- NFS lane: remote writer and hub reader see the same file contents.
- CSI lane: `VolumeAttachment.status.attached=true` when `attachRequired=true`.

### Single-Host Lab Pattern (full feature exercise)

Use this when hub + edge run on one host and you want a repeatable feature lab.

Step 1: Bring up the same-host overlay from Option B
- Follow Option B "Same-Host Variant" exactly (hub + edge + WG/Rosenpass).

Step 2: Restart nodes with NetFS + shared DSN enabled
- Hub node: set `AE_ENABLE_NETFS=1` and `AE_APISHIM_DSN`.
- Edge node: set `AE_ENABLE_NETFS=1` and `AE_APISHIM_DSN`.
- Keep distinct `AE_ROSENPASS_DIR`, `AE_WG_INTERFACE`, and `AE_WG_LISTEN_PORT`.

Step 3: Run storage validation workload set
```bash
source <(ae auth local)
KCTL="kubectl --server=${AE_APISHIM_SERVER} --token=${AE_APISHIM_TOKEN} --insecure-skip-tls-verify"
$KCTL apply -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml
python -m ae.cli apply -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml
python -m ae.cli apply -f specs/examples/netfs-nfs-hub-reader.yaml
ae status netfs-nfs-sea-edge-02-edge-1 --wide --events
ae status netfs-nfs-hub-reader --wide --events
ae shell netfs-nfs-sea-edge-02-edge-1 -- sh -lc "echo lab-$(date +%s) > /data/hello.txt && cat /data/hello.txt"
ae shell netfs-nfs-hub-reader -- cat /data/hello.txt
```

Step 4: Failure and recovery drill
1. Stop the edge `ae.node` process (Ctrl+C in the node shell).
2. Re-check status/events:
```bash
ae status netfs-nfs-sea-edge-02-edge-1 --wide --events
ae events netfs-nfs-sea-edge-02-edge-1
```
3. Restart the edge node with the same NetFS/WG env.
4. Re-run read/write checks:
```bash
ae shell netfs-nfs-sea-edge-02-edge-1 -- cat /data/hello.txt
ae shell netfs-nfs-hub-reader -- cat /data/hello.txt
```
Expected event patterns:
- NFS path: `NfsPrereqFailed`, `MountFailed`, or `MountConflict` while degraded.
- CSI path: `AttachFailed` or publish-stage failures until the node recovers.
- Workload returns to Ready after node restart and successful remount.

### Option C Cleanup
Assumes `KCTL` from Step 3a (or re-run the same `source <(ae auth local)` snippet).
```bash
python -m ae.cli delete netfs-nfs-hub-reader --purge
python -m ae.cli delete netfs-nfs-sea-edge-02-edge-1 --purge
python -m ae.cli delete netfs-csi-sea-edge-02-edge-1 --purge
$KCTL delete -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml --ignore-not-found
$KCTL delete -f specs/examples/netfs-csi-sea-edge-02-pvc.yaml --ignore-not-found
```

### Cleanup
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
