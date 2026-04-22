# Known-Good Validation Snapshot (2026-04-22)

This page tracks the latest validated operator workflows. It is a status index, not the full procedure source of truth.

Published command reference
- Use [Validated Procedures](validated-procedures.html) for the exact copy/paste command sequences behind this snapshot.

Validated workflows
- Full clean memory benchmark rerun passed:
  - k1s rootless
  - k1s rootful
  - k1nd
  - k3d
  - CRI/containerd
  - Current procedure and acceptance checks: [Memory Overhead Benchmarks](benchmarks.html)
- HA VM harness validation passed:
  - retained stage-1 flow on `ha-control-plane-attached-node`
  - live stage-2 helper on `ha-control-plane-core`
  - Current procedure and cleanup flow: [HA Cluster Bring-Up](ha-cluster-bring-up.html)
- User-facing docs/dashboard checks passed:
  - simple dashboard profile on `:8443`
  - advanced dashboard profile on `:10443`
  - Current layout expectations: [Demos & Examples](examples.html)

Current dashboard expectations
- Simple layout:
  - local single-controller demo/dev flows such as `make demo`
  - shared `System Graph` visible
  - `HA Control Plane` section hidden
  - `HA Members` legend key hidden
- Advanced layout:
  - HA/core/edge/site-aware flows
  - shared `System Graph` visible
  - `HA Control Plane` section visible

Current benchmark expectations
- The authoritative benchmark artifacts are `combined/combined.csv` and `combined/combined.json`.
- The current published retained set was rebuilt with:
  - `make bench-retained-rebuild PROFILE=final STAMP=r20260421-223436-authoritative2 DELETE_DROPPED=1`
- The retained publish set contains `110` rows:
  - `40` frozen `r20260203-legacy*` rows
  - `70` current rows from `r20260421-223436-authoritative2*`
- Published current families normalize to OCI-tagged names:
  - `r20260421-223436-authoritative2+podman+crun+rootless+cg2`
  - `r20260421-223436-authoritative2+podman+crun+priv+cg2`
  - `r20260421-223436-authoritative2+docker+runc+k1nd`
  - `r20260421-223436-authoritative2+k3d+runc`
  - `r20260421-223436-authoritative2+cri-runc-verify-run{1,2,3}+cri+containerd`
- Rollout reporting is dual-published:
  - `rollout-*-during`
  - `rollout-*-during-warm`
  - `rollout-*-post`
- Ranking and top-line comparisons exclude `*-during-warm` from stage weighting.
- `Ctrl/CP` in the summary table is scenario-aware:
  - k1s / k1nd: AE controller PSS
  - k3d: k3s control-plane PSS
- Missing `matplotlib` only blocks chart regeneration; it does not invalidate a completed run.

Current HA surface expectations
- Retained/operator-facing HA URLs:
  - `https://dash.home.arpa:10443/dashboard`
  - `https://docs.home.arpa:10443/`
  - `https://api.home.arpa:10443/swagger`
- Local/demo URLs:
  - `https://dash.home.arpa:8443/dashboard`
  - `https://docs.home.arpa:8443/`

Use the linked docs above for the exact commands and acceptance checks.
