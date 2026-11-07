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
- `0.1.8a1` is the next pre-1.0 candidate. It changes the installable
  distribution name to `k1s-core` while retaining the `ae` Python import and
  compatibility commands. A `1.0.0a1` version remains deferred until the
  project documents and validates a 1.0 compatibility contract.
