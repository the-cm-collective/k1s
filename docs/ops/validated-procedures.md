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
| Full benchmark rerun | split baseline + CRI flow | `combined/combined.csv` has 40 rows for the run stamp | 2026-04-14 |

Release policy for the 2026-04-15 tag
- Treat Debian and NixOS as pooled cross-host verification inputs for this tag; do not claim that each host independently passed the full release matrix.
- Standardize release verification on `AE_USE_REGISTRY_CACHE=0` on both hosts.
- Require the common baseline on both hosts:

```bash
cd /home/m4xx3d0ut/git/k1s-wt/k1s
export PATH="$PWD/.venv/bin:$PATH"
export AE_USE_REGISTRY_CACHE=0

make env-doctor
AE_CRI_REQUIRE_RUNTIME_READY=1 ./scripts/cri_preflight.sh
python -m pytest --maxfail=1 --disable-warnings -q
make docs-verify
make profile-smoke
make ha-closeout-e2e
```

- Authoritative host-owned lanes for this tag:
  - Debian: `make e2e`
  - Debian: `make strict-cri-smoke`
  - NixOS: `make lab-vm-ha-validation`
  - NixOS: full benchmark rerun from this page
- Starting with the next release, require both hosts to pass the full release matrix independently.

## Simple Dashboard User Test

Preconditions
- Run from the repo root with the repo venv on `PATH`.
- This procedure is for local single-controller demo/dev flows.

Commands

```bash
cd /home/m4xx3d0ut/git/k1s-wt/k1s
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
cd /home/m4xx3d0ut/git/k1s-wt/k1s
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
cd /home/m4xx3d0ut/git/k1s-wt/k1s
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
cd /home/m4xx3d0ut/git/k1s-wt/k1s
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

## Full Benchmark Rerun

Preconditions
- Run from the repo root with the repo venv on `PATH`.
- If `k3d` and `kubectl` are not installed on the host, use the Nix shell wrapper below.
- The benchmark helpers must be able to build or reuse `localhost/demo-blue:latest` and `localhost/demo-green:latest`.

Canonical command sequence

```bash
cd /home/m4xx3d0ut/git/k1s-wt/k1s
export PATH="$PWD/.venv/bin:$PATH"
export AE_USE_REGISTRY_CACHE=0

nix shell nixpkgs#k3d nixpkgs#kubectl -c bash -lc '
set -euo pipefail
cd /home/m4xx3d0ut/git/k1s-wt/k1s
export PATH="$PWD/.venv/bin:$PATH"
export AE_USE_REGISTRY_CACHE=0
STAMP="r$(date +%Y%m%d)-fullretest"

sudo -v

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
  "snapshots/${STAMP}+cri+containerd"* \
  combined charts

make bench-state-clean
sudo make bench-engines-clear CONFIRM=1

DISABLE_DEV_MIN=0 \
ALLOW_SUDO=1 \
LBL_K1S_ROOTLESS="${STAMP}+podman+rootless+cg2" \
LBL_K1S_ROOTFUL="${STAMP}+podman+priv+cg2" \
LBL_K1ND="${STAMP}+docker+k1nd" \
LBL_K3D="${STAMP}+k3d" \
APP="specs/examples/echo.yaml" \
APP_NAME="echo" \
K3S_MANIFEST="specs/examples/k3s-echo.yaml" \
DURATION=30 \
REPLICAS="1,5,10" \
make bench-mem-e2e-baselines-sudo

LABEL_CRI="${STAMP}+cri+containerd" \
APP="specs/examples/echo.yaml" \
APP_NAME="echo" \
DURATION=30 \
REPLICAS="1,5,10" \
ROLL_REPLICAS="2,5" \
make bench-mem-cri

scripts/bench/k1nd_single.sh down || true
make bench-k3s-down K3S_NAME=bench || true
sudo make bench-mem-finalize-sudo
'
```

Acceptance checks
- All five scenarios complete: `k1s rootless`, `k1s rootful`, `k1nd`, `k3d`, and `cri+containerd`.
- `combined/combined.csv` contains `40` rows for the fresh stamp (`5 scenarios x 8 stages`):

```bash
STAMP="r$(date +%Y%m%d)-fullretest" python - <<'PY'
import pathlib, os
stamp = os.environ["STAMP"]
rows = 0
for line in pathlib.Path("combined/combined.csv").read_text().splitlines():
    if stamp + "+" in line:
        rows += 1
print(rows)
PY
```

- The live summary is expected to show:
  - `Ctrl/CP` for k1s/k1nd as AE controller PSS
  - `Ctrl/CP` for k3d as k3s control-plane PSS
  - `AppCG` for k3d scaling with replicas
- CRI must include `idle`, `pods-1`, `pods-5`, `pods-10`, `rollout-2-during`, `rollout-2-post`, `rollout-5-during`, and `rollout-5-post`.
- `combined/combined.csv` and `combined/combined.json` are the authoritative artifacts.
- `matplotlib not available` only means chart generation was skipped; it does not invalidate the run.
