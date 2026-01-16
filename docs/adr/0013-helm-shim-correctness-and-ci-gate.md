# ADR 0013 - Helm shim correctness and CI gate

Date: 2026-01-16
Status: Accepted
Owners: apishim/ci/docs

## Context
- Helm release flows depend on list/watch selector filtering, release storage, and status semantics.
- The shim demo previously relied on `helm template`, which bypassed Helm's release records and hooks.
- Service target resolution used partial selectors, causing misrouting in charts with multiple labels.
- Workload emulation (Jobs/CronJobs/StatefulSets/DaemonSets) needed explicit documentation to avoid false expectations.
- CI had no live Helm install/upgrade/uninstall smoke to catch regressions.

## Decision
- Use real Helm install/upgrade/uninstall flow for the shim demo and CI gate.
- Apply label/field selector filtering to list/watch for core resources used by Helm.
- Resolve Service targets by matching the full selector against workload pod template labels, preferring exact matches.
- Document the emulated behavior and limitations for Jobs, CronJobs, StatefulSets, and DaemonSets.
- Add a Helm smoke gate in CI that runs the shim demo flow against a local apishim.

## Consequences
- Helm release records and status queries return accurate subsets.
- Charts that use multi-label selectors map Services/Ingress to the intended app.
- Users get a clear boundary for workload semantics in the shim.
- CI detects regressions in Helm install/upgrade/uninstall behavior early.

## References
- `scripts/helm_shim_demo.sh`
- `scripts/ci/helm-shim-smoke.sh`
- `.github/workflows/helm-shim-smoke.yml`
- `docs/guides/helm-shim.md`
- `docs/reference/apishim-compatibility-matrix.md`
