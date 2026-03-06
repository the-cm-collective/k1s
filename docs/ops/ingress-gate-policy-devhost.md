Ingress Gate Policy (Dev Host, Multinode k1s)

Purpose
- Keep CI/dev gate sensitive to real regressions while reducing false-red from host jitter.
- Apply the same decision logic on every run.

Scope
- Primary mode: `core-proxy`
- Archetypes: `ws-echo,lb-distribution,sticky-cookie`
- Topology: multinode k1s on a single dev host.

Policy
- Tier A (blocking gate): concurrencies `30,50`
- Tier B (non-blocking exploratory): concurrency `70` (separate run, non-blocking)

Tier A thresholds
- `PERF_MIN_RPS=210`
- `PERF_MAX_P95_MS=300`
- `PERF_MAX_P99_MS=500`
- `PERF_MAX_ERROR_RATE=0.01`
- `WS_MIN_CONNECTED_RATIO=1`
- `WS_MAX_CONNECT_FAILURE_RATE=0`
- `WS_MAX_MESSAGE_LOSS=0`

Decision outcomes
- `PASS`
  - All Tier A rows pass.
- `PASS_WITH_RECHECK`
  - Exactly one failed row in Tier A.
  - Failure is perf-only and near-threshold (soft fail).
  - Immediate recheck (`c50 x3`) yields at least `2/3` passing runs.
- `FAIL`
  - Any deep/functional failure.
  - Severe perf miss.
  - Multiple failed Tier A rows.
  - Soft fail that does not recover in recheck.

Soft-fail window (near-threshold)
- `rps >= 205`
- `p95 <= 315`
- `p99 <= 525`
- `error_rate <= 0.01`

Hard-fail perf cutoffs
- `rps < 200` or
- `p95 > 330` or
- `p99 > 550` or
- `error_rate > 0.01`

Automation script
- `scripts/dev/run_ingress_gate_policy.sh`
- Writes decision artifact:
  - `state/test-results/gate-decision-<RUN_STAMP_BASE>.json`
- Writes failed-row analysis table:
  - `state/test-results/gate-analysis-<RUN_STAMP_BASE>.tsv`

Usage (blocking gate)
```bash
RUN_STAMP_BASE="$(date -u +%Y%m%dT%H%M%SZ)-k1s-gate" \
RESULTS_DIR="state/test-results/parity/$RUN_STAMP_BASE/k1s" \
GATE_CONCURRENCIES_CSV=30,50 \
PERF_MIN_RPS=210 PERF_MAX_P95_MS=300 PERF_MAX_P99_MS=500 PERF_MAX_ERROR_RATE=0.01 \
WS_MIN_CONNECTED_RATIO=1 WS_MAX_CONNECT_FAILURE_RATE=0 WS_MAX_MESSAGE_LOSS=0 \
GATE_PREP_ETCD=1 \
scripts/dev/run_ingress_gate_policy.sh
```

Usage (non-blocking exploratory)
```bash
RUN_STAMP_BASE="$(date -u +%Y%m%dT%H%M%SZ)-k1s-exploratory" \
RESULTS_DIR="state/test-results/parity/$RUN_STAMP_BASE/k1s" \
CONCURRENCIES_CSV=70 \
PERF_MIN_RPS=210 PERF_MAX_P95_MS=300 PERF_MAX_P99_MS=500 PERF_MAX_ERROR_RATE=0.01 \
WS_MIN_CONNECTED_RATIO=1 WS_MAX_CONNECT_FAILURE_RATE=0 WS_MAX_MESSAGE_LOSS=0 \
scripts/dev/run_ingress_kpi_minimatrix.sh || true
```

Jitter hardening guidance
- Default behavior: `run_ingress_gate_policy.sh` runs etcd prep (`status` + `compact-defrag`) before Tier A and each soft-fail recheck.
- Disable prep only when needed: `GATE_PREP_ETCD=0`.
- Keep Tier A and Tier B separate.
- Keep background host activity low during Tier A.
- Run etcd compact/defrag before long gate loops when DB usage is high.
- Keep warmup/duration stable across runs.
- Track weekly medians and gate flake rate before adjusting thresholds.
