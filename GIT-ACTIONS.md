# Gitea Actions notes (updated Jan 15, 2026)

## Runner requirements
- GitHub first-party actions now require Node 20 at the runner level. The current Gitea runner image is Node 16, so we temporarily pinned `actions/checkout` to v3 and `actions/setup-python` to v4 across the push-triggered workflows. Upgrade the runner image (or bake Node 20 globally) so we can move back to v4/v5 and avoid future breaks.

## Fixes applied (Jan 15, 2026)
- Workflows updated: `ci.yml`, `apishim-smoke.yml`, `apishim-ssa-rbac.yml`, `helm-shim-smoke.yml`, `helm-dryrun-openapi.yml`, `kubectl-portforward-smoke.yml`.
- Action versions: `checkout@v3`, `setup-python@v4` to stay Node16-compatible until the runner is rebuilt.
- Added a shared concurrency group `port-8445-${{ github.ref }}` to the apishim/helm/portforward workflows to avoid 127.0.0.1:8445 port collisions during parallel pushes.
- CLI installs (kubectl/helm) now go to `$HOME/.local/bin` and are added to PATH, removing the need for sudo/root on the runner.

## Other workflow assessments (unchanged files)
- Docker/privileged needed: `kubectl-exec-smoke.yml`, `e2e-multiport.yml`, `multinode-smoke.yml`, `release.yml` (container job). Ensure runner has Docker socket or is privileged.
- Cache unsupported: `actions/cache@v4` remains disabled/not used; keep it that way until a cache service is configured.
- Live cluster gating: `apishim-live-openapi.yml` expects a base64 kubeconfig. A `KUBECONFIG_B64` secret exists and can be wired to `APISHIM_LIVE_KUBECONFIG_B64` if we want that job green.
- Port usage: `kubectl-exec-smoke.yml` binds 8446; add a concurrency group if parallel runs begin to clash.

## After runner upgrades to Node 20
1) Bump `actions/checkout` back to v4 and `actions/setup-python` to v5 in all workflows.
2) Optionally remove the temporary Node16 note here and keep the user-bin installs (they remain safer for non-root runners).
