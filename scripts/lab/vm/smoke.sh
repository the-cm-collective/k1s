#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${LAB_VM_SMOKE_V2:-0}" == "1" ]]; then
  exec "$SCRIPT_DIR/smoke_v2.py" "$@"
fi

# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
RUN_ID="$(resolve_run_id)"
SKIP_UP=0
SKIP_BOOTSTRAP=0
SKIP_VALIDATE=0
DOWN_ON_EXIT=0
DOWN_PURGE=0
DOWN_DESTROY_NETWORK=0

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>] [options]

Runs a single-command VM smoke flow:
  1) variant up
  2) bootstrap --execute
  3) variant validate

Options:
  --skip-up               Skip VM bring-up
  --skip-bootstrap        Skip bootstrap execution
  --skip-validate         Skip validation
  --down                  Run variant down on exit (success or failure)
  --purge                 With --down, also purge state dir
  --destroy-network       With --down, also remove bridge/NAT rules
  -h, --help              Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --skip-up) SKIP_UP=1; shift ;;
    --skip-bootstrap) SKIP_BOOTSTRAP=1; shift ;;
    --skip-validate) SKIP_VALIDATE=1; shift ;;
    --down) DOWN_ON_EXIT=1; shift ;;
    --purge) DOWN_PURGE=1; shift ;;
    --destroy-network) DOWN_DESTROY_NETWORK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { err "--variant is required"; usage; exit 2; }
[[ -f "$VARIANT" ]] || { err "variant not found: $VARIANT"; exit 2; }
require_cmd jq

if [[ "$SKIP_UP" -eq 0 || "$DOWN_ON_EXIT" -eq 1 ]]; then
  require_cmd sudo
  if ! sudo -n true >/dev/null 2>&1; then
    err "local sudo credentials are required; run 'sudo -v' and retry"
    exit 2
  fi
fi

cleanup() {
  local rc="$1"
  if [[ "$DOWN_ON_EXIT" -eq 1 ]]; then
    local down_args=(--variant "$VARIANT" --run-id "$RUN_ID")
    [[ "$DOWN_PURGE" -eq 1 ]] && down_args+=(--purge)
    [[ "$DOWN_DESTROY_NETWORK" -eq 1 ]] && down_args+=(--destroy-network)
    log "teardown requested; running variant down for run_id=${RUN_ID}"
    "$SCRIPT_DIR/variant_down.sh" "${down_args[@]}" || err "variant down failed for run_id=${RUN_ID}"
  fi
  return "$rc"
}

on_exit() {
  local rc="$?"
  trap - EXIT
  cleanup "$rc" || true
  exit "$rc"
}

trap on_exit EXIT

variant_json="$(variant_to_json "$VARIANT" --validate-images)"
variant_name="$(echo "$variant_json" | jq -r '.name')"
ensure_run_dir "$RUN_ID"

log "smoke start run_id=${RUN_ID} variant=${variant_name}"

if [[ "$SKIP_UP" -eq 0 ]]; then
  "$SCRIPT_DIR/variant_up.sh" --variant "$VARIANT" --run-id "$RUN_ID"
else
  log "skipping variant up"
fi

if [[ "$SKIP_BOOTSTRAP" -eq 0 ]]; then
  "$SCRIPT_DIR/k1s_bootstrap.sh" --variant "$VARIANT" --run-id "$RUN_ID" --execute
else
  log "skipping bootstrap"
fi

if [[ "$SKIP_VALIDATE" -eq 0 ]]; then
  "$SCRIPT_DIR/variant_validate.sh" --variant "$VARIANT" --run-id "$RUN_ID"
  out_file="$(run_dir "$RUN_ID")/variant_validate.json"
  if [[ ! -f "$out_file" ]]; then
    err "expected validation output missing: $out_file"
    exit 1
  fi

  failures="$(
    jq -r '
      .[]
      | select(
          (.ssh != true)
          or (.cloud_init != "ok")
          or (.gpu_check == "missing")
          or (.cri_preflight == "failed")
        )
      | "\(.name) ssh=\(.ssh) cloud_init=\(.cloud_init) gpu=\(.gpu_check) cri=\(.cri_preflight)"
    ' "$out_file"
  )"
  if [[ -n "$failures" ]]; then
    err "smoke validation reported failing hosts:"
    printf '%s\n' "$failures" >&2
    exit 1
  fi
else
  log "skipping validation"
fi

log "smoke complete run_id=${RUN_ID}"
log "artifacts: $(run_dir "$RUN_ID")"
