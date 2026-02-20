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

Optional Preflight Helper (Before Manual Benchmarks)
- Use the safe helper to initialize `RUN_ID`, create directories, and capture the environment baseline.
```bash
bash scripts/dev/parity_preflight.sh --print-exports
```
- This helper intentionally stops before benchmark execution.
- It validates core-proxy bootstrap readiness (listener `10080`) and exits non-zero if not ready.
  - Start core-proxy lane: `AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core`
  - Strict-CRI alt: `AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core-cri`
- If you only want directory/env scaffolding, run with `--skip-core-proxy-check`.
- It does not run Step 2 (`scripts/dev/run_ingress_kpi_minimatrix.sh`).
- It does not run Step 3 (`scripts/dev/ingress_deep_probe.py` probes).
- After it completes, run Step 2 and Step 3 manually using the command blocks it prints.

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
RUN_STAMP_BASE="${RUN_ID}-k1s-r1" \
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

Step 3: Run k3s Equivalent Lanes (Strict Sticky)
- Deploy equivalent `ws-echo`, `lb-distribution`, `sticky-cookie` routes on k3s.
- Ensure endpoint/Host layout matches k1s test contract.
- Start (or reuse) local k3s bench cluster and apply parity routes:
```bash
make bench-k3s-up K3S_NAME=bench
kubectl config use-context k3d-bench
kubectl delete ingress k3s-parity-sticky-cookie --ignore-not-found
kubectl apply -f specs/examples/k3s-ingress-parity.yaml

kubectl rollout status deployment/k3s-parity-ws-echo --timeout=180s
kubectl rollout status deployment/k3s-parity-lb-distribution --timeout=180s
kubectl rollout status deployment/k3s-parity-sticky-cookie --timeout=180s
kubectl get ingress -n default
kubectl get ingressroute.traefik.io -n default k3s-parity-sticky-cookie
```
- `specs/examples/k3s-ingress-parity.yaml` configures equivalent hosts/paths:
  - `ws-echo-core-proxy.home.arpa` on `/ws`
  - `lb-distribution-core-proxy.home.arpa` on `/id`
  - `sticky-cookie-core-proxy.home.arpa` on `/id` via Traefik `IngressRoute` sticky cookie with `nativeLB: true`.

Run k3s deep + perf matrix (same contract as k1s):
```bash
K3S_BASE_URL="https://127.0.0.1"
mkdir -p "$ROOT/k3s"

# Deep checks
python scripts/dev/ingress_deep_probe.py ws_soak \
  --url "$K3S_BASE_URL/ws" \
  --host "ws-echo-core-proxy.home.arpa" \
  --duration-seconds 600 \
  --connections 50 \
  --heartbeat-seconds 5 \
  > "$ROOT/k3s/k3s-r2-ws-echo-deep.json"

python scripts/dev/ingress_deep_probe.py lb_sample \
  --url "$K3S_BASE_URL/id" \
  --host "lb-distribution-core-proxy.home.arpa" \
  --strategy round_robin \
  --requests 5000 \
  --min-backends 2 \
  --max-skew-ratio 0.35 \
  > "$ROOT/k3s/k3s-r2-lb-distribution-deep.json"

python scripts/dev/ingress_deep_probe.py sticky_probe \
  --url "$K3S_BASE_URL/id" \
  --host "sticky-cookie-core-proxy.home.arpa" \
  --requests-per-client 100 \
  > "$ROOT/k3s/k3s-r2-sticky-deep.json"

# Perf matrix: ws-echo, lb-distribution, sticky-cookie x c30,c50,c70
for c in 30 50 70; do
  python scripts/dev/ingress_deep_probe.py http_bench \
    --url "$K3S_BASE_URL/id" \
    --host "ws-echo-core-proxy.home.arpa" \
    --duration-seconds 180 \
    --warmup-seconds 20 \
    --concurrency "$c" \
    > "$ROOT/k3s/k3s-r2-ws-echo-c${c}.json"

  python scripts/dev/ingress_deep_probe.py http_bench \
    --url "$K3S_BASE_URL/id" \
    --host "lb-distribution-core-proxy.home.arpa" \
    --duration-seconds 180 \
    --warmup-seconds 20 \
    --concurrency "$c" \
    > "$ROOT/k3s/k3s-r2-lb-c${c}.json"

  python scripts/dev/ingress_deep_probe.py http_bench \
    --url "$K3S_BASE_URL/id" \
    --host "sticky-cookie-core-proxy.home.arpa" \
    --duration-seconds 180 \
    --warmup-seconds 20 \
    --concurrency "$c" \
    > "$ROOT/k3s/k3s-r2-sticky-c${c}.json"
done
```

Monitoring during long k3s probe runs:
- Probe commands are quiet when redirected (`> file.json`). No terminal output while running is expected.
- Process monitor (separate terminal):
```bash
watch -n 2 'date; pgrep -af "ingress_deep_probe.py" || echo "no running probes"'
```
- Artifact counter (separate terminal):
```bash
watch -n 3 "date; ls -1 \"$ROOT/k3s\"/k3s-r2-*.json 2>/dev/null | wc -l"
```
- Bell on probe completion (separate terminal):
```bash
while pgrep -af "ingress_deep_probe.py" >/dev/null; do sleep 2; done; printf '\a'; echo "k3s probes complete"
```

- Optional cleanup after k3s shakedown:
```bash
kubectl delete -f specs/examples/k3s-ingress-parity.yaml
# Optional full cluster teardown
make bench-k3s-down K3S_NAME=bench
```

Step 4: Repeats for Statistical Confidence
- Run each lane at least 5 times per platform.
- Current baseline policy:
  - Keep existing `r2` artifacts as baseline.
  - Add `r3..r6` as four additional paired cycles.
  - Run order per cycle: `k1s` first, then `k3s`.

Paired cycle template (`r3..r6`):
```bash
for n in 3 4 5 6; do
  echo "=== cycle r${n}: k1s ==="
  RESULTS_DIR="$ROOT/k1s" \
  RUN_STAMP_BASE="${RUN_ID}-k1s-r${n}" \
  CONCURRENCIES_CSV=30,50,70 \
  PERF_MIN_RPS=220 \
  PERF_MAX_P95_MS=300 \
  PERF_MAX_P99_MS=500 \
  PERF_MAX_ERROR_RATE=0.01 \
  WS_MIN_CONNECTED_RATIO=1 \
  WS_MAX_CONNECT_FAILURE_RATE=0 \
  WS_MAX_MESSAGE_LOSS=0 \
  scripts/dev/run_ingress_kpi_minimatrix.sh

  echo "=== cycle r${n}: k3s ==="
  K3S_BASE_URL="https://127.0.0.1"

  python scripts/dev/ingress_deep_probe.py ws_soak \
    --url "$K3S_BASE_URL/ws" \
    --host "ws-echo-core-proxy.home.arpa" \
    --duration-seconds 600 \
    --connections 50 \
    --heartbeat-seconds 5 \
    > "$ROOT/k3s/k3s-r${n}-ws-echo-deep.json"

  python scripts/dev/ingress_deep_probe.py lb_sample \
    --url "$K3S_BASE_URL/id" \
    --host "lb-distribution-core-proxy.home.arpa" \
    --strategy round_robin \
    --requests 5000 \
    --min-backends 2 \
    --max-skew-ratio 0.35 \
    > "$ROOT/k3s/k3s-r${n}-lb-distribution-deep.json"

  python scripts/dev/ingress_deep_probe.py sticky_probe \
    --url "$K3S_BASE_URL/id" \
    --host "sticky-cookie-core-proxy.home.arpa" \
    --requests-per-client 100 \
    > "$ROOT/k3s/k3s-r${n}-sticky-deep.json"

  for c in 30 50 70; do
    python scripts/dev/ingress_deep_probe.py http_bench \
      --url "$K3S_BASE_URL/id" \
      --host "ws-echo-core-proxy.home.arpa" \
      --duration-seconds 180 \
      --warmup-seconds 20 \
      --concurrency "$c" \
      > "$ROOT/k3s/k3s-r${n}-ws-echo-c${c}.json"

    python scripts/dev/ingress_deep_probe.py http_bench \
      --url "$K3S_BASE_URL/id" \
      --host "lb-distribution-core-proxy.home.arpa" \
      --duration-seconds 180 \
      --warmup-seconds 20 \
      --concurrency "$c" \
      > "$ROOT/k3s/k3s-r${n}-lb-c${c}.json"

    python scripts/dev/ingress_deep_probe.py http_bench \
      --url "$K3S_BASE_URL/id" \
      --host "sticky-cookie-core-proxy.home.arpa" \
      --duration-seconds 180 \
      --warmup-seconds 20 \
      --concurrency "$c" \
      > "$ROOT/k3s/k3s-r${n}-sticky-c${c}.json"
  done
done
```

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
