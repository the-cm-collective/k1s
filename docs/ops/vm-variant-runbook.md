# VM Variant Runbook (A/B/C Fabric)

Purpose
- Start, configure, and run k1s fabric test variants on local KVM/QEMU.

Reference variants
- `lab/variants/test1-a-only-passthrough.yaml`
- `lab/variants/test2-ab-passthrough.yaml`
- `lab/variants/test3-abc-pp2.yaml`

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

If you run multi-site JetStream lanes, register edge sites from the core host before edge traffic tests:

```bash
cd /mnt/host
sudo -E make edge-site-cri SITE_ID=edge-b EDGE_PORT=4224 EDGE_HTTP_PORT=8224
sudo -E make edge-site-cri SITE_ID=edge-c EDGE_PORT=4324 EDGE_HTTP_PORT=8324
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
