#!/usr/bin/env bash
set -euo pipefail

git ls-files -z docs/site | xargs -0 git update-index --no-skip-worktree --
echo "Removed skip-worktree from docs/site (local only)."
