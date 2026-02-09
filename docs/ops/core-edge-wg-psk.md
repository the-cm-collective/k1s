# Core-Edge Rosenpass WG PSK Smoke Test

Purpose
- Verify managed Rosenpass (Option C) starts, peers are discovered from the controller, and WireGuard handshakes succeed.

Prereqs
- WireGuard tools installed (`wg`, `wg-quick`) on hub and edge hosts.
- Rosenpass installed on hub and edge hosts.
- Hub host reachable from edge on UDP `51820` (or your chosen WG listen port).
Notes
- Run `make k1s-core` without `sudo`. The controller stack writes under `state/`; if it becomes root-owned, subsequent non-root runs will fail. If you already ran `make k1s-core` with `sudo`, fix ownership with `sudo chown -R $USER:$USER state`.
- The node process needs elevated privileges to apply WireGuard configuration. Use `sudo -E make k1s-core-node` / `k1s-edge-node` and keep `AE_ROSENPASS_DIR` in a gitignored path (the defaults use `state/rosenpass`).

Install Rosenpass on Debian/Ubuntu (recommended: prebuilt binaries)
```bash
sudo apt --yes install wireguard
VERSION=0.2.2
cd /tmp
wget https://github.com/rosenpass/rosenpass/releases/download/v${VERSION}/rosenpass-x86_64-linux-${VERSION}.tar
tar xf rosenpass-x86_64-linux-${VERSION}.tar
sudo install bin/rosenpass /usr/local/bin
sudo install bin/rp /usr/local/bin
rosenpass help
rp help
```
Notes
- Rosenpass docs recommend the binary release path for Debian/Ubuntu; package manager support is not ready yet. citeturn1search1turn1search0
- If you need a different version, replace `VERSION` with a tag from the Rosenpass releases page. citeturn1search1
- You can run the install steps from any directory. If you run them from the repo root, the release tarball will unpack into the repo; prefer `/tmp` (or clean up after).
- By default, Rosenpass data lives in `/var/lib/ae/rosenpass`. If you override `AE_ROSENPASS_DIR` to a local path, keep it under `state/` or another gitignored directory. The repo `.gitignore` includes common Rosenpass artifacts.

Topology
- Hub site: `hub`
- Edge site: `edge`
- Hub WG endpoint: `<PUBLIC_IP>:51820`
- Hub pod CIDR: `10.42.0.0/24`
- Edge pod CIDR: `10.42.1.0/24`

Step 1: Start hub controller with Agent API
```bash
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
python -m ae.controller --loop
```

Alternate: Start hub controller with `make k1s-core`
```bash
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
make k1s-core
```

Step 2: Start hub node (controller-managed peers)
```bash
AE_NODE_ID=hub-1 \
AE_NODE_LABELS="role=hub,site=hub,wg_endpoint=<PUBLIC_IP>:51820" \
AE_POD_CIDR=10.42.0.0/24 \
AE_ROSENPASS_ENABLED=1 \
AE_ROSENPASS_CONFIG=controller \
AE_CONTROLLER_URL=http://127.0.0.1:9110 \
AE_AGENT_TOKEN=devtoken \
python -m ae.node --ensure-pod-net
```
Note
- You can also use `make k1s-core-node` which sets sensible defaults and runs `python -m ae.node`:
```bash
AE_WG_ENDPOINT=<PUBLIC_IP>:51820 \
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

Step 3: Start edge node (controller-managed peers)
```bash
AE_NODE_ID=edge-1 \
AE_NODE_LABELS="site=edge" \
AE_POD_CIDR=10.42.1.0/24 \
AE_ROSENPASS_ENABLED=1 \
AE_ROSENPASS_CONFIG=controller \
AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
AE_AGENT_TOKEN=devtoken \
python -m ae.node --ensure-pod-net
```
Note
- The node process must run with sufficient privileges to apply WireGuard. Prefer:
```bash
sudo -E AE_AGENT_TOKEN=devtoken \
  AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
  AE_ROSENPASS_DIR=/var/lib/ae/rosenpass \
  make k1s-edge-node
```
- The node agent only needs `AE_AGENT_TOKEN` for the controller API; no extra CLI credentials are required for this step.

Alternate: Start edge gateway stack with `make k1s-edge`
```bash
AE_SITE_ID=edge \
AE_NODE_ID=edge-1 \
make k1s-edge
```
Note
- `make k1s-edge` starts the gateway + worker stub only. You still need to run
  `ae.node` (Step 3) on the edge host to start Rosenpass/WireGuard.
- When the hub is running `make k1s-core` (JetStream), pair it with
  `make k1s-edge-core` (or `EDGE_PROFILE=k1s-core make k1s-edge`) to avoid
  `lease acquire failed: request failed: nats: timeout` warnings.
- You can also use `make k1s-edge-node` which sets sensible defaults and runs `python -m ae.node`:
```bash
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
make k1s-edge-node
```
Defaults set by `k1s-edge-node` (override as needed):
- `AE_NODE_ID=edge-1`
- `AE_NODE_LABELS=site=edge`
- `AE_POD_CIDR=10.42.1.0/24`
- `AE_ROSENPASS_ENABLED=1`
- `AE_ROSENPASS_CONFIG=controller`
- `AE_ROSENPASS_DIR=state/rosenpass`
- `AE_NODE_PORT=9112`

Step 4: Verify overlay config served by controller
```bash
curl -H "X-Agent-Token: devtoken" \
  http://<HUB_IP>:9110/v1/nodes/hub-1/overlay
curl -H "X-Agent-Token: devtoken" \
  http://<HUB_IP>:9110/v1/nodes/edge-1/overlay
```
Expected
- `peers` array populated for both nodes.
- `rosenpass_pubkey` is present for each peer once both nodes have generated keys.
- `errors` is empty or only reports missing `rp_pubkey` if Rosenpass keys were not created.

Step 5: Verify Rosenpass supervisor status
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

Step 6: Verify WireGuard handshake
```bash
sudo wg show wg0
```
Expected
- `latest handshake` advances.
- `transfer` counters increase after a few seconds.
- `preshared key: (hidden)` appears under the peer once Rosenpass has set the PSK.
If you see `Unable to access interface: No such device`
- Ensure the node process is running with sudo and that WireGuard tools are installed.
- Restart the node process; `wg0` is created on first apply.

Troubleshooting
- If overlay peers are empty, check `AE_AGENT_API_PORT` and `AE_AGENT_API_TOKEN`.
- If the hub endpoint is missing, ensure `AE_NODE_LABELS` includes `wg_endpoint=<PUBLIC_IP>:51820` or set `AE_OVERLAY_HUB_ENDPOINT`.
- If Rosenpass fails to start, set `AE_ROSENPASS_COMMAND` to the correct CLI for your version (default: `rosenpass exchange-config {config}`).
- If you see `rosenpass key generation failed; provide keys manually` but keys exist under `state/rosenpass`, confirm you are on the latest k1s build (Rosenpass keys are binary and must be base64-encoded in the generated config).
- If `rosenpass-status.json` shows a long base64 string followed by `does not exist`, the peer public key was treated as a file path. Ensure you are on the latest build; k1s now writes peer public keys to `${AE_ROSENPASS_DIR}/peers/*.rp.pub` and uses those paths in `rosenpass.conf`.
