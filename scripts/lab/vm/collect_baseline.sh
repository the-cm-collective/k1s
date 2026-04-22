#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
RUN_ID="$(resolve_run_id)"

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
variant_json="$(variant_to_json "$VARIANT")"
ensure_run_dir "$RUN_ID"

run_root="$(run_dir "$RUN_ID")"
mkdir -p "$run_root/ae" "$run_root/logs" "$run_root/metrics"

if command -v ae >/dev/null 2>&1; then
  ae nodes --json >"$run_root/ae/nodes.json" || true
else
  PYTHONPATH="$ROOT_DIR/src" python -m ae.cli nodes --json >"$run_root/ae/nodes.json" || true
fi

versions='[]'
mapfile -t rows < <(echo "$variant_json" | jq -r '.hosts[] | @base64')
for row in "${rows[@]}"; do
  host="$(printf '%s' "$row" | base64 -d)"
  name="$(echo "$host" | jq -r '.name')"
  ip="$(echo "$host" | jq -r '.ip')"
  gpu="$(echo "$host" | jq -r '.gpu')"

  kernel="$(run_remote "$ip" "uname -r" || true)"
  osrel="$(run_remote "$ip" "source /etc/os-release && echo \"$PRETTY_NAME\"" || true)"
  containerd_ver="$(run_remote "$ip" "containerd --version 2>/dev/null | head -n1" || true)"
  nvidia=""
  if [[ "$gpu" == "true" ]]; then
    nvidia="$(run_remote "$ip" "nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null" || true)"
  fi

  rec="$(jq -n \
    --arg name "$name" \
    --arg ip "$ip" \
    --arg os "$osrel" \
    --arg kernel "$kernel" \
    --arg containerd "$containerd_ver" \
    --arg nvidia "$nvidia" \
    '{name:$name,ip:$ip,os:$os,kernel:$kernel,containerd:$containerd,nvidia:$nvidia}')"
  versions="$(jq --argjson r "$rec" '. + [$r]' <<<"$versions")"

  run_remote "$ip" "sudo journalctl -u containerd --no-pager -n 200" >"$run_root/logs/${name}-containerd.log" 2>/dev/null || true
  run_remote "$ip" "tail -n 400 /home/ae/k1s-core.log" >"$run_root/logs/${name}-k1s-core.log" 2>/dev/null || true
  run_remote "$ip" "tail -n 400 /home/ae/k1s-core-node.log" >"$run_root/logs/${name}-k1s-core-node.log" 2>/dev/null || true
  run_remote "$ip" "tail -n 400 /home/ae/k1s-edge-core.log" >"$run_root/logs/${name}-k1s-edge-core.log" 2>/dev/null || true
  run_remote "$ip" "tail -n 400 /home/ae/k1s-edge-node.log" >"$run_root/logs/${name}-k1s-edge-node.log" 2>/dev/null || true

done

echo "$versions" >"$run_root/versions.json"
if [[ -f "$run_root/loadgen/summary-baseline.json" ]]; then
  cp "$run_root/loadgen/summary-baseline.json" "$run_root/metrics/baseline.json"
fi

log "baseline artifacts collected at $run_root"
