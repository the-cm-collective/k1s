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
ha_controller_row="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-ha-core") | @base64' | head -n1)"
if [[ -z "$controller_row" && -z "$ha_controller_row" ]]; then
  err "variant must include one host with role=k1s-core or k1s-ha-core"
  exit 2
fi
controller_host_row="$controller_row"
if [[ -z "$controller_host_row" ]]; then
  controller_host_row="$ha_controller_row"
fi
controller_host="$(printf '%s' "$controller_host_row" | base64 -d)"
controller_ip="$(echo "$controller_host" | jq -r '.ip')"
controller_host_override="$(echo "$variant_json" | jq -r '.k1s.controller_host // empty')"
if [[ -n "$controller_host_override" ]]; then
  controller_ip="$controller_host_override"
fi
controller_port="$(echo "$variant_json" | jq -r '.k1s.controller_port')"
controller_agent_port="$(echo "$variant_json" | jq -r '.k1s.agent_api_port')"
apishim_port="$(echo "$variant_json" | jq -r '.k1s.apishim_port')"
token="$(echo "$variant_json" | jq -r '.k1s.agent_token')"
leaf_uplink_mode="$(echo "$variant_json" | jq -r '.transport.leaf_uplink_mode')"
transport_hub_host="$(echo "$variant_json" | jq -r '.transport.hub_host // empty')"
edge_hub_leaf_port="$(echo "$variant_json" | jq -r '.transport.hub_leaf_port')"
ha_etcd_endpoints="$(echo "$variant_json" | jq -r '.ha.etcd_endpoints | join(",")')"
ha_etcd_prefix="$(echo "$variant_json" | jq -r '.ha.etcd_prefix // empty')"
ha_nats_url="$(echo "$variant_json" | jq -r '.ha.nats_url // empty')"
ha_apishim_cert_sans="$(echo "$variant_json" | jq -r '(
  ["DNS:apishim", "DNS:localhost", "IP:127.0.0.1", "IP:::1"]
  + [.hosts[] | select(.role=="k1s-ha-core") | "DNS:\(.name)"]
  + [.hosts[] | select(.role=="k1s-ha-core") | "IP:\(.ip)"]
) | unique | join(",")')"
edge_hub_leaf_host="127.0.0.1"
if [[ "$leaf_uplink_mode" == "direct_ip" ]]; then
  edge_hub_leaf_host="${transport_hub_host:-$controller_ip}"
fi
ha_profile_dir="$ROOT_DIR/state/profiles/k1s-ha-core"
ha_apishim_env_file="$ha_profile_dir/apishim.env"
ha_apishim_cli_env_file="$ha_profile_dir/apishim.cli.env"
ha_apishim_cert_file="$ha_profile_dir/apishim.crt"
ha_apishim_key_file="$ha_profile_dir/apishim.key"
ha_apishim_ca_file="$ha_profile_dir/apishim.ca.crt"
ha_apishim_ca_key_file="$ha_profile_dir/apishim.ca.key"
seed_manifest_default="/mnt/host/lab/variants/cri_seed_images.lock.json"
seed_bundle_default="/mnt/host/state/lab-vm/${RUN_ID}/seeds/cri-seed-images.oci.tar"
edge_core_site_ids="$(
  echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-edge-core" and (.site_id // "") != "") | .site_id' | sort -u | tr '\n' ' '
)"

mapfile -t rows < <(echo "$variant_json" | jq -r '.hosts[] | @base64')
plan_file="$(run_dir "$RUN_ID")/bootstrap/plan.txt"
: >"$plan_file"

ensure_ha_shared_apishim_tls() {
  [[ -n "$ha_controller_row" ]] || return 0
  mkdir -p "$ha_profile_dir"
  APISHIM_ENV_FILE="$ha_apishim_env_file" \
    APISHIM_CERT_FILE="$ha_apishim_cert_file" \
    APISHIM_KEY_FILE="$ha_apishim_key_file" \
    APISHIM_CA_FILE="$ha_apishim_ca_file" \
    APISHIM_CA_KEY_FILE="$ha_apishim_ca_key_file" \
    APISHIM_CERT_SANS="$ha_apishim_cert_sans" \
    "$ROOT_DIR/scripts/ensure_apishim_env.sh"
  APISHIM_ENV_FILE="$ha_apishim_env_file" \
    APISHIM_CLI_ENV_FILE="$ha_apishim_cli_env_file" \
    APISHIM_CERT_FILE="$ha_apishim_cert_file" \
    APISHIM_CA_FILE="$ha_apishim_ca_file" \
    "$ROOT_DIR/scripts/ensure_apishim_cli_env.sh"
}

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
      local register_edge_sites_snippet=""
      if [[ -n "$edge_core_site_ids" ]]; then
        register_edge_sites_snippet="$(cat <<CMD
if command -v bash >/dev/null 2>&1; then
  for _ in \$(seq 1 90); do
    if bash -c 'exec 3<>/dev/tcp/127.0.0.1/7422' >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi
for site in ${edge_core_site_ids}; do
  (cd /mnt/host && sudo -E AE_RUNTIME_BACKEND=cri AE_INFRA_BACKEND=cri AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} REGISTER_ONLY=1 SITE_ID="\$site" ./scripts/dev/add_edge_site.sh)
done
CMD
)"
      fi
      cat <<CMD
cd /mnt/host
bootstrap_pip_install
bootstrap_seed_cri_cache core
nohup sudo env \
  PYTHON_BIN=python3 \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  AE_APISHIM_MODE=\${AE_APISHIM_MODE:-host} \
  make k1s-core-cri > /home/ae/k1s-core.log 2>&1 &
${register_edge_sites_snippet}
CMD
      ;;
    k1s-core-node)
      if [[ -z "$labels" ]]; then
        labels="role=hub,site=${site_id:-hub}"
      fi
      cat <<CMD
cd /mnt/host
bootstrap_pip_install
bootstrap_seed_cri_cache core
nohup sudo env \
  PYTHON_BIN=python3 \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID=${node_id} \
  AE_NODE_LABELS='${labels}' \
  AE_ROSENPASS_ENABLED=\${AE_ROSENPASS_ENABLED:-0} \
  AE_CONTROLLER_URL=http://${controller_ip}:${controller_agent_port} \
  AE_AGENT_ENDPOINT=http://${ip}:${agent_port} \
  AE_AGENT_TOKEN=${token} \
  AE_NODE_PORT=${agent_port} \
  make k1s-core-node > /home/ae/k1s-core-node.log 2>&1 &
CMD
      ;;
    k1s-ha-core)
      cat <<CMD
cd /mnt/host
bootstrap_pip_install
bootstrap_seed_cri_cache core
nohup sudo env \
  PYTHON_BIN=python3 \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  AE_APISHIM_MODE=\${AE_APISHIM_MODE:-cri} \
  AE_APISHIM_PRESEEDED=1 \
  APISHIM_HOST=\${APISHIM_HOST:-0.0.0.0} \
  APISHIM_CERT_SANS='${ha_apishim_cert_sans}' \
  AE_HA_MODE=1 \
  AE_AGENT_API_PORT=${controller_agent_port} \
  AE_AGENT_API_TOKEN=${token} \
  AE_CONTROLLER_ID=${node_id} \
  AE_CONTROLLER_ADVERTISE_ADDR=http://${ip}:${controller_port} \
  AE_ETCD_ENDPOINTS='${ha_etcd_endpoints}' \
  AE_APISHIM_ETCD_ENDPOINTS='${ha_etcd_endpoints}' \
  AE_ETCD_PREFIX='${ha_etcd_prefix}' \
  AE_NATS_URL='${ha_nats_url}' \
  APISHIM_PORT=${apishim_port} \
  make k1s-ha-core > /home/ae/k1s-ha-core.log 2>&1 &
CMD
      ;;
    k1s-edge-core)
      if [[ -z "$site_id" ]]; then
        err "host ${name} role=k1s-edge-core requires site_id"
        exit 2
      fi
      cat <<CMD
cd /mnt/host
bootstrap_pip_install
bootstrap_seed_cri_cache edge
sudo mkdir -p /var/lib/ae/gateway
nohup sudo env \
  PYTHON_BIN=python3 \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_SITE_ID=${site_id} \
  AE_NODE_ID=${node_id} \
  AE_GATEWAY_SPOOL_PATH=/var/lib/ae/gateway/gateway-${site_id}-${node_id}.db \
  AE_GATEWAY_FENCE_DB=/var/lib/ae/gateway/fence-${site_id}-${node_id}.db \
  AE_NATS_HUB_LEAF_HOST=${edge_hub_leaf_host} \
  AE_NATS_HUB_LEAF_PORT=${edge_hub_leaf_port} \
  make k1s-edge-core-cri > /home/ae/k1s-edge-core.log 2>&1 &
CMD
      ;;
    k1s-edge-node)
      if [[ -z "$labels" ]]; then
        labels="site=${site_id:-edge}"
      fi
      cat <<CMD
cd /mnt/host
bootstrap_pip_install
nohup sudo env \
  PYTHON_BIN=python3 \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID=${node_id} \
  AE_NODE_LABELS='${labels}' \
  AE_CONTROLLER_URL=http://${controller_ip}:${controller_agent_port} \
  AE_AGENT_ENDPOINT=http://${ip}:${agent_port} \
  AE_AGENT_TOKEN=${token} \
  AE_NODE_PORT=${agent_port} \
  make k1s-edge-node > /home/ae/k1s-edge-node.log 2>&1 &
CMD
      ;;
    *)
      err "unsupported role ${role} on host ${name}"
      exit 2
      ;;
  esac
}

ensure_ha_shared_apishim_tls

for row in "${rows[@]}"; do
  host_json="$(printf '%s' "$row" | base64 -d)"
  name="$(echo "$host_json" | jq -r '.name')"
  ip="$(echo "$host_json" | jq -r '.ip')"
  script_path="$(run_dir "$RUN_ID")/bootstrap/${name}.sh"

  {
    echo "#!/usr/bin/env bash"
    echo "set -euo pipefail"
    echo "export AE_CRI_ENDPOINT=\${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
    echo "bootstrap_pip_install() {"
    echo "  local req='requirements.in'"
    echo "  if sudo python3 -m pip install --help 2>/dev/null | grep -q -- --break-system-packages; then"
    echo "    sudo python3 -m pip install -r \"\$req\" --break-system-packages"
    echo "  else"
    echo "    sudo python3 -m pip install -r \"\$req\""
    echo "  fi"
    echo "}"
    echo "bootstrap_seed_cri_cache() {"
    echo "  local profile=\"\${1:-}\""
    echo "  if [[ -z \"\$profile\" ]]; then"
    echo "    echo \"[cri-seed] missing profile argument\" >&2"
    echo "    return 1"
    echo "  fi"
    echo "  local mode_raw=\"\${AE_CRI_CACHE_SEED_MODE:-best_effort}\""
    echo "  local mode=\"\$(printf '%s' \"\$mode_raw\" | tr '[:upper:]' '[:lower:]')\""
    echo "  local manifest=\"\${AE_CRI_CACHE_SEED_MANIFEST:-${seed_manifest_default}}\""
    echo "  local bundle=\"\${AE_CRI_CACHE_SEED_BUNDLE:-${seed_bundle_default}}\""
    echo "  case \"\$mode\" in"
    echo "    off|0|false|no)"
    echo "      echo \"[cri-seed] mode=off profile=\$profile\""
    echo "      return 0"
    echo "      ;;"
    echo "  esac"
    echo "  if [[ ! -f \"\$manifest\" ]]; then"
    echo "    echo \"[cri-seed] manifest missing: \$manifest\" >&2"
    echo "    [[ \"\$mode\" == \"required\" ]] && return 1"
    echo "    return 0"
    echo "  fi"
    echo "  if [[ -f \"\$bundle\" ]]; then"
    echo "    echo \"[cri-seed] import bundle=\$bundle profile=\$profile\""
    echo "    if ! sudo ctr -n k8s.io images import \"\$bundle\" >/dev/null 2>&1; then"
    echo "      echo \"[cri-seed] import failed: \$bundle\" >&2"
    echo "      [[ \"\$mode\" == \"required\" ]] && return 1"
    echo "    fi"
    echo "  else"
    echo "    echo \"[cri-seed] bundle missing: \$bundle\" >&2"
    echo "    [[ \"\$mode\" == \"required\" ]] && return 1"
    echo "  fi"
    echo "  mapfile -t required_images < <(jq -r --arg p \"\$profile\" '.images[\$p][]?' \"\$manifest\")"
    echo "  mapfile -t image_refs < <(sudo ctr -n k8s.io images ls -q 2>/dev/null || true)"
    echo "  local missing=()"
    echo "  local image"
    echo "  for image in \"\${required_images[@]}\"; do"
    echo "    if ! printf '%s\n' \"\${image_refs[@]}\" | grep -Fx -- \"\$image\" >/dev/null 2>&1; then"
    echo "      missing+=(\"\$image\")"
    echo "    fi"
    echo "  done"
    echo "  if [[ \"\${#missing[@]}\" -gt 0 ]]; then"
    echo "    echo \"[cri-seed] missing_images profile=\$profile: \${missing[*]}\" >&2"
    echo "    [[ \"\$mode\" == \"required\" ]] && return 1"
    echo "  fi"
    echo "  echo \"[cri-seed] ready profile=\$profile missing=\${#missing[@]}\""
    echo "  return 0"
    echo "}"
    echo "cloud-init status --wait"
    echo "sudo mkdir -p /mnt/host"
    echo "sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true"
    echo "source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh"
    echo "ensure_vm_bootstrap_prereqs"
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
