# Core-Edge WireGuard PSK Smoke Test (with/without Rosenpass)

Purpose
- Verify WireGuard overlay setup for core/edge sites with either plain PSK or Rosenpass-managed PSK.
- Validate peer discovery, node registration, and overlay handshakes across NAT/CGNAT.

Prereqs
- WireGuard tools installed (`wg`, `wg-quick`) on hub and edge hosts.
- Rosenpass installed on hub and edge hosts (Flow B only).
- Hub host reachable from edge on UDP `51820` (or your chosen WG listen port).
Notes
- Run `make k1s-core` without `sudo`. The controller stack writes under `state/`; if it becomes root-owned, subsequent non-root runs will fail. If you already ran `make k1s-core` with `sudo`, fix ownership with `sudo chown -R $USER:$USER state`.
- The node process needs elevated privileges to apply WireGuard configuration. Use `sudo -E make k1s-core-node` / `k1s-edge-node` and keep `AE_ROSENPASS_DIR` in a gitignored path (the defaults use `state/rosenpass`).
- Rosenpass `verbosity` only accepts `Quiet` or `Verbose`. `AE_ROSENPASS_LOG_LEVEL=verbose` maps to `Verbose` in the generated config.
- The make helpers default `AE_ROSENPASS_DIR` to `/var/lib/ae/rosenpass` when running as root, so sudo-running nodes do not create root-owned folders under `state/`.
- Default ingress for remote sites should use `EDGE_INGRESS_MODE=core-proxy` unless you are testing edge-local ingress.

Flow A — Plain WireGuard PSK (no Rosenpass)
- Use this flow when you want a minimal dependency setup and manage WireGuard configs yourself.
- Ensure your WG configs include `PersistentKeepalive=25` on edge peers for NAT/CGNAT traversal.
- Hub must have a reachable public UDP endpoint; edges only need outbound UDP.

Step A1: Start the hub controller with Agent API
```bash
AE_LOG_LEVEL=debug \
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
python -m ae.controller --loop
```

Step A2: Start the hub node with a WG config (PSK)
```bash
sudo -E \
  AE_NODE_ID=hub-1 \
  AE_NODE_LABELS="role=hub,site=hub,wg_role=hub,wg_psk=psk,wg_endpoint=<PUBLIC_IP>:51820" \
  AE_WG_CONFIG="$(cat /etc/wireguard/wg-hub.conf)" \
  AE_WG_INTERFACE=wg-hub \
  AE_AGENT_TOKEN=devtoken \
  AE_CONTROLLER_URL=http://127.0.0.1:9110 \
  python -m ae.node --ensure-pod-net
```

Step A3: Start the edge node with a WG config (PSK)
```bash
sudo -E \
  AE_NODE_ID=edge-1 \
  AE_NODE_LABELS="site=sea-edge-02,wg_role=spk,wg_psk=psk" \
  AE_WG_CONFIG="$(cat /etc/wireguard/wg-edge.conf)" \
  AE_WG_INTERFACE=wg-edge \
  AE_AGENT_TOKEN=devtoken \
  AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
  python -m ae.node --ensure-pod-net
```
Notes
- On single-host labs, set `AE_WG_TABLE=off` for the edge node.
- If both sites are behind CGNAT, you will need a relay or public hub endpoint.

Flow B — WireGuard + Rosenpass
Single-host note
- If you run hub + edge on the same host, use distinct `AE_ROSENPASS_DIR` and `AE_WG_INTERFACE` values per node to avoid clobbering keys and WG config. Example:
  - Hub: `AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-hub` `AE_ROSENPASS_INTERFACE=wg-hub` `AE_WG_LISTEN_PORT=51820`
  - Edge: `AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-edge` `AE_ROSENPASS_INTERFACE=wg-edge` `AE_WG_LISTEN_PORT=51821`
  - If you see `RTNETLINK answers: Address already in use`, set `AE_WG_ADDRESS` to a unique /32 per node (or `AE_WG_ADDRESS=none` to skip assigning an address).
  - If you see `RTNETLINK answers: File exists` when the edge starts, set `AE_WG_TABLE=off` on the edge node. This skips adding routes on a single host where the hub already owns `10.42.0.0/24`.

Single-host (tested command sequence)
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

Install Rosenpass on Debian/Ubuntu (recommended: prebuilt binaries)
```bash
sudo apt --yes install wireguard
VERSION="<release-tag>"
cd /tmp
wget https://github.com/rosenpass/rosenpass/releases/download/v${VERSION}/rosenpass-x86_64-linux-${VERSION}.tar
tar xf rosenpass-x86_64-linux-${VERSION}.tar
sudo install bin/rosenpass /usr/local/bin
sudo install bin/rp /usr/local/bin
rosenpass help
rp help
```
Notes
- Rosenpass docs recommend the binary release path for Debian/Ubuntu; package manager support is still limited.
- Replace `VERSION` with a tag from the Rosenpass releases page.
- You can run the install steps from any directory. If you run them from the repo root, the release tarball will unpack into the repo; prefer `/tmp` (or clean up after).
- By default, Rosenpass data lives in `/var/lib/ae/rosenpass`. If you override `AE_ROSENPASS_DIR` to a local path, keep it under `state/` or another gitignored directory. The repo `.gitignore` includes common Rosenpass artifacts.

Topology
- Hub site: `hub`
- Remote edge site: `sea-edge-02`
- Hub WG endpoint: `<PUBLIC_IP>:51820`
- Hub pod CIDR: `10.42.0.0/24`
- Edge pod CIDR: `10.42.1.0/24`
NAT/CGNAT guidance
- Hub must expose a public UDP endpoint for WireGuard.
- Edges behind NAT/CGNAT should set `PersistentKeepalive=25` in their WG configs.
- If both sites are behind CGNAT, use a relay or move the hub to a public endpoint.
Label conventions (for dashboard clarity)
- Flow A (plain PSK)
  - Hub: `wg_role=hub`, `wg_psk=psk`
  - Edge: `wg_role=spk`, `wg_psk=psk`
- Flow B (Rosenpass)
  - Hub: `wg_role=hub`, `wg_psk=rp` (rp = rosenpass)
  - Edge: `wg_role=spk`, `wg_psk=rp`
Note
- When any node advertises `wg_role`, the overlay endpoints and `/system` graph only include nodes with `wg_role` set. This keeps controller/gateway nodes out of the WireGuard overlay view.

Step 1: Start hub controller with Agent API
```bash
AE_LOG_LEVEL=debug \
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
python -m ae.controller --loop
```

Postgres for apishim (k1s-core profile)
- Bind Postgres to the hub WG IP so edge nodes can read apishim storage over the tunnel:
```bash
POSTGRES_BIND_IP=<HUB_WG_IP> POSTGRES_PORT=5432 make k1s-core
```
- On edge nodes, set:
```bash
export AE_APISHIM_DSN=postgresql://shim:shim@<HUB_WG_IP>:5432/shim
```
Example (hub WG IP `10.255.0.1`):
```bash
POSTGRES_BIND_IP=10.255.0.1 POSTGRES_PORT=5432 make k1s-core
export AE_APISHIM_DSN=postgresql://shim:shim@10.255.0.1:5432/shim
```

Alternate: Start hub controller with `make k1s-core`
```bash
AE_LOG_LEVEL=debug \
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
make k1s-core
```
Note
- If your nodes write Rosenpass status under `/var/lib/ae/rosenpass`, set `AE_ROSENPASS_DIR=/var/lib/ae/rosenpass` on the controller so `/system` can report Rosenpass state. Ensure the status file is readable by the controller user (or run on the same host with permissive permissions).

Step 2: Start hub node (controller-managed peers)
```bash
AE_NODE_ID=hub-1 \
AE_NODE_LABELS="role=hub,site=hub,wg_endpoint=<PUBLIC_IP>:51820,wg_role=hub,wg_psk=rp" \
AE_POD_CIDR=10.42.0.0/24 \
AE_ROSENPASS_ENABLED=1 \
AE_ROSENPASS_CONFIG=controller \
AE_ROSENPASS_INTERFACE=wg-hub \
AE_WG_LISTEN_PORT=51820 \
AE_WG_ADDRESS=10.255.0.1/32 \
AE_LOG_LEVEL=debug \
AE_ROSENPASS_LOG_LEVEL=verbose \
AE_CONTROLLER_URL=http://127.0.0.1:9110 \
AE_AGENT_TOKEN=devtoken \
python -m ae.node --ensure-pod-net
```
Note
- You can also use `make k1s-core-node` which sets sensible defaults and runs `python -m ae.node`:
```bash
AE_WG_ENDPOINT=<PUBLIC_IP>:51820 \
AE_NODE_LABELS="role=hub,site=hub,wg_role=hub,wg_psk=rp" \
AE_ROSENPASS_INTERFACE=wg-hub \
AE_WG_LISTEN_PORT=51820 \
AE_WG_ADDRESS=10.255.0.1/32 \
AE_LOG_LEVEL=debug \
AE_ROSENPASS_LOG_LEVEL=verbose \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://127.0.0.1:9110 \
make k1s-core-node
```
Defaults set by `k1s-core-node` (override as needed):
- `AE_NODE_ID=hub-1`
- `AE_NODE_LABELS=role=hub,site=hub` (+ `wg_endpoint=` when `AE_WG_ENDPOINT` is set)
- `AE_POD_CIDR=10.42.0.0/24`
- `AE_ROSENPASS_ENABLED=1`
- `AE_ROSENPASS_CONFIG=controller`
- `AE_ROSENPASS_DIR=state/rosenpass`
- `AE_NODE_PORT=9111` (avoids conflicts with the docs server on 9109 when `make k1s-core` runs with dev-local)
- `sudo -E` must be followed by a command (it prints a usage error otherwise). Optional: pre-auth with `sudo -v`, then run `sudo -E` with the `make` command.
Note
- If you prefer not to write root-owned files into the repo, override `AE_ROSENPASS_DIR=/var/lib/ae/rosenpass` when running the node helpers with `sudo`.

Step 3: Register the remote edge site in the hub (creates NATS leaf creds)
```bash
make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```
Note
- This helper updates the hub NATS config with the `site-sea-edge-02-uplink` user and starts a local edge NATS leader bound to `127.0.0.1:4224`. Use the manual steps in `docs/ops/core-edge-manual-test.md` if the edge NATS leader runs on another host.

Manual remote edge NATS leader (when edge runs on a different host)
1) On the hub host, ensure the uplink user exists:
- Add a `site-sea-edge-02-uplink` user to the hub NATS config (see `docs/ops/core-edge-manual-test.md`), then reload NATS.

2) On the remote edge host, copy and edit the edge NATS config:
```bash
EDGE_SITE=sea-edge-02
cp ops/dev/nats-edge.conf /tmp/nats-edge-${EDGE_SITE}.conf
sed -i "s/sfo-edge-01/${EDGE_SITE}/g" /tmp/nats-edge-${EDGE_SITE}.conf
sed -i "s/nats-hub:7422/<HUB_PUBLIC>:7422/g" /tmp/nats-edge-${EDGE_SITE}.conf
```
Then start NATS:
```bash
nats-server -c /tmp/nats-edge-${EDGE_SITE}.conf
```
Notes
- `nats-edge.conf` is Core NATS only (correct for JetStream hub + lightweight edges).
- Ensure `<HUB_PUBLIC>:7422` is reachable from the remote site.

Step 4: Start the edge gateway (JetStream hub pairing)
```bash
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224 \
AE_LOG_LEVEL=debug \
make k1s-edge-core
```
Note
- Replace `127.0.0.1:4224` with the edge NATS leader address when running on a remote host.

Step 5: Start the edge node (controller-managed peers)
```bash
AE_NODE_ID=edge-1 \
AE_NODE_LABELS="site=sea-edge-02,wg_role=spk,wg_psk=rp" \
AE_POD_CIDR=10.42.1.0/24 \
AE_ROSENPASS_ENABLED=1 \
AE_ROSENPASS_CONFIG=controller \
AE_ROSENPASS_INTERFACE=wg-edge \
AE_WG_LISTEN_PORT=51821 \
AE_WG_ADDRESS=10.255.0.2/32 \
AE_WG_TABLE=off \
AE_LOG_LEVEL=debug \
AE_ROSENPASS_LOG_LEVEL=verbose \
AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
AE_AGENT_TOKEN=devtoken \
python -m ae.node --ensure-pod-net
```
Note
- The node process must run with sufficient privileges to apply WireGuard. Prefer:
```bash
sudo -E AE_LOG_LEVEL=debug \
  AE_ROSENPASS_LOG_LEVEL=verbose \
  AE_NODE_LABELS="site=sea-edge-02,wg_role=spk,wg_psk=rp" \
  AE_ROSENPASS_INTERFACE=wg-edge \
  AE_WG_LISTEN_PORT=51821 \
  AE_WG_ADDRESS=10.255.0.2/32 \
  AE_WG_TABLE=off \
  AE_AGENT_TOKEN=devtoken \
  AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
  AE_ROSENPASS_DIR=/var/lib/ae/rosenpass \
  make k1s-edge-node
```
- The node agent only needs `AE_AGENT_TOKEN` for the controller API; no extra CLI credentials are required for this step.

Step 6: Verify overlay config served by controller
```bash
curl -s -H "X-Agent-Token: devtoken" \
  http://<HUB_IP>:9110/v1/nodes/hub-1/overlay \
  | jq '{node_id, errors, peers:[.peers[] | {node_id, endpoint, role, rosenpass_pubkey}]}'
curl -s -H "X-Agent-Token: devtoken" \
  http://<HUB_IP>:9110/v1/nodes/edge-1/overlay \
  | jq '{node_id, errors, peers:[.peers[] | {node_id, endpoint, role, rosenpass_pubkey}]}'
curl -s -H "Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}" \
  http://<HUB_IP>:9108/system \
  | jq '.overlay | {topology, links, errors}'
```
Expected
- `peers` array populated for both nodes.
- `rosenpass_pubkey` is present for each peer once both nodes have generated keys.
- `errors` is empty or only reports missing `rp_pubkey` if Rosenpass keys were not created.
Note
- If `/system` returns `{"error": "unauthorized"}`, export tokens with `source <(ae auth local)` and pass the bearer token header as shown.

Step 6a (optional): Allow rootless controller to read WireGuard handshakes
```bash
WG_BIN=$(command -v wg)
echo "$USER ALL=(root) NOPASSWD: ${WG_BIN} show wg-hub dump" | sudo tee /etc/sudoers.d/k1s-wg-dump
sudo chmod 440 /etc/sudoers.d/k1s-wg-dump
```
Then run the controller with:
```bash
AE_WG_DUMP_CMD="sudo -n ${WG_BIN} show {iface} dump" \
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
make k1s-core
```
Notes
- The helper command is only used for `/system` overlay handshake info; it does not affect node overlay setup.
- You can use a different interface by changing `wg-hub` (and `{iface}`).

Step 7: Verify Rosenpass supervisor status
```bash
cat /var/lib/ae/rosenpass/rosenpass-status.json
```
Expected
- `state` is `running` and `pid` is present.
If you see `state=waiting-for-peers`
- This means no peers with `rosenpass_pubkey` were available when the node started.
- Ensure the edge node is running with Rosenpass enabled, then restart the hub node so it refetches peers.
If peer updates should be picked up automatically
- Set `AE_ROSENPASS_PEER_REFRESH_SEC=30` (default) to refresh peers periodically.
Note
- Peer refresh now reapplies WireGuard configs whenever peers change, even if Rosenpass peers are still missing. This keeps the WireGuard interface in sync without restarting the hub node.
Debug tip
- Set `AE_WG_DEBUG_DUMP=1` to write the last applied WireGuard config to `${AE_ROSENPASS_DIR}/wg-debug.conf` (mode 0600).
  Use `sudo cat` when reading from `/var/lib/ae/...`.
If `make dev-state-clean` fails with permissions
- Run `sudo rm -rf state/rosenpass` (or `sudo chown -R $USER:$USER state`) and retry. This typically happens if you ran a node with `sudo` while `AE_ROSENPASS_DIR` pointed into `state/`.

Step 8: Verify WireGuard handshake
```bash
sudo wg show wg-hub
```
Expected
- `latest handshake` advances.
- `transfer` counters increase after a few seconds.
- `preshared key: (hidden)` appears under the peer once Rosenpass has set the PSK.
If you see `Unable to access interface: No such device`
- Ensure the node process is running with sudo and that WireGuard tools are installed.
- Restart the node process; the WireGuard interface is created on first apply.

Step 9: Deploy shell demo to the Sea site (SPDY shell + port-forward validation)
```bash
source <(ae auth local)
# If api.home.arpa does not resolve, bypass local DNS:
export AE_APISHIM_SERVER=https://127.0.0.1:8445
ae apply -f docs/site/examples/shell-demo-node-sea-edge-02-edge-1.yaml
ae status shell-demo-node-sea-edge-02-edge-1 --wide --events
```
Remote shell (SPDY):
```bash
ae shell shell-demo-node-sea-edge-02-edge-1 -- /bin/sh
```
Port-forward (SPDY/WebSocket via API shim):
```bash
ae port-forward shell-demo-node-sea-edge-02-edge-1 18082:8080
curl -fsS http://127.0.0.1:18082/healthz
```
Deploy to the hub node (role=hub, site=hub):
```bash
ae apply -f docs/site/examples/shell-demo-node-hub.yaml
ae status shell-demo-node-hub --wide --events
```
Remote shell (hub):
```bash
ae shell shell-demo-node-hub -- /bin/sh
```
Port-forward (hub):
```bash
ae port-forward shell-demo-node-hub 18084:8080
curl -fsS http://127.0.0.1:18084/healthz
```
Notes
- This smoke test targets the Sea site gateway node (`role=gateway`, `site=sea-edge-02`). Use a different manifest if you want to pin to an edge worker node instead.
- The hub node (`k1s-core-node`, `role=hub`) is assignable and can run workloads. The core controller (`role=controller`, `profile=k1s-core`) is not a runtime node in this flow unless you explicitly run a workload-capable node there.
- If you want to keep `api.home.arpa` instead of overriding `AE_APISHIM_SERVER`, add a hosts entry:
  - `127.0.0.1 api.home.arpa` (or map to the hub IP for remote use).

Step 10: NetFS + CSI storage over the WG overlay
Overview
- NetFS mounts happen on the node agent. The controller only seeds PVC/PV/StorageClass objects.
- For remote edge nodes, NetFS requires access to the apishim store. Prefer `AE_APISHIM_DSN` (Postgres). `state/apishim.db` is local-only.
- The SEA node mounts NFS under `AE_NETFS_ROOT` (default `/var/lib/ae/netfs`) and bind-mounts that path into the pod.
Enable NetFS on the edge node
- Add `AE_ENABLE_NETFS=1` when starting the SEA node.
- Set `AE_APISHIM_DSN` (or `AE_APISHIM_DB` if the node runs on the same host as the controller).
NFS over the overlay
- Start the hub controller with `AE_STORAGE_NFS_SERVER=<HUB_WG_IP>` and `AE_STORAGE_NFS_PATH=<EXPORT_PATH>` so it seeds the `k1s-nfs` StorageClass.
- Use the hub WG IP (example: `10.255.0.1`) so the SEA node routes over the overlay.
- Ensure the NFS server is reachable from the SEA node and that NFS ports are allowed.
- Ensure the SEA node has `mount`/`umount` plus an NFS helper (`mount.nfs` or `mount.nfs4`).
- If you set `AE_STORAGE_NFS_HOSTPATH`, the controller host must also be the NFS server host (it creates per-PVC directories). Otherwise pre-create exports and omit `hostPath`.
Minimal cross-site NetFS smoke (SEA site)
```bash
# Hub controller (before start)
export AE_STORAGE_NFS_SERVER=10.255.0.1
export AE_STORAGE_NFS_PATH=/exports/k1s
# Optional: only when the controller runs on the NFS server host
# export AE_STORAGE_NFS_HOSTPATH=/srv/nfs/k1s

# SEA edge node (add to Step 5 env)
sudo -E AE_ENABLE_NETFS=1 AE_APISHIM_DSN=postgres://... make k1s-edge-node

source <(ae auth local)
ae apply -f docs/site/examples/netfs-nfs-sea-edge-02-edge-1.yaml
ae status netfs-nfs-sea-edge-02-edge-1 --wide --events
```
Stage 2: Shared RWX validation (core + edge)
- Ensure node labels include `role=hub,site=hub` on the hub node and `role=worker,site=sea-edge-02` on the edge node (or adjust nodeSelectors in the manifests below).
- These manifests pin workloads to each site and use the same NFS-backed StorageClass.
```bash
source <(ae auth local)
ae apply -f specs/examples/echo-storage-node-hub.yaml --storage-class-name k1s-nfs --pvc-access-modes ReadWriteMany
ae apply -f specs/examples/echo-storage-node-sea-edge-02-edge-1.yaml --storage-class-name k1s-nfs --pvc-access-modes ReadWriteMany
ae status echo-storage-node-hub --wide --events
ae status echo-storage-node-sea-edge-02-edge-1 --wide --events
```
Verify shared data via `ae shell`:
```bash
ae shell echo-storage-node-hub -- sh -c 'echo core-flag > /var/lib/echo/flag.txt'
ae shell echo-storage-node-sea-edge-02-edge-1 -- cat /var/lib/echo/flag.txt
```
CSI over the overlay
- Configure controller + node endpoints in `AE_STORAGE_PROVISIONERS` (see `configs/storage-provisioners.yaml`).
- Ensure the CSI controller plugin runs on the hub and the CSI node plugin runs on each edge node.
- StorageClass should include `topologyKeys: ["site"]` and required CSI secrets.
- CSI staging paths default to `AE_CSI_STAGE_ROOT=/var/lib/ae/csi`.
Minimal CSI smoke (CephFS example)
```bash
# Hub controller (before start)
export AE_STORAGE_PROVISIONERS=/path/to/configs/storage-provisioners.yaml

source <(ae auth local)
ae apply -f specs/examples/echo-stateful.yaml --storage-class-name cephfs-rwx --pvc-access-modes ReadWriteMany
ae status echo-stateful --wide --events
```

Troubleshooting
- If overlay peers are empty, check `AE_AGENT_API_PORT` and `AE_AGENT_API_TOKEN`.
- If the hub endpoint is missing, ensure `AE_NODE_LABELS` includes `wg_endpoint=<PUBLIC_IP>:51820` or set `AE_OVERLAY_HUB_ENDPOINT`.
- If you see `RTNETLINK answers: File exists` when starting the edge on a single host, set `AE_WG_TABLE=off` for the edge so wg-quick does not attempt to add a duplicate route for `10.42.0.0/24`.
- If Rosenpass fails to start, set `AE_ROSENPASS_COMMAND` to the correct CLI for your version (default: `rosenpass exchange-config {config}`).
- If you see `rosenpass key generation failed; provide keys manually` but keys exist under `state/rosenpass`, confirm you are on the latest k1s build (Rosenpass keys are binary and must be base64-encoded in the generated config).
- If `rosenpass-status.json` shows a long base64 string followed by `does not exist`, the peer public key was treated as a file path. Ensure you are on the latest build; k1s now writes peer public keys to `${AE_ROSENPASS_DIR}/peers/*.rp.pub` and uses those paths in `rosenpass.conf`.

Security notes (production hardening)
- Replace dev NATS credentials (`gateway:dev`, `site-<id>-uplink:dev`) with per-site creds (NKeys/JWT or creds files) and lock down subject permissions.
- Keep the node agent API (`AE_AGENT_TOKEN`) reachable only over WG/LAN; do not expose it publicly.
- Keep the API shim (exec/port-forward) behind TLS with scoped tokens; avoid sharing admin tokens across environments.
- Ensure `AE_ROSENPASS_DIR` permissions are restricted (root-owned, 0700/0600) and avoid writing keys under the repo when running nodes with sudo.
