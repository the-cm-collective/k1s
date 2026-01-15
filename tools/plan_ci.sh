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
  base=$(basename "$f")
  # Skip multi-doc and non-app examples that the planner can't parse in JSON mode.
  case "$base" in
    *secret*.yaml|*secret*.yml|*k8s*.yaml|*k8s*.yml|*rollout*.yaml|*rollout*.yml)
      echo "[plan-ci] skipping $f (not a single-doc app manifest)"
      continue
      ;;
    *k3s*.yaml|*k3s*.yml)
      echo "[plan-ci] skipping $f (k3s multi-doc example)"
      continue
      ;;
  esac
  echo "[plan-ci] checking $f"
  if ! python -m ae.cli plan --json -f "$f"; then
    echo "[plan-ci] planner reported issues for $f"
    rc=1
  fi
done

exit $rc
