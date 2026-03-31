#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=scripts/lib/nixos_bridge.sh
source "$ROOT_DIR/scripts/lib/nixos_bridge.sh"

DEFAULT_VARIANT="$ROOT_DIR/lab/variants/ha-control-plane-hub-node.yaml"
DEFAULT_RUN_ID="ha-dashboard-local"
DEFAULT_APISHIM_IMAGE="localhost:5001/k1s-apishim:dev"
DEFAULT_DEMO_SHELL_IMAGE="docker.io/library/demo-shell:latest"

SUBCOMMAND="${1:-}"
if [[ -z "$SUBCOMMAND" ]]; then
  SUBCOMMAND="help"
else
  shift || true
fi

VARIANT="${VARIANT:-$DEFAULT_VARIANT}"
RUN_ID="${RUN_ID:-$DEFAULT_RUN_ID}"
TARGET="${TARGET:-all}"
REBUILD_IMAGES=0
PURGE=0
DESTROY_NETWORK=0

usage() {
  cat <<EOF
Usage:
  $0 up [--variant <path>] [--run-id <id>]
  $0 status [--variant <path>] [--run-id <id>]
  $0 workload-smoke [--variant <path>] [--run-id <id>]
  $0 core-workload-smoke [--variant <path>] [--run-id <id>]
  $0 down [--variant <path>] [--run-id <id>] [--purge] [--destroy-network]
  $0 purge [--variant <path>] [--run-id <id>] [--destroy-network]
  $0 reseed-core [--variant <path>] [--run-id <id>] [--target <csv|all>]
  $0 restart-core [--variant <path>] [--run-id <id>] [--target <csv|all>]
  $0 restart-apishim [--variant <path>] [--run-id <id>] [--target <csv|all>]
  $0 restart-hub-node [--variant <path>] [--run-id <id>]
  $0 refresh-all [--variant <path>] [--run-id <id>]
  $0 reset [--variant <path>] [--run-id <id>] [--destroy-network] [--rebuild-images]

Defaults:
  --variant $DEFAULT_VARIANT
  --run-id  $DEFAULT_RUN_ID

Targets:
  all         all HA cores for restart-core/restart-apishim, or all core-profile VMs for reseed-core
  core-a      selected HA core VM
  core-b      selected HA core VM
  core-c      selected HA core VM
  hub-1       retained workload-capable hub node (valid for reseed-core only)
EOF
}

parse_common_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --variant)
        VARIANT="$2"
        shift 2
        ;;
      --run-id)
        RUN_ID="$2"
        shift 2
        ;;
      --target)
        TARGET="$2"
        shift 2
        ;;
      --purge)
        PURGE=1
        shift
        ;;
      --destroy-network)
        DESTROY_NETWORK=1
        shift
        ;;
      --rebuild-images)
        REBUILD_IMAGES=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        err "unknown arg: $1"
        usage
        exit 2
        ;;
    esac
  done
}

parse_common_args "$@"

[[ -f "$VARIANT" ]] || { err "variant not found: $VARIANT"; exit 2; }
require_cmd jq
variant_json="$(variant_to_json "$VARIANT")"
variant_name="$(echo "$variant_json" | jq -r '.name')"
controller_port="$(echo "$variant_json" | jq -r '.k1s.controller_port')"
controller_agent_port="$(echo "$variant_json" | jq -r '.k1s.agent_api_port')"
apishim_port="$(echo "$variant_json" | jq -r '.k1s.apishim_port')"
agent_token="$(echo "$variant_json" | jq -r '.k1s.agent_token')"
ha_etcd_endpoints="$(echo "$variant_json" | jq -r '.ha.etcd_endpoints | join(",")')"
ha_etcd_prefix="$(echo "$variant_json" | jq -r '.ha.etcd_prefix // empty')"
ha_nats_url="$(echo "$variant_json" | jq -r '.ha.nats_url // empty')"
seed_bundle_path="$ROOT_DIR/state/lab-vm/$RUN_ID/seeds/cri-seed-images.oci.tar"

mapfile -t ha_host_rows < <(
  echo "$variant_json" \
    | jq -r '.hosts[] | select(.role=="k1s-ha-core") | [.name, .ip, (.node_id // .name)] | @tsv'
)

hub_node_name="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-core-node") | .name' | head -n1)"
hub_node_ip="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-core-node") | .ip' | head -n1)"
hub_node_id="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-core-node") | (.node_id // .name)' | head -n1)"
hub_node_labels="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-core-node") | (.node_labels // "")' | head -n1)"
hub_node_agent_port="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-core-node") | (.agent_port // 9111)' | head -n1)"
first_core_ip="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-ha-core") | .ip' | head -n1)"
ingress_tls_port="${AE_EDGE_INGRESS_TLS_PORT:-10443}"
dash_host="${AE_CONTROLPLANE_DASH_HOST:-dash.home.arpa}"
docs_host="${AE_CONTROLPLANE_DOCS_HOST:-docs.home.arpa}"
api_host="${AE_CONTROLPLANE_API_HOST:-api.home.arpa}"
retained_workload_smoke_manifest="${AE_RETAINED_WORKLOAD_SMOKE_MANIFEST:-$ROOT_DIR/docs/site/examples/ha-web-smoke.yaml}"
retained_workload_smoke_app="${AE_RETAINED_WORKLOAD_SMOKE_APP:-ha-web-smoke}"
retained_workload_smoke_host="${AE_RETAINED_WORKLOAD_SMOKE_HOST:-ha-web-smoke.home.arpa}"
retained_workload_smoke_expected_text="${AE_RETAINED_WORKLOAD_SMOKE_EXPECTED_TEXT:-Shell + Port-Forward Smoke}"
ha_core_workload_smoke_manifest="${AE_HA_CORE_WORKLOAD_SMOKE_MANIFEST:-$ROOT_DIR/docs/site/examples/ha-web-smoke-edge.yaml}"
ha_core_workload_smoke_app="${AE_HA_CORE_WORKLOAD_SMOKE_APP:-ha-edge-web-smoke}"
ha_core_workload_smoke_host="${AE_HA_CORE_WORKLOAD_SMOKE_HOST:-ha-edge-web-smoke.home.arpa}"
ha_core_workload_smoke_expected_text="${AE_HA_CORE_WORKLOAD_SMOKE_EXPECTED_TEXT:-Shell + Port-Forward Smoke}"
edge_runtime_name="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-edge-node") | .name' | head -n1)"
edge_runtime_ip="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-edge-node") | .ip' | head -n1)"
edge_runtime_id="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-edge-node") | (.node_id // .name)' | head -n1)"
edge_runtime_labels="$(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-edge-node") | (.node_labels // "")' | head -n1)"
local_dev_hosts_dir="$(run_dir "$RUN_ID")"
local_dev_hosts_state_file="$local_dev_hosts_dir/local-dev-hosts.env"
local_dev_hosts_snapshot_file="$local_dev_hosts_dir/local-dev-hosts.snapshot"
local_dev_hosts_apply_file="$local_dev_hosts_dir/local-dev-hosts.apply"
local_dev_hosts_block_begin="# BEGIN k1s-local-dev"
local_dev_hosts_block_end="# END k1s-local-dev"
nixos_bridge_hosts_file="$(k1s_nixos_bridge_hosts_file)"

require_local_sudo() {
  require_cmd sudo
  if ! sudo -n true >/dev/null 2>&1; then
    err "local sudo credentials are required; run 'sudo -v' and retry"
    exit 2
  fi
}

require_remote_hosts() {
  local state_dir="$ROOT_DIR/state/lab-vm/$RUN_ID"
  if [[ ! -d "$state_dir" ]]; then
    err "state dir not found for run_id=${RUN_ID}: $state_dir"
    case "$SUBCOMMAND" in
      workload-smoke)
        err "workload-smoke requires a live retained HA run; run 'make lab-vm-ha-dashboard-up' first"
        ;;
      core-workload-smoke)
        err "core-workload-smoke requires a live HA VM run; bring the target variant up first"
        ;;
      status|reseed-core|restart-core|restart-apishim|restart-hub-node|refresh-all)
        err "${SUBCOMMAND} requires a live retained HA run; run 'make lab-vm-ha-dashboard-up' first"
        ;;
    esac
    exit 1
  fi
}

load_local_auth_env() {
  local apishim_env_file="$ROOT_DIR/state/profiles/k1s-ha-core/apishim.env"
  local controller_env_file="$ROOT_DIR/state/profiles/k1s-ha-core/controller.env"
  if [[ ! -f "$apishim_env_file" || ! -f "$controller_env_file" ]]; then
    return 1
  fi
  local exports_text=""
  exports_text="$(
    APISHIM_ENV_FILE="$apishim_env_file" CONTROLLER_ENV_FILE="$controller_env_file" \
      "$ROOT_DIR/scripts/ae-env.sh" local
  )"
  if [[ -n "$exports_text" ]]; then
    # Local env file generated by repo tooling; this is the same contract used in docs.
    eval "$exports_text"
  fi
  return 0
}

require_http_tools() {
  require_cmd curl
}

load_retained_local_dev_target_hosts() {
  local hosts_raw="${DEV_LOCAL_HOSTS:-$dash_host $docs_host $api_host}"
  local item=""
  local -A seen=()
  RETAINED_LOCAL_DEV_TARGET_HOSTS=()
  for item in ${hosts_raw}; do
    [[ -n "$item" ]] || continue
    if [[ -z "${seen[$item]:-}" ]]; then
      seen["$item"]=1
      RETAINED_LOCAL_DEV_TARGET_HOSTS+=("$item")
    fi
  done
}

read_current_local_dev_hosts_lines() {
  if [[ -s "$nixos_bridge_hosts_file" ]]; then
    awk 'NF && $1 !~ /^#/' "$nixos_bridge_hosts_file"
    return 0
  fi
  if grep -Fqx "$local_dev_hosts_block_begin" /etc/hosts 2>/dev/null; then
    awk -v begin="$local_dev_hosts_block_begin" -v end="$local_dev_hosts_block_end" '
      $0 == begin { capture = 1; next }
      $0 == end { capture = 0; next }
      capture && NF && $1 !~ /^#/ { print }
    ' /etc/hosts
    return 0
  fi
  return 1
}

snapshot_local_dev_hosts_state() {
  local current_lines=""
  local restore_mode="clean"
  if [[ -f "$local_dev_hosts_state_file" ]]; then
    return 0
  fi
  mkdir -p "$local_dev_hosts_dir"
  current_lines="$(read_current_local_dev_hosts_lines || true)"
  if [[ -n "${current_lines// }" ]]; then
    printf '%s\n' "$current_lines" >"$local_dev_hosts_snapshot_file"
    restore_mode="restore"
  else
    rm -f "$local_dev_hosts_snapshot_file"
  fi
  printf 'LOCAL_DEV_HOSTS_RESTORE_MODE=%q\n' "$restore_mode" >"$local_dev_hosts_state_file"
}

render_retained_local_dev_apply_map() {
  local target_ip="${DEV_LOCAL_HOSTS_IP:-$first_core_ip}"
  local line="" ip="" host="" rest=""
  local -A host_ip=()
  local -A seen=()
  local -a ordered_hosts=()

  load_retained_local_dev_target_hosts

  if [[ -s "$local_dev_hosts_snapshot_file" ]]; then
    while read -r ip host rest; do
      [[ -n "${ip// }" && -n "${host// }" ]] || continue
      if [[ -z "${seen[$host]:-}" ]]; then
        ordered_hosts+=("$host")
        seen["$host"]=1
      fi
      host_ip["$host"]="$ip"
    done <"$local_dev_hosts_snapshot_file"
  fi

  for host in "${RETAINED_LOCAL_DEV_TARGET_HOSTS[@]}"; do
    if [[ -z "${seen[$host]:-}" ]]; then
      ordered_hosts+=("$host")
      seen["$host"]=1
    fi
    host_ip["$host"]="$target_ip"
  done

  : >"$local_dev_hosts_apply_file"
  for host in "${ordered_hosts[@]}"; do
    printf '%s %s\n' "${host_ip[$host]}" "$host" >>"$local_dev_hosts_apply_file"
  done
}

apply_retained_local_dev_hosts() {
  local target_ip="${DEV_LOCAL_HOSTS_IP:-$first_core_ip}"
  snapshot_local_dev_hosts_state
  render_retained_local_dev_apply_map
  load_retained_local_dev_target_hosts
  log "mapping local DNS/TLS for ${RETAINED_LOCAL_DEV_TARGET_HOSTS[*]} to ${target_ip}"
  AE_NIXOS_REBUILD=always \
  DEV_LOCAL_HOSTS_MAP_FILE="$local_dev_hosts_apply_file" \
    "$ROOT_DIR/scripts/dev/ensure_dev_local.sh"
}

restore_retained_local_dev_hosts() {
  local restore_mode=""
  if [[ ! -f "$local_dev_hosts_state_file" ]]; then
    return 0
  fi
  # shellcheck source=/dev/null
  source "$local_dev_hosts_state_file"
  case "${LOCAL_DEV_HOSTS_RESTORE_MODE:-clean}" in
    restore)
      if [[ -s "$local_dev_hosts_snapshot_file" ]]; then
        log "restoring prior local DNS/TLS mapping for retained HA hosts"
        AE_NIXOS_REBUILD=always \
        DEV_LOCAL_HOSTS_MAP_FILE="$local_dev_hosts_snapshot_file" \
          "$ROOT_DIR/scripts/dev/ensure_dev_local.sh"
      else
        log "retained HA host snapshot missing; removing managed local DNS/TLS state"
        AE_NIXOS_REBUILD=always \
        AE_DEV_LOCAL_ACTION=clean "$ROOT_DIR/scripts/dev/ensure_dev_local.sh"
      fi
      ;;
    *)
      log "removing retained HA local DNS/TLS state"
      AE_NIXOS_REBUILD=always \
      AE_DEV_LOCAL_ACTION=clean "$ROOT_DIR/scripts/dev/ensure_dev_local.sh"
      ;;
  esac
  rm -f "$local_dev_hosts_state_file" "$local_dev_hosts_snapshot_file" "$local_dev_hosts_apply_file"
}

verify_retained_local_dev_hosts_applied() {
  local target_ip="${DEV_LOCAL_HOSTS_IP:-$first_core_ip}"
  local attempts="${1:-5}"
  local delay_s="${2:-1}"
  local attempt=1
  local current_output="" host=""

  require_cmd getent
  load_retained_local_dev_target_hosts

  while (( attempt <= attempts )); do
    current_output="$(getent hosts "${RETAINED_LOCAL_DEV_TARGET_HOSTS[@]}" 2>/dev/null || true)"
    if [[ -n "${current_output// }" ]]; then
      local all_hosts_match=1
      for host in "${RETAINED_LOCAL_DEV_TARGET_HOSTS[@]}"; do
        if ! awk -v expected_ip="$target_ip" -v expected_host="$host" '
          $1 == expected_ip && $2 == expected_host { found = 1 }
          END { exit(found ? 0 : 1) }
        ' <<<"$current_output"; then
          all_hosts_match=0
          break
        fi
      done
      if [[ "$all_hosts_match" -eq 1 ]]; then
        return 0
      fi
    fi

    if (( attempt < attempts )); then
      sleep "$delay_s"
    fi
    attempt=$((attempt + 1))
  done

  err "retained HA local DNS/TLS mapping did not apply to ${target_ip}"
  err "expected: getent hosts ${RETAINED_LOCAL_DEV_TARGET_HOSTS[*]}"
  if [[ -n "${current_output// }" ]]; then
    err "actual getent output:"
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      err "  ${line}"
    done <<<"$current_output"
  else
    err "actual getent output: <empty>"
  fi
  err "per-core Envoy smoke may still be healthy because retained status checks use curl --resolve"
  return 1
}

wait_for_http_version() {
  local url="$1"
  local timeout_s="${2:-60}"
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_https_version() {
  local url="$1"
  local timeout_s="${2:-60}"
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    if curl -fsSk "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

probe_resolved_https_status() {
  local host="$1"
  local path="$2"
  local ip="$3"
  local http_code=""

  http_code="$(
    curl -sk \
      --resolve "${host}:${ingress_tls_port}:${ip}" \
      -o /dev/null \
      -w '%{http_code}' \
      "https://${host}:${ingress_tls_port}${path}" 2>/dev/null || true
  )"
  if [[ -z "$http_code" ]]; then
    http_code="000"
  fi
  printf '%s' "$http_code"
}

read_api_system_summary_with_retry() {
  local ip="$1"
  local attempts="${2:-6}"
  local delay_s="${3:-2}"
  local response_text=""
  local http_code=""
  local body=""
  local attempt=1

  while (( attempt <= attempts )); do
    response_text="$(
      curl -sk \
        --resolve "${api_host}:${ingress_tls_port}:${ip}" \
        -H "Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}" \
        "https://${api_host}:${ingress_tls_port}/system" \
        -w $'\n%{http_code}' 2>/dev/null || true
    )"
    http_code="${response_text##*$'\n'}"
    if [[ "$http_code" =~ ^[0-9]{3}$ && "$http_code" != "000" ]]; then
      body="${response_text%$'\n'*}"
      printf '%s\n' "$http_code"
      printf '%s' "$body"
      return 0
    fi
    if (( attempt < attempts )); then
      sleep "$delay_s"
    fi
    attempt=$((attempt + 1))
  done

  return 1
}

wait_for_hub_node_registration() {
  local python_bin
  local deadline=$((SECONDS + 90))
  python_bin="$(lab_python)"
  while (( SECONDS < deadline )); do
    if PYTHONPATH="$ROOT_DIR/src" \
      AE_STATE_BACKEND=etcd \
      AE_ETCD_ENDPOINTS="$ha_etcd_endpoints" \
      AE_ETCD_PREFIX="$ha_etcd_prefix" \
      "$python_bin" -m ae.cli nodes 2>/dev/null | grep -Fqx "${hub_node_id}: status=Ready cordoned=no backend=cri endpoint=http://${hub_node_ip}:${hub_node_agent_port} labels=role=hub,site=hub"; then
      return 0
    fi
    if PYTHONPATH="$ROOT_DIR/src" \
      AE_STATE_BACKEND=etcd \
      AE_ETCD_ENDPOINTS="$ha_etcd_endpoints" \
      AE_ETCD_PREFIX="$ha_etcd_prefix" \
      "$python_bin" -m ae.cli nodes 2>/dev/null | grep -Fq "${hub_node_id}:"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

append_label_args_from_csv() {
  local -n out_args="$1"
  local raw_labels="${2:-}"
  local item=""
  local -a items=()
  IFS=',' read -r -a items <<<"$raw_labels"
  for item in "${items[@]}"; do
    item="${item//[[:space:]]/}"
    [[ -n "$item" && "$item" == *=* ]] || continue
    out_args+=(--label "$item")
  done
}

check_stack_ready() {
  local row="" name="" ip="" node_id=""
  for row in "${ha_host_rows[@]}"; do
    IFS=$'\t' read -r name ip node_id <<<"$row"
    if ! wait_for_http_version "http://${ip}:${controller_port}/__ae/version" 90; then
      err "controller not ready on ${name} (${ip}:${controller_port})"
      print_remote_ha_failure_context "$name" "$ip" controller
      return 1
    fi
    if ! wait_for_https_version "https://${ip}:${apishim_port}/__ae/version" 90; then
      err "apishim not ready on ${name} (${ip}:${apishim_port})"
      print_remote_ha_failure_context "$name" "$ip" apishim
      return 1
    fi
  done
  if [[ -n "$hub_node_id" ]] && ! wait_for_hub_node_registration; then
    err "hub node registration was not observed for ${hub_node_id}"
    return 1
  fi
  return 0
}

run_remote_inline() {
  local ip="$1"
  local script_text="$2"
  ensure_ssh_key
  if ! wait_for_ssh "$ip" 80; then
    err "ssh not ready for ${ip}"
    exit 1
  fi
  with_repo_host_mount "$ip" >/dev/null 2>&1 || true
  run_remote "$ip" "bash -s" <<<"$script_text"
}

print_remote_ha_failure_context() {
  local name="$1"
  local ip="$2"
  local focus="${3:-controller}"
  log "collecting HA failure context from ${name} (${ip})"
  run_remote_inline "$ip" "$(cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
echo "[ha-debug] host=${name} ip=${ip} focus=${focus}"
if [[ -f /home/ae/k1s-ha-core.log ]]; then
  echo "[ha-debug] tail /home/ae/k1s-ha-core.log"
  sudo tail -n 80 /home/ae/k1s-ha-core.log || true
else
  echo "[ha-debug] missing /home/ae/k1s-ha-core.log"
fi
echo "[ha-debug] crictl ps -a (ha filter)"
ps_out="\$(sudo crictl ps -a 2>/dev/null || true)"
if [[ -n "\$ps_out" ]]; then
  printf '%s\n' "\$ps_out" | awk 'NR==1 || /k1s-ha-core|k1s-core-(nats-hub|apishim)/'
else
  echo "[ha-debug] crictl output unavailable"
fi
if [[ "${focus}" == "apishim" ]]; then
  apishim_id="\$(sudo crictl ps -a --name k1s-core-apishim -q 2>/dev/null | head -n1)"
  if [[ -n "\$apishim_id" ]]; then
    echo "[ha-debug] apishim inspect summary"
    sudo crictl inspect "\$apishim_id" 2>/dev/null | grep -E '"(state|reason|message|exitCode)"' | sed -n '1,20p' || true
  else
    echo "[ha-debug] apishim inspect unavailable"
  fi
fi
echo "[ha-debug] ss -ltn (controller/apishim)"
ss -ltn 2>/dev/null | awk 'NR==1 || \$4 ~ /:${controller_port}\$/ || \$4 ~ /:${apishim_port}\$/'
EOF
)" || true
}

reseed_target_rows() {
  local requested="$1"
  local host_rows=()
  if [[ "$requested" == "all" ]]; then
    mapfile -t host_rows < <(
      echo "$variant_json" \
        | jq -r '.hosts[] | select(.role=="k1s-ha-core" or .role=="k1s-core-node") | [.name, .ip, .role, (.node_id // .name)] | @tsv'
    )
  else
    mapfile -t host_rows < <(
      echo "$variant_json" \
        | jq -r --arg requested "$requested" '
            ($requested | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))) as $targets
            | .hosts[]
            | select((.role=="k1s-ha-core" or .role=="k1s-core-node") and ((.name // "") as $name | $targets | index($name)))
            | [.name, .ip, .role, (.node_id // .name)] | @tsv
          '
    )
  fi
  printf '%s\n' "${host_rows[@]}"
}

core_target_rows() {
  local requested="$1"
  local host_rows=()
  if [[ "$requested" == "all" ]]; then
    host_rows=("${ha_host_rows[@]}")
  else
    mapfile -t host_rows < <(
      echo "$variant_json" \
        | jq -r --arg requested "$requested" '
            ($requested | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))) as $targets
            | .hosts[]
            | select(.role=="k1s-ha-core" and ((.name // "") as $name | $targets | index($name)))
            | [.name, .ip, (.node_id // .name)] | @tsv
          '
    )
  fi
  printf '%s\n' "${host_rows[@]}"
}

ensure_non_empty_selection() {
  local rows="$1"
  local context="$2"
  if [[ -z "$rows" ]]; then
    err "no hosts matched target=${TARGET} for ${context}"
    exit 2
  fi
}

ensure_core_seed_bundle() {
  if [[ -f "$seed_bundle_path" ]]; then
    log "using existing core seed bundle: $seed_bundle_path"
    return 0
  fi
  log "building core seed bundle for run_id=${RUN_ID}: $seed_bundle_path"
  AE_CRI_CACHE_SEED_ENGINE="${AE_CRI_CACHE_SEED_ENGINE:-}" \
  AE_CRI_CACHE_SEED_ALWAYS_PULL="${AE_CRI_CACHE_SEED_ALWAYS_PULL:-0}" \
    "$SCRIPT_DIR/image_seed_bundle.sh" \
      --run-id "$RUN_ID" \
      --profile core \
      --output "$seed_bundle_path"
}

resolve_local_image_engine() {
  local requested="${AE_CRI_CACHE_SEED_ENGINE:-}"
  local candidate=""
  if [[ -n "$requested" ]]; then
    case "$requested" in
      docker|nerdctl|podman)
        if command -v "$requested" >/dev/null 2>&1; then
          printf '%s' "$requested"
          return 0
        fi
        log "requested host image cleanup engine not found: $requested"
        return 1
        ;;
      *)
        log "skipping host image cleanup for unsupported engine: $requested"
        return 1
        ;;
    esac
  fi
  for candidate in docker nerdctl podman; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  log "no supported local image engine found; skipping host image cleanup"
  return 1
}

host_image_present() {
  local engine="$1"
  local image="$2"
  case "$engine" in
    docker)
      docker image inspect "$image" >/dev/null 2>&1
      ;;
    nerdctl)
      nerdctl --namespace "${AE_NERDCTL_NAMESPACE:-k8s.io}" image inspect "$image" >/dev/null 2>&1
      ;;
    podman)
      podman image inspect "$image" >/dev/null 2>&1
      ;;
    *)
      return 1
      ;;
  esac
}

remove_host_image() {
  local engine="$1"
  local image="$2"
  if ! host_image_present "$engine" "$image"; then
    log "host image already absent: $image"
    return 0
  fi
  log "removing host image via ${engine}: $image"
  case "$engine" in
    docker)
      docker rmi -f "$image" >/dev/null 2>&1 || log "host image removal failed: $image"
      ;;
    nerdctl)
      nerdctl --namespace "${AE_NERDCTL_NAMESPACE:-k8s.io}" rmi -f "$image" >/dev/null 2>&1 || log "host image removal failed: $image"
      ;;
    podman)
      podman rmi -f "$image" >/dev/null 2>&1 || log "host image removal failed: $image"
      ;;
  esac
}

cleanup_repo_built_host_images() {
  local engine=""
  engine="$(resolve_local_image_engine)" || return 0
  remove_host_image "$engine" "$DEFAULT_APISHIM_IMAGE"
  remove_host_image "$engine" "$DEFAULT_DEMO_SHELL_IMAGE"
}

cmd_up() {
  require_local_sudo
  require_http_tools
  log "bringing up retained HA dashboard smoke stack run_id=${RUN_ID} variant=${variant_name}"
  "$SCRIPT_DIR/image_verify.sh" --variant all
  "$SCRIPT_DIR/host_prepare.sh" --variant "$VARIANT" --apply
  "$SCRIPT_DIR/variant_up.sh" --variant "$VARIANT" --run-id "$RUN_ID"
  ensure_core_seed_bundle
  "$SCRIPT_DIR/ha_shared_infra.sh" --variant "$VARIANT" --run-id "$RUN_ID" --execute
  AE_CRI_CACHE_SEED_MODE="${AE_CRI_CACHE_SEED_MODE:-required}" \
  AE_CRI_CACHE_SEED_MANIFEST="${AE_CRI_CACHE_SEED_MANIFEST:-$ROOT_DIR/lab/variants/cri_seed_images.lock.json}" \
  AE_CRI_CACHE_SEED_BUNDLE="${AE_CRI_CACHE_SEED_BUNDLE:-$seed_bundle_path}" \
    "$SCRIPT_DIR/k1s_bootstrap.sh" --variant "$VARIANT" --run-id "$RUN_ID" --execute
  check_stack_ready
  apply_retained_local_dev_hosts
  verify_retained_local_dev_hosts_applied
  cmd_status
}

cmd_status() {
  local auth_loaded=0
  local row="" name="" ip="" node_id=""
  local system_result="" system_json="" system_http_code="" leader_id="" member_count="" etcd_healthy="" etcd_unhealthy="" transport_backend=""
  local dash_code="" docs_code="" api_swagger_code="" api_redoc_code="" api_dashboard_code=""

  require_remote_hosts
  require_http_tools
  if load_local_auth_env; then
    auth_loaded=1
  fi

  printf 'Retained HA dashboard smoke\n'
  printf 'run_id=%s variant=%s\n' "$RUN_ID" "$variant_name"
  printf '\n'

  printf 'Public Envoy URLs\n'
  printf '  dashboard: https://%s:%s/dashboard\n' "$dash_host" "$ingress_tls_port"
  printf '  docs: https://%s:%s/\n' "$docs_host" "$ingress_tls_port"
  printf '  api swagger: https://%s:%s/swagger\n' "$api_host" "$ingress_tls_port"
  printf '  api redoc: https://%s:%s/redoc\n' "$api_host" "$ingress_tls_port"
  printf '\n'

  printf 'Local host mapping\n'
  printf '  getent hosts %s %s %s\n' "$dash_host" "$docs_host" "$api_host"
  printf '  expected after up: dash/docs/api resolve to %s from this host\n' "${DEV_LOCAL_HOSTS_IP:-$first_core_ip}"
  printf '  successful retained up verifies this host mapping before reporting success\n'
  printf '  purge/reset restore the prior managed local-dev mapping when one was captured\n'
  printf '\n'

  printf 'Public ingress smoke\n'
  for row in "${ha_host_rows[@]}"; do
    IFS=$'\t' read -r name ip node_id <<<"$row"
    dash_code="$(probe_resolved_https_status "$dash_host" "/dashboard" "$ip")"
    docs_code="$(probe_resolved_https_status "$docs_host" "/" "$ip")"
    api_swagger_code="$(probe_resolved_https_status "$api_host" "/swagger" "$ip")"
    api_redoc_code="$(probe_resolved_https_status "$api_host" "/redoc" "$ip")"
    api_dashboard_code="$(probe_resolved_https_status "$api_host" "/dashboard" "$ip")"
    printf '  %s: dash=%s docs=%s api_swagger=%s api_redoc=%s api_dashboard=%s\n' \
      "$name" "$dash_code" "$docs_code" "$api_swagger_code" "$api_redoc_code" "$api_dashboard_code"
  done
  printf '\n'

  printf 'Direct diagnostics\n'
  for row in "${ha_host_rows[@]}"; do
    IFS=$'\t' read -r name ip node_id <<<"$row"
    printf '  %s: controller=http://%s:%s/dashboard apishim=https://%s:%s\n' \
      "$name" "$ip" "$controller_port" "$ip" "$apishim_port"
  done
  printf '\n'

  printf 'Auth\n'
  printf '  source <(APISHIM_ENV_FILE=state/profiles/k1s-ha-core/apishim.env CONTROLLER_ENV_FILE=state/profiles/k1s-ha-core/controller.env bash scripts/ae-env.sh local)\n'
  if [[ "$auth_loaded" -eq 1 && -n "${AE_API_ADMIN_TOKEN:-}" ]]; then
    printf '  dashboard bearer: %s\n' "$AE_API_ADMIN_TOKEN"
  else
    printf '  dashboard bearer: unavailable\n'
  fi
  printf '  note: paste the dashboard bearer value into the dashboard Bearer field.\n'
  printf '  curl -sk --resolve %s:%s:%s -H "Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}" https://%s:%s/system | jq .\n' \
    "$api_host" "$ingress_tls_port" "$first_core_ip" "$api_host" "$ingress_tls_port"
  printf '  note: test dash/docs/api without auth first; bearer auth is only required for API reads like /system.\n'
  printf '  note: https://%s:%s/dashboard is expected to return 404; dashboard lives on %s.\n' \
    "$api_host" "$ingress_tls_port" "$dash_host"
  printf '\n'

  if [[ "$auth_loaded" -eq 1 ]]; then
    for row in "${ha_host_rows[@]}"; do
      IFS=$'\t' read -r name ip node_id <<<"$row"
      system_result="$(read_api_system_summary_with_retry "$ip" || true)"
      if [[ -z "$system_result" ]]; then
        printf '  %s: system=000 unavailable\n' "$name"
        continue
      fi
      system_http_code="${system_result%%$'\n'*}"
      system_json="${system_result#*$'\n'}"
      if [[ "$system_http_code" == "401" ]]; then
        printf '  %s: system=401 auth_required\n' "$name"
        continue
      fi
      if [[ "$system_http_code" == "403" ]]; then
        printf '  %s: system=403 forbidden\n' "$name"
        continue
      fi
      if [[ "$system_http_code" != "200" ]]; then
        printf '  %s: system=%s\n' "$name" "$system_http_code"
        continue
      fi
      leader_id="$(printf '%s' "$system_json" | jq -r '.ha.authority.leader_id // empty' 2>/dev/null || true)"
      member_count="$(printf '%s' "$system_json" | jq -r '.ha.authority.member_count // empty' 2>/dev/null || true)"
      if [[ -z "$leader_id" || -z "$member_count" ]]; then
        printf '  %s: system=200 ha=redacted_or_converging\n' "$name"
        continue
      fi
      etcd_healthy="$(printf '%s' "$system_json" | jq -r '.ha.etcd.healthy_endpoints // 0')"
      etcd_unhealthy="$(printf '%s' "$system_json" | jq -r '.ha.etcd.unhealthy_endpoints // 0')"
      transport_backend="$(printf '%s' "$system_json" | jq -r '.ha.transport.backend // "-"')"
      printf '  %s: leader=%s members=%s etcd=%s healthy/%s unhealthy transport=%s\n' \
        "$name" "$leader_id" "$member_count" "$etcd_healthy" "$etcd_unhealthy" "$transport_backend"
    done
  else
    printf 'System summary unavailable: missing state/profiles/k1s-ha-core/apishim.env or state/profiles/k1s-ha-core/controller.env\n'
  fi
  printf '\n'

  if [[ -n "$hub_node_id" ]]; then
    printf 'Hub node\n'
    printf '  %s: agent=http://%s:%s labels=%s\n' \
      "$hub_node_id" "$hub_node_ip" "$hub_node_agent_port" "$hub_node_labels"
  fi
}

cmd_workload_smoke() {
  require_remote_hosts
  require_http_tools
  [[ -n "$hub_node_id" ]] || { err "variant does not include a retained hub node"; exit 2; }
  [[ -f "$retained_workload_smoke_manifest" ]] || {
    err "retained workload smoke manifest not found: $retained_workload_smoke_manifest"
    exit 2
  }
  if ! wait_for_hub_node_registration; then
    err "retained hub node did not register as Ready"
    exit 1
  fi
  local -a label_args=()
  append_label_args_from_csv label_args "${hub_node_labels:-role=hub,site=hub}"
  if [[ "${#label_args[@]}" -eq 0 ]]; then
    label_args=(--label "role=hub" --label "site=hub")
  fi
  log "deploying retained HA workload smoke app=${retained_workload_smoke_app} on ${hub_node_id}"
  log "verifying core-local Envoy ingress host=${retained_workload_smoke_host}:${ingress_tls_port} via ${first_core_ip}"
  PYTHONPATH="$ROOT_DIR/src" \
    AE_STATE_BACKEND=etcd \
    AE_ETCD_ENDPOINTS="$ha_etcd_endpoints" \
    AE_ETCD_PREFIX="$ha_etcd_prefix" \
    "$(lab_python)" "$ROOT_DIR/scripts/dev/ha_core_node_smoke.py" ingress-smoke \
      --node-id "$hub_node_id" \
      "${label_args[@]}" \
      --manifest "$retained_workload_smoke_manifest" \
      --app-name "$retained_workload_smoke_app" \
      --ingress-host "$retained_workload_smoke_host" \
      --ingress-port "$ingress_tls_port" \
      --resolve-ip "$first_core_ip" \
      --direct-probe-host "$first_core_ip" \
      --health-path /healthz \
      --root-path / \
      --expected-text "$retained_workload_smoke_expected_text" \
      --timeout 180 \
      --poll 2 \
      --purge-history
}

cmd_core_workload_smoke() {
  require_remote_hosts
  require_http_tools
  [[ -n "$edge_runtime_id" ]] || { err "variant does not include an HA edge runtime node"; exit 2; }
  [[ -f "$ha_core_workload_smoke_manifest" ]] || {
    err "HA core workload smoke manifest not found: $ha_core_workload_smoke_manifest"
    exit 2
  }
  local -a label_args=()
  append_label_args_from_csv label_args "$edge_runtime_labels"
  [[ "${#label_args[@]}" -gt 0 ]] || { err "edge runtime node labels missing for ${edge_runtime_id}"; exit 2; }
  log "deploying HA core-proxy workload smoke app=${ha_core_workload_smoke_app} on ${edge_runtime_id}"
  log "verifying Envoy core-proxy host=${ha_core_workload_smoke_host}:${ingress_tls_port} via ${first_core_ip}"
  PYTHONPATH="$ROOT_DIR/src" \
    AE_STATE_BACKEND=etcd \
    AE_ETCD_ENDPOINTS="$ha_etcd_endpoints" \
    AE_ETCD_PREFIX="$ha_etcd_prefix" \
    "$(lab_python)" "$ROOT_DIR/scripts/dev/ha_core_node_smoke.py" ingress-smoke \
      --node-id "$edge_runtime_id" \
      "${label_args[@]}" \
      --manifest "$ha_core_workload_smoke_manifest" \
      --app-name "$ha_core_workload_smoke_app" \
      --ingress-host "$ha_core_workload_smoke_host" \
      --ingress-port "$ingress_tls_port" \
      --resolve-ip "$first_core_ip" \
      --health-path /healthz \
      --root-path / \
      --expected-text "$ha_core_workload_smoke_expected_text" \
      --timeout 180 \
      --poll 2 \
      --purge-history
}

cmd_down() {
  require_local_sudo
  if [[ "$PURGE" -eq 1 ]]; then
    cmd_purge
    return 0
  fi
  log "tearing down retained HA dashboard smoke stack run_id=${RUN_ID}"
  local down_args=(--variant "$VARIANT" --run-id "$RUN_ID")
  [[ "$DESTROY_NETWORK" -eq 1 ]] && down_args+=(--destroy-network)
  "$SCRIPT_DIR/variant_down.sh" "${down_args[@]}"
}

purge_retained_artifacts() {
  local state_dir="$ROOT_DIR/state/lab-vm/$RUN_ID"
  local run_path=""
  run_path="$(run_dir "$RUN_ID")"
  local down_args=(--variant "$VARIANT" --run-id "$RUN_ID" --best-effort)

  [[ "$DESTROY_NETWORK" -eq 1 ]] && down_args+=(--destroy-network)
  "$SCRIPT_DIR/variant_down.sh" "${down_args[@]}" || true
  restore_retained_local_dev_hosts
  if [[ -d "$state_dir" ]]; then
    if pgrep -f -- "$state_dir" >/dev/null 2>&1; then
      log "state dir still referenced by running processes; leaving in place: $state_dir"
    else
      rm -rf "$state_dir"
      log "purged state dir $state_dir"
    fi
  else
    log "state dir already absent: $state_dir"
  fi
  if [[ -d "$run_path" ]]; then
    rm -rf "$run_path"
    log "purged run dir $run_path"
  else
    log "run dir already absent: $run_path"
  fi
  cleanup_repo_built_host_images
}

cmd_purge() {
  require_local_sudo
  log "purging retained HA dashboard smoke artifacts run_id=${RUN_ID}"
  purge_retained_artifacts
}

cmd_reseed_core() {
  require_remote_hosts
  local -a selected_rows=()
  local row="" name="" ip="" role="" node_id=""
  mapfile -t selected_rows < <(reseed_target_rows "$TARGET")
  if [[ "${#selected_rows[@]}" -eq 0 ]]; then
    err "no hosts matched target=${TARGET} for reseed-core"
    exit 2
  fi
  log "rebuilding core seed bundle for run_id=${RUN_ID}"
  AE_CRI_CACHE_SEED_ENGINE="${AE_CRI_CACHE_SEED_ENGINE:-}" \
  AE_CRI_CACHE_SEED_ALWAYS_PULL="${AE_CRI_CACHE_SEED_ALWAYS_PULL:-0}" \
    "$SCRIPT_DIR/image_seed_bundle.sh" --run-id "$RUN_ID" --profile core --output "$seed_bundle_path"
  for row in "${selected_rows[@]}"; do
    [[ -n "$row" ]] || continue
    IFS=$'\t' read -r name ip role node_id <<<"$row"
    log "importing core seed bundle on ${name} (${ip})"
    run_remote_inline "$ip" "$(cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh
ensure_vm_bootstrap_prereqs
bundle="/mnt/host/state/lab-vm/${RUN_ID}/seeds/cri-seed-images.oci.tar"
if [[ ! -f "\$bundle" ]]; then
  echo "seed bundle missing: \$bundle" >&2
  exit 1
fi
sudo ctr -n k8s.io images import "\$bundle" >/dev/null
echo seed-import-complete
EOF
)"
  done
}

cmd_restart_core() {
  require_remote_hosts
  local -a selected_rows=()
  local row="" name="" ip="" node_id=""
  mapfile -t selected_rows < <(core_target_rows "$TARGET")
  if [[ "${#selected_rows[@]}" -eq 0 ]]; then
    err "no hosts matched target=${TARGET} for restart-core"
    exit 2
  fi
  for row in "${selected_rows[@]}"; do
    [[ -n "$row" ]] || continue
    IFS=$'\t' read -r name ip node_id <<<"$row"
    log "restarting controller on ${name} (${ip})"
    run_remote_inline "$ip" "$(cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh
ensure_vm_bootstrap_prereqs
controller_pattern='python3 -m ae.controller --loop --metrics-port ${controller_port}'
old_pids="\$(sudo pgrep -f -- "\$controller_pattern" | tr '\n' ' ' || true)"
if [[ -n "\$old_pids" ]]; then
  sudo pkill -TERM -f -- "\$controller_pattern" >/dev/null 2>&1 || true
fi
drain_deadline=\$((SECONDS + 45))
while (( SECONDS < drain_deadline )); do
  port_busy=0
  if ss -ltn | awk '\$4 ~ /:${controller_port}\$/ {found=1} END {exit(found?0:1)}'; then
    port_busy=1
  fi
  stale_pid=0
  for pid in \$old_pids; do
    if sudo kill -0 "\$pid" >/dev/null 2>&1; then
      stale_pid=1
      break
    fi
  done
  if (( stale_pid == 0 && port_busy == 0 )); then
    break
  fi
  sleep 1
done
if [[ -n "\$old_pids" ]]; then
  for pid in \$old_pids; do
    if sudo kill -0 "\$pid" >/dev/null 2>&1; then
      sudo kill -KILL "\$pid" >/dev/null 2>&1 || true
    fi
  done
fi
if ss -ltn | awk '\$4 ~ /:${controller_port}\$/ {found=1} END {exit(found?0:1)}'; then
  echo "controller port ${controller_port} is still busy after stop attempt" >&2
  exit 1
fi
cd /mnt/host
nohup sudo env \
  PYTHONPATH=/mnt/host/src \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_RUNTIME_HANDLER=\${AE_CRI_RUNTIME_HANDLER:-runc} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  DEV_PROFILE_DIR=/mnt/host/state/profiles/k1s-ha-core \
  AE_PROJECTION_ROOT=/mnt/host/state/profiles/k1s-ha-core/projections \
  AE_APISHIM_MODE=\${AE_APISHIM_MODE:-cri} \
  AE_APISHIM_PRESEEDED=1 \
  AE_APISHIM_IMAGE=\${AE_APISHIM_IMAGE:-${DEFAULT_APISHIM_IMAGE}} \
  AE_STATE_BACKEND=\${AE_STATE_BACKEND:-etcd} \
  AE_TRANSPORT_BACKEND=\${AE_TRANSPORT_BACKEND:-nats-js} \
  AE_JS_DOMAIN=\${AE_JS_DOMAIN:-K1S} \
  AE_NODE_PROFILE=\${AE_NODE_PROFILE:-k1s-ha-core} \
  AE_EDGE_INGRESS_MODE=\${AE_EDGE_INGRESS_MODE:-core-proxy} \
  AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS=\${AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS:-1} \
  AE_EDGE_INGRESS_CONFIG_DIR=\${AE_EDGE_INGRESS_CONFIG_DIR:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress} \
  AE_EDGE_INGRESS_ENVOY_CONFIG=\${AE_EDGE_INGRESS_ENVOY_CONFIG:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/envoy.yaml} \
  AE_RATHOLE_SERVER_CONFIG=\${AE_RATHOLE_SERVER_CONFIG:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/rathole-server.toml} \
  AE_RATHOLE_CLIENT_DIR=\${AE_RATHOLE_CLIENT_DIR:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/clients} \
  AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX=\${AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX:-edge.local} \
  AE_EDGE_INGRESS_LOCAL_ADDR=\${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081} \
  AE_EDGE_INGRESS_HTTP_PORT=\${AE_EDGE_INGRESS_HTTP_PORT:-10080} \
  AE_EDGE_INGRESS_TLS_PORT=\${AE_EDGE_INGRESS_TLS_PORT:-10443} \
  AE_EDGE_INGRESS_CORE_PROXY=\${AE_EDGE_INGRESS_CORE_PROXY:-1} \
  AE_EDGE_INGRESS_RATHOLE_RELOAD=\${AE_EDGE_INGRESS_RATHOLE_RELOAD:-1} \
  AE_EDGE_INGRESS_RELOAD_CMD="python3 /mnt/host/scripts/dev/cri_stack.py up-envoy --profile k1s-ha-core --config \${AE_EDGE_INGRESS_ENVOY_CONFIG:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/envoy.yaml}" \
  AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD="python3 /mnt/host/scripts/dev/cri_stack.py up-rathole-server --profile k1s-ha-core --config \${AE_RATHOLE_SERVER_CONFIG:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/rathole-server.toml}" \
  AE_RATHOLE_BIND_ADDR=\${AE_RATHOLE_BIND_ADDR:-0.0.0.0:2333} \
  AE_RATHOLE_DEFAULT_TOKEN=\${AE_RATHOLE_DEFAULT_TOKEN:-dev} \
  AE_RATHOLE_SERVER_ADDR=\${AE_RATHOLE_SERVER_ADDR:-127.0.0.1:2333} \
  AE_CONTROLPLANE_PUBLIC_ENABLE=\${AE_CONTROLPLANE_PUBLIC_ENABLE:-1} \
  AE_CONTROLPLANE_DASH_HOST=\${AE_CONTROLPLANE_DASH_HOST:-dash.home.arpa} \
  AE_CONTROLPLANE_DOCS_HOST=\${AE_CONTROLPLANE_DOCS_HOST:-docs.home.arpa} \
  AE_CONTROLPLANE_API_HOST=\${AE_CONTROLPLANE_API_HOST:-api.home.arpa} \
  AE_CONTROLPLANE_PROXY_ADDR=\${AE_CONTROLPLANE_PROXY_ADDR:-127.0.0.1} \
  AE_CONTROLPLANE_PROXY_PORT=\${AE_CONTROLPLANE_PROXY_PORT:-10081} \
  AE_CONTROLPLANE_CONTROLLER_UPSTREAM=\${AE_CONTROLPLANE_CONTROLLER_UPSTREAM:-127.0.0.1:${controller_port}} \
  AE_CONTROLPLANE_API_CONTROLLER_UPSTREAM=\${AE_CONTROLPLANE_API_CONTROLLER_UPSTREAM:-127.0.0.1:${controller_port}} \
  AE_CONTROLPLANE_APISHIM_UPSTREAM=\${AE_CONTROLPLANE_APISHIM_UPSTREAM:-127.0.0.1:${apishim_port}} \
  AE_CONTROLPLANE_API_APISHIM_UPSTREAM=\${AE_CONTROLPLANE_API_APISHIM_UPSTREAM:-127.0.0.1:${apishim_port}} \
  AE_CONTROLPLANE_API_APISHIM_TLS=\${AE_CONTROLPLANE_API_APISHIM_TLS:-1} \
  AE_ETCD_MAINTENANCE_ENABLE=\${AE_ETCD_MAINTENANCE_ENABLE:-0} \
  AE_ETCD_MAINTENANCE_THRESHOLD_PCT=\${AE_ETCD_MAINTENANCE_THRESHOLD_PCT:-80} \
  APISHIM_HOST=\${APISHIM_HOST:-0.0.0.0} \
  AE_HA_MODE=1 \
  AE_AGENT_API_PORT=${controller_agent_port} \
  AE_AGENT_API_TOKEN=${agent_token} \
  AE_CONTROLLER_ID=${node_id} \
  AE_CONTROLLER_ADVERTISE_ADDR=http://${ip}:${controller_port} \
  AE_ETCD_ENDPOINTS='${ha_etcd_endpoints}' \
  AE_APISHIM_ETCD_ENDPOINTS='${ha_etcd_endpoints}' \
  AE_ETCD_PREFIX='${ha_etcd_prefix}' \
  AE_NATS_URL='${ha_nats_url}' \
  APISHIM_PORT=${apishim_port} \
  python3 -m ae.controller --loop --metrics-port ${controller_port} > /home/ae/k1s-ha-core.log 2>&1 </dev/null &
new_pid=\$!
deadline=\$((SECONDS + 45))
while (( SECONDS < deadline )); do
  if ! kill -0 "\$new_pid" >/dev/null 2>&1; then
    echo "controller process exited early; tailing /home/ae/k1s-ha-core.log" >&2
    tail -n 80 /home/ae/k1s-ha-core.log >&2 || true
    exit 1
  fi
  if ss -ltn | awk '\$4 ~ /:${controller_port}\$/ {found=1} END {exit(found?0:1)}'; then
    echo controller-restart-complete
    exit 0
  fi
  sleep 1
done
echo "controller restart was not observed on port ${controller_port}" >&2
exit 1
EOF
)"
  done
}

cmd_restart_apishim() {
  require_remote_hosts
  local -a selected_rows=()
  local row="" name="" ip="" node_id=""
  mapfile -t selected_rows < <(core_target_rows "$TARGET")
  if [[ "${#selected_rows[@]}" -eq 0 ]]; then
    err "no hosts matched target=${TARGET} for restart-apishim"
    exit 2
  fi
  for row in "${selected_rows[@]}"; do
    [[ -n "$row" ]] || continue
    IFS=$'\t' read -r name ip node_id <<<"$row"
    log "restarting apishim on ${name} (${ip})"
    run_remote_inline "$ip" "$(cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh
ensure_vm_bootstrap_prereqs
sudo env \
  PYTHONPATH=/mnt/host/src \
  AE_CRI_ENDPOINT=\${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock} \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_RUNTIME_HANDLER=\${AE_CRI_RUNTIME_HANDLER:-runc} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  AE_APISHIM_IMAGE=\${AE_APISHIM_IMAGE:-${DEFAULT_APISHIM_IMAGE}} \
  python3 /mnt/host/scripts/dev/cri_stack.py up-apishim \
    --profile k1s-ha-core \
    --host 0.0.0.0 \
    --port ${apishim_port} \
    --env-file /mnt/host/state/profiles/k1s-ha-core/apishim.env \
    --cert-file /mnt/host/state/profiles/k1s-ha-core/apishim.crt \
    --key-file /mnt/host/state/profiles/k1s-ha-core/apishim.key \
    --recreate
deadline=\$((SECONDS + 45))
while (( SECONDS < deadline )); do
  if ss -ltn | awk '\$4 ~ /:${apishim_port}\$/ {found=1} END {exit(found?0:1)}'; then
    echo apishim-restart-complete
    exit 0
  fi
  sleep 1
done
echo "apishim restart was not observed on port ${apishim_port}" >&2
exit 1
EOF
)"
  done
}

cmd_restart_hub_node() {
  require_remote_hosts
  [[ -n "$hub_node_name" ]] || { err "variant does not include a retained hub node"; exit 2; }
  log "restarting hub node on ${hub_node_name} (${hub_node_ip})"
  run_remote_inline "$hub_node_ip" "$(cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh
ensure_vm_bootstrap_prereqs
sudo pkill -f -- 'k1s-core-node|ae\.node' >/dev/null 2>&1 || true
sleep 2
cd /mnt/host
nohup sudo env \
  PYTHON_BIN=python3 \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID=${hub_node_id} \
  AE_NODE_LABELS='${hub_node_labels}' \
  AE_ROSENPASS_ENABLED=\${AE_ROSENPASS_ENABLED:-0} \
  AE_CONTROLLER_URL=http://${first_core_ip}:${controller_agent_port} \
  AE_AGENT_ENDPOINT=http://${hub_node_ip}:${hub_node_agent_port} \
  AE_AGENT_TOKEN=${agent_token} \
  AE_NODE_PORT=${hub_node_agent_port} \
  make k1s-core-node > /home/ae/k1s-core-node.log 2>&1 </dev/null &
deadline=\$((SECONDS + 45))
while (( SECONDS < deadline )); do
  if ss -ltn | awk '\$4 ~ /:${hub_node_agent_port}\$/ {found=1} END {exit(found?0:1)}'; then
    echo hub-node-restart-complete
    exit 0
  fi
  if pgrep -f 'k1s-core-node|ae\.node' >/dev/null 2>&1; then
    echo hub-node-process-running
    exit 0
  fi
  sleep 1
done
echo "hub node restart was not observed" >&2
exit 1
EOF
)"
}

cmd_refresh_all() {
  local previous_target="$TARGET"
  TARGET=all
  cmd_reseed_core
  cmd_restart_apishim
  cmd_restart_core
  TARGET="$previous_target"
  cmd_restart_hub_node
  check_stack_ready
  cmd_status
}

cmd_reset() {
  require_local_sudo
  require_http_tools
  if [[ "$REBUILD_IMAGES" -eq 1 ]]; then
    log "rebuilding VM images before retained stack reset"
    "$SCRIPT_DIR/image_build.sh" --variant all
    "$SCRIPT_DIR/image_verify.sh" --variant all
  fi
  purge_retained_artifacts
  cmd_up
}

case "$SUBCOMMAND" in
  up)
    cmd_up
    ;;
  status)
    cmd_status
    ;;
  workload-smoke)
    cmd_workload_smoke
    ;;
  core-workload-smoke)
    cmd_core_workload_smoke
    ;;
  down)
    cmd_down
    ;;
  purge)
    cmd_purge
    ;;
  reseed-core)
    cmd_reseed_core
    ;;
  restart-core)
    cmd_restart_core
    ;;
  restart-apishim)
    cmd_restart_apishim
    ;;
  restart-hub-node)
    cmd_restart_hub_node
    ;;
  refresh-all)
    cmd_refresh_all
    ;;
  reset)
    cmd_reset
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    err "unknown subcommand: $SUBCOMMAND"
    usage
    exit 2
    ;;
esac
