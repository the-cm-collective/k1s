# Validated Procedures

Purpose
- Keep the currently validated command readouts in one published docs page for copy/paste use.
- Treat this page as the exact procedure reference.
- Use the surrounding docs for context, rationale, topology details, and troubleshooting.

Latest validation snapshot

| Procedure | Primary entrypoint | Success signal | Last validated |
| --- | --- | --- | --- |
| Simple dashboard user test | `make demo` | docs + dashboard load on `:8443`, simple layout visible | 2026-04-14 |
| Advanced dashboard user test | `make lab-vm-ha-attached-node-up` | docs + dashboard load on `:10443`, HA section visible | 2026-04-14 |
| HA stage 1/2 validation | `make lab-vm-ha-validation` | `stage1`, `retained`, `stage2`, `stage2-live` green | 2026-04-14 |
| Benchmark retained rebuild | `make bench-retained-rebuild PROFILE=final STAMP=r20260421-223436-authoritative2 DELETE_DROPPED=1` | `combined/combined.csv` has `110` retained rows (`40` frozen legacy + `70` current) | 2026-04-22 |
| Full benchmark rerun | split baseline + CRI flow | fresh `STAMP` contributes `70` rows; retained publish set has `110` rows after rebuild | 2026-04-22 |

Current pre-tag release verification baseline
- Treat Debian and NixOS as pooled cross-host verification inputs for the current pre-tag pass; do not claim that each host independently passed the full release matrix.
- Standardize release verification on `AE_USE_REGISTRY_CACHE=0` on both hosts.
- Require the common baseline on both hosts:

```bash
cd /path/to/k1s
export PATH="$PWD/.venv/bin:$PATH"
export AE_USE_REGISTRY_CACHE=0

make env-doctor
AE_CRI_REQUIRE_RUNTIME_READY=1 ./scripts/cri_preflight.sh
python -m pytest --maxfail=1 --disable-warnings -q
make docs-verify
make profile-smoke
make ha-closeout-e2e
```

- Authoritative host-owned lanes for the current pre-tag pass:
  - Debian: `make e2e`
  - Debian: `make strict-cri-smoke`
  - NixOS: `make lab-vm-ha-validation`
  - NixOS: full benchmark rerun from this page
- Target posture for the next release: both hosts pass the full release matrix independently.

## Simple Dashboard User Test

Preconditions
- Run from the repo root with the repo venv on `PATH`.
- This procedure is for local single-controller demo/dev flows.

Commands

```bash
cd /path/to/k1s
export PATH="$PWD/.venv/bin:$PATH"

sudo -v
make demo
getent hosts dash.home.arpa docs.home.arpa api.home.arpa
```

User-test URLs
- `https://docs.home.arpa:8443/`
- `https://dash.home.arpa:8443/dashboard`
- `https://api.home.arpa:8443/swagger`

Acceptance checks
- Docs and dashboard load from the host on `:8443`.
- The dashboard shows the shared `System Graph`.
- The `HA Control Plane` section is hidden.
- The `HA Members` legend key is hidden.

Cleanup

```bash
make demo-down
```

## Advanced Dashboard User Test (Retained HA VM)

Preconditions
- Use this for the retained HA attached-node lane.
- If image/bootstrap contracts changed, rebuild/verify images first.

Commands

```bash
cd /path/to/k1s
export PATH="$PWD/.venv/bin:$PATH"

sudo -v
scripts/lab/vm/labctl.sh image verify --variant all
make lab-vm-ha-attached-node-up
make lab-vm-ha-attached-node-status
getent hosts dash.home.arpa docs.home.arpa api.home.arpa

source <(APISHIM_ENV_FILE=state/profiles/k1s-ha-core/apishim.env CONTROLLER_ENV_FILE=state/profiles/k1s-ha-core/controller.env bash scripts/ae-env.sh local)
curl -sk \
  --resolve api.home.arpa:10443:192.168.155.10 \
  -H "Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}" \
  https://api.home.arpa:10443/system | python -m json.tool
```

User-test URLs
- `https://docs.home.arpa:10443/`
- `https://dash.home.arpa:10443/dashboard`
- `https://api.home.arpa:10443/swagger`

Acceptance checks
- Docs and dashboard load from the host on `:10443`.
- The dashboard shows the shared `System Graph`.
- The `HA Control Plane` section is visible.
- `/system` returns the HA payload when queried with the read/admin bearer token.

Cleanup

```bash
make lab-vm-ha-attached-node-down
# or, for authoritative cleanup:
make lab-vm-ha-attached-node-purge
```

## HA Stage 1/2 Validation

Preferred umbrella runner

```bash
cd /path/to/k1s
export PATH="$PWD/.venv/bin:$PATH"

sudo -v
make lab-vm-ha-validation
```

Expected green stages
- `stage1`
- `retained`
- `drain`
- `stage2`
- `stage2-live`
- `drills`

Narrow retained/live helper flow

```bash
cd /path/to/k1s
export PATH="$PWD/.venv/bin:$PATH"

sudo -v
make lab-vm-ha-attached-node-up
make lab-vm-ha-attached-node-status
make lab-vm-ha-attached-node-workload-smoke

RUN_ID=<live-ha-core-run> make lab-vm-ha-core-workload-smoke
```

One-shot reruns

```bash
sudo -v
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_ha_attached_node_stage1" \
AE_CRI_CACHE_SEED_ENGINE=docker \
AE_CRI_CACHE_SEED_MODE=required \
AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 \
make lab-vm-smoke \
  VARIANT=lab/variants/ha-control-plane-attached-node.yaml \
  RUN_ID="$RUN_ID"

sudo -v
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_ha_core_stage2" \
AE_CRI_CACHE_SEED_ENGINE=docker \
AE_CRI_CACHE_SEED_MODE=required \
AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 \
make lab-vm-smoke \
  VARIANT=lab/variants/ha-control-plane-core.yaml \
  RUN_ID="$RUN_ID"
```

Cleanup

```bash
make lab-vm-ha-attached-node-purge
# or retained reset:
make lab-vm-ha-attached-node-reset
```

## Current Retained Benchmark Publish Set

Use this to rebuild the currently published retained benchmark artifacts from the latest validated authoritative rerun.

```bash
cd /path/to/k1s
export PATH="$PWD/.venv/bin:$PATH"

make bench-retained-rebuild PROFILE=final STAMP=r20260421-223436-authoritative2 DELETE_DROPPED=1
```

Acceptance checks
- `combined/combined.csv` contains `110` rows total:
  - `40` frozen `r20260203-legacy*` rows
  - `70` retained current rows from `r20260421-223436-authoritative2*`
- The current retained families normalize to:
  - `r20260421-223436-authoritative2+podman+crun+rootless+cg2`
  - `r20260421-223436-authoritative2+podman+crun+priv+cg2`
  - `r20260421-223436-authoritative2+docker+runc+k1nd`
  - `r20260421-223436-authoritative2+k3d+runc`
  - `r20260421-223436-authoritative2+cri-runc-verify-run1+cri+containerd`
  - `r20260421-223436-authoritative2+cri-runc-verify-run2+cri+containerd`
  - `r20260421-223436-authoritative2+cri-runc-verify-run3+cri+containerd`
- `combined/combined.csv` and `combined/combined.json` remain the authoritative artifacts.

## Historical Interim Benchmark Review (2026-04-17)

Use this only when you need to reconstruct the older April 17, 2026 retained review set.

```bash
cd /path/to/k1s
export PATH="$PWD/.venv/bin:$PATH"

make bench-retained-rebuild PROFILE=interim-20260417 DELETE_DROPPED=1
```

Acceptance checks
- `combined/combined.csv` contains `89` rows total:
  - `40` frozen `r20260203-legacy*` rows
  - `24` retained `r20260417-cri-runc-baseline-clean5-run*+cri+containerd` rows
  - `25` retained `r20260417-overlap-smoke-*` rows
- `combined/combined.csv` contains no `r20260413*`, `r20260415*`, or `r20260417-cri-runc-rollout-probe-*` families.
- `combined/combined.csv` and `combined/combined.json` remain the authoritative artifacts.

## Full Benchmark Rerun

Preconditions
- Run from the repo root with the repo venv on `PATH`.
- The benchmark helpers must be able to build or reuse `localhost/demo-blue:latest` and `localhost/demo-green:latest`.
- On this host, `k3d` must be able to reach the dedicated Docker socket at `unix:///run/docker-k3d.sock`.
- If `k3d cluster list` fails with a missing `/run/docker-k3d.sock`, restart the Docker socket/service before starting the rerun.

Canonical command sequence

```bash
cd /path/to/k1s
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH="${PYTHONPATH:-src}"
export AE_USE_REGISTRY_CACHE=0
STAMP="r$(date +%Y%m%d-%H%M%S)-authoritative"

sudo -v
sudo systemctl daemon-reload
sudo systemctl restart docker.socket docker.service
ss -lx | rg 'docker(.sock|-k3d.sock)' || true
ls -l /run/docker.sock /run/docker-k3d.sock
k3d cluster list
AE_CRI_REQUIRE_RUNTIME_READY=1 ./scripts/cri_preflight.sh

pytest tests/unit/test_bench_script_contracts.py \
       tests/unit/test_audit_runtime_attribution.py \
       tests/unit/test_mem_aggregate_podman.py \
       tests/unit/test_rebuild_retained_artifacts.py -q

bash -n scripts/bench/run_all_baselines.sh
bash -n scripts/bench/run_rollout_tuning_experiment.sh
python -m py_compile \
  scripts/bench/audit_runtime_attribution.py \
  scripts/bench/mem_aggregate.py

scripts/bench/k1nd_single.sh down || true
make bench-k3s-down K3S_NAME=bench || true
./scripts/bench/bench_env_teardown.sh --env state/bench-env/env.sh || true
./scripts/bench/bench_env_teardown.sh --env state/bench-cri/env.sh || true
sudo pkill -f "python .*ae\\.controller.*state/bench-env/specs" || true
sudo pkill -f "python .*ae\\.controller.*state/bench-baselines/rootful/specs" || true
sudo pkill -f "python .*ae\\.controller.*state/bench-cri/specs" || true

sudo make bench-fix-perms

rm -rf \
  "snapshots/${STAMP}+podman+rootless+cg2"* \
  "snapshots/${STAMP}+podman+priv+cg2"* \
  "snapshots/${STAMP}+docker+k1nd"* \
  "snapshots/${STAMP}+k3d"* \
  "snapshots/${STAMP}+cri-runc-verify-run"* \
  combined charts

make bench-state-clean
sudo make bench-engines-clear CONFIRM=1

# The `LBL_*` values and `snapshots/${STAMP}+...` cleanup globs below target the
# raw on-disk snapshot directories. The retained publish step later normalizes
# those families to the OCI-tagged public names listed in Acceptance checks.
DISABLE_DEV_MIN=0 \
ALLOW_SUDO=1 \
BENCH_BASELINE_STEADY_QUIET=1 \
BASELINE_STEADY_TIMEOUT=180 \
BASELINE_STEADY_DELAY=2 \
BASELINE_STEADY_POLLS=3 \
LBL_K1S_ROOTLESS="${STAMP}+podman+rootless+cg2" \
LBL_K1S_ROOTFUL="${STAMP}+podman+priv+cg2" \
LBL_K1ND="${STAMP}+docker+k1nd" \
LBL_K3D="${STAMP}+k3d" \
APP="specs/examples/echo.yaml" \
APP_NAME="echo" \
K3S_MANIFEST="specs/examples/k3s-echo.yaml" \
DURATION=30 \
REPLICAS="1,5,10" \
ROLL_REPLICAS="2,5" \
make bench-mem-e2e-baselines-sudo

BASE="${STAMP}+cri-runc-verify" \
BENCH_CRI_ROLLOUT_STRATEGY=parallel \
BENCH_CRI_ROLLOUT_MAX_SURGE=0 \
BENCH_CRI_ROLLOUT_MAX_UNAVAILABLE=1 \
BENCH_CRI_STEADY_QUIET=1 \
APP="specs/examples/echo.yaml" \
APP_NAME="echo" \
DURATION=30 \
REPLICAS="1,5,10" \
ROLL_REPLICAS="2,5" \
RUNS="1 2 3" \
./scripts/bench/run_cri_verify.sh

scripts/bench/k1nd_single.sh down || true
make bench-k3s-down K3S_NAME=bench || true

make bench-retained-rebuild PROFILE=final STAMP="$STAMP" DELETE_DROPPED=1
```

Acceptance checks
- All baseline scenarios complete: `k1s rootless`, `k1s rootful`, `k1nd`, and `k3d`.
- CRI verify completes three clean runs: `run1`, `run2`, and `run3`.
- CRI verify is invoked with the accepted rollout-policy publish profile:
  - `BENCH_CRI_ROLLOUT_STRATEGY=parallel`
  - `BENCH_CRI_ROLLOUT_MAX_SURGE=0`
  - `BENCH_CRI_ROLLOUT_MAX_UNAVAILABLE=1`
  - `BENCH_CRI_STEADY_QUIET=1`
- Baseline publish flow is invoked with the accepted steady-state profile:
  - `BENCH_BASELINE_STEADY_QUIET=1`
  - `BASELINE_STEADY_TIMEOUT=180`
  - `BASELINE_STEADY_DELAY=2`
  - `BASELINE_STEADY_POLLS=3`
- Baseline and CRI families publish `10` stages each:
  - `idle`
  - `pods-1`
  - `pods-5`
  - `pods-10`
  - `rollout-2-during`
  - `rollout-2-during-warm`
  - `rollout-2-post`
  - `rollout-5-during`
  - `rollout-5-during-warm`
  - `rollout-5-post`
- Published baseline families normalize to OCI-tagged names:
  - `${STAMP}+podman+crun+rootless+cg2`
  - `${STAMP}+podman+crun+priv+cg2`
  - `${STAMP}+docker+runc+k1nd`
  - `${STAMP}+k3d+runc`
- Published CRI families remain:
  - `${STAMP}+cri-runc-verify-run1+cri+containerd`
  - `${STAMP}+cri-runc-verify-run2+cri+containerd`
  - `${STAMP}+cri-runc-verify-run3+cri+containerd`
- `combined/combined.csv` contains `70` rows for the fresh stamp:

```bash
STAMP="r$(date +%Y%m%d-%H%M%S)-authoritative" python - <<'PY'
import csv
import os
from pathlib import Path

stamp = os.environ["STAMP"]
rows = 0
with Path("combined/combined.csv").open(newline="", encoding="utf-8") as handle:
    for row in csv.DictReader(handle):
        if row["label"].startswith(stamp + "+"):
            rows += 1
print(rows)
PY
```

- the retained publish set contains `110` rows after the final rebuild:

```bash
python - <<'PY'
import csv
from pathlib import Path

with Path("combined/combined.csv").open(newline="", encoding="utf-8") as handle:
    print(sum(1 for _ in csv.DictReader(handle)))
PY
```

- CRI wrapper log (`state/bench-cri-rerun-*.log`) must end with:
  - `rows ${STAMP}+cri-runc-verify-run1+cri+containerd: 10`
  - `rows ${STAMP}+cri-runc-verify-run2+cri+containerd: 10`
  - `rows ${STAMP}+cri-runc-verify-run3+cri+containerd: 10`
- Finalized CRI rows must also count as `10 / 10 / 10`:

```bash
BASE="${STAMP}+cri-runc-verify"
for run in 1 2 3; do
  grep -c "^${BASE}-run${run}+cri+containerd-" combined/combined.csv
done
```

- Optional rollback/comparison rerun for the old CRI parallel baseline:

```bash
STAMP="r$(date +%Y%m%d-%H%M%S)-compare"
BENCH_CRI_ROLLOUT_STRATEGY=parallel \
BENCH_CRI_STEADY_QUIET=0 \
BASE="${STAMP}+cri-runc-parallel-verify" \
APP="specs/examples/echo.yaml" \
APP_NAME="echo" \
DURATION=30 \
REPLICAS="1,5,10" \
ROLL_REPLICAS="2,5" \
RUNS="1 2 3" \
./scripts/bench/run_cri_verify.sh
```

- The live summary is expected to show:
  - `Ctrl/CP` for k1s/k1nd as AE controller PSS
  - `Ctrl/CP` for k3d as k3s control-plane PSS
  - `AppCG` for k3d scaling with replicas
- `combined/combined.csv` and `combined/combined.json` are the authoritative artifacts.
- `matplotlib not available` only means chart generation was skipped; it does not invalidate the run.
