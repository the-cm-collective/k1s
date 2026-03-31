# VM Variant Runbook (A/B/C Fabric)

Purpose
- Start, configure, and run k1s fabric test variants on local KVM/QEMU.
- Canonical HA operator bootstrap sequence: [HA Cluster Bring-Up](ha-cluster-bring-up.html)

Reference variants
- `lab/variants/test1-a-only-passthrough.yaml`
- `lab/variants/test2-ab-passthrough.yaml`
- `lab/variants/test3-abc-no-gpu.yaml` (validated non-GPU baseline)
- `lab/variants/test3-abc-pp2.yaml` (passthrough profile)
- `lab/variants/ha-control-plane-hub-node.yaml` (retained/manual HA smoke: 3 `k1s-ha-core` + 1 `k1s-core-node`)
- `lab/variants/ha-control-plane-core.yaml` (HA closeout topology: 3 `k1s-ha-core` + 1 `k1s-edge-core` site)
- `lab/variants/ha-control-plane-core-drills.yaml` (same HA topology, with disruptive drill commands enabled)

Transport defaults
- Variants default to `transport.leaf_uplink_mode: direct_ip`.
- Edge-core bootstrap resolves hub leaf endpoint from `transport.hub_host` (or core host IP) and `transport.hub_leaf_port` (default `7422`).
- Use `local_tunnel` only when each edge host intentionally forwards hub leaf traffic to localhost.

## 0) Single-command smoke (recommended Make + helper flow)

```bash
sudo -v
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_multi_non_gpu"
export RUN_ID

AE_CRI_CACHE_SEED_ENGINE=docker \
AE_CRI_CACHE_SEED_MODE=required \
AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 \
make lab-vm-smoke \
  VARIANT=lab/variants/test3-abc-no-gpu.yaml \
  RUN_ID="$RUN_ID" \
  LAB_VM_SMOKE_ARGS="--purge --destroy-network --lanes multi_non_gpu"
```

What this runs:
- `make lab-vm-smoke`, which now wraps `scripts/lab/vm/smoke_helper.py`
- `scripts/lab/vm/smoke_helper.py`, which in turn wraps `smoke_v2.py`
- `smoke_v2.py` underneath, with live phase/check status projected from `runs/<RUN_ID>/...`
- `variant_down.sh` automatically on success when `--teardown on-success` is in effect
- failed runs are kept by default for inspection

Notes:
- `AE_CRI_CACHE_SEED_ENGINE=docker` uses host Docker credentials/tokens for seed pulls.
- `AE_CRI_CACHE_SEED_MODE=required` fails early if seed bundle import/coverage is incomplete.
- `AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0` prefers cached source images and avoids unnecessary remote pulls.
- `smoke_helper.py` expects `sudo -v` to have been run already; it keeps `sudo -n true` warm for the run and teardown.
- Helper-owned wrapper flags are `--teardown on-success|always|never`, `--purge`, `--destroy-network`, and `--console`; pass any remaining `smoke_v2.py` flags after those helper flags.
- `LAB_VM_SMOKE_ARGS` is forwarded to the helper; helper flags and smoke_v2 passthrough flags can both be passed there.
- The smoke/drill lanes now assume prereq-ready qcow2 images. Re-run `scripts/lab/vm/labctl.sh image build --variant all` and `scripts/lab/vm/labctl.sh image verify --variant all` after image/bootstrap changes before treating a fresh-VM bootstrap failure as product regressions.
- `AE_VM_BOOTSTRAP_AUTOFIX=1` can re-enable guest-side repair for manual debugging, but it is intentionally not the default lane contract.

Key artifacts:
- `runs/<RUN_ID>/plan.json`
- `runs/<RUN_ID>/global_phases.json`
- `runs/<RUN_ID>/ha_summary.json` (when the lane is `ha_control_plane`)
- `runs/<RUN_ID>/lanes/<lane>/phase_status.json`
- `runs/<RUN_ID>/lanes/<lane>/checks/service_ready.json`
- `runs/<RUN_ID>/lanes/<lane>/checks/fabric_validate.json`
- `runs/<RUN_ID>/lanes/<lane>/checks/functional_basic.json`
- `runs/<RUN_ID>/summary.json`

HA closeout lane example:

```bash
sudo -v
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_ha_control_plane"
export RUN_ID

AE_CRI_CACHE_SEED_ENGINE=docker \
AE_CRI_CACHE_SEED_MODE=required \
AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 \
make lab-vm-smoke \
  VARIANT=lab/variants/ha-control-plane-core.yaml \
  RUN_ID="$RUN_ID" \
  LAB_VM_SMOKE_ARGS="--purge --destroy-network"
```

Deeper disruptive HA drill example:

```bash
sudo -v
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_ha_drills"
export RUN_ID

AE_CRI_CACHE_SEED_ENGINE=docker \
AE_CRI_CACHE_SEED_MODE=required \
AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 \
make lab-vm-smoke \
  VARIANT=lab/variants/ha-control-plane-core-drills.yaml \
  RUN_ID="$RUN_ID" \
  LAB_VM_SMOKE_ARGS="--teardown never"
```

That drill-enabled variant wires the optional HA drill hooks through `scripts/lab/vm/ha_drill_actions.sh`, so the wrapper will report `ha_drill_leader_failover`, `ha_drill_etcd_restart`, and `ha_drill_transport_recovery` instead of skipping them.

Image readiness for retained/manual HA smoke:

```bash
scripts/lab/vm/labctl.sh image verify --variant all
```

If verify fails, or you changed image/bootstrap contents, rebuild the images:

```bash
scripts/lab/vm/labctl.sh image build --variant all
scripts/lab/vm/labctl.sh image verify --variant all
```

Normal reruns now auto-clean the matching per-variant Packer work directory. If
you are troubleshooting a badly interrupted local build, you can still manually
remove `artifacts/images/build-base` and `artifacts/images/build-gpu` first.

If you are reusing an existing retained HA run id, tear that run down before host prep:

```bash
scripts/lab/vm/labctl.sh variant down \
  --variant lab/variants/ha-control-plane-hub-node.yaml \
  --run-id "$RUN_ID" \
  --purge
```

Preferred retained operator flow:

```bash
sudo -v
make lab-vm-ha-dashboard-up
make lab-vm-ha-dashboard-status
make lab-vm-ha-dashboard-workload-smoke
```

On NixOS, this retained helper path now applies the local DNS/TLS bridge automatically; no separate `nixos-rebuild` should be required after a successful `up`.

Normal retained rerun sequence:

```bash
make lab-vm-ha-dashboard-purge
make lab-vm-ha-dashboard-up
make lab-vm-ha-dashboard-status
make lab-vm-ha-dashboard-workload-smoke
```

Override the retained run id or variant when needed:

```bash
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_smoke" \
VARIANT=lab/variants/ha-control-plane-hub-node.yaml \
make lab-vm-ha-dashboard-up
```

Retained rebuild/restart commands:

```bash
make lab-vm-ha-dashboard-refresh-all \
  LAB_VM_HA_DASHBOARD_ARGS="--target all"

make lab-vm-ha-dashboard-down

make lab-vm-ha-dashboard-purge

make lab-vm-ha-dashboard-workload-smoke

RUN_ID=<live-ha-core-run> make lab-vm-ha-core-workload-smoke

make lab-vm-ha-dashboard-reset

make lab-vm-ha-dashboard-reset \
  LAB_VM_HA_DASHBOARD_ARGS="--rebuild-images --destroy-network"
```

- `make lab-vm-ha-dashboard-refresh-all` is the retained-VM rebuild and restart path on the current VMs.
- `make lab-vm-ha-dashboard-down` stops the retained VMs but keeps retained run metadata for a later restart.
- `make lab-vm-ha-dashboard-purge` is the authoritative retained cleanup path; it does best-effort teardown for partial or orphaned runs, removes retained VM state and `runs/<RUN_ID>`, cleans retained host mappings, and removes the repo-built host images used by this lane.
- `make lab-vm-ha-dashboard-workload-smoke` is the retained stage-1 ingress check: it deploys the retained HA web smoke app onto `hub-1`, translates app ingress to `core-local`, verifies `/healthz` and `/` through the HA core Envoy, and then cleans the workload up again before exiting.
- `make lab-vm-ha-core-workload-smoke` is the stage-2 helper for a live `lab/variants/ha-control-plane-core.yaml` run; it deploys a worker-pinned smoke app onto `edge-sea-node` and verifies true `core-proxy` routing through the gateway-capable HA topology.
- `make lab-vm-ha-dashboard-reset` runs that same purge logic and then brings the retained lane back up.
- `make lab-vm-ha-dashboard-reset LAB_VM_HA_DASHBOARD_ARGS="--rebuild-images --destroy-network"` is the full expensive path when guest/bootstrap contracts changed.

Lower-level commands remain available when you want the raw orchestration steps:

```bash
sudo -v
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_smoke
scripts/lab/vm/labctl.sh host prepare \
  --variant lab/variants/ha-control-plane-hub-node.yaml \
  --apply
AE_CRI_CACHE_SEED_ENGINE=docker \
AE_CRI_CACHE_SEED_MODE=required \
AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=0 \
make lab-vm-smoke \
  VARIANT=lab/variants/ha-control-plane-hub-node.yaml \
  RUN_ID="$RUN_ID" \
  LAB_VM_SMOKE_ARGS="--teardown never"
```

Use that retained HA run as the Envoy-host manual-smoke lane on one workstation:
- open `https://dash.home.arpa:10443/dashboard`
- open `https://docs.home.arpa:10443/`
- open `https://api.home.arpa:10443/swagger` or `https://api.home.arpa:10443/redoc`
- after `make lab-vm-ha-dashboard-up`, `getent hosts dash.home.arpa docs.home.arpa api.home.arpa` should resolve to the retained HA ingress IP instead of the prior localhost dev mapping
- successful retained `up` now verifies that host-side mapping before it reports success
- `make lab-vm-ha-dashboard-purge` and `make lab-vm-ha-dashboard-reset` restore the prior localhost-oriented mapping on purge/reset when one was already present; if no prior snapshot exists they remove the retained managed mapping instead
- treat `https://api.home.arpa:10443/dashboard` as an expected `404`; dashboard lives on `dash.home.arpa`
- use `curl --resolve <host>:10443:<core-ip> ...` when you want to verify a specific core behind the shared hostnames
- keep direct controller `http://192.168.155.10:9108/dashboard` and direct API shim `https://192.168.155.10:8445` as secondary diagnostics
- the harness automatically validates that `hub-1` (`192.168.155.20`, `role=hub,site=hub`) registers and runs the pinned `shell-demo-node-hub` workload
- `hub-1` reaches the controller agent API on `:9110` and runs without Rosenpass/WireGuard in this retained lane; this stage-1 lane validates HA control-plane health plus `core-local` ingress to the retained compute node, while edge/gateway transport validation stays with the stage-2 HA closeout topology
- export local auth, verify `/system`, and use the same bearer token for the dashboard data panels:

```bash
source <(APISHIM_ENV_FILE=state/profiles/k1s-ha-core/apishim.env CONTROLLER_ENV_FILE=state/profiles/k1s-ha-core/controller.env bash scripts/ae-env.sh local)
curl -sk \
  --resolve api.home.arpa:10443:192.168.155.10 \
  -H "Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}" \
  https://api.home.arpa:10443/system | python -m json.tool
```

- rebuild host docs against the Envoy hosts if you want a local static docs entrypoint:

```bash
DOCS_API_BASE=https://docs.home.arpa:10443 \
DOCS_DASHBOARD_URL=https://dash.home.arpa:10443/dashboard \
python docs/build_docs.py
python -m http.server 9109 --directory docs/site
```

Retained stage-1 workload deploy and core-local Envoy validation (self-cleaning smoke):

```bash
make lab-vm-ha-dashboard-workload-smoke
```

This helper deploys `ha-web-smoke`, verifies direct pod reachability plus Envoy ingress, and then removes the workload during cleanup. A later host-side `curl --resolve ha-web-smoke.home.arpa:10443:192.168.155.10 ...` is expected to return `404` unless you deploy the manifest manually and leave it running.

For persistent manual deploys that stay up for host-side dev testing:

```bash
set -a
source state/profiles/k1s-ha-core/controller.env
set +a

source <(APISHIM_ENV_FILE=state/profiles/k1s-ha-core/apishim.env CONTROLLER_ENV_FILE=state/profiles/k1s-ha-core/controller.env bash scripts/ae-env.sh local)

PYTHONPATH=src ./.venv/bin/python -m ae.cli apply \
  -f docs/site/examples/ha-web-smoke.yaml

PYTHONPATH=src ./.venv/bin/python -m ae.cli status \
  --watch 2 \
  --timeout 180 \
  ha-web-smoke

PYTHONPATH=src ./.venv/bin/python -m ae.cli events \
  --limit 20 \
  ha-web-smoke

curl --noproxy "*" -sk \
  --resolve ha-web-smoke.home.arpa:10443:192.168.155.10 \
  https://ha-web-smoke.home.arpa:10443/healthz

curl --noproxy "*" -sk \
  --resolve ha-web-smoke.home.arpa:10443:192.168.155.10 \
  https://ha-web-smoke.home.arpa:10443/ | rg 'Shell + Port-Forward Smoke'
```

Clean it up when you are done:

```bash
PYTHONPATH=src ./.venv/bin/python -m ae.cli delete --purge ha-web-smoke
```

Stage-2 gateway/worker `core-proxy` validation against a live `ha-control-plane-core` run:

```bash
RUN_ID=<live-ha-core-run> make lab-vm-ha-core-workload-smoke

curl -sk \
  --resolve ha-edge-web-smoke.home.arpa:10443:192.168.155.10 \
  https://ha-edge-web-smoke.home.arpa:10443/healthz

curl -sk \
  --resolve ha-edge-web-smoke.home.arpa:10443:192.168.155.10 \
  https://ha-edge-web-smoke.home.arpa:10443/ | rg 'Shell + Port-Forward Smoke'
```

Notes:
- Treat this as a retained VM smoke environment, not a supported single-host HA dev profile.
- Treat `dash.home.arpa`, `docs.home.arpa`, and `api.home.arpa` on `:10443` as the primary public control-plane surface for this retained lane.
- In HA, the docs Playground is disabled by default, including this retained VM lane. Use `AE_PLAYGROUND=1` only for exceptional local testing on a non-public control plane.
- Quick host check from this workstation: `getent hosts dash.home.arpa docs.home.arpa api.home.arpa`
- Read surfaces stay usable on any healthy controller; leader-only mutations still return `not_leader` on followers.
- In this HA VM profile, `/system` is bearer-protected and the dashboard needs the same bearer token for its data panels.
- If `AE_API_READ_TOKEN` is unset, use `AE_API_ADMIN_TOKEN` in the header above and in the dashboard `Bearer` field.
- The retained workload smoke uses the shared HA state store because controller HTTP mutations stay disabled in this profile.
- For manual persistent deploys, load the generated shared-store env from `state/profiles/k1s-ha-core/controller.env` before running `ae apply/status/delete` so the CLI targets the HA etcd authority instead of the default local SQLite DB.
- `ha-web-smoke.home.arpa` is intentionally not added to the managed workstation host mapping; use `curl --resolve ...` for both the self-cleaning smoke follow-up and the manual persistent deploy path instead of extending `/etc/hosts`.
- Use `lab/variants/ha-control-plane-core.yaml` instead when you want the checked-in stage-2 HA edge/gateway transport topology and true `core-proxy` validation.
- If you are reusing `k1s-br0` from a different variant CIDR, tear that lane down with `--destroy-network` before running the HA host prep command above.
- If the controller backing the local docs build changes, rebuild with another core URL.

Or explicit teardown after result inspection. Use `--destroy-network` only when you want full bridge cleanup or are switching to another subnet:

```bash
scripts/lab/vm/labctl.sh variant down \
  --variant lab/variants/ha-control-plane-hub-node.yaml \
  --run-id "$RUN_ID" \
  --purge
```

Direct helper usage remains available when you want to bypass Make and call the retained helper explicitly:

```bash
scripts/lab/vm/ha_dashboard_smoke.sh up \
  --variant lab/variants/ha-control-plane-hub-node.yaml \
  --run-id "$RUN_ID"
```

## 1) Host prerequisite check

```bash
scripts/lab/vm/labctl.sh host prepare
```

Apply bridge/NAT setup:

```bash
scripts/lab/vm/labctl.sh host prepare --apply
```

For the checked-in HA closeout topology, prefer the variant-aware form so the host bridge matches the variant CIDR exactly:

```bash
scripts/lab/vm/labctl.sh host prepare \
  --variant lab/variants/ha-control-plane-core.yaml \
  --apply
```

## 2) Bring up a variant

```bash
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_test3
scripts/lab/vm/labctl.sh variant up \
  --variant lab/variants/test3-abc-no-gpu.yaml \
  --run-id "$RUN_ID"
```

This writes:
- `runs/<RUN_ID>/topology.json`
- `runs/<RUN_ID>/qemu_inventory.json`

## 3) Bootstrap k1s services

For the ordered strict-CRI HA control-plane sequence, use [HA Cluster Bring-Up](ha-cluster-bring-up.html). This section focuses on variant-backed bootstrap mechanics and helper-driven execution.

Generate host bootstrap scripts:

```bash
scripts/lab/vm/k1s_bootstrap.sh \
  --variant lab/variants/test3-abc-no-gpu.yaml \
  --run-id "$RUN_ID"
```

Execute bootstrap directly over SSH:

```bash
scripts/lab/vm/k1s_bootstrap.sh \
  --variant lab/variants/test3-abc-no-gpu.yaml \
  --run-id "$RUN_ID" \
  --execute
```

Default strict-CRI core bootstrap behavior now includes:
- `AE_CRI_REGISTRY_TRUST_SYSTEM=1` for managed-registry CA installation into system trust.
- `AE_CRI_REGISTRY_PRELOAD=1` so core strict-CRI images are mirrored/pulled before `k1s-core` starts.
- `AE_CRI_CACHE_SEED_MODE=required` (when run via `smoke_v2`) to enforce pre-seeded image availability during bootstrap.
- A strict guest prereq check for `python`, `crictl`, `/etc/crictl.yaml`, CNI binaries/config, and valid containerd config. Missing prerequisites now fail fast as a stale-image problem unless `AE_VM_BOOTSTRAP_AUTOFIX=1` is set explicitly.

Override when needed:

```bash
AE_CRI_REGISTRY_TRUST_SYSTEM=0 \
AE_CRI_REGISTRY_PRELOAD=0 \
AE_CRI_CACHE_SEED_MODE=best_effort \
scripts/lab/vm/k1s_bootstrap.sh \
  --variant lab/variants/test3-abc-no-gpu.yaml \
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

For the HA closeout variant, do not use manual `REGISTER_ONLY` registration. The `ha_shared_infra` phase pre-renders the shared hub NATS configs with the required site uplink users before `k1s-ha-core` bootstrap begins.

To generate or execute that HA shared-backend step directly:

```bash
scripts/lab/vm/ha_shared_infra.sh \
  --variant lab/variants/ha-control-plane-core.yaml \
  --run-id "$RUN_ID"

scripts/lab/vm/ha_shared_infra.sh \
  --variant lab/variants/ha-control-plane-core.yaml \
  --run-id "$RUN_ID" \
  --execute
```

## 4) Validate host readiness

```bash
scripts/lab/vm/labctl.sh variant validate \
  --variant lab/variants/test3-abc-no-gpu.yaml \
  --run-id "$RUN_ID"
```

Validation report:
- `runs/<RUN_ID>/variant_validate.json`

## 5) Teardown

```bash
scripts/lab/vm/labctl.sh variant down \
  --variant lab/variants/test3-abc-no-gpu.yaml \
  --run-id "$RUN_ID"
```

Optional full cleanup:

```bash
scripts/lab/vm/labctl.sh variant down \
  --variant lab/variants/test3-abc-no-gpu.yaml \
  --run-id "$RUN_ID" \
  --purge \
  --destroy-network
```

## 6) Troubleshooting and result interpretation

Common failures:
- `inventory not found for run_id=...` during `variant down`: run ID is stale/missing. Use an existing run ID from `state/lab-vm/` or skip teardown.
- `qemu failed to start ... missing pidfile`: inspect `state/lab-vm/<RUN_ID>/logs/<host>.qemu.log`, clear stale processes/resources, then retry.
- `toomanyrequests` / `429 Too Many Requests` during image preload: ensure `AE_CRI_CACHE_SEED_ENGINE=docker` is set, Docker auth is configured (`docker login`), and keep `AE_CRI_CACHE_SEED_MODE=required`.

`fabric_validate` interpretation:
- Healthy pass: `status=passed`, `leafz_count` meets expected edge count, `failing_edges=[]`.
- Telemetry-only note: `detail=ok (leafz+partial-log-signals)` can still be acceptable when `leafz_count` is correct and `failing_edges=[]`.
- Fabric failure: `detail=fabric_not_stable`, `leafz_count` below expected, or non-empty `failing_edges`.
