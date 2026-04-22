# Branch Protection Checklist

Use this checklist to ensure the lean blocking gates are required on `main`.

## Required checks
1. Open repository settings → Branch protection → `main`.
2. Enable “Require status checks to pass before merging.”
3. Add these required checks:
   - `ci-core / unit-and-lint`
   - `ci-core / planner-and-k8s-export`
   - `ci-core / versioning`
   - `ci-docs / docs-verify`
4. Save the rule and verify a new PR shows the required checks.

Notes:
- `ci-core` always reports the required checks, but skips the heavy jobs automatically on docs-only changes.
- `ci-docs` always reports the `docs-verify` check, but only runs `make docs-verify` when README/docs surfaces changed.
- Nightly/manual workflows such as `nightly-apishim.yml` and `nightly-runtime.yml` should not be marked required unless the repo has stable dedicated runners for those lanes.
