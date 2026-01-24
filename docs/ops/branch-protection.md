# Branch Protection Checklist (apishim-live-openapi)

Use this checklist to ensure the live OpenAPI gate is required on `main`.

## Required checks
1. Open repository settings → Branch protection → `main`.
2. Enable “Require status checks to pass before merging.”
3. Add these required checks:
   - `apishim-live-openapi / live-openapi (target=local, runtime=stub)`
   - `apishim-live-openapi / live-openapi (target=local, runtime=docker)`
4. Optional (only if external kubeconfig/kind is configured and always available):
   - `apishim-live-openapi / live-openapi (target=external, runtime=stub)`
5. Save the rule and verify a new PR shows the required checks.

Notes:
- Do not require the external check unless `APISHIM_LIVE_KUBECONFIG_B64` or `APISHIM_KIND_CLUSTER`
  is set for the repo/org; skipped checks cannot satisfy required status rules.
- The release tag workflow also runs the live gate; it should fail if the gate fails.
