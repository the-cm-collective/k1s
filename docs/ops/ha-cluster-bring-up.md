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
export AE_CONTROLLER_ADVERTISE_ADDR=https://core-a.example.net:9108
```

On `core-b`:

```bash
export AE_CONTROLLER_ID=core-b
export AE_CONTROLLER_ADVERTISE_ADDR=https://core-b.example.net:9108
```

On `core-c`:

```bash
export AE_CONTROLLER_ID=core-c
export AE_CONTROLLER_ADVERTISE_ADDR=https://core-c.example.net:9108
```

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

Inspect the HA snapshot served by `/system`:

```bash
curl -fsS http://127.0.0.1:9108/system | python -c 'import json,sys; data=json.load(sys.stdin); print(json.dumps(data.get("ha", {}), indent=2))'
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
scripts/lab/vm/labctl.sh host prepare --apply
```

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

## Next Procedures

After the day-0 bring-up succeeds:
- use [Operations Runbook](runbook.html) for recovery, rolling upgrades, and transport procedures
- use [VM Variant Runbook](vm-variant-runbook.html) for QEMU/KVM orchestration details
- use [HA Closeout](ha-closeout.html) for the audited evidence lane and closure criteria
- use [Observability Reference](observability.html) for the built-in HA dashboard and `GET /system.ha`
