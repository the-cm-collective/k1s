# Open Items and Follow-ups

This file tracks follow-up tasks and references for recent k3s-focused work and exporter hints.

## Closed (Q4 2025)
- PDB percent on CLI (2025-11-12)
- RBAC emitters tied to ServiceAccount (Role/RoleBinding) (2025-11-12)
- TLS Secret generator (`ae tls kubesecret`) (2025-11-12)
- Batch exporters: Job/CronJob (2025-11-12)
- emptyDir support (2025-11-12)
- PodSecurity Namespace labels preset (`--emit-namespace --psa-enforce`) (2025-11-12)
- NetworkPolicy provider notes for k3s (2025-11-12)

## Exporter hints (new)
- `exportHints.emitPDB`: Asks the exporter to emit a PodDisruptionBudget when `replicas > 1`. The `k8s-check` tool respects this hint and does not warn about a missing PDB.
- `exportHints.suppressImageMultiArchWarning`: Suppresses the informational "IMAGE_MULTI_ARCH_UNKNOWN" warning in `k8s-check` when you know the image is multi-arch.

## Follow-ups
- Consider adding a `--emit-namespace`/`--psa-enforce` preset in `ae k1s` examples to demonstrate PSA out-of-the-box.
- Evaluate adding a `--np-preset db` with a parametrized list of ports for common stateful services.
- Optional: add a `--suppress-image-arch-warn` flag to `k8s-check` (CLI-level) mirroring the manifest hint.
- Optional: provide a published demo image with multi-arch tags to replace the local `demo-blue:latest` reference in examples.

