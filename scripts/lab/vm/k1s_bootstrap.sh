#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
RUN_ID="$(resolve_run_id)"
EXECUTE=0

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>] [--execute]

Default behavior writes per-host bootstrap commands under runs/<RUN_ID>/bootstrap.
Use --execute to run them over SSH.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { err "--variant required"; exit 2; }
variant_json="$(variant_to_json "$VARIANT")"
ensure_run_dir "$RUN_ID"
mkdir -p "$(run_dir "$RUN_ID")/bootstrap"

controller_row="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-core") | @base64' | head -n1)"
if [[ -z "$controller_row" ]]; then
  err "variant must include one host with role=k1s-core"
  exit 2
fi
controller_host="$(printf '%s' "$controller_row" | base64 -d)"
controller_ip="$(echo "$controller_host" | jq -r '.ip')"
controller_port="$(echo "$variant_json" | jq -r '.k1s.controller_port')"
token="$(echo "$variant_json" | jq -r '.k1s.agent_token')"

mapfile -t rows < <(echo "$variant_json" | jq -r '.hosts[] | @base64')
plan_file="$(run_dir "$RUN_ID")/bootstrap/plan.txt"
: >"$plan_file"

build_command() {
  local host_json="$1"
  local name role site_id node_id labels ip agent_port
  name="$(echo "$host_json" | jq -r '.name')"
  role="$(echo "$host_json" | jq -r '.role')"
  site_id="$(echo "$host_json" | jq -r '.site_id // empty')"
  node_id="$(echo "$host_json" | jq -r '.node_id')"
  labels="$(echo "$host_json" | jq -r '.node_labels // empty')"
  ip="$(echo "$host_json" | jq -r '.ip')"
  agent_port="$(echo "$host_json" | jq -r '.agent_port')"

  case "$role" in
    k1s-core)
      cat <<CMD
cd /mnt/host
python3 -m pip install -e .[dev] --break-system-packages
nohup sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  make k1s-core-cri > /home/ae/k1s-core.log 2>&1 &
CMD
      ;;
    k1s-edge-core)
      if [[ -z "$site_id" ]]; then
        err "host ${name} role=k1s-edge-core requires site_id"
        exit 2
      fi
      cat <<CMD
cd /mnt/host
python3 -m pip install -e .[dev] --break-system-packages
nohup sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_SITE_ID=${site_id} \
  AE_NODE_ID=${node_id} \
  make k1s-edge-core-cri > /home/ae/k1s-edge-core.log 2>&1 &
CMD
      ;;
    k1s-edge-node)
      if [[ -z "$labels" ]]; then
        labels="site=${site_id:-edge}"
      fi
      cat <<CMD
cd /mnt/host
python3 -m pip install -e .[dev] --break-system-packages
nohup sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID=${node_id} \
  AE_NODE_LABELS='${labels}' \
  AE_CONTROLLER_URL=http://${controller_ip}:${controller_port} \
  AE_AGENT_ENDPOINT=http://${ip}:${agent_port} \
  AE_AGENT_TOKEN=${token} \
  make k1s-edge-node > /home/ae/k1s-edge-node.log 2>&1 &
CMD
      ;;
    *)
      err "unsupported role ${role} on host ${name}"
      exit 2
      ;;
  esac
}

for row in "${rows[@]}"; do
  host_json="$(printf '%s' "$row" | base64 -d)"
  name="$(echo "$host_json" | jq -r '.name')"
  ip="$(echo "$host_json" | jq -r '.ip')"
  script_path="$(run_dir "$RUN_ID")/bootstrap/${name}.sh"

  {
    echo "#!/usr/bin/env bash"
    echo "set -euo pipefail"
    echo "cloud-init status --wait"
    echo "sudo mkdir -p /mnt/host"
    echo "sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true"
    build_command "$host_json"
    echo "echo bootstrap-complete"
  } >"$script_path"
  chmod +x "$script_path"

  {
    echo "[${name} ${ip}]"
    echo "ssh -i \${SSH_KEY_PATH:-$HOME/.ssh/id_rsa} ae@${ip} 'bash -s' < ${script_path}"
    echo
  } >>"$plan_file"

  if [[ "$EXECUTE" -eq 1 ]]; then
    log "executing bootstrap on ${name} (${ip})"
    if ! wait_for_ssh "$ip" 80; then
      err "ssh not ready for ${name} (${ip})"
      exit 1
    fi
    run_remote "$ip" "bash -s" <"$script_path"
  fi
done

log "bootstrap scripts written under $(run_dir "$RUN_ID")/bootstrap"
if [[ "$EXECUTE" -eq 0 ]]; then
  log "run with --execute to apply automatically"
fi
