#!/usr/bin/env bash
set -euo pipefail

# CI gating: run planner in strict JSON mode over example specs.
# Uses stub runtime to avoid requiring Docker/Podman in CI.

export AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND:-stub}

shopt -s nullglob
files=(specs/examples/*.yaml specs/examples/*.yml)
shopt -u nullglob

if [[ ${#files[@]} -eq 0 ]]; then
  echo "no example specs found; skipping planner gating"
  exit 0
fi

rc=0
for f in "${files[@]}"; do
  echo "[plan-ci] checking $f"
  if ! python -m ae.cli plan --json --strict -f "$f"; then
    echo "[plan-ci] planner reported issues for $f"
    rc=1
  fi
done

exit $rc

