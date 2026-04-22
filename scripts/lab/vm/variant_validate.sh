#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
RUN_ID="$(resolve_run_id)"
CLOUD_INIT_WAIT_TIMEOUT="${CLOUD_INIT_WAIT_TIMEOUT:-300}"

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { err "--variant required"; exit 2; }
if ! [[ "$CLOUD_INIT_WAIT_TIMEOUT" =~ ^[0-9]+$ ]]; then
  err "CLOUD_INIT_WAIT_TIMEOUT must be an integer number of seconds"
  exit 2
fi
variant_json="$(variant_to_json "$VARIANT")"
ensure_run_dir "$RUN_ID"
out_file="$(run_dir "$RUN_ID")/variant_validate.json"

results='[]'
mapfile -t rows < <(echo "$variant_json" | jq -r '.hosts[] | @base64')

for row in "${rows[@]}"; do
  host="$(printf '%s' "$row" | base64 -d)"
  name="$(echo "$host" | jq -r '.name')"
  ip="$(echo "$host" | jq -r '.ip')"
  gpu="$(echo "$host" | jq -r '.gpu')"

  if ! wait_for_ssh "$ip" 40; then
    rec="$(jq -n \
      --arg name "$name" \
      --arg ip "$ip" \
      '{name:$name,ip:$ip,ssh:false,cloud_init:"ssh_unreachable",cloud_init_done:false,cloud_init_timeout:false,cloud_init_detail:"",gpu_check:"unknown",cri_preflight:"unknown"}')"
    results="$(jq --argjson r "$rec" '. + [$r]' <<<"$results")"
    continue
  fi

  cloud_init_state="$(
    run_remote "$ip" \
      "timeout ${CLOUD_INIT_WAIT_TIMEOUT}s cloud-init status --wait >/dev/null 2>&1; rc=\$?; if [ \$rc -eq 0 ]; then echo done; elif [ \$rc -eq 124 ]; then echo timeout; else echo failed; fi" \
      || echo "failed"
  )"
  cloud_init_done=false
  cloud_init_timeout=false
  cloud_init="failed"
  case "$cloud_init_state" in
    done)
      cloud_init="ok"
      cloud_init_done=true
      ;;
    timeout)
      cloud_init="timeout"
      cloud_init_timeout=true
      ;;
  esac
  cloud_init_detail="$(run_remote "$ip" "cloud-init status --long 2>/dev/null | sed -n '1,12p'" || true)"
  gpu_status="not_requested"
  cri_status="not_requested"
  if [[ "$gpu" == "true" ]]; then
    gpu_status="$(run_remote "$ip" "nvidia-smi -L >/dev/null 2>&1 && echo ok || echo missing")"
    cri_status="$(run_remote "$ip" "AE_CRI_RUNTIME_HANDLER=nvidia /mnt/host/scripts/cri_preflight.sh >/dev/null 2>&1 && echo ok || echo failed")"
  fi

  rec="$(jq -n \
    --arg name "$name" \
    --arg ip "$ip" \
    --arg cloud_init "$cloud_init" \
    --arg cloud_init_detail "$cloud_init_detail" \
    --argjson cloud_init_done "$cloud_init_done" \
    --argjson cloud_init_timeout "$cloud_init_timeout" \
    --arg gpu "$gpu_status" \
    --arg cri "$cri_status" \
    '{name:$name,ip:$ip,ssh:true,cloud_init:$cloud_init,cloud_init_done:$cloud_init_done,cloud_init_timeout:$cloud_init_timeout,cloud_init_detail:$cloud_init_detail,gpu_check:$gpu,cri_preflight:$cri}')"
  results="$(jq --argjson r "$rec" '. + [$r]' <<<"$results")"
done

echo "$results" >"$out_file"
log "wrote ${out_file}"
