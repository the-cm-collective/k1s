GitHub/Gitea Actions: CI layout

The repository now uses five canonical workflows:

- `ci-core.yml`: blocking pull request and branch gate for unit tests, planner validation, Kubernetes export checks, OpenAPI drift validation, and versioning checks. The workflow always reports branch-protection checks, but skips the heavy jobs when a change set is docs-only.
- `ci-docs.yml`: docs gate for `README`, `docs/**`, docs build helpers, and docs tests. The workflow always reports its check, but only runs `make docs-verify` when docs-surface files changed.
- `nightly-apishim.yml`: manual/nightly API-shim coverage for shim smoke, Helm, RBAC/SSA, exec, port-forward, SPDY, and live OpenAPI checks.
- `nightly-runtime.yml`: manual/nightly runtime and end-to-end coverage for Podman, CRI, kind/k3s conformance, multinode, service port-forward, `/plan`, core/edge, and HA closeout lanes.
- `release.yml`: tag-only artifact pipeline for wheels, controller image tarball, OpenAPI snapshots, and the API-shim compatibility matrix.

Runner compatibility follows ADR 0008:

- `actions/checkout@v3`
- `actions/setup-python@v4`
- repo-local shell helpers in `scripts/ci/lib.sh`

PRs intentionally use a lean blocking gate. Heavy runtime and E2E lanes run outside the default PR path because they are slower, more environment-sensitive, and more appropriate for scheduled/manual validation.

Representative commands behind the workflows:

```bash
pytest --maxfail=1 --disable-warnings -q
tools/plan_ci.sh
make k8s-smoke
scripts/validate-openapi.sh
python scripts/check_versioning.py
make docs-verify
```

`ruff check` is intentionally not a blocking repo-wide CI gate yet. The current repository still carries substantial pre-existing Ruff debt in tests and helper scripts, so the blocking path stays aligned with the existing passing baseline instead of introducing a mass cleanup requirement into unrelated PRs.

Representative manual/nightly commands:

```bash
sudo -E ./scripts/cri_ci_setup.sh
bash scripts/ci/k8s-conformance.sh
bash scripts/ci/k3s-conformance.sh
pytest tests/integration/test_multinode_agent_flow.py \
  tests/integration/test_overlay_vip.py \
  tests/integration/test_apishim_persistence.py -q
bash scripts/e2e/plan_validate.sh
python scripts/e2e_k1s_core_edge.py
make ha-closeout-e2e
```
