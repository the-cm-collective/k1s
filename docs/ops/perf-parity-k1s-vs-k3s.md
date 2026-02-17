Performance Parity Benchmark: k1s vs k3s

Purpose
- Define a repeatable, apples-to-apples process to compare ingress behavior between k1s and k3s.
- Measure tail latency, throughput, reliability, and websocket stability under the same test contract.
- Produce a decision-ready parity report with explicit pass/fail gates.

Scope
- Workloads: `ws-echo`, `lb-distribution`, `sticky-cookie`.
- Traffic shape: deep + perf with concurrency lanes `30,50,70`.
- KPI set:
  - Primary: p95 latency.
  - Guardrails: p99 latency, error rate, RPS floor.
  - WS stability: connected ratio, connect failure rate, message loss.

Non-goals
- This process does not prove absolute cluster capacity.
- This process does not compare different hardware or mixed runtime backends.

Success Criteria
- Both platforms are run under the same contract and produce machine-readable artifacts.
- Ratios and deltas are computed from repeated runs.
- A parity decision is made using the policy in this document.

Test Contract (Must Match Across Platforms)
- Same host or dedicated identical hosts.
- Same CPU governor, no background load, same kernel settings.
- Same TLS mode for test path.
- Same ingress endpoint shape and Host routing behavior.
- Same test durations, warmup, concurrency lanes.
- Same KPI thresholds and acceptance policy.

Reference KPI Policy (Starting Point)
- `p95_ratio <= 1.15` where `p95_ratio = p95_k1s / p95_k3s`.
- `p99_ratio <= 1.20`.
- `rps_ratio >= 0.90` where `rps_ratio = rps_k1s / rps_k3s`.
- `error_rate_delta <= 0.002` (0.2%).
- WS stability equal on both lanes:
  - `connected_ratio = 1.0`
  - `connect_failure_rate = 0.0`
  - `message_loss = 0`

Artifacts Layout
- Keep outputs under `state/test-results/parity/<run-id>/`.
- Suggested structure:
  - `k1s/` raw JSONs
  - `k3s/` raw JSONs
  - `env/` hardware, kernel, runtime metadata
  - `summary/` computed parity table and decision

Step 1: Capture Environment Baseline
- Record these before any benchmark run:
  - `uname -a`
  - `lscpu`
  - `free -h`
  - Runtime versions (`containerd`, `k3s`, ingress controller image/version)
  - Active env vars relevant to test
- Recommended commands:
```bash
mkdir -p state/test-results/parity/$RUN_ID/env
uname -a > state/test-results/parity/$RUN_ID/env/uname.txt
lscpu > state/test-results/parity/$RUN_ID/env/lscpu.txt
free -h > state/test-results/parity/$RUN_ID/env/mem.txt
env | rg '^AE_|^KUBECONFIG|^PATH' > state/test-results/parity/$RUN_ID/env/env.txt
```

Step 2: Run k1s Mini-Matrix (KPI-Gated)
- Use the KPI-gated mini-matrix runner.
```bash
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-parity"
OUT_DIR="state/test-results/parity/$RUN_ID/k1s"
mkdir -p "$OUT_DIR"

RESULTS_DIR="$OUT_DIR" \
CONCURRENCIES_CSV=30,50,70 \
PERF_MIN_RPS=220 \
PERF_MAX_P95_MS=300 \
PERF_MAX_P99_MS=500 \
PERF_MAX_ERROR_RATE=0.01 \
WS_MIN_CONNECTED_RATIO=1 \
WS_MAX_CONNECT_FAILURE_RATE=0 \
WS_MAX_MESSAGE_LOSS=0 \
scripts/dev/run_ingress_kpi_minimatrix.sh
```

Step 3: Run k3s Equivalent Lanes
- Deploy equivalent `ws-echo`, `lb-distribution`, `sticky-cookie` routes on k3s.
- Ensure endpoint/Host layout matches k1s test contract.
- Use `scripts/dev/ingress_deep_probe.py` directly for the same probes.

Example probe commands (repeat per concurrency lane):
```bash
# Example variables for k3s ingress endpoint
K3S_BASE_URL="https://127.0.0.1:10443"

# ws-echo deep
python scripts/dev/ingress_deep_probe.py ws_soak \
  --url "$K3S_BASE_URL/ws" \
  --host "ws-echo-core-proxy.home.arpa" \
  --duration-seconds 600 \
  --connections 50 \
  --heartbeat-seconds 5 > ws-echo-deep.json

# perf (repeat with --concurrency 30,50,70 for each archetype)
python scripts/dev/ingress_deep_probe.py http_bench \
  --url "$K3S_BASE_URL/id" \
  --host "lb-distribution-core-proxy.home.arpa" \
  --duration-seconds 180 \
  --warmup-seconds 20 \
  --concurrency 50 > lb-distribution-perf-c50.json

python scripts/dev/ingress_deep_probe.py sticky_probe \
  --url "$K3S_BASE_URL/id" \
  --host "sticky-cookie-core-proxy.home.arpa" \
  --requests-per-client 100 > sticky-deep.json
```

- Normalize k3s outputs into a JSON schema matching k1s fields:
  - `platform`, `concurrency`, `archetype`, `rps`, `p95_ms`, `p99_ms`, `error_rate`, `ws_connected_ratio`, `ws_connect_failure_rate`, `ws_message_loss`, `status`.

Step 4: Repeats for Statistical Confidence
- Run each lane at least 5 times per platform.
- Discard first run as warmup if host jitter is high.
- Compare medians across repeats.

Step 5: Compute Ratios and Deltas
- Compute for each archetype and concurrency:
  - `p95_ratio = p95_k1s / p95_k3s`
  - `p99_ratio = p99_k1s / p99_k3s`
  - `rps_ratio = rps_k1s / rps_k3s`
  - `error_rate_delta = error_rate_k1s - error_rate_k3s`
- Then compute aggregate medians by concurrency and overall.

Recommended summary columns:
- `platform_pair`
- `concurrency`
- `archetype`
- `k1s_rps`, `k3s_rps`, `rps_ratio`
- `k1s_p95`, `k3s_p95`, `p95_ratio`
- `k1s_p99`, `k3s_p99`, `p99_ratio`
- `k1s_error_rate`, `k3s_error_rate`, `error_rate_delta`
- `k1s_ws_connected_ratio`, `k3s_ws_connected_ratio`
- `k1s_ws_connect_failure_rate`, `k3s_ws_connect_failure_rate`
- `k1s_ws_message_loss`, `k3s_ws_message_loss`
- `gate_pass`

Step 6: Decision Policy
- `PASS` if all are true:
  - `p95_ratio <= 1.15`
  - `p99_ratio <= 1.20`
  - `rps_ratio >= 0.90`
  - `error_rate_delta <= 0.002`
  - WS stability parity passes
- `CONDITIONAL PASS` if exactly one non-reliability gate misses by <=5% and reliability/WS gates pass.
- `FAIL` otherwise.

Step 7: Publish Findings
- Include in final report:
  - Test contract and exact command lines.
  - Run IDs and artifact paths.
  - Median results by lane.
  - Ratio/delta table.
  - Final PASS/FAIL/CONDITIONAL decision and follow-up actions.

Common Pitfalls
- Comparing different TLS or ingress controller modes between platforms.
- Mixing single-run outliers into decision criteria.
- Restarting stack between each lane without documenting it.
- Interpreting core-proxy LB policy/observability as strict backend distribution proof.

Recommended Next Enhancements
1. Add a `scripts/dev/parity_compare.py` utility to ingest k1s/k3s JSON and emit parity table + gate result.
2. Add CI job to run reduced parity smoke nightly on fixed hardware.
3. Add regression alerting on ratio drift over time (weekly rolling median).
