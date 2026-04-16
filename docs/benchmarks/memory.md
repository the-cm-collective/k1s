# Memory Overhead Benchmarks

This guide describes how to profile and compare memory overhead for k1s versus k3s on a single host.
It consolidates the k1s memory profiling and k3s comparison workflows in one place.

Goals
- Separate app memory from control-plane/system overhead.
- Produce repeatable snapshots with raw artifacts and lightweight summaries.
- Keep tooling simple: bash + Python, no invasive agents.

What we measure
- Processes (PSS/RSS/USS) via `/proc/<pid>/smaps_rollup` and `/proc/<pid>/status`:
  - k1s: `python -m ae.controller`, ingress proxy (Caddy), Docker/Podman runtime processes.
  - k3s: `k3s`, `containerd`, `coredns`, ingress controller (Traefik if enabled).
- Containers (cgroups): `memory.current` for each container cgroup via the container PID (split into app vs non‑app based on labels/names).
- Host services (cgroups): sum of `memory.current` for leaf cgroups under `system.slice` plus `init.scope` (cgroup v2), excluding `user.slice` entirely. This avoids parent+child double counting.

Outputs
- `snapshots/<label>/<timestamp>/raw/*`: raw text and JSON (ps, free, vmstat, smaps_rollup, docker/podman inspect, per‑container memory CSV).
- `summary.json`: totals and breakdowns for processes, container cgroups, host system cgroups, and MemAvailable before/after from `free -b`.
- `summary.csv`: one‑line rollup: total PSS, control‑plane PSS, app/system container bytes, host system cgroup bytes.
- `combined/combined.{json,csv}`: merged rollups across labels (for charts and reports).
- `charts/*.png`: control‑plane/system charts and per‑pod overhead plots.

Quick start
1) Take a snapshot (k1s):
```
make bench-mem-k1s LABEL=idle DURATION=30
```

2) Aggregate latest snapshot for a label:
```
make bench-mem-agg LABEL=idle
```

3) Compare scenarios by repeating with different labels:
```
make bench-mem-k1s LABEL=pods-1 DURATION=30
make bench-mem-agg LABEL=pods-1

make bench-mem-k1s LABEL=pods-5 DURATION=30
make bench-mem-agg LABEL=pods-5
```

Scenarios (suggested)
- `idle`: controller running, no apps.
- `pods-1`: one app with 1 replica.
- `pods-5`: one app with 5 replicas.
- `rollout-5`: rolling update across 5 replicas (start snapshot while rollout is in progress).
- `logs-5`: 5 replicas with the dashboard logs panel open.

Tips for consistency
- Use the same host and OS between runs; minimize background services.
- Keep ingress enabled or disabled across both systems for apples-to-apples.
- Allow a 2-minute warm-up before snapshotting busy scenarios.
- If Podman/Docker is not installed, snapshots still run but container-level cgroup metrics are skipped (process PSS totals are still reported).
- CI or advanced users can bypass safety checks by setting `SKIP_GUARDS=1` in the environment.

OCI runtime note
- Using Podman (`AE_RUNTIME_BACKEND=podman`) typically reduces idle control‑plane footprint on long‑lived hosts compared to Docker. Keep the runtime consistent across runs when comparing results.

Interpreting results
- Process PSS approximates unique+fair‑share memory for control‑plane processes.
- Container `memory.current` shows cgroup‑resident memory per container (includes cache); we split into app vs non‑app containers.
- Host system cgroups reflects only OS services under `system.slice` and `init.scope` (excludes user/container trees), summed over leaf cgroups to avoid double counting.
- Per‑pod app footprint (rough) ≈ app_container_bytes / pod_count.
- MemAvailable Δ validates totals at the kernel level: `delta = MemAvailable(before) − MemAvailable(after)` for the snapshot window. Small negatives are clamped to 0.

Limitations
- cgroup paths vary across distros; the snapshotter falls back gracefully.
- If Docker is unavailable, container-level stats are skipped; process PSS is still reported.
- USS is approximated from `Private_*` in `smaps_rollup`.

Caveats
- k3s (via k3d) enables Traefik by default; the provided Ingress uses class `traefik`. Keep ingress turned on in k1s for apples-to-apples, or disable both.
- The echo image `ealen/echo-server:0.7.0` serves `/` on port 80 and has a lightweight memory footprint suitable for baseline comparisons.
- If you prefer your demo image, push it to a registry and update `specs/examples/k3s-echo.yaml` accordingly.

Automate a small matrix (k1s)
- Run idle + scale-out snapshots in one go (requires controller running and echo example available):
```
make bench-mem-matrix-k1s LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30
```
- Combine all summaries into one CSV/JSON for charting:
```
make bench-mem-combine GLOB='snapshots/*/*'
```

End-to-end (k1s one-liner)
```
make bench-mem-e2e-k1s LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
```
This runs matrix + rollout, combines all summaries, and writes charts/.

End-to-end (k1nd one-liner; single-container stack started on demand)
```
make bench-mem-e2e-k1nd LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
```
- Brings up the k1s-in-Docker single-container stack (`ops/bench/k1nd-compose.yaml`) with controller + apishim + Caddy and then runs the same matrix + rollout sequence against that environment.
- Uses Docker for host-side preflights and container cgroup metrics; ensure the current user can run `docker ps`.
- Leave it running for repeated runs by setting `K1ND_SKIP_POST_DOWN=1`, or tear down with `scripts/bench/k1nd_single.sh down` when done.

End-to-end (k1nd with teardown)
```
make bench-mem-e2e-k1nd-down LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
```
- Runs the same k1nd sequence and then stops the k1nd compose stack.

End-to-end (all suites, automated prep)
```
make bench-mem-e2e-all DURATION=30 REPLICAS=1,5,10 ROLL_REPLICAS=5
```
- Stops the dev/labs compose stacks, clones `specs/` into `state/bench-env/`, prunes every Deployment manifest except the requested one (defaults to `specs/examples/echo.yaml`), and launches a dedicated controller against that sandbox.
- Runs k1s rootful (snapshots elevated), k1s rootless, and k1nd in one shot, then backfills OCI labels, recombines, regenerates charts, and rebuilds docs. Only the snapshot helpers call `sudo`; the top-level `make` runs as your user.
- Defaults: `OCI_RUNTIME=crun`, `AE_ENGINE_STRICT=1`, `AE_ALLOW_PLAINTEXT_SECRETS=1`, `PRUNE_OLD=1`. Override `LABEL_ROOTFUL`, `LABEL_ROOTLESS`, or `LABEL_K1ND` to tag the suites differently.

Minimal smoke (rootful only)
```
make bench-mem-e2e-minimal DURATION=10 REPLICAS=1 ROLL_REPLICAS=2
```
- Uses the same prep/teardown scripts but only runs the k1s rootful matrix (single replica) plus a tiny rollout. Handy while iterating on manifests or Podman tuning before committing to the full matrix.

Backfill OCI runtime into past snapshots
```
make bench-mem-backfill-oci LABEL=r20251110+podman+rootless+cg2* REBUILD_DOCS=1
```
- Detects the OCI runtime per-snapshot by reading `raw/podman_inspect.json` (`.OCIRuntime`) or `raw/docker_inspect.json` (`HostConfig.Runtime`), then writes it to `meta.json` and injects `+<oci>+` into the label (e.g., `+crun+`) without renaming folders.
- Then recombines and regenerates charts. Add `OCI=runc` to force an override if needed. Use `GLOB='snapshots/r2025*/*'` to target explicit paths.

Backfill just the latest label
```
make bench-mem-backfill-oci-latest REBUILD_DOCS=1
```
- Picks the most recently modified label directory under `snapshots/` and runs the same backfill → recombine → charts (and docs) sequence. Pass `OCI=crun` to override detection if desired.

Automate a small matrix (k3s via k3d)
- Create cluster and expose ports 80/443 for Traefik:
```
make bench-k3s-up K3S_NAME=bench
```
- Run idle + scale-out snapshots using a simple echo Deployment/Service/Ingress:
```
make bench-mem-matrix-k3s LABEL_SUITE=baseline MANIFEST=specs/examples/k3s-echo.yaml REPLICAS=1,5,10 DURATION=30
```
- Combine across runs:
```
make bench-mem-combine GLOB='snapshots/*/*'
```
- Tear down cluster when finished:
```
make bench-k3s-down K3S_NAME=bench
```

End-to-end (k3s one-liner; cluster must be up)
```
make bench-mem-e2e-k3s LABEL_SUITE=baseline MANIFEST=specs/examples/k3s-echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
```

## Validated Full Clean Rerun

Use [Validated Procedures](validated-procedures.html#full-benchmark-rerun) for the exact full clean rerun command block and acceptance checks. That page is the published copy/paste source of truth for the validated split baseline + CRI flow.

For CRI reruns, use `scripts/bench/run_cri_verify.sh` instead of looping over
`make bench-mem-cri` manually. The wrapper:
- tears down `state/bench-cri` between runs and kills stale bench controllers
- writes a durable operator log under `state/bench-cri-rerun-*.log`
- pins the bench-local manifest to `runtimeClassName: runc`
- rejects `/k8s.io/kata` cgroup paths in the `pods-1` snapshot
- checks for `8` combined rows per run before and after finalization

Recommended smoke lane:

```bash
./scripts/bench/bench_env_teardown.sh --env state/bench-cri/env.sh || true
sudo pkill -f "python .*ae\\.controller.*state/bench-cri/specs" || true

export BASE="r$(date +%Y%m%d-%H%M)-cri-runc-wrapper-check"
RUNS="1" ./scripts/bench/run_cri_verify.sh
grep -c "^${BASE}-run1+cri+crun+containerd-" combined/combined.csv
```

Important: set `BASE=...` / `RUNS=...` before the script name (or `export`
them). `./scripts/bench/run_cri_verify.sh BASE=...` passes positional args and
does not override the wrapper environment.

Result interpretation
- Treat `combined/combined.csv` and `combined/combined.json` as the authoritative artifacts.
- If `bench-mem-finalize-sudo` prints `matplotlib not available`, the benchmark run is still valid; only chart regeneration was skipped.
- `Ctrl/CP` is scenario-aware:
  - k1s / k1nd: AE controller PSS
  - k3d: k3s control-plane PSS
- `HostCG` is the absolute host system cgroup sum, not a per-scenario delta.

## Automated Prep/Teardown

The helper scripts `scripts/bench/bench_env_prep.sh` and `scripts/bench/bench_env_teardown.sh` power both `make bench-mem-e2e-all` and `make bench-mem-e2e-minimal`:

- Prep copies `specs/` into `state/bench-env/specs/`, removes every Deployment manifest except the allowlist, and leaves shared configs/secrets intact so relative `configRefs` still work.
- A dedicated controller (log + pid under `state/bench-env/`) watches only that sandbox, so background demos or labs can’t steal ports or replicas.
- The scripts set `AE_SPECS_DIR`, `AE_STATE_DB`, `AE_CADDY_DIR`, and the primary manifest/app name in an env file; each benchmark stage sources it before calling `run_matrix.sh`/`run_rollout_k1s.sh`.
- Teardown stops the sandbox controller and deletes `state/bench-env/` unless you set `BENCH_KEEP_ENV=1` to inspect artifacts.
- If rootful Podman containers with the `ae.app` label are still running, prep lists them and asks whether to remove them (set `BENCH_AUTOCLEAN_PODMAN=1` to auto-approve in CI, or `=0` to refuse). This prevents stray demos from holding ports like 18080.
- Ingress writes are disabled by default for benches (`BENCH_DISABLE_INGRESS=1`). Set `BENCH_DISABLE_INGRESS=0` if a scenario actually needs Caddy updates; otherwise the sandbox exports `AE_DISABLE_INGRESS=1` and skips writing to `ops/dev/caddy/*`, avoiding permission chatter.

## Convenience Targets

- `make bench-mem-e2e-all` is still useful for a quick all-baselines sweep.
- `make bench-mem-e2e-minimal` is the fast rootful-only sanity lane while iterating on manifests or Podman tuning.
- For release-grade reruns, prefer the validated full clean sequence above because it includes:
  - explicit pre-teardown
  - baseline + CRI split
  - final artifact normalization
  - the k3d and CRI fixes validated in the 2026-04-13 rerun

<details>
<summary><strong>Troubleshooting: Rootful Podman readiness timeouts (host ports hang)</strong></summary>

Symptoms
- Bench runs stall at `wait_ready` with `ready=0` and probe messages like `startup http error ... 127.0.0.1:<port> timed out`.
- `podman run -p 18080:8080 ...` succeeds but `curl http://127.0.0.1:18080/` times out or returns `No route to host`.
- Inside-container HTTP works, but host->container or host->published port fails.

Quick diagnosis flow (rootful Podman)
1) Confirm the rootful API socket is alive:
```
sudo systemctl status podman.socket
sudo curl --unix-socket /run/podman/podman.sock http://d/_ping
```

2) Run a minimal echo test (rootful, host port publish):
```
sudo podman rm -f echo-test >/dev/null 2>&1 || true
sudo podman run -d --name echo-test -p 18080:8080 docker.io/mendhak/http-https-echo:37
```

3) Verify the app responds inside the container netns:
```
sudo podman run --rm --net container:echo-test docker.io/curlimages/curl:8.5.0 -fsS http://127.0.0.1:8080/
```
If this fails, the image or container itself is unhealthy (not a networking issue).

4) Verify host reachability (this is what readiness uses):
```
sudo curl -v --max-time 2 http://127.0.0.1:18080/
```

5) If the netns curl succeeds but host curl fails, check the bridge and routes:
```
ip link show podman0
ip addr show podman0
ip route show | rg 10.88
```
Look for:
- `podman0` up with `10.88.0.1/16`
- Stale route like `10.88.0.0/16 dev cni0 ... linkdown`

Fix: disable strict rp_filter + remove stale CNI route
```
sudo sysctl -w net.ipv4.conf.all.rp_filter=0
sudo sysctl -w net.ipv4.conf.default.rp_filter=0
sudo sysctl -w net.ipv4.conf.podman0.rp_filter=0
sudo ip route del 10.88.0.0/16 dev cni0 || true
```
Then re-test:
```
IP=$(sudo podman inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' echo-test)
sudo curl -v --max-time 2 http://$IP:8080/
sudo curl -v --max-time 2 http://127.0.0.1:18080/
```

If host access still fails, check forwarding/firewall policy:
```
sudo nft list ruleset | rg -n 'hook forward|podman|netavark'
sudo iptables -S FORWARD
```
Ensure `podman0` traffic is accepted. Example (iptables-nft):
```
sudo iptables -I FORWARD -i podman0 -j ACCEPT
sudo iptables -I FORWARD -o podman0 -j ACCEPT
```

Clean up after the test:
```
sudo podman rm -f echo-test
```
Then re-run the benchmark.

Persist the fix (optional)
1) Keep rp_filter relaxed for Podman:
```
echo -e "net.ipv4.conf.all.rp_filter=0\nnet.ipv4.conf.default.rp_filter=0\nnet.ipv4.conf.podman0.rp_filter=0" | sudo tee /etc/sysctl.d/99-podman.conf
sudo sysctl --system
```
Note: Some distros apply other sysctl profiles later. Verify with:
```
sudo sysctl net.ipv4.conf.all.rp_filter net.ipv4.conf.default.rp_filter net.ipv4.conf.podman0.rp_filter
```

2) Prevent subnet conflicts with CNI (if you use containerd/k3s):
- Avoid sharing `10.88.0.0/16` between `podman0` and `cni0`.
- Option A (Podman): recreate the default podman network on a different subnet:
```
sudo podman network rm podman
sudo podman network create --driver bridge --subnet 10.89.0.0/16 --gateway 10.89.0.1 podman
```
- Option B (CNI): adjust the CNI bridge subnet in your CNI config so it does not overlap Podman.

</details>

<details>
<summary><strong>Troubleshooting: CRI readiness timeouts (pod IP unreachable)</strong></summary>

Symptoms
- CRI bench stalls at `wait_ready` with `ready=0` even though pods exist.
- `crictl pods` shows Ready pods, but readiness probes keep timing out.
- `curl http://<pod-ip>:8080/` from the host times out.

Why this happens
- In the CRI backend, readiness probes target the pod IP (`pod_ip:container_port`).
- If the host no longer routes `10.88.0.0/16` to `cni0`, probes go out the default gateway and time out.
- Strict reverse-path filtering (`rp_filter=2`) on `cni0` can drop replies even if the route is present.

Quick diagnosis
1) Get a pod IP and confirm how the host routes to it:
```
POD_ID=$(crictl --runtime-endpoint unix:///run/containerd/containerd.sock pods -q --name echo | head -n1)
POD_IP=$(crictl --runtime-endpoint unix:///run/containerd/containerd.sock inspectp "$POD_ID" -o json \
  | python -c 'import json,sys; print(json.load(sys.stdin)["status"]["network"]["ip"])')
ip route get "$POD_IP"
```
Expected: route should go via `cni0`, not `wlan0` or another uplink.

2) Verify the pod responds from the host:
```
curl -v --max-time 2 "http://$POD_IP:8080/"
```

Fix: restore route + relax rp_filter
```
sudo ip route replace 10.88.0.0/16 dev cni0
sudo sysctl -w net.ipv4.conf.cni0.rp_filter=0
```
Re-test:
```
ip route get "$POD_IP"
curl -v --max-time 2 "http://$POD_IP:8080/"
```

If it still fails
- Confirm `cni0` is up and has `10.88.0.1/16`:
```
ip link show cni0
ip addr show cni0
```
- Check for overlapping subnets (Podman default is also `10.88.0.0/16`):
```
ip route show | rg 10.88
```
If both `cni0` and `podman0` claim `10.88.0.0/16`, pick one to move.

Persist the fix (optional)
1) Keep rp_filter relaxed for the CNI bridge:
```
echo "net.ipv4.conf.cni0.rp_filter=0" | sudo tee /etc/sysctl.d/99-cni.conf
sudo sysctl --system
```
2) Avoid Podman/CNI subnet collisions:
- Option A (Podman): recreate the Podman bridge on a different subnet (e.g. `10.89.0.0/16`).
- Option B (CNI): edit the CNI config to use a non-overlapping subnet.

</details>
