#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

log() { echo "[cri-verify] $*"; }
trap 'rc=$?; log "error at line $LINENO: $BASH_COMMAND (exit=$rc)"; exit $rc' ERR

count_requested_replicas() {
  local raw="$1"
  local count=0
  local item=""
  raw="${raw//,/ }"
  for item in $raw; do
    item="${item// /}"
    [[ -z "$item" ]] && continue
    if [[ ! "$item" =~ ^[0-9]+$ ]]; then
      return 1
    fi
    ((count += 1))
  done
  printf '%s\n' "$count"
}

first_requested_replica() {
  local raw="$1"
  local item=""
  raw="${raw//,/ }"
  for item in $raw; do
    item="${item// /}"
    [[ -z "$item" ]] && continue
    if [[ ! "$item" =~ ^[0-9]+$ ]]; then
      return 1
    fi
    printf '%s\n' "$item"
    return 0
  done
  return 1
}

count_combined_rows() {
  local label="$1"
  python - "$label" <<'PY'
from __future__ import annotations

import csv
import sys
from pathlib import Path

label = sys.argv[1]
path = Path("combined/combined.csv")
if not path.exists():
    print(0)
    raise SystemExit(0)

base, sep, engine = label.rpartition("+")
count = 0

with path.open(newline="", encoding="utf-8") as handle:
    for row in csv.DictReader(handle):
        current = str(row.get("label", ""))
        if current.startswith(label + "-"):
            count += 1
            continue
        oci = str(row.get("oci_runtime", "")).strip()
        if sep and oci and current.startswith(f"{base}+{oci}+{engine}-"):
            count += 1

print(count)
PY
}

timestamp=$(date +%Y%m%d-%H%M%S)
log_file="${CRI_VERIFY_LOG_FILE:-state/bench-cri-rerun-${timestamp}.log}"
mkdir -p "$(dirname "$log_file")"
exec > >(tee -a "$log_file") 2>&1
trap 'rc=$?; log "exit=$rc log=$log_file"' EXIT

if [[ ! -f .venv/bin/activate ]]; then
  log "missing .venv/bin/activate"
  exit 2
fi

# shellcheck disable=SC1091
source .venv/bin/activate
export PATH="$PWD/.venv/bin:$PATH"
export AE_USE_REGISTRY_CACHE="${AE_USE_REGISTRY_CACHE:-0}"

BASE="${BASE:-r$(date +%Y%m%d-%H%M)-cri-runc-verify}"
RUNS_RAW="${RUNS:-1 2 3}"
PURGE_EXISTING_RUNS="${PURGE_EXISTING_RUNS:-0}"
APP="${APP:-specs/examples/echo.yaml}"
APP_NAME="${APP_NAME:-echo}"
DURATION="${DURATION:-30}"
REPLICAS="${REPLICAS:-1,5,10}"
ROLL_REPLICAS="${ROLL_REPLICAS:-2,5}"

RUNS_RAW="${RUNS_RAW//,/ }"
read -r -a runs <<<"$RUNS_RAW"
if (( ${#runs[@]} == 0 )); then
  log "no runs requested (RUNS='${RUNS_RAW}')"
  exit 2
fi
for run in "${runs[@]}"; do
  if [[ ! "$run" =~ ^[0-9]+$ ]]; then
    log "invalid run id '${run}'"
    exit 2
  fi
done

first_steady_replica="$(first_requested_replica "$REPLICAS")" || {
  log "invalid or empty REPLICAS='${REPLICAS}'"
  exit 2
}
steady_count="$(count_requested_replicas "$REPLICAS")" || {
  log "invalid REPLICAS='${REPLICAS}'"
  exit 2
}
rollout_count="$(count_requested_replicas "$ROLL_REPLICAS")" || {
  log "invalid ROLL_REPLICAS='${ROLL_REPLICAS}'"
  exit 2
}
expected_rows=$((1 + steady_count + (3 * rollout_count)))

sudo -v
sudo make bench-fix-perms

for run in "${runs[@]}"; do
  label="${BASE}-run${run}+cri+containerd"

  if [[ "$PURGE_EXISTING_RUNS" == "1" ]]; then
    find snapshots -maxdepth 1 -type d \
      -name "${label}-*" \
      -exec sudo rm -rf -- {} +
  fi

  ./scripts/bench/bench_env_teardown.sh --env state/bench-cri/env.sh || true
  sudo pkill -f "python .*ae\\.controller.*state/bench-cri/specs" || true

  LABEL_CRI="$label" \
  APP="$APP" \
  APP_NAME="$APP_NAME" \
  DURATION="$DURATION" \
  REPLICAS="$REPLICAS" \
  ROLL_REPLICAS="$ROLL_REPLICAS" \
  make bench-mem-cri

  latest_csv="$(find "snapshots/${label}-pods-${first_steady_replica}" -type f -path '*/raw/containers_mem.csv' | sort | tail -n1)"
  test -n "$latest_csv"
  ! rg -n '/k8s.io/kata' "$latest_csv"
  rows="$(count_combined_rows "$label")"
  test "$rows" -eq "$expected_rows"
done

sudo make bench-fix-perms
sudo -E env PATH="$PATH" make bench-mem-finalize-sudo
make docs-verify

for run in "${runs[@]}"; do
  label="${BASE}-run${run}+cri+containerd"
  log "rows ${label}: $(count_combined_rows "$label")"
done
