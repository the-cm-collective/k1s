# Sonobuoy Harness (WIP)

Status: active WIP tracker
Owner area: ops/testing
Intended destination: `docs/ops/runbook.md` plus tooling docs once the harness is implemented

This document defines the build and implementation plan for a Sonobuoy-based
conformance harness for k1s. The goal is to produce a repeatable baseline,
track deltas over time, and evolve toward a curated “conformance-lite” profile
aligned with k1s scope.

Status: WIP (planning and initial implementation).

## Goals

- Establish a **baseline failure list** for k1s using Sonobuoy.
- Create a **conformance-lite** profile that matches k1s scope and documented
  non-goals.
- Produce **actionable artifacts** (results, logs, fail list) for operators and
  developers.
- Keep the harness **repeatable** and **non-destructive** for lab usage.

## Non-goals (initial)

- Passing upstream Kubernetes conformance.
- Enforcing Sonobuoy as a release gate immediately.
- Testing with stub runtimes; a real runtime is required for meaningful signal.

## Scope alignment

Use this harness to validate the “compatibility subset” described in:
- `CONFORMANCE.md`
- `docs/reference/apishim-compatibility-matrix.md`
- `docs/reference/k8s-compliance.md`

Known gaps (initially excluded or expected to fail):
- PodSecurity admission / webhooks
- NetworkPolicy enforcement (depends on CNI)
- PV/PVC/CSI semantics (runtime storage parity)
- metrics.k8s.io / aggregated APIs
- kubelet-style Node/Lease behavior

## Prerequisites

- Real runtime (Podman/Docker), not stub.
- Working multi-node lab or single-node setup with agents.
- Optional: policy-capable CNI if NetworkPolicy tests are included.
- Optional: metrics-server if metrics tests are included.

## Harness architecture

Components:
- **Sonobuoy CLI** for orchestration.
- **Plugin profiles** for:
  - baseline (run and collect full results)
  - conformance-lite (curated allowlist/skiplist)
- **Results collector**: extracts summaries and generates a short “fail list”.
- **Artifacts**: raw results tarball + parsed summary.

Suggested locations:
- `tools/sonobuoy/` for scripts + configs
- `docs/wip/sonobuoy.md` for WIP plan (this doc)
- `docs/ops/runbook.md` for the finalized operator runbook steps

## Implementation plan

### Phase 1: Local baseline runner

Deliverables:
- `tools/sonobuoy/run-baseline.sh`
  - Runs Sonobuoy against the current kubeconfig
  - Stores results tarball + summary under `artifacts/sonobuoy/`
- `tools/sonobuoy/parse-results.py`
  - Extracts failed tests and durations
  - Emits `artifacts/sonobuoy/failures.json` and a short markdown summary

Notes:
- Initial baseline is expected to fail extensively; we need the fail list.
- Keep it “best effort” so it doesn’t block local dev.

### Phase 2: Conformance-lite profile

Deliverables:
- `tools/sonobuoy/profiles/k1s-lite.yaml`
  - Starts from upstream conformance profile
  - Applies skiplist for known gaps
  - Optionally includes a label/regex allowlist for “in-scope” tests
- `tools/sonobuoy/skiplist.yaml`
  - Explicit and versioned skip rules (reason + link to issue/doc)

Behavior:
- Produce a pass/fail summary for only the in-scope tests.
- Keep the skiplist small and auditable.

### Phase 3: CI integration (non-gating)

Deliverables:
- `.github/workflows/sonobuoy-baseline.yml`
  - Runs nightly or on demand
  - Uploads artifacts and summary
  - Does **not** block merges initially

Promotion criteria to gating:
- Core workloads + networking tests reliably pass in labs
- Known gaps are captured in skiplist
- Flake rate acceptable (<5% over N runs)

## Harness usage (initial draft)

Baseline run:
```
tools/sonobuoy/run-baseline.sh
```

Conformance-lite run:
```
tools/sonobuoy/run-lite.sh
```

Parse results:
```
tools/sonobuoy/parse-results.py artifacts/sonobuoy/results.tar.gz
```

## Output artifacts

Minimum artifacts per run:
- `artifacts/sonobuoy/results.tar.gz` (raw Sonobuoy output)
- `artifacts/sonobuoy/summary.json` (pass/fail counts + durations)
- `artifacts/sonobuoy/failures.json` (test cases + reasons/links)
- `artifacts/sonobuoy/summary.md` (human summary)

## Skiplist governance

Rules:
- Every skip must include a **reason** and **link** to an issue or doc.
- Re-evaluate skips monthly or after major runtime/shim milestones.
- Prefer “allowlist” for conformance-lite when practical.

## Risks and mitigations

- **Flaky tests**: isolate by label/regex; pin cluster versions; include retries.
- **NetworkPolicy variance**: document required CNI; run a “policy-on” lane if
  needed.
- **Storage tests**: keep out of lite profile until storage parity is in place.
- **Metrics tests**: exclude until metrics-server support is formalized.

## Open questions

- Which Kubernetes minor versions should we target in CI?
- Do we want separate lanes for shim-only vs runtime-backed tests?
- Should we run Sonobuoy against a local kind cluster as a control?

## Next steps

- Create `tools/sonobuoy/` with baseline runner + parser.
- Draft the conformance-lite profile and skiplist.
- Add a CI workflow (non-gating) to publish artifacts.
- Once stable, move this doc into `docs/adr/` or `docs/ops/`.
