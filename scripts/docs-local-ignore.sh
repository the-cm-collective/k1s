#!/usr/bin/env bash
set -euo pipefail

git ls-files -z docs/site | xargs -0 git update-index --skip-worktree --
echo "Marked docs/site as skip-worktree (local only)."
