# HA Cluster Bring-Up

Status: canonical operator bootstrap sequence for a strict-CRI HA `k1s-ha-core` control plane.

Use this page when you want to bring up the shared-authority HA control plane on three core nodes. For single-host or dev-oriented startup, use [Start Here](start-here.html) instead.

This guide is manual-first:
- it assumes three core hosts that will run `k1s-ha-core`
- it assumes shared `etcd` plus shared hub NATS/JetStream already exist and are reachable
- it does not use local singleton `etcd`, NATS, or Postgres
- it ends at a healthy HA control plane plus the first snapshot checkpoint

If you want the repo-managed equivalent of the same contract, use the VM-lab path later on this page.

## Topology

Expected shape:
- `core-a`, `core-b`, and `core-c` each run `make k1s-ha-core`
- all three nodes point at the same `AE_ETCD_ENDPOINTS`, `AE_ETCD_PREFIX`, and `AE_NATS_URL`
- each node has its own `AE_CONTROLLER_ID` and `AE_CONTROLLER_ADVERTISE_ADDR`
- containerd, CNI, and `crictl` are already working on every core host

This page is only the control-plane bring-up. Edge gateways and workload traffic can be added after the core HA cluster is healthy.

## Prerequisites

From a repo checkout on each core host:

```bash
python -m pip install -e .[dev]
```

Before starting any `k1s-ha-core` node, confirm:
- the three core hosts can reach the shared `etcd` quorum and shared hub NATS/JetStream cluster
- the shared backends are already provisioned and healthy
- each core host has a stable hostname/IP that can be used in `AE_CONTROLLER_ADVERTISE_ADDR`

This guide does not define a vendor-specific manual install of clustered `etcd` or clustered NATS. If you want the repo's reproducible backend bootstrap path, use the VM-lab flow below with `ha_shared_infra.sh`.

## 1) Choose shared HA endpoints

Set the same shared HA endpoints on every core node:

```bash
export AE_ETCD_ENDPOINTS=http://10.0.0.11:2379,http://10.0.0.12:2379,http://10.0.0.13:2379
export AE_ETCD_PREFIX=/k1s/prod
export AE_NATS_URL=nats://10.0.0.21:4222,nats://10.0.0.22:4222,nats://10.0.0.23:4222
```

Notes:
- `AE_ETCD_PREFIX` should be unique per cluster.
- `AE_APISHIM_ETCD_ENDPOINTS` defaults from `AE_ETCD_ENDPOINTS` when left unset.

## 2) Set per-node identity

Set a unique controller identity on each host before startup.

On `core-a`:

```bash
export AE_CONTROLLER_ID=core-a
export AE_CONTROLLER_ADVERTISE_ADDR=http://core-a.example.net:9108
```

On `core-b`:

```bash
export AE_CONTROLLER_ID=core-b
export AE_CONTROLLER_ADVERTISE_ADDR=http://core-b.example.net:9108
```

On `core-c`:

```bash
export AE_CONTROLLER_ID=core-c
export AE_CONTROLLER_ADVERTISE_ADDR=http://core-c.example.net:9108
```

## Command Readout (Strict-CRI HA) {#ha-command-readout}

Use this section when you want exact role-by-role commands in the same style as the single-host strict-CRI guide, but for the real HA control-plane contract.

Assumptions:
- the real HA path uses three separate `k1s-ha-core` hosts
- shared `etcd` and shared hub NATS/JetStream already exist
- nodes and gateways reach the controller agent API on `:9110`
- optional NetFS or storage flags from [Multi-node Edge Gateway Manual Test](multinode-lab.html#cri-deployment) can be layered on after the base HA cluster is healthy

### Shared Variables

Run these once in your shell before using the commands below:

```bash
ROOT="/home/$USER/git/k1s"
VENV_BIN="$ROOT/.venv/bin"
SUDO_PATH="${VENV_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
WG_BIN=$(command -v wg)
APISHIM_TAG="localhost:5001/k1s-apishim:dev-$(date +%s)"

AGENT_TOKEN=replace-me-shared-agent-token
HA_ETCD_ENDPOINTS="http://10.0.0.11:2379,http://10.0.0.12:2379,http://10.0.0.13:2379"
HA_ETCD_PREFIX="/k1s/prod"
HA_NATS_URL="nats://10.0.0.21:4222,nats://10.0.0.22:4222,nats://10.0.0.23:4222"

# Use a stable VIP or load balancer for nodes and gateways.
HA_CONTROLLER_URL="http://core-vip.example.net:9110"
```

Notes:
- `AE_CONTROLLER_ADVERTISE_ADDR` stays on the controller HTTP surface, typically `:9108`.
- `HA_CONTROLLER_URL` is the node/gateway agent API URL, typically `:9110` when `AE_AGENT_API_PORT=9110` is enabled on the controllers.
- Reuse the same `AGENT_TOKEN` value for `AE_AGENT_API_TOKEN` on controllers and `AE_AGENT_TOKEN` on nodes and gateways.

### HA Core Controllers

Run one controller per host. Reuse the same block on `core-a`, `core-b`, and `core-c`, changing only `AE_CONTROLLER_ID` and `AE_CONTROLLER_ADVERTISE_ADDR`.

Core-proxy:

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri AE_INFRA_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_REGISTRY_MODE=managed AE_CRI_REGISTRY_INSECURE=0 \
  AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN="${AGENT_TOKEN}" \
  AE_HA_MODE=1 AE_STATE_BACKEND=etcd AE_TRANSPORT_BACKEND=nats-js AE_JS_DOMAIN=K1S \
  AE_ETCD_ENDPOINTS="${HA_ETCD_ENDPOINTS}" \
  AE_APISHIM_ETCD_ENDPOINTS="${HA_ETCD_ENDPOINTS}" \
  AE_ETCD_PREFIX="${HA_ETCD_PREFIX}" \
  AE_ETCD_MAINTENANCE_ENABLE=0 \
  AE_NATS_URL="${HA_NATS_URL}" \
  AE_CONTROLLER_ID=core-a \
  AE_CONTROLLER_ADVERTISE_ADDR=http://core-a.example.net:9108 \
  AE_APISHIM_MODE=cri \
  AE_APISHIM_IMAGE="${APISHIM_TAG}" \
  EDGE_INGRESS_MODE=core-proxy \
  AE_EDGE_INGRESS_RATHOLE_RELOAD=1 \
  AE_WG_INTERFACE=wg-hub \
  AE_WG_DUMP_CMD="sudo -n ${WG_BIN} show {iface} dump" \
  make k1s-ha-core
```

Core-to-edge-public:

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri AE_INFRA_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_REGISTRY_MODE=managed AE_CRI_REGISTRY_INSECURE=0 \
  AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN="${AGENT_TOKEN}" \
  AE_HA_MODE=1 AE_STATE_BACKEND=etcd AE_TRANSPORT_BACKEND=nats-js AE_JS_DOMAIN=K1S \
  AE_ETCD_ENDPOINTS="${HA_ETCD_ENDPOINTS}" \
  AE_APISHIM_ETCD_ENDPOINTS="${HA_ETCD_ENDPOINTS}" \
  AE_ETCD_PREFIX="${HA_ETCD_PREFIX}" \
  AE_ETCD_MAINTENANCE_ENABLE=0 \
  AE_NATS_URL="${HA_NATS_URL}" \
  AE_CONTROLLER_ID=core-a \
  AE_CONTROLLER_ADVERTISE_ADDR=http://core-a.example.net:9108 \
  AE_APISHIM_MODE=cri \
  AE_APISHIM_IMAGE="${APISHIM_TAG}" \
  EDGE_INGRESS_MODE=core-to-edge-public \
  AE_WG_INTERFACE=wg-hub \
  AE_WG_DUMP_CMD="sudo -n ${WG_BIN} show {iface} dump" \
  make k1s-ha-core
```

Edge-local:

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri AE_INFRA_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_REGISTRY_MODE=managed AE_CRI_REGISTRY_INSECURE=0 \
  AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN="${AGENT_TOKEN}" \
  AE_HA_MODE=1 AE_STATE_BACKEND=etcd AE_TRANSPORT_BACKEND=nats-js AE_JS_DOMAIN=K1S \
  AE_ETCD_ENDPOINTS="${HA_ETCD_ENDPOINTS}" \
  AE_APISHIM_ETCD_ENDPOINTS="${HA_ETCD_ENDPOINTS}" \
  AE_ETCD_PREFIX="${HA_ETCD_PREFIX}" \
  AE_ETCD_MAINTENANCE_ENABLE=0 \
  AE_NATS_URL="${HA_NATS_URL}" \
  AE_CONTROLLER_ID=core-a \
  AE_CONTROLLER_ADVERTISE_ADDR=http://core-a.example.net:9108 \
  AE_APISHIM_MODE=cri \
  AE_APISHIM_IMAGE="${APISHIM_TAG}" \
  EDGE_INGRESS_MODE=edge-local \
  AE_ROUTE_BUNDLE_ENABLED=1 \
  AE_ENABLE_SERVICE_PROXY=1 \
  AE_SERVICE_PROVIDER=iptables \
  AE_WG_INTERFACE=wg-hub \
  AE_WG_DUMP_CMD="sudo -n ${WG_BIN} show {iface} dump" \
  make k1s-ha-core
```

Notes:
- For `core-b` and `core-c`, rerun the chosen block with that host's `AE_CONTROLLER_ID` and `AE_CONTROLLER_ADVERTISE_ADDR`.
- If you want local docs and playground helpers while testing, layer `AE_DEV_LOCAL=1` on top. Do not treat that as the default operator bootstrap posture.

### Core Node

There is no `k1s-ha-core-node` target. HA still uses `make k1s-core-node`, pointed at the HA controller API:

```bash
sudo -E env PATH="$SUDO_PATH" \
  AE_RUNTIME_BACKEND=cri \
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
  AE_AGENT_TOKEN="${AGENT_TOKEN}" \
  AE_CONTROLLER_URL="${HA_CONTROLLER_URL}" \
  AE_AGENT_ENDPOINT=http://10.255.0.1:9111 \
  make k1s-core-node
```

### Edge Site and Edge Gateway

For the repo-managed dev or lab path, register the site against the HA hub profile first:

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  HUB_PROFILE=k1s-ha-core \
  AE_NATS_HUB_LEAF_HOST=10.0.0.21 \
  AE_NATS_HUB_LEAF_PORT=7422 \
  make edge-site-cri SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```

If you are using a real external shared hub cluster, create the `site-<id>-uplink` user/operator config outside `edge-site-cri` and treat the helper above as out of scope.

Gateway in `core-proxy` mode:

```bash
sudo -E env PATH="$SUDO_PATH" \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_REGISTRY_MODE=managed AE_CRI_REGISTRY_INSECURE=0 \
  EDGE_INGRESS_MODE=core-proxy \
  AE_SITE_ID=sea-edge-02 \
  AE_NODE_ID=edge-1 \
  AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224 \
  AE_LOG_LEVEL=debug \
  make k1s-edge-core-cri
```

Gateway in `edge-local` mode:

```bash
EDGE_LOCAL_DIR="$ROOT/state/profiles/k1s-core/edge-local"
RELOAD_CMD="/usr/bin/install -D -m 0644 ${EDGE_LOCAL_DIR}/edge-local.caddy ${ROOT}/state/caddy/edge-local.caddy && ${VENV_BIN}/python ${ROOT}/scripts/dev/cri_stack.py up-caddy --profile k1s-ha-core --metrics-port 9108 --apishim-port 8445 --recreate"

sudo -E env PATH="$SUDO_PATH" \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_REGISTRY_MODE=managed AE_CRI_REGISTRY_INSECURE=0 \
  EDGE_INGRESS_MODE=edge-local \
  AE_EDGE_LOCAL_UPSTREAM_MODE=bundle-endpoints \
  AE_EDGE_LOCAL_INGRESS_CONFIG_DIR="$EDGE_LOCAL_DIR" \
  AE_EDGE_LOCAL_INGRESS_CONFIG_FILE="$EDGE_LOCAL_DIR/edge-local.caddy" \
  AE_EDGE_LOCAL_INGRESS_RELOAD_CMD="$RELOAD_CMD" \
  AE_SITE_ID=sea-edge-02 \
  AE_NODE_ID=edge-1 \
  AE_NATS_URL=nats://gateway:dev@127.0.0.1:4224 \
  AE_LOG_LEVEL=debug \
  make k1s-edge-core-cri
```

Notes:
- `k1s-edge-core-cri` still uses the `k1s-core` edge profile internally, so the gateway's edge-local config dir remains under `state/profiles/k1s-core/edge-local`.
- The reload command targets the HA core caddy stack, so it uses `--profile k1s-ha-core`.

### Edge Node

The HA edge-node lane still uses `make k1s-edge-node`, pointed at the HA controller API:

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
  AE_AGENT_TOKEN="${AGENT_TOKEN}" \
  AE_CONTROLLER_URL="${HA_CONTROLLER_URL}" \
  AE_AGENT_ENDPOINT=http://10.255.0.3:9112 \
  make k1s-edge-node
```

### Reduced One-Box HA Regression Lane {#ha-command-readout-one-box}

There is no supported single-host 3x `k1s-ha-core` cluster. The local approximation is the reduced closeout harness:

```bash
make ha-closeout-e2e
```

Direct wrapper:

```bash
scripts/dev/ha_closeout_e2e.sh
```

What this lane proves:
- controller failover still advances authority to a new leader
- shared-authority writes remain usable in HA mode
- gateway replay stays bounded after restart under the new leader

What it does not prove:
- full 3-controller HA topology on one host
- multi-host transport fidelity
- production-equivalent JetStream replication; the reduced harness forces `AE_JS_REPLICAS=1`

### What This Is Not

- It is not a supported single-host 3x `k1s-ha-core` cluster.
- It is not a replacement for the numbered bootstrap sequence above.
- It does not redefine the base feature flags from the single-host CRI guide; optional feature-specific env like NetFS or storage seeding should be layered on after the HA control plane is healthy.

## 3) Run HA preflight on each core node

With both the shared HA env and the node-specific identity set, run:

```bash
PYTHONPATH=src python scripts/dev/ha_core_preflight.py
```

Do this on all three core hosts. Do not start a node that fails preflight.

## 4) Start the three `k1s-ha-core` nodes

Start the core nodes one host at a time:

On `core-a`:

```bash
make k1s-ha-core
```

Repeat the same command on `core-b`, then on `core-c`, after exporting that node's `AE_CONTROLLER_ID` and `AE_CONTROLLER_ADVERTISE_ADDR`.

What this profile does:
- forces `AE_HA_MODE=1`
- uses `etcd` as shared authority and NATS/JetStream as shared transport
- uses the strict-CRI runtime and infra profiles
- does not auto-start local singleton `etcd`, NATS, or Postgres
- does not treat local `specs/` import as the HA desired-state path

For long-lived installed nodes, use the installed-service surface described in [Operations Runbook](runbook.html) after the direct-process bootstrap contract is understood.

## 5) Validate authority, API, and dashboard state

From each core node, inspect the controller metrics:

```bash
curl -fsS http://127.0.0.1:9108/metrics | rg 'ae_controller_is_leader|ae_controller_epoch|ae_controller_authority_healthy'
```

Expected result:
- exactly one controller reports `ae_controller_is_leader 1`
- healthy controllers report `ae_controller_authority_healthy 1`
- all nodes show the same current `ae_controller_epoch`

Export local auth before reading `/system` or populating the dashboard:

```bash
source <(ae auth local --strict)
```

Inspect the HA snapshot served by `/system`:

```bash
curl -fsS \
  -H "Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}" \
  http://127.0.0.1:9108/system \
  | python -c 'import json,sys; data=json.load(sys.stdin); print(json.dumps(data.get("ha", {}), indent=2))'
```

Expected result:
- `enabled` is `true`
- `authority.healthy` is `true`
- `authority.leader_id` is present
- `transport.backend` is `nats-js`

Open the integrated dashboard on a core node:

```text
http://<core-host>:9108/dashboard
```

If the dashboard shows `Unauthorized (401)`, paste `AE_API_READ_TOKEN` or
`AE_API_ADMIN_TOKEN` into the `Bearer` field and save.

Confirm the `HA Control Plane` section is present and shows leader, epoch, `etcd`, and transport state.

## 6) Take the first etcd snapshot

Once the cluster is healthy, take the first HA snapshot from any core node:

```bash
PYTHONPATH=src python scripts/dev/etcd_snapshot.py \
  --runner auto save \
  --output state/backups/ha-$(date +%Y%m%d-%H%M%S).db
```

Optionally verify the resulting snapshot file:

```bash
PYTHONPATH=src python scripts/dev/etcd_snapshot.py \
  --runner auto status \
  --input state/backups/ha-20260318-120000.db
```

This is the day-0 checkpoint before moving on to recovery, upgrade, or edge-site procedures.

## VM-Lab Equivalent

Use this when you want the repo-managed, reproducible equivalent of the same HA contract.

Prepare the host:

```bash
scripts/lab/vm/labctl.sh host prepare \
  --variant lab/variants/ha-control-plane-core.yaml \
  --apply
```

If `k1s-br0` was left behind by another variant on a different CIDR, tear that lane down with `--destroy-network` before retrying this HA flow.

Bring up the checked-in HA topology:

```bash
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_ha_control_plane
scripts/lab/vm/labctl.sh variant up \
  --variant lab/variants/ha-control-plane-core.yaml \
  --run-id "$RUN_ID"
```

Bootstrap the shared HA backends:

```bash
scripts/lab/vm/ha_shared_infra.sh \
  --variant lab/variants/ha-control-plane-core.yaml \
  --run-id "$RUN_ID" \
  --execute
```

Bootstrap the `k1s-ha-core` nodes:

```bash
scripts/lab/vm/k1s_bootstrap.sh \
  --variant lab/variants/ha-control-plane-core.yaml \
  --run-id "$RUN_ID" \
  --execute
```

Validate the resulting variant:

```bash
scripts/lab/vm/labctl.sh variant validate \
  --variant lab/variants/ha-control-plane-core.yaml \
  --run-id "$RUN_ID"
```

The one-command acceptance wrapper for the same lane is:

```bash
AE_CRI_CACHE_SEED_ENGINE=docker \
AE_CRI_CACHE_SEED_MODE=required \
AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 \
make lab-vm-smoke \
  VARIANT=lab/variants/ha-control-plane-core.yaml \
  RUN_ID="$RUN_ID" \
  LAB_VM_SMOKE_ARGS="--purge --destroy-network"
```

That wrapper produces the machine-readable `runs/<RUN_ID>/ha_summary.json` artifact used by the HA closeout lane.

### Single-Workstation Manual Smoke (Retained Make Helpers)

Use this when you want to keep a checked-in HA topology running on one workstation for manual inspection and a small workload smoke. This is a retained VM smoke lane, not a supported single-host HA dev profile.

Verify the qcow2 images first:

```bash
scripts/lab/vm/labctl.sh image verify --variant all
```

If verify fails, or you changed image/bootstrap contents, rebuild the images:

```bash
scripts/lab/vm/labctl.sh image build --variant all
scripts/lab/vm/labctl.sh image verify --variant all
```

Normal reruns now auto-clean the matching per-variant Packer work directory. If
you are troubleshooting a badly interrupted local build, you can still manually
remove `artifacts/images/build-base` and `artifacts/images/build-gpu` first.

If you are reusing an existing retained HA run id, tear that run down before host prep:

```bash
scripts/lab/vm/labctl.sh variant down \
  --variant lab/variants/ha-control-plane-hub-node.yaml \
  --run-id "$RUN_ID" \
  --purge
```

Preferred retained operator path:

```bash
sudo -v
make lab-vm-ha-dashboard-up
make lab-vm-ha-dashboard-status
```

Override the retained run id or variant when needed:

```bash
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_ha_manual" \
VARIANT=lab/variants/ha-control-plane-hub-node.yaml \
make lab-vm-ha-dashboard-up
```

Retained iteration commands:

```bash
make lab-vm-ha-dashboard-refresh-all \
  LAB_VM_HA_DASHBOARD_ARGS="--target all"

make lab-vm-ha-dashboard-down \
  LAB_VM_HA_DASHBOARD_ARGS="--purge"

make lab-vm-ha-dashboard-purge

make lab-vm-ha-dashboard-reset
```

`make lab-vm-ha-dashboard-refresh-all` is the retained-VM "rebuild and restart all" path on the current VMs. `make lab-vm-ha-dashboard-down LAB_VM_HA_DASHBOARD_ARGS="--purge"` removes the retained VMs plus their per-run VM state. `make lab-vm-ha-dashboard-purge` additionally removes `runs/<RUN_ID>` plus the repo-built host images used by this retained lane. `make lab-vm-ha-dashboard-reset` is the hard recycle path that tears the retained lane down and brings it back.

Lower-level equivalent commands remain available when you need the raw building blocks.

Prepare the host for the HA variant:

```bash
scripts/lab/vm/labctl.sh host prepare \
  --variant lab/variants/ha-control-plane-hub-node.yaml \
  --apply
```

Bring the retained HA lane up without auto teardown via the older generic wrapper:

```bash
sudo -v
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_ha_manual
AE_CRI_CACHE_SEED_ENGINE=docker \
AE_CRI_CACHE_SEED_MODE=required \
AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 \
make lab-vm-smoke \
  VARIANT=lab/variants/ha-control-plane-hub-node.yaml \
  RUN_ID="$RUN_ID" \
  LAB_VM_SMOKE_ARGS="--teardown never"
```

This retained variant keeps the three HA controllers and replaces the previous `sea` edge pair with one workload-capable hub node:
- `hub-1`: `192.168.155.20` (`role=hub,site=hub`)
- the smoke helper automatically verifies that `hub-1` registers Ready and can run the pinned `shell-demo-node-hub` workload

Public Envoy access from the host:
- dashboard: `https://dash.home.arpa:10443/dashboard`
- docs: `https://docs.home.arpa:10443/`
- API docs: `https://api.home.arpa:10443/swagger` and `https://api.home.arpa:10443/redoc`
- `https://api.home.arpa:10443/dashboard` returns `404` by design; dashboard lives on `dash.home.arpa`
- verify one specific core with `curl --resolve dash.home.arpa:10443:192.168.155.10 ...`, `curl --resolve docs.home.arpa:10443:192.168.155.10 ...`, and `curl --resolve api.home.arpa:10443:192.168.155.10 ...`

Direct per-node diagnostics remain available when you need them:
- controller UI: `http://192.168.155.10:9108/dashboard`, `http://192.168.155.11:9108/dashboard`, `http://192.168.155.12:9108/dashboard`
- API shim: `https://192.168.155.10:8445`, `https://192.168.155.11:8445`, `https://192.168.155.12:8445`

Export local auth for the retained HA profile before reading `/system` or using the dashboard data panels:

```bash
source <(APISHIM_ENV_FILE=state/profiles/k1s-ha-core/apishim.env bash scripts/ae-env.sh local)
curl -sk \
  --resolve api.home.arpa:10443:192.168.155.10 \
  -H "Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}" \
  https://api.home.arpa:10443/system | python -m json.tool
```

Use the same bearer token in the dashboard `Bearer` field when the data panels need auth.

Host docs remain a static convenience layer. Point them at the Envoy hosts and serve them locally:

```bash
DOCS_API_BASE=https://docs.home.arpa:10443 \
DOCS_DASHBOARD_URL=https://dash.home.arpa:10443/dashboard \
python docs/build_docs.py
python -m http.server 9109 --directory docs/site
```

Notes:
- Treat `dash.home.arpa`, `docs.home.arpa`, and `api.home.arpa` on `:10443` as the primary public control-plane surface in this retained VM lane.
- Any healthy controller can serve `/dashboard`, `/system`, and `/metrics` during normal HA operation.
- In this retained VM profile, `/system` is bearer-protected and only `AE_API_ADMIN_TOKEN` may be configured for controller HTTP reads.
- `/dashboard` serves without auth, but its data panels fetch `/system`; paste the bearer token into the page when prompted.
- `hub-1` reaches the HA controller agent API on `:9110` and runs with Rosenpass/WireGuard disabled in this retained lane; this lane validates HA control-plane health plus workload placement, not overlay networking.
- Followers still reject leader-only mutation with `not_leader`; use the current leader for apply/scale/delete or retry after reading the leader hint.
- The retained workload smoke uses the shared HA state store, not controller HTTP mutations, because this lane keeps `AE_API_MUTATIONS=0`.
- `lab/variants/ha-control-plane-core.yaml` remains the separate HA closeout topology when you want edge/gateway transport coverage instead of a hub workload node.
- If you are switching from another VM lane that used a different `k1s-br0` subnet, tear it down with `--destroy-network` before rerunning host prep for this HA variant.
- If the controller used by `DOCS_API_BASE` goes away, rebuild docs with another core URL or open the remaining controllers directly.
- Clean up manually when you are done. Use `--destroy-network` only when you want full bridge cleanup or are switching to another subnet.
- Full retained cleanup is `make lab-vm-ha-dashboard-purge`; add `LAB_VM_HA_DASHBOARD_ARGS="--destroy-network"` if you also want bridge teardown.
- The fast retained reset path is `make lab-vm-ha-dashboard-reset`.
- If you also changed guest/bootstrap contracts, run:

```bash
make lab-vm-ha-dashboard-reset \
  LAB_VM_HA_DASHBOARD_ARGS="--rebuild-images --destroy-network"
```

Lower-level teardown remains:

```bash
scripts/lab/vm/labctl.sh variant down \
  --variant lab/variants/ha-control-plane-hub-node.yaml \
  --run-id "$RUN_ID" \
  --purge
```

## Next Procedures

After the day-0 bring-up succeeds:
- use [Operations Runbook](runbook.html) for recovery, rolling upgrades, and transport procedures
- use [VM Variant Runbook](vm-variant-runbook.html) for QEMU/KVM orchestration details
- use [HA Closeout](ha-closeout.html) for the audited evidence lane and closure criteria
- use [Observability Reference](observability.html) for the built-in HA dashboard and `GET /system.ha`
