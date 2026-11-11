# Versioning

We follow SemVer intent with PEP 440-compliant version strings so Python tooling
recognizes pre-releases correctly.

## Pre-release cadence

- Alpha: `0.1.0a1`, `0.1.0a2`, ...
- Beta: `0.1.0b1`, `0.1.0b2`, ...
- Release candidate: `0.1.0rc1`, `0.1.0rc2`, ...
- Final: `0.1.0`

## Tagging

- Git tags match the package version, prefixed with `v` (example: `v0.1.0`).
- Tag the commit that lands the release in `main`.
- Use annotated tags; if GPG is configured, prefer signed tags.
- Example commands:

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

- Example (signed tag):

```bash
git tag -s v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

## Post-release bump

- Immediately after tagging, bump the authoritative `ae.__version__` literal to
  the next dev version (example: `0.1.0a2.dev0`) so new work is clearly ahead of
  the tag. `pyproject.toml` resolves that literal through setuptools dynamic
  metadata; it must not contain a second version literal.

## Changelog flow

- Keep an `Unreleased (YYYY-MM-DD)` section at the top of `CHANGELOG.md`.
- When releasing, move its entries under a new section like
  `0.1.0 - 2026-01-18`, then create a fresh empty Unreleased section.

## Automation

- `src/ae/__init__.py` is the single authoritative version source. Packaging,
  compatibility CLIs, OpenAPI documents, and release checks resolve it without
  maintaining independent version literals.
- `scripts/check_versioning.py` enforces the dynamic metadata contract, PEP 440
  formatting, changelog parity, and tag/version matching in CI and pre-commit.
- `scripts/bump_version.py --dry-run` previews the next `.dev0` version; run it
  without `--dry-run` after tagging to update the authoritative package literal.

## Current status

- `0.1.7` (2026-08-27) is the latest stable release.
- `0.1.8a1` changed the distribution name to `k1s-core` while retaining the
  `ae` Python import and compatibility commands.
- `1.0.0a1` established the documented 1.0-alpha compatibility contract but
  stopped at the GitHub TestPyPI build gate before any package upload.
- `1.0.0a2` retained that boundary and added reproducible, digest-bound
  synthesis for a source-only dependency. Its approved TestPyPI job stopped
  before OIDC upload when a shell glob supplied three subjects to a
  one-subject attestation command.
- `1.0.0a3` is the current candidate. It verifies all seven exact publication
  subjects with one GitHub CLI invocation per subject in both index workflows.
  Stable `1.0.0` remains gated by exact-candidate publication, WorkerBee
  integration, combined qualification, and RC review.
- The WorkerBee profile evidence adapter is an experimental v1 wire contract
  within `k1s-core`; its `v1` schema identifier does not declare K1s 1.0 API
  stability. Unknown fields/versions fail closed. The K1 review at
  `c9178a5235b695784ed839f31901d476afdd6a98` qualifies the exact wire and pure
  same-runtime projection against the locked W-P0-1 consumer design. Live MCP
  acquisition/channel binding, provider-process identity, retained W4 evidence,
  and combined K1s/WorkerBee disposable-guest qualification remain later
  release gates.
- `review/v1.0.0rc1-prep-20261110` and the annotated
  `review/k1s-1.0.0rc1-prep-20261110` marker identify a non-release review point
  only. They are not `v1.0.0rc1`, do not change the package version, and confer
  no publication or promotion authority.

## 1.0 alpha compatibility contract

`1.0.0a1` establishes the compatibility boundary documented in
`docs/reference/public-contracts.md`. Distribution identity, the `ae` import,
the installed command inventory, the canonical profile catalog and pure
inspection commands, single-source version metadata, and packaged CRI protobuf
resources are release-gated surfaces.

Alpha and beta releases may still correct a contract before final `1.0.0`, but
an incompatible change to a stable surface requires an explicit changelog
entry, migration guidance, and contract-test updates. Experimental surfaces
remain outside that guarantee and must identify their posture in public output.
The alpha version does not claim broad production readiness, complete
Kubernetes compatibility, or implicit permission to start or prepare a host
runtime.
