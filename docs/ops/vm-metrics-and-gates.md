# VM Metrics and Throughput Gates

Purpose
- Enforce a two-phase workflow:
  1. Functional baseline first.
  2. Throughput gate against baseline.

## Run artifact contract

Each run stores data in `runs/<RUN_ID>/`:
- `topology.json`
- `qemu_inventory.json`
- `ae/nodes.json`
- `versions.json`
- `loadgen/requests-*.jsonl`
- `loadgen/summary-*.json`
- `metrics/baseline.json`
- `metrics/gate_result.json`

## 1) Baseline run

```bash
BASELINE_RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_baseline
scripts/lab/vm/labctl.sh variant baseline \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$BASELINE_RUN_ID" \
  --endpoint http://192.168.152.21:8080/v1/completions
```

## 2) Throughput run

```bash
CURRENT_RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_throughput
scripts/lab/vm/labctl.sh variant throughput \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$CURRENT_RUN_ID" \
  --endpoint http://192.168.152.21:8080/v1/completions
```

## 3) Gate

```bash
scripts/lab/vm/labctl.sh variant gate \
  --variant lab/variants/test3-abc-pp2.yaml \
  --baseline-run-id "$BASELINE_RUN_ID" \
  --current-run-id "$CURRENT_RUN_ID"
```

Default gate thresholds (variant-overridable):
- `tokens_out_per_s >= 0.90 x baseline`
- `latency_p95 <= 1.20 x baseline`
- `error_rate <= 0.001`

## 4) Manual collection only (optional)

```bash
scripts/lab/vm/labctl.sh collect \
  --variant lab/variants/test3-abc-pp2.yaml \
  --run-id "$CURRENT_RUN_ID"
```

This captures node inventory, host versions, and runtime logs even when load generation is external.
