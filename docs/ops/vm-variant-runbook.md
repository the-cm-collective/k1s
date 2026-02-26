# VM Variant Runbook (A/B/C Fabric)

Purpose
- Start, configure, and run k1s fabric test variants on local KVM/QEMU.

Reference variants
- `lab/variants/test1-a-only-passthrough.yaml`
- `lab/variants/test2-ab-passthrough.yaml`
- `lab/variants/test3-abc-pp2.yaml`

Transport defaults
- Variants default to `transport.leaf_uplink_mode: direct_ip`.
- Edge-core bootstrap resolves hub leaf endpoint from `transport.hub_host` (or core host IP) and `transport.hub_leaf_port` (default `7422`).
- Use `local_tunnel` only when each edge host intentionally forwards hub leaf traffic to localhost.

## 0) Single-command smoke (A/B/C on one host)

```bash
sudo -v
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_smoke
make lab-vm-smoke VARIANT=lab/variants/test3-abc-pp2.yaml RUN_ID="$RUN_ID"
```

What this runs:
- `variant up`
- `k1s_bootstrap --execute`
- `variant validate` (fails if any host is not ready)

To run the phased lane-aware harness (v2) in parallel with the legacy flow:

```bash
sudo -v
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_smoke
LAB_VM_SMOKE_V2=1 make lab-vm-smoke \
  VARIANT=lab/variants/test3-abc-pp2.yaml \
  RUN_ID="$RUN_ID"
```

`smoke_v2` writes:
- `runs/<RUN_ID>/plan.json`
- `runs/<RUN_ID>/global_phases.json`
- `runs/<RUN_ID>/lanes/<lane>/phase_status.json`
- `runs/<RUN_ID>/summary.json`

Optional automatic teardown in the same run:

```bash
sudo -v
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_smoke
make lab-vm-smoke \
  VARIANT=lab/variants/test3-abc-pp2.yaml \
  RUN_ID="$RUN_ID" \
  LAB_VM_SMOKE_ARGS="--down --purge"
```

## 1) Host prerequisite check

```bash
scripts/lab/vm/labctl.sh host prepare
```

Apply bridge/NAT setup:

```bash
scripts/lab/vm/labctl.sh host prepare --apply
```

## 2) Bring up a variant

```bash
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_test3
scripts/lab/vm/labctl.sh variant up \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$RUN_ID"
```

This writes:
- `runs/<RUN_ID>/topology.json`
- `runs/<RUN_ID>/qemu_inventory.json`

## 3) Bootstrap k1s services

Generate host bootstrap scripts:

```bash
scripts/lab/vm/k1s_bootstrap.sh \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$RUN_ID"
```

Execute bootstrap directly over SSH:

```bash
scripts/lab/vm/k1s_bootstrap.sh \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$RUN_ID" \
  --execute
```

Default strict-CRI core bootstrap behavior now includes:
- `AE_CRI_REGISTRY_TRUST_SYSTEM=1` for managed-registry CA installation into system trust.
- `AE_CRI_REGISTRY_PRELOAD=1` so core strict-CRI images are mirrored/pulled before `k1s-core` starts.

Override when needed:

```bash
AE_CRI_REGISTRY_TRUST_SYSTEM=0 AE_CRI_REGISTRY_PRELOAD=0 \
scripts/lab/vm/k1s_bootstrap.sh \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$RUN_ID" \
  --execute
```

For multi-site JetStream lanes, bootstrap now auto-registers edge-site uplink users on the core hub before edge traffic tests.
If you need to register one site manually (without starting edge NATS), use:

```bash
cd /mnt/host
sudo -E REGISTER_ONLY=1 SITE_ID=edge-b ./scripts/dev/add_edge_site.sh
sudo -E REGISTER_ONLY=1 SITE_ID=edge-c ./scripts/dev/add_edge_site.sh
```

## 4) Validate host readiness

```bash
scripts/lab/vm/labctl.sh variant validate \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$RUN_ID"
```

Validation report:
- `runs/<RUN_ID>/variant_validate.json`

## 5) Teardown

```bash
scripts/lab/vm/labctl.sh variant down \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$RUN_ID"
```

Optional full cleanup:

```bash
scripts/lab/vm/labctl.sh variant down \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$RUN_ID" \
  --purge \
  --destroy-network
```
