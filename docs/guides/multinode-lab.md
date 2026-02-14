# Multi-node Edge Gateway Manual Test

This walkthrough is a manual test pattern for the core hub plus edge gateways. It focuses on a JetStream hub with lightweight edge sites and multiple gateways per site. For the ops-focused runbook, see `docs/ops/core-edge-manual-test.md`.

## Quick Navigation

Podman / mixed-runtime patterns:
- [Option A - LAN-only multi-node](#option-a-lan-only)
- [Option B - Core + edge gateways (WireGuard + NATS)](#option-b-core-edge-gateways)
- [Option C - Site-to-site NetFS storage](#option-c-netfs)
- [Option D - Canonical ingress modes](#option-d-ingress)

CRI patterns:
- [CRI Deployment (strict CRI)](#cri-deployment)
- [Single-Host Dev Ops Patterns (CRI)](#cri-single-host)
- [Same-Host Variant (hub + edge on one box)](#cri-same-host-variant)
- [CRI troubleshooting notes](#cri-troubleshooting)

## CRI Deployment (strict CRI) {#cri-deployment}

Use this section when running core + edge in strict CRI mode with containerd.
Existing Podman sections remain valid for homelab/dev; this section is additive.

### Prereqs (CRI)

- containerd is running and reachable at `unix:///run/containerd/containerd.sock`.
- BuildKit is installed and active (`buildkitd` + `buildctl`) for on-demand apishim image builds.
- CRI registry mode is available (`localhost:5001` managed registry or equivalent external registry).
- Use a venv-first sudo path for node commands:
```bash
VENV_BIN="/home/$USER/git/k1s/.venv/bin"
SUDO_PATH="${VENV_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
sudo -E env PATH="$SUDO_PATH" python -c "import sys,grpc; print(sys.executable, grpc.__version__)"
```

### Core stack (controller + infra + apishim)

```bash
WG_BIN=$(command -v wg)
APISHIM_TAG="localhost:5001/k1s-apishim:dev-$(date +%s)"
sudo -E \
  AE_DEV_LOCAL=1 \
  EDGE_INGRESS_MODE=edge-local \
  AE_ROUTE_BUNDLE_ENABLED=1 \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_REGISTRY_MODE=managed \
  AE_CRI_REGISTRY_INSECURE=1 \
  AE_LABS=1 \
  AE_APISHIM_AUTOSTART=1 \
  AE_APISHIM_MODE=cri \
  AE_APISHIM_IMAGE="${APISHIM_TAG}" \
  AE_APISHIM_STARTUP_TIMEOUT=60 \
  BENCH_MODE=0 \
  AE_WG_INTERFACE=wg-hub \
  AE_WG_DUMP_CMD="sudo -n ${WG_BIN} show {iface} dump" \
  AE_AGENT_API_PORT=9110 \
  AE_AGENT_API_TOKEN=devtoken \
  AE_ENABLE_NETFS=1 \
  AE_STORAGE_SEED_DEFAULTS=1 \
  AE_STORAGE_NFS_SERVER=10.255.0.1 \
  AE_STORAGE_NFS_PATH=/exports/k1s \
  AE_STORAGE_NFS_CLASS=k1s-nfs \
  AE_APISHIM_ETCD_ENDPOINTS=http://127.0.0.1:2379 \
  make k1s-core
```

Notes:
- Keep `AE_DEV_LOCAL=1` enabled for local docs/playground/dashboard behavior in this lane.
- Use a unique `AE_APISHIM_IMAGE` tag per run when iterating apishim changes to avoid stale image reuse.
- For ingress baseline validation, keep service proxy disabled unless explicitly testing it.
  Enabling `AE_ENABLE_SERVICE_PROXY=1` with `AE_SERVICE_PROVIDER=iptables` can
  alter local docs/dashboard routing behavior and should be validated in a
  separate lane.

### Core node (hub)

```bash
sudo -E env PATH="$SUDO_PATH" \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID=hub-1 \
  AE_NODE_LABELS="role=hub,site=hub,wg_role=hub,wg_psk=rp,wg_endpoint=<HUB_PUBLIC_IP>:51820" \
  AE_POD_CIDR=10.42.0.0/24 \
  AE_ROSENPASS_ENABLED=1 \
  AE_ROSENPASS_CONFIG=controller \
  AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-hub \
  AE_ROSENPASS_INTERFACE=wg-hub \
  AE_WG_LISTEN_PORT=51820 \
  AE_WG_ADDRESS=10.255.0.1/32 \
  AE_ENABLE_NETFS=1 \
  AE_APISHIM_DSN=postgresql://shim:shim@127.0.0.1:5432/shim \
  AE_AGENT_TOKEN=devtoken \
  AE_CONTROLLER_URL=http://127.0.0.1:9110 \
  AE_AGENT_ENDPOINT=http://10.255.0.1:9111 \
  make k1s-core-node
```

### Edge site and edge core

```bash
ROOT="/home/$USER/git/k1s"
VENV_BIN="$ROOT/.venv/bin"
SUDO_PATH="${VENV_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EDGE_LOCAL_DIR="$ROOT/state/profiles/k1s-core/edge-local"
RELOAD_CMD="/usr/bin/install -D -m 0644 $EDGE_LOCAL_DIR/edge-local.caddy $ROOT/state/caddy/edge-local.caddy && $VENV_BIN/python $ROOT/scripts/dev/cri_stack.py up-caddy --profile k1s-core --metrics-port 9108 --apishim-port 8445 --recreate"

sudo -E make edge-site-cri SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224

sudo -E env PATH="$SUDO_PATH" \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  EDGE_INGRESS_MODE=edge-local \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_REGISTRY_MODE=managed \
  AE_CRI_REGISTRY_INSECURE=1 \
  AE_SITE_ID=sea-edge-02 \
  AE_NODE_ID=edge-1 \
  AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224 \
  AE_EDGE_LOCAL_INGRESS_CONFIG_DIR="$EDGE_LOCAL_DIR" \
  AE_EDGE_LOCAL_INGRESS_CONFIG_FILE="$EDGE_LOCAL_DIR/edge-local.caddy" \
  AE_EDGE_LOCAL_INGRESS_RELOAD_CMD="$RELOAD_CMD" \
  AE_EDGE_LOCAL_UPSTREAM_MODE=bundle-endpoints \
  AE_LOG_LEVEL=debug \
  make k1s-edge-core-cri
```

Edge-local preflight (root-safe):
```bash
cpid=$(pgrep -f "python -m ae.controller" | head -n1)
gpid=$(pgrep -f "python -m ae.gateway" | head -n1)
test -n "$cpid" && test -n "$gpid"

sudo cat "/proc/$cpid/environ" | tr '\0' '\n' | rg 'EDGE_INGRESS_MODE|AE_ROUTE_BUNDLE_ENABLED|AE_TRANSPORT_BACKEND|AE_NATS_URL'
sudo cat "/proc/$gpid/environ" | tr '\0' '\n' | rg 'EDGE_INGRESS_MODE|AE_EDGE_LOCAL_INGRESS_CONFIG_DIR|AE_SITE_ID|AE_NODE_ID|AE_TRANSPORT_BACKEND|AE_NATS_URL'

ls -ld state/profiles/k1s-core/edge-local
ls -l state/profiles/k1s-core/edge-local/edge-local.caddy || true
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18081/ || true

# hub-controller must be allowed to publish route bundles
rg -n 'user: "hub-controller"|k1s.v1.site.\*.routes.bundle' ops/dev/nats-hub.conf
```
Expected:
- controller env includes `EDGE_INGRESS_MODE=edge-local` and `AE_ROUTE_BUNDLE_ENABLED=1`
- gateway env includes `EDGE_INGRESS_MODE=edge-local` and `AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=.../state/profiles/k1s-core/edge-local`
- gateway env includes `AE_EDGE_LOCAL_INGRESS_CONFIG_FILE`, `AE_EDGE_LOCAL_INGRESS_RELOAD_CMD`, and `AE_EDGE_LOCAL_UPSTREAM_MODE=bundle-endpoints`
- controller and gateway both include `AE_TRANSPORT_BACKEND=nats-core` or `nats-js`, and `AE_NATS_URL` is set
- `state/profiles/k1s-core/edge-local` exists before tier checks run
- backend probe should become `2xx` once app workload is ready (for tier checks)
- `ops/dev/nats-hub.conf` includes `k1s.v1.site.*.routes.bundle` in `hub-controller` publish permissions

Bundle behavior note:
- Edge-local route bundles publish for site IDs discovered from node leases and
  `EdgeIngressRoute` placement sites. A missing lease does not block publish if
  routes exist for that site.

Run this preflight before invoking:
`scripts/dev/test_ingress_modes_single_host.sh --mode edge-local --tier tier1`.

If tier checks still time out after route staging:
```bash
rg -n "route bundle" state/profiles/k1s-core/controller.log || true
rg -n "route bundle|edge-local" state/profiles/k1s-core/gateway-sea-edge-02-edge-1.log || true
rg -n "route bundle|edge-local" state/profiles/k1s-edge/gateway-sea-edge-02-edge-1.log || true
ls -l state/profiles/k1s-core/edge-local/edge-local.caddy || true
```

### Edge node

Verified command set:
```bash
sudo -E env PATH="$SUDO_PATH" \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_NODE_ID=edge-1 \
  AE_POD_CIDR=10.42.1.0/24 \
  AE_NODE_LABELS="role=worker,site=sea-edge-02,wg_role=spk,wg_psk=rp" \
  AE_WG_INTERFACE=wg-edge \
  AE_ROSENPASS_INTERFACE=wg-edge \
  AE_WG_ADDRESS=10.255.0.3/32 \
  AE_WG_TABLE=off \
  AE_WG_LISTEN_PORT=51821 \
  AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-edge \
  AE_ENABLE_NETFS=1 \
  AE_APISHIM_DSN=postgresql://shim:shim@127.0.0.1:5432/shim \
  AE_AGENT_TOKEN=devtoken \
  AE_CONTROLLER_URL=http://127.0.0.1:9110 \
  AE_AGENT_ENDPOINT=http://10.255.0.3:9112 \
  make k1s-edge-node
```

### CRI Validation Checklist

```bash
sudo crictl pods
ae nodes
ae status
bash scripts/dev/netfs_validate.sh \
  --runtime auto \
  --namespace default \
  --writer-app netfs-nfs-sea-edge-02-edge-1 \
  --reader-app netfs-nfs-hub-reader \
  --mount-path /data
```

### CRI Troubleshooting Notes {#cri-troubleshooting}

- `spdy upgrade failed: 404` with repeated apishim warnings about CRI unavailable usually means apishim cannot reach the containerd socket; confirm `AE_CRI_ENDPOINT` and apishim runtime env/socket mount.
- `error: registry endpoint responded unexpectedly at https://localhost:5001/v2/` means core registry is not healthy/reachable before edge CRI startup.
- If `crictl` commands differ from examples, check your local `crictl` version/flags (`crictl pods` on some builds does not support `-a`).
- In CRI, workload pod names typically include revision suffixes (for example `...-rev1-0`); avoid assuming Podman container naming patterns.

## Single-Host Dev Ops Patterns (CRI) {#cri-single-host}

This is the fastest strict-CRI dev loop on one workstation:
1. Start `k1s-core` with `AE_DEV_LOCAL=1`, CRI env, and apishim autostart.
2. Start `k1s-core-node` (hub node) with WG/Rosenpass and `AE_ENABLE_NETFS=1`.
3. Start edge site + edge core with `make edge-site-cri` then `make k1s-edge-core-cri`.
4. Start `k1s-edge-node` with the verified command block above.
5. Run `ae auth local --strict`, `ae nodes`, `ae status`, and `scripts/dev/netfs_validate.sh`.

Expected:
- `ae nodes` shows controller, hub CRI node, edge CRI node, and edge gateway node as `Ready`.
- `ae status` workloads reach `ready`.
- CRI pods are visible in `sudo crictl pods` for core/edge components.

## Option A — LAN-only multi-node (no WireGuard) {#option-a-lan-only}

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

## Option B — Core + edge gateways (WireGuard + NATS) {#option-b-core-edge-gateways}

### Topology
- Site A (LAN): `k1s-core` + one gateway on `sfo-edge-01`.
- Site B (remote): one edge NATS leader with outbound leaf to the hub, two gateways on `sea-edge-02`.
- Transport: JetStream on hub only; gateways use a local SQLite spool for durability.

### Prereqs
- Hub host reachable on TCP `4222` (NATS) and TCP `7422` (leaf node port).
- Remote edge NATS can open outbound TCP to `HUB_PUBLIC:7422`.
- Unique node ids (recommended format: `<site_id>--<node_id>`).

### One-time non-root CLI setup (shell/port-forward)
Run this once per workstation user:
```bash
sudo groupadd -f aecli
sudo usermod -aG aecli "$USER"
newgrp aecli
id -nG | tr ' ' '\n' | grep -x aecli
```
Expected:
- `aecli` appears in the current shell groups.
- Core startup syncs `state/profiles/<profile>/apishim.cli.env` as `640 root:aecli`.
- `ae auth local --strict` infers the active profile and prefers `apishim.cli.env` automatically (no `--apishim-env` argument needed).

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
AE_NODE_LABELS="role=worker,site=sea-edge-02,wg_role=spk,wg_psk=rp" \
AE_ROSENPASS_INTERFACE=wg-edge \
AE_WG_LISTEN_PORT=51821 \
AE_WG_ADDRESS=10.255.0.2/32 \
AE_WG_TABLE=off \
AE_ENABLE_NETFS=1 \
AE_APISHIM_DSN=postgresql://shim:shim@<HUB_DB_BIND_IP>:5432/shim \
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
If `ae shell` fails with `spdy upgrade failed: 401` while dashboard shell still works:
- Ensure non-root CLI group setup is complete once: `sudo groupadd -f aecli && sudo usermod -aG aecli $USER`, then refresh shell groups (`newgrp aecli` or re-login).
- Ensure core startup syncs `state/profiles/<profile>/apishim.cli.env` as `640 root:aecli` with `AE_APISHIM_SERVER`, `AE_APISHIM_MINT_TOKEN`, and `AE_APISHIM_CA_BUNDLE`.
- Ensure `state/profiles/<profile>/apishim.ca.crt` is present/readable for group `aecli`.
- Re-run `source <(ae auth local --strict)` to refresh exports.
- If `AE_LABS_TOKEN` is also available, CLI can mint a short-lived fallback session token through controller `/api/apishim/session`.
- Set `AE_CLI_LABS_MINT_FALLBACK=0` only if you want to force shim-only token behavior for debugging.
Note
- For strict site-to-site SPDY simulation, keep node-advertised endpoints on WG IPs (`AE_AGENT_ENDPOINT=http://<WG_IP>:<port>`). Do not switch to `host.containers.internal` in this test lane.
- `ae auth local --strict` sets `AE_APISHIM_SERVER` to `https://127.0.0.1:8445`. On a remote host, override it to the hub, e.g. `export AE_APISHIM_SERVER=https://<HUB_IP>:8445`, then re-run strict auth in that shell.

### Same-Host Variant (hub + edge on one box) {#cri-same-host-variant}
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

2. Start the hub controller with overlay dump support (rootful validated sequence):
```bash
WG_BIN=$(command -v wg)
APISHIM_TAG="localhost:5001/k1s-apishim:dev-$(date +%s)"
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  EDGE_INGRESS_MODE=edge-local \
  AE_ROUTE_BUNDLE_ENABLED=1 \
  AE_CRI_REGISTRY_MODE=managed \
  AE_CRI_REGISTRY_INSECURE=1 \
  AE_DEV_LOCAL=1 \
  AE_LABS=1 \
  AE_APISHIM_AUTOSTART=1 \
  AE_APISHIM_MODE=cri \
  AE_APISHIM_IMAGE="${APISHIM_TAG}" \
  AE_APISHIM_STARTUP_TIMEOUT=60 \
  AE_WG_INTERFACE=wg-hub \
  AE_WG_DUMP_CMD="sudo -n ${WG_BIN} show {iface} dump" \
  AE_AGENT_API_PORT=9110 \
  AE_AGENT_API_TOKEN=devtoken \
  AE_ENABLE_NETFS=1 \
  AE_STORAGE_SEED_DEFAULTS=1 \
  AE_STORAGE_NFS_SERVER=10.255.0.1 \
  AE_STORAGE_NFS_PATH=/exports/k1s \
  AE_STORAGE_NFS_CLASS=k1s-nfs \
  AE_APISHIM_ETCD_ENDPOINTS=http://127.0.0.1:2379 \
  make k1s-core
```
This seeds `k1s-nfs` in apishim for the NetFS single-host flow.
Strict CRI expectation for this lane:
- core infra components appear in `sudo crictl pods`
- root Podman does not run `dev-etcd-1`/`dev-nats-hub-1`/`dev-postgres-1` for this profile.
- stack sandboxes use host-network CRI config without explicit hostname
  (avoids runc UTS namespace errors).

Note
- Keep Rosenpass dirs under `/var/lib/ae/` (or another non-repo path) when running nodes with `sudo` to avoid root-owned files under `state/`.

3. Start the hub node:
```
sudo -E env PATH="$SUDO_PATH" \
AE_RUNTIME_BACKEND=cri \
AE_CRI_RUNTIME_HANDLER=runc \
AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
AE_NODE_ID=hub-1 \
AE_NODE_LABELS="role=hub,site=hub,wg_role=hub,wg_psk=rp,wg_endpoint=192.168.29.143:51820" \
AE_POD_CIDR=10.42.0.0/24 \
AE_ROSENPASS_ENABLED=1 \
AE_ROSENPASS_CONFIG=controller \
AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-hub \
AE_ROSENPASS_INTERFACE=wg-hub \
AE_WG_LISTEN_PORT=51820 \
AE_WG_ADDRESS=10.255.0.1/32 \
AE_ENABLE_NETFS=1 \
AE_APISHIM_DSN=postgresql://shim:shim@127.0.0.1:5432/shim \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://127.0.0.1:9110 \
AE_AGENT_ENDPOINT=http://10.255.0.1:9111 \
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
sudo -E make edge-site-cri SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224

sudo -E env PATH="$SUDO_PATH" \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  EDGE_INGRESS_MODE=edge-local \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_REGISTRY_MODE=managed \
  AE_CRI_REGISTRY_INSECURE=1 \
  AE_SITE_ID=sea-edge-02 \
  AE_NODE_ID=edge-1 \
  AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224 \
  AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=state/profiles/k1s-core/edge-local \
  AE_LOG_LEVEL=debug \
  make k1s-edge-core-cri
```
Note:
- Keep `AE_NATS_URL` aligned with the `EDGE_PORT` passed to `make edge-site-cri`. In strict CRI mode, `k1s-edge-core-cri` will derive the edge NATS listen port from `EDGE_PORT` or the explicit port in `AE_NATS_URL`.

4a. Validate edge-local preconditions (root-safe):
```bash
cpid=$(pgrep -f "python -m ae.controller" | head -n1)
gpid=$(pgrep -f "python -m ae.gateway" | head -n1)
test -n "$cpid" && test -n "$gpid"

sudo cat "/proc/$cpid/environ" | tr '\0' '\n' | rg 'EDGE_INGRESS_MODE|AE_ROUTE_BUNDLE_ENABLED|AE_TRANSPORT_BACKEND|AE_NATS_URL'
sudo cat "/proc/$gpid/environ" | tr '\0' '\n' | rg 'EDGE_INGRESS_MODE|AE_EDGE_LOCAL_INGRESS_CONFIG_DIR|AE_SITE_ID|AE_NODE_ID|AE_TRANSPORT_BACKEND|AE_NATS_URL'

ls -ld state/profiles/k1s-core/edge-local
ls -l state/profiles/k1s-core/edge-local/edge-local.caddy || true
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18081/ || true

# hub-controller must be allowed to publish route bundles
rg -n 'user: "hub-controller"|k1s.v1.site.\*.routes.bundle' ops/dev/nats-hub.conf
```
Expected:
- core and gateway both report `EDGE_INGRESS_MODE=edge-local`.
- core reports `AE_ROUTE_BUNDLE_ENABLED=1`.
- core and gateway both report `AE_TRANSPORT_BACKEND=nats-core` or `nats-js`, and `AE_NATS_URL` is set.
- gateway reports `AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=.../state/profiles/k1s-core/edge-local`.
- backend probe should become `2xx` once app workload is ready (for tier checks).
- `ops/dev/nats-hub.conf` includes `k1s.v1.site.*.routes.bundle` in `hub-controller` publish permissions.

If you updated `ops/dev/nats-hub.conf`, recreate/restart hub NATS before re-running edge-local checks.

Bundle behavior note:
- Edge-local route bundles publish for site IDs discovered from node leases and
  `EdgeIngressRoute` placement sites. A missing lease does not block publish if
  routes exist for that site.

5. Start the edge node (single-host routing adjustments):
```
sudo -E env PATH="$SUDO_PATH" \
AE_RUNTIME_BACKEND=cri \
AE_INFRA_BACKEND=cri \
AE_CRI_RUNTIME_HANDLER=runc \
AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
AE_NODE_ID=edge-1 \
AE_POD_CIDR=10.42.1.0/24 \
AE_WG_INTERFACE=wg-edge \
AE_ROSENPASS_INTERFACE=wg-edge \
AE_WG_ADDRESS=10.255.0.3/32 \
AE_WG_TABLE=off \
AE_WG_LISTEN_PORT=51821 \
AE_ROSENPASS_DIR=/var/lib/ae/rosenpass-edge \
AE_NODE_LABELS="role=worker,site=sea-edge-02,wg_role=spk,wg_psk=rp" \
AE_ENABLE_NETFS=1 \
AE_APISHIM_DSN=postgresql://shim:shim@127.0.0.1:5432/shim \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://127.0.0.1:9110 \
AE_AGENT_ENDPOINT=http://10.255.0.3:9112 \
make k1s-edge-node
```
Note:
- `AE_NODE_ID=edge-1` keeps scheduler targeting aligned with
  `specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml`.
- `role=worker` is required for `specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml`.
- `AE_ENABLE_NETFS=1` + `AE_APISHIM_DSN` are required for Option C PVC mounts.

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
3. Confirm node agents are reachable over WG endpoints from the hub host
   (strict-CRI apishim uses host networking).
```bash
curl -sSf http://10.255.0.1:9111/readyz
curl -sSf http://10.255.0.3:9112/readyz
```
If either endpoint fails, stop and fix node endpoint advertisement/WG routing before continuing shell tests.
4a. Validate non-root auth exports (once per new shell).
```bash
test -r state/profiles/k1s-core/apishim.cli.env && echo "shared CLI env readable"
test -r state/profiles/k1s-core/apishim.ca.crt && echo "shared CA bundle readable"
grep -E '^AE_APISHIM_(SERVER|MINT_TOKEN|CA_BUNDLE)=' state/profiles/k1s-core/apishim.cli.env
source <(ae auth local --strict)
env | grep -E '^AE_APISHIM_(SERVER|MINT_TOKEN|CA_BUNDLE)='
```
4b. Test exec and port-forward against pods pinned to Site B.
```
source <(ae auth local --strict)
ae apply -f docs/site/examples/shell-demo-node-sea-edge-02-edge-1.yaml
ae status shell-demo-node-sea-edge-02-edge-1 --wide --events
ae shell shell-demo-node-sea-edge-02-edge-1 -- /bin/sh
ae port-forward shell-demo-node-sea-edge-02-edge-1 18082:8080
```
Note
- `/system` overlay and Rosenpass indicators are best-effort; missing status usually means the controller cannot read `wg dump` or `rosenpass-status.json`.

#### Troubleshooting exec/port-forward
If `spdy exec failed: [Errno 111] Connection refused` persists:
1. Confirm apishim endpoint is listening and healthy (host-mode or CRI apishim).
```bash
ss -ltn | rg ':8445'
sudo crictl pods | rg 'k1s-core-apishim|k1s-core-caddy' || true
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:8445/healthz
tail -n 80 state/profiles/k1s-core/apishim.log
```
If `:8445` is not listening or health is not `200`, restart core to recreate
the apishim component:
```bash
make k1s-core
```
2. Confirm the node endpoints are reachable from apishim.
```
ae nodes
```
If endpoints show hostnames/loopback (for example `http://127.0.0.1:9111`, `http://127.0.0.1:9112`, or `http://h4ckt0p:9111`) in this strict WG lane, restart the nodes with explicit WG endpoints (single-host example: `AE_AGENT_ENDPOINT=http://10.255.0.1:9111` and `AE_AGENT_ENDPOINT=http://10.255.0.3:9112`).
3. From the host, probe node agents over WG:
```
curl -sSf http://10.255.0.1:9111/readyz
curl -sSf http://10.255.0.3:9112/readyz
```
4. Verify your CLI points to the correct apishim server:
```
echo "$AE_APISHIM_SERVER"
```
On a remote host, override with `export AE_APISHIM_SERVER=https://<HUB_IP>:8445`, then run `source <(ae auth local --strict)` again.

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

## Option C — Site-to-site NetFS storage over WireGuard/Rosenpass {#option-c-netfs}

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
source <(ae auth local --strict)
export AE_APISHIM_TOKEN=$(sudo awk -F= '/^AE_APISHIM_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
export AE_APISHIM_READ_TOKEN=$(sudo awk -F= '/^AE_APISHIM_READ_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
kctl_ro() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_READ_TOKEN}" --insecure-skip-tls-verify "$@"; }
kctl_rw() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_TOKEN}" --insecure-skip-tls-verify "$@"; }
```
Note:
- In `zsh`, avoid `KCTL="kubectl ..."` followed by `$KCTL ...` because it can be
  parsed as a single command path. Use shell helpers (`kctl_ro` / `kctl_rw`).
- `ae auth local --strict` is enough for `ae shell`/`ae port-forward`, but this kubectl path still needs direct read/write apishim bearer tokens.
- When `k1s-core` is started with `sudo`, `ae auth local --strict` can hold stale direct bearer tokens from a prior run.
  Refreshing from `state/profiles/k1s-core/apishim.env` avoids `missing/invalid bearer token`.

Step 3b: Preflight checks (required before applying NetFS workloads)
```bash
ae nodes
kctl_ro get storageclass k1s-nfs
python -m ae.cli plan -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml --verbose
```
Required state:
- Hub node labels include `role=hub,site=hub`.
- Edge node labels include `role=worker,site=sea-edge-02`.
- Nodes that mount PVCs were started with `AE_ENABLE_NETFS=1`.
- Storage class `k1s-nfs` exists in apishim.
- Plan output shows at least one eligible edge node for
  `netfs-nfs-sea-edge-02-edge-1`.
If `kctl_ro get storageclass k1s-nfs` returns `NotFound`, restart core with NFS
seeding env and re-check:
```bash
AE_DEV_LOCAL=1 \
EDGE_INGRESS_MODE=core-proxy \
POSTGRES_BIND_IP=<HUB_DB_BIND_IP> \
POSTGRES_PORT=5432 \
AE_STORAGE_NFS_SERVER=<HUB_WG_OR_LAN_IP> \
AE_STORAGE_NFS_PATH=/exports/k1s \
AE_STORAGE_PROVISIONERS=/etc/ae/storage-provisioners.yaml \
make k1s-core
kctl_ro get storageclass k1s-nfs
```
If the edge node is missing `role=worker`, restart it with:
```bash
sudo -E \
AE_NODE_ID=edge-1 \
AE_NODE_LABELS="role=worker,site=sea-edge-02,wg_role=spk,wg_psk=rp" \
AE_ENABLE_NETFS=1 \
AE_APISHIM_DSN=postgresql://shim:shim@<HUB_DB_BIND_IP>:5432/shim \
make k1s-edge-node
```

Step 4A: Primary lane (CSI CephFS)
- Ensure `cephfs-rwx` exists in `/etc/ae/storage-provisioners.yaml` with:
  - `type: csi`
  - valid `controllerEndpoint` and `nodeEndpoint`
  - `topologyKeys: ["site"]`
- Apply CSI workload:
```bash
kctl_rw apply -f specs/examples/netfs-csi-sea-edge-02-pvc.yaml
python -m ae.cli apply -f specs/examples/netfs-csi-sea-edge-02-edge-1.yaml
```

Step 4B: Fallback lane (NFS)
- Ensure `k1s-nfs` exists in `/etc/ae/storage-provisioners.yaml` or seed it via
  `AE_STORAGE_NFS_SERVER` + `AE_STORAGE_NFS_PATH`.
- Apply NFS workload:
```bash
kctl_rw apply -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml --validate=false
python -m ae.cli apply -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml
python -m ae.cli apply -f specs/examples/netfs-nfs-hub-reader.yaml
```
If `kubectl` is not configured against apishim, use the REST pattern in
`scripts/netfs_smoke.sh` (`PUT /api/v1/namespaces/<ns>/persistentvolumeclaims/<name>`).
If apishim returns `proto: cannot parse invalid wire-format data`, keep
`--validate=false` on PVC apply or use the REST `PUT` fallback.

Step 5: Validate PVC, attachments, and workload health
```bash
source <(ae auth local --strict)
export AE_APISHIM_TOKEN=$(sudo awk -F= '/^AE_APISHIM_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
export AE_APISHIM_READ_TOKEN=$(sudo awk -F= '/^AE_APISHIM_READ_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
kctl_ro() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_READ_TOKEN}" --insecure-skip-tls-verify "$@"; }
kctl_rw() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_TOKEN}" --insecure-skip-tls-verify "$@"; }
ae nodes
kctl_ro get pvc sea-netfs-pvc -n default -o wide
ae status netfs-nfs-sea-edge-02-edge-1 --wide --events
ae status netfs-nfs-hub-reader --wide --events
bash scripts/dev/netfs_validate.sh \
  --runtime auto \
  --namespace default \
  --writer-app netfs-nfs-sea-edge-02-edge-1 \
  --reader-app netfs-nfs-hub-reader \
  --mount-path /data

# Optional: force CRI runtime fallback on CRI lanes
bash scripts/dev/netfs_validate.sh --runtime cri
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
source <(ae auth local --strict)
export AE_APISHIM_TOKEN=$(sudo awk -F= '/^AE_APISHIM_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
export AE_APISHIM_READ_TOKEN=$(sudo awk -F= '/^AE_APISHIM_READ_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
kctl_ro() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_READ_TOKEN}" --insecure-skip-tls-verify "$@"; }
kctl_rw() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_TOKEN}" --insecure-skip-tls-verify "$@"; }
ae nodes
kctl_ro get storageclass k1s-nfs
python -m ae.cli plan -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml --verbose
kctl_rw apply -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml --validate=false
python -m ae.cli apply -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml
python -m ae.cli apply -f specs/examples/netfs-nfs-hub-reader.yaml
kctl_ro get pvc sea-netfs-pvc -n default -o wide
ae status netfs-nfs-sea-edge-02-edge-1 --wide --events
ae status netfs-nfs-hub-reader --wide --events
bash scripts/dev/netfs_validate.sh \
  --runtime auto \
  --namespace default \
  --writer-app netfs-nfs-sea-edge-02-edge-1 \
  --reader-app netfs-nfs-hub-reader \
  --mount-path /data

# Optional: force CRI runtime fallback on CRI lanes
bash scripts/dev/netfs_validate.sh --runtime cri
```

If storage validation output is noisy but workloads are `ready`:
- Prefer the runtime-aware validator script (uses `ae exec` first, then runtime fallback):
```bash
bash scripts/dev/netfs_validate.sh --runtime auto

# For CRI lanes, force CRI fallback explicitly
bash scripts/dev/netfs_validate.sh --runtime cri
```
- This indicates NetFS data path is healthy and the remaining issue is shell/stream plumbing.
- On CRI lanes, do not run Podman fallback commands just because `podman` is installed.
- Confirm by checking apishim logs for exec status lines:
```bash
tail -n 120 state/profiles/k1s-core/apishim.log | rg -n 'exec.start|exec.end|SPDY exec|WS exec'
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

### Troubleshooting: edge writer degraded (`ScheduleWarning`)

Symptom:
- `netfs-nfs-sea-edge-02-edge-1` is `degraded`.
- Events include `ScheduleWarning: no eligible nodes after storage constraints; skipping placement`.
- `netfs-nfs-hub-reader` may still be `ready`.

Decision path:
1. Confirm selector labels first (most common root cause):
```bash
ae nodes
python -m ae.cli plan -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml --verbose
```
`netfs-nfs-sea-edge-02-edge-1` requires `role=worker,site=sea-edge-02`.
If missing, restart edge node with:
```bash
sudo -E \
AE_NODE_ID=edge-1 \
AE_NODE_LABELS="role=worker,site=sea-edge-02,wg_role=spk,wg_psk=rp" \
AE_ENABLE_NETFS=1 \
AE_APISHIM_DSN=postgresql://shim:shim@<HUB_DB_BIND_IP>:5432/shim \
make k1s-edge-node
```
2. Confirm `k1s-nfs` exists:
```bash
source <(ae auth local --strict)
export AE_APISHIM_TOKEN=$(sudo awk -F= '/^AE_APISHIM_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
export AE_APISHIM_READ_TOKEN=$(sudo awk -F= '/^AE_APISHIM_READ_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
kctl_ro() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_READ_TOKEN}" --insecure-skip-tls-verify "$@"; }
kctl_rw() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_TOKEN}" --insecure-skip-tls-verify "$@"; }
kctl_ro get storageclass k1s-nfs
```
If it returns `NotFound`, restart core with `AE_STORAGE_NFS_SERVER` and
`AE_STORAGE_NFS_PATH`, then re-check.
3. Apply PVC with apishim-safe validation settings:
```bash
kctl_rw apply -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml --validate=false
```
If this still fails with wire-format validation issues, use REST `PUT` as shown
in `scripts/netfs_smoke.sh`.
4. Confirm PVC exists and is bound:
```bash
kctl_ro get pvc sea-netfs-pvc -n default -o wide
```
5. Confirm storage class topology is compatible with edge labels:
```bash
kctl_ro get storageclass k1s-nfs -o yaml
```
If `topologyKeys` or `allowedTopologies` exclude the edge labels, adjust the
storage class or node labels and re-apply workloads.

### Recovery sequence (single-host, copy/paste)

Use this when the writer is degraded and events show both `ScheduleWarning` and
PVC/storageclass failures.
```bash
source <(ae auth local --strict)
export AE_APISHIM_TOKEN=$(sudo awk -F= '/^AE_APISHIM_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
export AE_APISHIM_READ_TOKEN=$(sudo awk -F= '/^AE_APISHIM_READ_TOKEN=/{print $2}' state/profiles/k1s-core/apishim.env)
kctl_ro() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_READ_TOKEN}" --insecure-skip-tls-verify "$@"; }
kctl_rw() { kubectl --kubeconfig=/dev/null --server="${AE_APISHIM_SERVER}" --token="${AE_APISHIM_TOKEN}" --insecure-skip-tls-verify "$@"; }

# 1) reset failed workloads and stale PVC
python -m ae.cli delete netfs-nfs-sea-edge-02-edge-1 --purge
python -m ae.cli delete netfs-nfs-hub-reader --purge
kctl_rw delete -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml --ignore-not-found --validate=false

# 2) verify scheduler prerequisites
ae nodes
python -m ae.cli plan -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml --verbose
kctl_ro get storageclass k1s-nfs

# 3) re-apply PVC and workloads
kctl_rw apply -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml --validate=false
python -m ae.cli apply -f specs/examples/netfs-nfs-sea-edge-02-edge-1.yaml
python -m ae.cli apply -f specs/examples/netfs-nfs-hub-reader.yaml

# 4) verify ready + RWX behavior
kctl_ro get pvc sea-netfs-pvc -n default -o wide
ae status netfs-nfs-sea-edge-02-edge-1 --wide --events
ae status netfs-nfs-hub-reader --wide --events
ae shell netfs-nfs-sea-edge-02-edge-1 -- sh -lc "echo recover-$(date +%s) > /data/hello.txt && cat /data/hello.txt"
ae shell netfs-nfs-hub-reader -- cat /data/hello.txt
```

### Option C Cleanup
Assumes `kctl_ro`/`kctl_rw` from Step 3a (or re-run the same auth/token snippet).
```bash
python -m ae.cli delete netfs-nfs-hub-reader --purge
python -m ae.cli delete netfs-nfs-sea-edge-02-edge-1 --purge
python -m ae.cli delete netfs-csi-sea-edge-02-edge-1 --purge
kctl_rw delete -f specs/examples/netfs-nfs-sea-edge-02-pvc.yaml --ignore-not-found --validate=false
kctl_rw delete -f specs/examples/netfs-csi-sea-edge-02-pvc.yaml --ignore-not-found --validate=false
```

## Option D — Canonical ingress modes over WireGuard/Rosenpass {#option-d-ingress}

Treat these three ingress modes as canonical for core/edge deployments:
- `core-proxy`
- `core-to-edge-public`
- `edge-local`

This option layers ingress validation on top of Option B (WG/Rosenpass + hub/edge
transport). Use `core-to-edge-public` as the canonical name in docs/config.
For the full capability sequence (single-host, multi-host, faults, repeatability),
see `docs/guides/ingress-capability-test-sequence.md`.

Shared setup:
- Complete Option B first (hub + edge gateways + node agents).
- Use the active core specs directory so the controller can import
  `EdgeIngressRoute`/`SiteIngressEndpoint` resources.

```bash
CORE_SPECS=${CORE_SPECS:-state/profiles/k1s-core/specs}
mkdir -p "$CORE_SPECS"

# Normalize ownership after any prior sudo-driven runs.
CORE_SPECS_GROUP="$(id -gn)"
if getent group aecli >/dev/null 2>&1; then
  CORE_SPECS_GROUP="aecli"
fi
sudo chown -R "$USER:$CORE_SPECS_GROUP" "$CORE_SPECS"
sudo chmod -R g+rwX "$CORE_SPECS"
sudo find "$CORE_SPECS" -type d -exec chmod 2775 {} \;
test -w "$CORE_SPECS" && echo "core specs writable: $CORE_SPECS"
```

### Deterministic single-host sequence (CRI, two-tier gate)

Use this lane when you want repeatable functional ingress checks on one host.
Run the long-lived `make` targets in separate terminals.

1. Bring up Option B (same-host CRI) first so hub + edge nodes are Ready.
2. Deploy a pinned edge workload used by all three ingress modes:
```bash
python -m ae.cli apply -f specs/examples/app-svc-node-sea-edge-02-edge-1.yaml
python -m ae.cli status app-svc --watch 2 --timeout 180 --events
```
3. Validate `core-proxy`:
```bash
# terminal A (core)
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core

# terminal B (edge gateway)
AE_SITE_ID=sea-edge-02 AE_NODE_ID=edge-1 EDGE_INGRESS_MODE=core-proxy make k1s-edge-core

# terminal C (checks)
scripts/dev/test_ingress_modes_single_host.sh --mode core-proxy --tier tier1 --keep-specs
```
Expected:
- `HTTP OK (2xx/3xx)` on `http://127.0.0.1:10080/` (commonly `301` when redirect is enabled).
- `HTTP OK` on `https://127.0.0.1:10443/` (commonly `200`).
- Final line `PASS mode=core-proxy tier=tier1`.

Optional direct probe without redirect-follow (avoids DNS ambiguity from `curl -L`):
```bash
curl -sS -k -o /dev/null -w 'http:%{http_code}\n' \
  --header 'Host: app-core-proxy.home.arpa' \
  http://127.0.0.1:10080/
curl -sS -k -o /dev/null -w 'https:%{http_code}\n' \
  --header 'Host: app-core-proxy.home.arpa' \
  https://127.0.0.1:10443/
```
4. Validate `core-to-edge-public`:
```bash
# terminal A (restart core in public mode)
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-to-edge-public make k1s-core

# terminal B (gateway can remain running; restart if needed)
AE_SITE_ID=sea-edge-02 AE_NODE_ID=edge-1 EDGE_INGRESS_MODE=core-proxy make k1s-edge-core

# terminal C (checks)
scripts/dev/test_ingress_modes_single_host.sh \
  --mode core-to-edge-public \
  --tier tier1 \
  --core-ingress-url https://127.0.0.1:10443/
```
5. Validate `edge-local` Tier 1 (required baseline gate):
```bash
# terminal A (restart core with bundle publisher enabled)
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=edge-local AE_ROUTE_BUNDLE_ENABLED=1 make k1s-core

# verify hub-controller publish permission includes route bundles
rg -n 'user: "hub-controller"|k1s.v1.site.\*.routes.bundle' ops/dev/nats-hub.conf

# terminal B (restart gateway in edge-local mode)
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
EDGE_INGRESS_MODE=edge-local \
AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=state/profiles/k1s-core/edge-local \
make k1s-edge-core

# terminal C (checks)
scripts/dev/test_ingress_modes_single_host.sh --mode edge-local --tier tier1
```
6. Optional stricter gates (run after Tier 1 baseline is green):
```bash
# stricter negatives for core-proxy and core-to-edge-public
scripts/dev/test_ingress_modes_single_host.sh --mode core-proxy --tier tier1 --strict
scripts/dev/test_ingress_modes_single_host.sh \
  --mode core-to-edge-public \
  --tier tier1 \
  --strict \
  --core-ingress-url https://127.0.0.1:10443/

# edge-local strict mutation check (bundle enabled)
scripts/dev/test_ingress_modes_single_host.sh --mode edge-local --tier tier1 --strict

# edge-local strict negative (bundle disabled):
# restart core with AE_ROUTE_BUNDLE_ENABLED=0, then run:
scripts/dev/test_ingress_modes_single_host.sh \
  --mode edge-local \
  --tier tier1 \
  --strict \
  --expect-bundle-disabled
```
7. Optional Tier 2 data-plane checks (all modes):
```bash
# core-proxy tier2
scripts/dev/test_ingress_modes_single_host.sh --mode core-proxy --tier tier2

# core-to-edge-public tier2
scripts/dev/test_ingress_modes_single_host.sh --mode core-to-edge-public --tier tier2

# edge-local tier2 (default listener: https://127.0.0.1:${CADDY_HTTPS_PORT:-8443}/)
scripts/dev/test_ingress_modes_single_host.sh \
  --mode edge-local \
  --tier tier2

# edge-local tier2 (explicit listener override)
scripts/dev/test_ingress_modes_single_host.sh \
  --mode edge-local \
  --tier tier2 \
  --edge-local-listener-url https://127.0.0.1:11443/
```
Tier 2 is intentionally optional in this lane; track failures separately while
keeping Tier 1 as the mandatory day-to-day gate.

8. Run the workload-variation ingress matrix across all canonical modes:
```bash
scripts/dev/test_ingress_matrix_single_host.sh \
  --modes core-proxy,core-to-edge-public,edge-local \
  --archetypes http-static,http-path-routing,http-multi-replica,http-multiport,http-redirect,http-large-payload,http2-unary \
  --tier tier1
```
Known-good edge-local deep proof pattern:
```bash
scripts/dev/test_ingress_matrix_single_host.sh \
  --modes edge-local \
  --archetypes lb-distribution \
  --tier tier2 \
  --validation-profile deep \
  --lb-proof-scope edge-only \
  --lb-sample-requests 5000 \
  --lb-min-backends 2 \
  --lb-max-skew-ratio 0.35 \
  --edge-local-listener-url https://lb-distribution-edge-local.home.arpa:443/
```
Notes:
- Archetype manifests live under `specs/examples/ingress-matrix/`.
- The matrix script generates per-row `EdgeIngressRoute` fixtures and calls
  `scripts/dev/test_ingress_modes_single_host.sh` for each mode/archetype pair.
- If `state/profiles/k1s-core/specs` ownership drifts after sudo runs, re-apply:
```bash
CORE_SPECS=state/profiles/k1s-core/specs
sudo chown -R "$USER:$(id -gn)" "$CORE_SPECS"
sudo chmod -R g+rwX "$CORE_SPECS"
sudo find "$CORE_SPECS" -type d -exec chmod 2775 {} \;
test -w "$CORE_SPECS" && echo "core specs writable: $CORE_SPECS"
```
- Matrix results are written to:
  `state/test-results/ingress-matrix-<timestamp>.json`
- On failure, diagnostics are collected under:
  `state/test-results/failures/ingress-matrix-<timestamp>/`

9. Capability test track 1 (multi-host CRI topology):
```bash
scripts/dev/test_ingress_matrix_cri.sh \
  --topology multi-host \
  --core-host <core-host-ip> \
  --edge-host <edge-host-ip> \
  --modes core-proxy,core-to-edge-public,edge-local \
  --archetypes http-static,http-path-routing,http-multi-replica,http-multiport,http-redirect,http-large-payload,http2-unary \
  --tier tier1
```

10. Capability test tracks 3 and 4 (fault cycles + repeatability):
```bash
scripts/dev/test_ingress_matrix_repeat.sh \
  --iterations 10 \
  --topology multi-host \
  --include-faults \
  --faults specs-permission-drift,backend-unavailable,nats-route-bundle-permission \
  --modes core-proxy,core-to-edge-public,edge-local \
  --archetypes http-static,http-path-routing,http-multi-replica,http-multiport,http-redirect,http-large-payload,http2-unary \
  --tier tier1
```
Outputs:
- Per-run matrix JSON: `state/test-results/ingress-matrix-<timestamp>-iterN.json`
- Per-run logs: `state/test-results/ingress-matrix-<timestamp>-iterN.log`
- Aggregate summary: `state/test-results/ingress-matrix-summary-<timestamp>.json`

11. Run individual fault injection/recovery scenarios when debugging:
```bash
# simulate specs-dir permission drift and recover
scripts/dev/ingress_fault_injection.sh --fault specs-permission-drift --action cycle

# simulate backend removal and restore
scripts/dev/ingress_fault_injection.sh --fault backend-unavailable --action cycle \
  --app-name ingress-matrix-static \
  --app-manifest specs/examples/ingress-matrix/http-static.yaml

# temporarily remove route-bundle publish permission and restore
scripts/dev/ingress_fault_injection.sh --fault nats-route-bundle-permission --action cycle \
  --route-bundle-config ops/dev/nats-hub.conf \
  --nats-reload-cmd "make k1s-core"
```

### Mode 1 — `core-proxy` (default, NAT-friendly)

#### Production pattern (split hosts)
1. Start core with core-proxy mode:
```bash
AE_DEV_LOCAL=1 \
EDGE_INGRESS_MODE=core-proxy \
make k1s-core
```
2. Start edge gateway/node per Option B.
3. Add the route resource into the core specs dir:
```bash
cp specs/examples/edge-ingress-route-core-proxy.yaml "$CORE_SPECS"/
```

#### Single-host lab pattern
Use hostnames that target core ingress and per-site fallback host:
```hosts
127.0.0.1 app-core-proxy.home.arpa
127.0.0.1 sea-edge-02.edge.local
```
Deploy the pinned edge workload (preferred over a throwaway local responder):
```bash
python -m ae.cli apply -f specs/examples/app-svc-node-sea-edge-02-edge-1.yaml
python -m ae.cli status app-svc --watch 2 --timeout 180 --events
```

#### Routing and behavior
- Client request hits core Envoy for `app-core-proxy.home.arpa`.
- Core route forwards to the assigned per-site `core_proxy_port`.
- Rathole forwards that traffic to `AE_EDGE_INGRESS_LOCAL_ADDR` on the edge side.
- Edge does not need a public endpoint; this mode is NAT/CGNAT-friendly.

#### Validation
```bash
rg -n "app-core-proxy.home.arpa|sea-edge-02.edge.local" state/profiles/k1s-core/edge-ingress/envoy.yaml
curl -sS -k -o /dev/null -w 'http:%{http_code}\n' \
  --header 'Host: app-core-proxy.home.arpa' \
  http://127.0.0.1:10080/
curl -sS -k -o /dev/null -w 'https:%{http_code}\n' \
  --header 'Host: app-core-proxy.home.arpa' \
  https://127.0.0.1:10443/
```
Expected:
- Envoy config includes `app-core-proxy.home.arpa`.
- HTTP listener returns `2xx/3xx` (most commonly `301` when redirect is enabled).
- HTTPS listener returns `2xx` when tunnel/upstream is healthy.
- `5xx` indicates route exists but upstream/tunnel failed.
- `000` indicates probe/client path failure; do not use `-L` for this check.

#### Cleanup
```bash
rm -f "$CORE_SPECS/edge-ingress-route-core-proxy.yaml"
```

### Mode 2 — `core-to-edge-public` (core forwards to edge POP)

#### Production pattern (split hosts)
1. Start core with public-edge mode:
```bash
AE_DEV_LOCAL=1 \
EDGE_INGRESS_MODE=core-to-edge-public \
make k1s-core
```
2. Start edge gateway/node per Option B.
3. Add the site public endpoint and route resources:
```bash
cp specs/examples/site-ingress-endpoint-sea-edge-02-public.yaml "$CORE_SPECS"/
cp specs/examples/edge-ingress-route-core-to-edge-public.yaml "$CORE_SPECS"/
```

#### Single-host lab pattern
Use hostnames for core ingress host + simulated edge POP host:
```hosts
127.0.0.1 app-public.home.arpa
127.0.0.1 pop-sea-edge-02.home.arpa
```
Use the pinned edge workload host port as the local POP endpoint:
```bash
cat > "$CORE_SPECS/site-ingress-endpoint-sea-edge-02-public.yaml" <<'EOF'
apiVersion: k1s.io/v1
kind: SiteIngressEndpoint
metadata:
  name: sea-edge-02
spec:
  mode: core-to-edge-public
  public:
    urls:
      - url: http://127.0.0.1:18081
        expectedSANs:
          - pop-sea-edge-02.home.arpa
EOF
```

#### Routing and behavior
- Client request hits core Envoy for `app-public.home.arpa`.
- Core resolves `SiteIngressEndpoint.public.urls` and forwards directly to the
  configured public POP endpoint.
- Rathole is not required for this mode.
- If `public.urls` is missing/unreachable, traffic fails from the core side.

#### Validation
```bash
rg -n "app-public.home.arpa|pop-sea-edge-02.home.arpa" state/profiles/k1s-core/edge-ingress/envoy.yaml
curl -sS -k -o /dev/null -w 'http:%{http_code}\n' \
  --header 'Host: app-public.home.arpa' \
  http://127.0.0.1:10080/
curl -sS -k -o /dev/null -w 'https:%{http_code}\n' \
  --header 'Host: app-public.home.arpa' \
  https://127.0.0.1:10443/
```
Expected:
- Envoy config includes `app-public.home.arpa` and a DNS cluster for the POP.
- HTTP listener returns `2xx/3xx` (often `301` when redirect is enabled).
- Reachable POP returns `2xx` on HTTPS.
- Unreachable POP returns `5xx`.

#### Cleanup
```bash
rm -f "$CORE_SPECS/site-ingress-endpoint-sea-edge-02-public.yaml"
rm -f "$CORE_SPECS/edge-ingress-route-core-to-edge-public.yaml"
```

### Mode 3 — `edge-local` (edge-only ingress path)

#### Production pattern (split hosts)
1. Start core with edge-local mode and route bundle publishing enabled:
```bash
AE_DEV_LOCAL=1 \
EDGE_INGRESS_MODE=edge-local \
AE_ROUTE_BUNDLE_ENABLED=1 \
make k1s-core
```
1a. Ensure hub-controller can publish route bundles:
```bash
rg -n 'user: "hub-controller"|k1s.v1.site.\*.routes.bundle' ops/dev/nats-hub.conf
```
2. Start edge gateway in edge-local mode:
```bash
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
EDGE_INGRESS_MODE=edge-local \
AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=state/profiles/k1s-core/edge-local \
make k1s-edge-core
```
3. Run the Tier 1 edge-local gate (the script stages `edge-ingress-route-edge-local.yaml` automatically):
```bash
scripts/dev/test_ingress_modes_single_host.sh --mode edge-local --tier tier1
```

#### Single-host lab pattern
Use a local-only edge hostname:
```hosts
127.0.0.1 app-edge-local.home.arpa
```
Keep this hostname unadvertised publicly; test from the edge network/host only.

#### Routing and behavior
- Core publishes route bundles (control plane only).
- Edge gateway applies bundles and renders edge-local ingress config.
- Data path is local to the edge site; core ingress is not in request path.
- If core is unavailable after bundles are applied, edge can continue serving
  existing routes until a config change is needed.

#### Validation
Tier 1 (required baseline gate, script-driven):
```bash
scripts/dev/test_ingress_modes_single_host.sh --mode edge-local --tier tier1
```
Expected:
- `edge-local preflight OK ...`
- `edge-local route site OK ...`
- `edge-local tier1 checks passed`
- final line `PASS mode=edge-local tier=tier1`

Optional manual spot-checks after Tier 1 passes:
```bash
ls state/profiles/k1s-core/edge-local/edge-local.caddy
rg -n "app-edge-local.home.arpa" state/profiles/k1s-core/edge-local/edge-local.caddy
rg -n "app-edge-local.home.arpa" state/profiles/k1s-core/edge-ingress/envoy.yaml || true
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18081/
```
Expected:
- Edge-local Caddyfile is rendered with `app-edge-local.home.arpa`.
- Core Envoy config does not need a route for this host.
- If `AE_ROUTE_BUNDLE_ENABLED` is unset/0, edge-local config will not update.

Tier 2 (optional strict gate, full listener data path):
```bash
# core-proxy
scripts/dev/test_ingress_modes_single_host.sh --mode core-proxy --tier tier2

# core-to-edge-public
scripts/dev/test_ingress_modes_single_host.sh --mode core-to-edge-public --tier tier2

# edge-local (default listener uses CADDY_HTTPS_PORT, fallback 8443)
scripts/dev/test_ingress_modes_single_host.sh \
  --mode edge-local \
  --tier tier2

# edge-local explicit listener override
scripts/dev/test_ingress_modes_single_host.sh \
  --mode edge-local \
  --tier tier2 \
  --edge-local-listener-url https://127.0.0.1:11443/
```
Use Tier 2 when you need release-level confidence in ingress serving. Keep
Tier 1 mandatory for routine dev/CI loops.

#### Cleanup
```bash
rm -f "$CORE_SPECS/edge-ingress-route-edge-local.yaml"
```

### Post-storage ingress regression check (all canonical modes)

Run this after Option C storage validation to confirm ingress behavior did not regress:
1. Confirm storage workloads are healthy:
```bash
ae status netfs-nfs-sea-edge-02-edge-1 --wide --events
ae status netfs-nfs-hub-reader --wide --events
```
2. Validate each ingress mode endpoint behavior:
```bash
curl -sS -k -o /dev/null -w 'core-proxy:http:%{http_code}\n' \
  --header 'Host: app-core-proxy.home.arpa' \
  http://127.0.0.1:10080/
curl -sS -k -o /dev/null -w 'core-proxy:https:%{http_code}\n' \
  --header 'Host: app-core-proxy.home.arpa' \
  https://127.0.0.1:10443/
curl -sS -k -o /dev/null -w 'core-to-edge-public:http:%{http_code}\n' \
  --header 'Host: app-public.home.arpa' \
  http://127.0.0.1:10080/
curl -sS -k -o /dev/null -w 'core-to-edge-public:https:%{http_code}\n' \
  --header 'Host: app-public.home.arpa' \
  https://127.0.0.1:10443/
rg -n "app-edge-local.home.arpa" state/profiles/k1s-core/edge-local/edge-local.caddy
```
Expected:
- `core-proxy`: request path is core ingress -> tunnel/proxy -> edge local backend.
- `core-to-edge-public`: core ingress forwards to edge POP endpoint from `SiteIngressEndpoint`.
- `edge-local`: route is present in edge-local config; core ingress is not required in data path.

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
