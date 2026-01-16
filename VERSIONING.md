# Versioning

We follow SemVer intent with PEP 440-compliant version strings so Python tooling
recognizes pre-releases correctly.

## Pre-release cadence

- Alpha: `0.1.0a1`, `0.1.0a2`, ...
- Beta: `0.1.0b1`, `0.1.0b2`, ...
- Release candidate: `0.1.0rc1`, `0.1.0rc2`, ...
- Final: `0.1.0`

## Tagging

- Git tags match the package version, prefixed with `v` (example: `v0.1.0a1`).
- Tag the commit that lands the release in `main`.
- Use annotated tags; if GPG is configured, prefer signed tags.
- Example commands:

```bash
git tag -a v0.1.0a1 -m "v0.1.0a1"
git push origin v0.1.0a1
```

- Example (signed tag):

```bash
git tag -s v0.1.0a1 -m "v0.1.0a1"
git push origin v0.1.0a1
```

## Post-release bump

- Immediately after tagging, bump `pyproject.toml` to the next dev version
  (example: `0.1.0a2.dev0`) so new work is clearly ahead of the tag.

## Changelog flow

- Keep an `Unreleased (YYYY-MM-DD)` section at the top of `CHANGELOG.md`.
- When releasing, move its entries under a new section like
  `0.1.0a1 - 2026-01-16`, then create a fresh empty Unreleased section.

## Automation

- `scripts/check_versioning.py` enforces PEP 440 formatting, changelog parity, and
  tag/version matching in CI and pre-commit.
- `scripts/bump_version.py --dry-run` previews the next `.dev0` version; run it
  without `--dry-run` after tagging to bump `pyproject.toml`.

## Current status

- `0.1.0a1` (2026-01-16) is the first official alpha release.
