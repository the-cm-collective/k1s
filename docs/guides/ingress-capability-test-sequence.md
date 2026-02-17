# Ingress Capability Test Sequence (Multi-Site CRI)

This runbook is the canonical sequence for exercising ingress capability tests across:

- `core-proxy`
- `core-to-edge-public`
- `edge-local`

It covers:

1. Single-host matrix validation
2. Multi-host matrix validation
3. Fault injection and recovery drills
4. Repeatability/stability gate

## Scope and Outcomes

This sequence validates that ingress mode behavior, route propagation, and workload serving are correct for the supported workload archetypes in CRI-based multi-site setups.

This runbook does **not** claim throughput/latency benchmarking beyond basic stability checks.

## Prerequisites

- Working CRI core/edge environment (controller, core node, edge core, edge node).
- For `edge-local` tests:
  - core started with `EDGE_INGRESS_MODE=edge-local`
  - core started with `AE_ROUTE_BUNDLE_ENABLED=1`
  - core started with `AE_ENABLE_SERVICE_PROXY=1` and `AE_SERVICE_PROVIDER=iptables` (strict `bundle-endpoints` lane)
  - gateway started with `AE_EDGE_LOCAL_UPSTREAM_MODE=bundle-endpoints`
- Required commands available:
  - `python`, `curl`, `rg`, `sudo`
  - `crictl` (recommended for diagnostics/restart counters)
- Repo root is current working directory.

## Step 1: Normalize Core Specs Directory Permissions

Run before each test session, especially after `sudo` runs:

```bash
CORE_SPECS=state/profiles/k1s-core/specs
sudo chown -R "$USER:$(id -gn)" "$CORE_SPECS"
sudo chmod -R g+rwX "$CORE_SPECS"
sudo find "$CORE_SPECS" -type d -exec chmod 2775 {} \;
test -w "$CORE_SPECS" && echo "core specs writable: $CORE_SPECS"
```

## Step 2: Single-Host Capability Matrix (Mode-Isolated)

Run each mode on a stack started for that mode. Do not run mixed-mode rows on a single stack profile (for example `edge-local` rows on a `core-proxy` stack).

Preflight before long lanes:

```bash
sudo -v
scripts/dev/validate_ingress_env.sh --lane core-proxy --watchdog
```

Optional wrappers (recommended for mode transitions):

```bash
scripts/dev/run_ingress_lanes.sh --lanes all
scripts/dev/run_ingress_mode_lanes.sh --lanes all
```

Use `--yes` to skip checkpoint prompts when running non-interactively.
Use `--security-all` to run baseline + active security checks after each lane.

### 2.1 Core-Proxy Mini Sanity Lane

```bash
CORE_PROXY_FORCE_RATHOLE_RESTART=0 \
scripts/dev/test_ingress_matrix_single_host.sh \
  --modes core-proxy \
  --archetypes ws-echo \
  --tier tier2 \
  --validation-profile standard
```

### 2.2 Core-Proxy Primary Deep Lane (Policy + Observability)

```bash
CORE_PROXY_FORCE_RATHOLE_RESTART=0 \
scripts/dev/test_ingress_matrix_single_host.sh \
  --modes core-proxy \
  --archetypes ws-echo,lb-distribution,sticky-cookie \
  --tier tier2 \
  --validation-profile deep+perf \
  --perf-profile sample \
  --lb-proof-scope auto
```

### 2.3 Optional Full Core-Proxy Lane

```bash
CORE_PROXY_FORCE_RATHOLE_RESTART=0 \
scripts/dev/test_ingress_matrix_single_host.sh \
  --modes core-proxy \
  --archetypes http-static,http-path-routing,ws-echo,lb-distribution,sticky-cookie \
  --tier tier2 \
  --validation-profile deep+perf \
  --perf-profile sample \
  --lb-proof-scope auto
```

### 2.4 Core-To-Edge-Public Lane (Separate Stack Start)

Restart core with `EDGE_INGRESS_MODE=core-to-edge-public`, then run:

```bash
scripts/dev/test_ingress_matrix_single_host.sh \
  --modes core-to-edge-public \
  --archetypes http-static,http-path-routing \
  --tier tier1 \
  --validation-profile standard
```

### 2.5 Edge-Local Strict LB Proof Lane (Separate Stack Start)

Start core/gateway in edge-local mode (`EDGE_INGRESS_MODE=edge-local`, `AE_ROUTE_BUNDLE_ENABLED=1`, `AE_ENABLE_SERVICE_PROXY=1`, `AE_SERVICE_PROVIDER=iptables`, `AE_EDGE_LOCAL_UPSTREAM_MODE=bundle-endpoints`, and `AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=...`), then run:

```bash
EDGE_LOCAL_LISTENER_URL="https://lb-distribution-edge-local.home.arpa/" \
scripts/dev/run_ingress_lanes.sh --lanes edge-local --yes
```

Equivalent direct matrix command:

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
  --edge-local-listener-url https://lb-distribution-edge-local.home.arpa/
```

Notes:
- `core-proxy` maps each row workload service port to the fixed local tunnel target (`AE_EDGE_INGRESS_LOCAL_ADDR`, default `127.0.0.1:18081`).
- Keep `CORE_PROXY_FORCE_RATHOLE_RESTART=0` for long lanes to avoid restart churn.
- If etcd churn is high, run `scripts/dev/etcd_maintenance.sh compact-defrag` before re-running long lanes.
- If edge-local deep LB proof reports `backend_count=1` with `counts_by_backend={"empty":...}`, use host-based listener URL (`https://<edge-hostname>/`) instead of `:8443`.
- Result JSON is written under `state/test-results/ingress-matrix-<timestamp>.json`.

### 2.6 Security Baseline and Active Auth Probes

Run after each lane or at least after the core-proxy lane:

```bash
scripts/dev/security_baseline_check.sh --fail-on high
scripts/dev/security_active_tests.sh --fail-on high
```

Artifacts:
- `state/test-results/security-baseline-<timestamp>.json`
- `state/test-results/security-active-<timestamp>.json`

## Step 3: Multi-Host Capability Matrix

Use when core and edge run on separate hosts.

```bash
scripts/dev/test_ingress_matrix_cri.sh \
  --topology multi-host \
  --core-host <core-host-ip> \
  --edge-host <edge-host-ip> \
  --modes core-proxy,core-to-edge-public,edge-local \
  --archetypes http-static,http-path-routing,http-multi-replica,http-multiport,http-redirect,http-large-payload,http2-unary \
  --tier tier1
```

If needed, explicitly set edge-local listener URL:

```bash
scripts/dev/test_ingress_matrix_cri.sh \
  --topology multi-host \
  --core-host <core-host-ip> \
  --edge-host <edge-host-ip> \
  --edge-local-listener-url https://<edge-host-fqdn>/ \
  --modes core-proxy,core-to-edge-public,edge-local \
  --archetypes http-static,http-path-routing,http-multi-replica,http-multiport,http-redirect,http-large-payload,http2-unary \
  --tier tier1
```

If your edge listener uses a non-default TLS port, include it explicitly (for example `https://<edge-host-fqdn>:11443/`).

## Step 4: Fault Injection and Recovery Drills

Use these to confirm expected failure signatures and deterministic recovery.

### 4.1 Specs Permission Drift

```bash
scripts/dev/ingress_fault_injection.sh --fault specs-permission-drift --action cycle
```

### 4.2 Backend Unavailable / Restore

```bash
scripts/dev/ingress_fault_injection.sh --fault backend-unavailable --action cycle \
  --app-name ingress-matrix-static \
  --app-manifest specs/examples/ingress-matrix/http-static.yaml
```

### 4.3 Route-Bundle Permission Regression / Restore

```bash
scripts/dev/ingress_fault_injection.sh --fault nats-route-bundle-permission --action cycle \
  --route-bundle-config ops/dev/nats-hub.conf \
  --nats-reload-cmd "make k1s-core"
```

### 4.4 Controller Restart (requires start command)

```bash
scripts/dev/ingress_fault_injection.sh --fault controller-restart --action cycle \
  --controller-start-cmd "AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=edge-local AE_ROUTE_BUNDLE_ENABLED=1 make k1s-core"
```

### 4.5 Gateway Restart (requires start command)

```bash
scripts/dev/ingress_fault_injection.sh --fault gateway-restart --action cycle \
  --gateway-start-cmd "AE_SITE_ID=sea-edge-02 AE_NODE_ID=edge-1 EDGE_INGRESS_MODE=edge-local AE_EDGE_LOCAL_UPSTREAM_MODE=bundle-endpoints AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=state/profiles/k1s-core/edge-local make k1s-edge-core"
```

## Step 5: Repeatability / Stability Gate

Run repeated matrix checks and aggregate results:

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

Optional fail-fast:

```bash
scripts/dev/test_ingress_matrix_repeat.sh \
  --iterations 10 \
  --topology multi-host \
  --include-faults \
  --fail-fast \
  --faults specs-permission-drift,backend-unavailable,nats-route-bundle-permission \
  --modes core-proxy,core-to-edge-public,edge-local \
  --archetypes http-static,http-path-routing,http-multi-replica,http-multiport,http-redirect,http-large-payload,http2-unary \
  --tier tier1
```

## Step 6: Pass/Fail Gates

Matrix run pass criteria:

- JSON summary has `failed_rows = 0`.
- Primary core-proxy deep/deep+perf lanes should report:
  - `lb_policy_passed = true`
  - `lb_observability_passed = true`
- Strict edge-local audit lane should report:
  - `lb_strict_proof_passed = true`

Repeat run pass criteria:

- Summary has `failed_iterations = 0`.
- `total_failed_rows = 0`.

If any gate fails, inspect failure bundles before re-running.

## Artifacts and Where to Look

- Per matrix run:
  - `state/test-results/ingress-matrix-<timestamp>.json`
- Per failure bundle:
  - `state/test-results/failures/ingress-matrix-<timestamp>/...`
- Repeat aggregate summary:
  - `state/test-results/ingress-matrix-summary-<timestamp>.json`
- Repeat per-iteration logs:
  - `state/test-results/ingress-matrix-<timestamp>-iterN.log`

## Failure Triage Sequence

Run in order:

1. Check specs directory writability:

```bash
CORE_SPECS=state/profiles/k1s-core/specs
ls -ld "$CORE_SPECS"
test -w "$CORE_SPECS" && echo "writable" || echo "not writable"
```

2. Confirm edge-local env flags (if testing edge-local):

```bash
cpid=$(pgrep -f "python -m ae.controller" | head -n1)
gpid=$(pgrep -f "python -m ae.gateway" | head -n1)
sudo cat "/proc/$cpid/environ" | tr '\0' '\n' | rg 'EDGE_INGRESS_MODE|AE_ROUTE_BUNDLE_ENABLED|AE_TRANSPORT_BACKEND|AE_NATS_URL'
sudo cat "/proc/$gpid/environ" | tr '\0' '\n' | rg 'EDGE_INGRESS_MODE|AE_EDGE_LOCAL_INGRESS_CONFIG_DIR|AE_SITE_ID|AE_NODE_ID|AE_TRANSPORT_BACKEND|AE_NATS_URL'
```

3. Verify route-bundle permission in hub NATS config:

```bash
rg -n 'user: "hub-controller"|k1s.v1.site.\*.routes.bundle' ops/dev/nats-hub.conf
```

4. Verify rendered config presence:

```bash
ls -l state/profiles/k1s-core/edge-ingress/envoy.yaml
ls -l state/profiles/k1s-core/edge-local/edge-local.caddy || true
```

5. Probe backend directly:

```bash
EDGE_LOCAL_ADDR="${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081}"
curl -sS -o /dev/null -w '%{http_code}\n' "http://${EDGE_LOCAL_ADDR}/"
```

## Quick Command Reference

| Command | Purpose | Primary Output |
|---|---|---|
| `scripts/dev/test_ingress_matrix_single_host.sh` | Single-host capability matrix | `state/test-results/ingress-matrix-*.json` |
| `scripts/dev/test_ingress_matrix_cri.sh` | Topology-aware wrapper (single/multi-host) | Matrix JSON + failure bundles |
| `scripts/dev/ingress_fault_injection.sh` | Fault inject/recover/cycle | Fault action logs |
| `scripts/dev/test_ingress_matrix_repeat.sh` | Multi-iteration stability gate | `state/test-results/ingress-matrix-summary-*.json` |
