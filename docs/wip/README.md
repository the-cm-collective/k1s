# Work in Progress

This directory is only for short-lived engineering trackers that are still changing rapidly. Stable references belong in `docs/reference/`, architecture direction belongs in `docs/design/`, and formal milestone tracking belongs in `docs/roadmap/`.

## Rules

- Add a clear owner area and intended destination before treating a file as active.
- Keep only live engineering trackers here.
- Move or delete completed items instead of leaving stale notes behind.
- Use git history for retired drafts instead of building an archive pile inside `docs/wip/`.

## Active Inventory

| File | Status | Owner area | Last reviewed | Target destination |
| --- | --- | --- | --- | --- |
| `cri-parity.md` | Active gap tracker | runtime/controller/ops | 2026-03-11 | stay in `docs/wip/` until CRI default-readiness work is complete |
| `csi.md` | Active integration plan | storage/controller | 2026-03-11 | `docs/design/` when CSI contract stabilizes |
| `storage-parity.md` | Active architecture tracker | storage/apishim/export | 2026-03-11 | `docs/design/` or `docs/reference/` after parity scope is reduced to committed work |
| `sonobuoy.md` | Active validation harness plan | ops/testing | 2026-03-11 | `docs/ops/runbook.md` plus tooling docs once the harness exists |

## Promoted Recently

- API shim roadmap moved to `docs/reference/apishim-roadmap.md`
- Site-to-site CSI design moved to `docs/design/site-to-site-csi-storage.md`
- API shim source-of-truth design moved to `docs/design/apishim-source-of-truth.md`
