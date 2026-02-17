#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

MATRIX_SCRIPT="${MATRIX_SCRIPT:-$ROOT_DIR/scripts/dev/test_ingress_matrix_single_host.sh}"
VALIDATE_SCRIPT="${VALIDATE_SCRIPT:-$ROOT_DIR/scripts/dev/validate_ingress_env.sh}"
ETCD_MAINTENANCE_SCRIPT="${ETCD_MAINTENANCE_SCRIPT:-$ROOT_DIR/scripts/dev/etcd_maintenance.sh}"
SECURITY_BASELINE_SCRIPT="${SECURITY_BASELINE_SCRIPT:-$ROOT_DIR/scripts/dev/security_baseline_check.sh}"
SECURITY_ACTIVE_SCRIPT="${SECURITY_ACTIVE_SCRIPT:-$ROOT_DIR/scripts/dev/security_active_tests.sh}"

SITE_ID="${SITE_ID:-sea-edge-02}"
NODE_ID="${NODE_ID:-edge-1}"

LANES_CSV="${LANES_CSV:-core-proxy,core-to-edge-public,edge-local}"
PROMPT_CHECKPOINTS=1
KEEP_GOING=0
SKIP_ETCD_WATCHDOG=0
SKIP_ENV_VALIDATE=0
RUN_SECURITY_BASELINE=0
RUN_SECURITY_ACTIVE=0

CORE_PROXY_ARCHETYPES="${CORE_PROXY_ARCHETYPES:-ws-echo,lb-distribution,sticky-cookie}"
CORE_PROXY_TIER="${CORE_PROXY_TIER:-tier2}"
CORE_PROXY_VALIDATION_PROFILE="${CORE_PROXY_VALIDATION_PROFILE:-deep+perf}"
CORE_PROXY_PERF_PROFILE="${CORE_PROXY_PERF_PROFILE:-sample}"
CORE_PROXY_LB_PROOF_SCOPE="${CORE_PROXY_LB_PROOF_SCOPE:-auto}"

PUBLIC_ARCHETYPES="${PUBLIC_ARCHETYPES:-http-static,http-path-routing}"
PUBLIC_TIER="${PUBLIC_TIER:-tier1}"
PUBLIC_VALIDATION_PROFILE="${PUBLIC_VALIDATION_PROFILE:-standard}"

EDGE_LOCAL_ARCHETYPES="${EDGE_LOCAL_ARCHETYPES:-lb-distribution}"
EDGE_LOCAL_TIER="${EDGE_LOCAL_TIER:-tier2}"
EDGE_LOCAL_VALIDATION_PROFILE="${EDGE_LOCAL_VALIDATION_PROFILE:-deep}"
EDGE_LOCAL_LB_PROOF_SCOPE="${EDGE_LOCAL_LB_PROOF_SCOPE:-edge-only}"
EDGE_LOCAL_LB_SAMPLE_REQUESTS="${EDGE_LOCAL_LB_SAMPLE_REQUESTS:-5000}"
EDGE_LOCAL_LB_MIN_BACKENDS="${EDGE_LOCAL_LB_MIN_BACKENDS:-2}"
EDGE_LOCAL_LB_MAX_SKEW_RATIO="${EDGE_LOCAL_LB_MAX_SKEW_RATIO:-0.35}"
EDGE_LOCAL_LISTENER_URL="${EDGE_LOCAL_LISTENER_URL:-}"
REQUIRED_EDGE_LOCAL_UPSTREAM_MODE="${REQUIRED_EDGE_LOCAL_UPSTREAM_MODE:-bundle-endpoints}"

CORE_START_CORE_PROXY_CMD="${CORE_START_CORE_PROXY_CMD:-AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core}"
CORE_START_PUBLIC_CMD="${CORE_START_PUBLIC_CMD:-AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-to-edge-public make k1s-core}"
CORE_START_EDGE_LOCAL_CMD="${CORE_START_EDGE_LOCAL_CMD:-AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=edge-local AE_ROUTE_BUNDLE_ENABLED=1 make k1s-core}"
GATEWAY_START_CORE_PROXY_CMD="${GATEWAY_START_CORE_PROXY_CMD:-AE_SITE_ID=${SITE_ID} AE_NODE_ID=${NODE_ID} EDGE_INGRESS_MODE=core-proxy make k1s-edge-core}"
GATEWAY_START_EDGE_LOCAL_CMD="${GATEWAY_START_EDGE_LOCAL_CMD:-AE_SITE_ID=${SITE_ID} AE_NODE_ID=${NODE_ID} EDGE_INGRESS_MODE=edge-local AE_EDGE_LOCAL_UPSTREAM_MODE=${REQUIRED_EDGE_LOCAL_UPSTREAM_MODE} AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=state/profiles/k1s-core/edge-local make k1s-edge-core}"

usage() {
  cat <<'USAGE'
Usage: scripts/dev/run_ingress_mode_lanes.sh [options]

Runs mode-isolated ingress verification lanes with explicit restart checkpoints
between lanes. The script does not auto-restart stacks; it prints required
restart commands and hard-fails when runtime mode/env preconditions are not met.

Options:
  --lanes <csv>                  Lanes to run (default: core-proxy,core-to-edge-public,edge-local)
                                 Allowed: core-proxy,core-to-edge-public,edge-local,all
  --site-id <id>                 Site ID used for gateway checks (default: sea-edge-02)
  --node-id <id>                 Node ID used for gateway checks (default: edge-1)
  --yes                          Do not pause at checkpoints
  --keep-going                   Continue remaining lanes if one lane fails
  --skip-etcd-watchdog           Skip etcd watchdog preflight before each lane
  --skip-env-validate            Skip validate_ingress_env preflight before each lane
  --security-baseline            Run security baseline checks after each lane
  --security-active              Run staged active security probes after each lane
  --security-all                 Run baseline + active security checks after each lane
  --validate-script <path>       Override preflight script path
  --security-baseline-script <path>
                                 Override baseline script path
  --security-active-script <path>
                                 Override active security script path
  -h, --help                     Show help

Environment overrides:
  MATRIX_SCRIPT                  Matrix runner path
  ETCD_MAINTENANCE_SCRIPT        etcd maintenance helper path
  VALIDATE_SCRIPT                Ingress preflight validator path
  SECURITY_BASELINE_SCRIPT       Security baseline script path
  SECURITY_ACTIVE_SCRIPT         Security active test script path

  CORE_START_CORE_PROXY_CMD      Printed core restart command for core-proxy lane
  CORE_START_PUBLIC_CMD          Printed core restart command for core-to-edge-public lane
  CORE_START_EDGE_LOCAL_CMD      Printed core restart command for edge-local lane
  GATEWAY_START_CORE_PROXY_CMD   Printed gateway restart command for core-proxy/public lanes
  GATEWAY_START_EDGE_LOCAL_CMD   Printed gateway restart command for edge-local lane
  REQUIRED_EDGE_LOCAL_UPSTREAM_MODE
                                Required gateway AE_EDGE_LOCAL_UPSTREAM_MODE (default: bundle-endpoints)

  CORE_PROXY_* / PUBLIC_* / EDGE_LOCAL_*
    Per-lane test knobs (archetypes, tier, validation profile, etc.).
USAGE
}

log() {
  printf '[ingress-lanes] %s\n' "$*"
}

die() {
  printf '[ingress-lanes] ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

trim() {
  local s="${1:-}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

split_csv() {
  local csv="$1"
  local -n out_ref="$2"
  local raw
  IFS=',' read -r -a raw <<<"$csv"
  out_ref=()
  local item
  for item in "${raw[@]}"; do
    item="$(trim "$item")"
    [[ -n "$item" ]] && out_ref+=("$item")
  done
}

ensure_edge_local_listener_url() {
  if [[ -n "$EDGE_LOCAL_LISTENER_URL" ]]; then
    return 0
  fi
  local archetypes=()
  split_csv "$EDGE_LOCAL_ARCHETYPES" archetypes
  local first="${archetypes[0]:-}"
  [[ -n "$first" ]] || return 0
  EDGE_LOCAL_LISTENER_URL="https://${first}-edge-local.home.arpa/"
  log "edge-local listener URL defaulted to $EDGE_LOCAL_LISTENER_URL"
}

validate_lane() {
  case "$1" in
    core-proxy|core-to-edge-public|edge-local) ;;
    *) die "invalid lane '$1'" ;;
  esac
}

read_environ_raw() {
  local pid="$1"
  if [[ -r "/proc/${pid}/environ" ]]; then
    if cat "/proc/${pid}/environ" 2>/dev/null; then
      return 0
    fi
  fi
  if sudo -n true >/dev/null 2>&1; then
    if sudo -n cat "/proc/${pid}/environ" 2>/dev/null; then
      return 0
    fi
  fi
  die "cannot read /proc/${pid}/environ; run 'sudo -v' before running lane checks"
}

proc_env_get() {
  local pid="$1"
  local key="$2"
  local line

  while IFS= read -r line; do
    [[ "$line" == "${key}="* ]] || continue
    printf '%s' "${line#*=}"
    return 0
  done < <(read_environ_raw "$pid" | tr '\0' '\n')

  return 1
}

find_controller_pid() {
  local pid
  pid="$(pgrep -f 'python -m ae.controller' | head -n1 || true)"
  [[ -n "$pid" ]] || die "controller is not running"
  printf '%s' "$pid"
}

find_gateway_pid() {
  local pids=()
  mapfile -t pids < <(pgrep -f 'python -m ae.gateway' || true)
  (( ${#pids[@]} > 0 )) || die "gateway is not running"

  local pid cmdline
  for pid in "${pids[@]}"; do
    if [[ -r "/proc/${pid}/cmdline" ]]; then
      cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
    else
      cmdline="$(sudo -n cat "/proc/${pid}/cmdline" 2>/dev/null | tr '\0' ' ' || true)"
    fi
    if [[ "$cmdline" == *"${SITE_ID}"* ]] || [[ "$cmdline" == *"${NODE_ID}"* ]]; then
      printf '%s' "$pid"
      return 0
    fi
  done

  printf '%s' "${pids[0]}"
}

ss_has_port() {
  local port="$1"
  ss -ltn 2>/dev/null | rg -q ":${port}\\b"
}

checkpoint_prompt() {
  local lane="$1"
  if (( PROMPT_CHECKPOINTS == 0 )); then
    return 0
  fi
  printf '\n'
  printf '[ingress-lanes] Lane checkpoint: %s\n' "$lane"
  printf '[ingress-lanes] Press Enter when restart steps are complete, or type q to abort: '
  local reply
  IFS= read -r reply
  if [[ "$reply" == "q" || "$reply" == "Q" ]]; then
    die "aborted at lane checkpoint: $lane"
  fi
}

print_checkpoint() {
  local lane="$1"
  printf '\n'
  printf '============================================================\n'
  printf 'Lane: %s\n' "$lane"
  printf 'Required stack mode:\n'
  case "$lane" in
    core-proxy)
      printf '  controller EDGE_INGRESS_MODE=core-proxy\n'
      printf '  gateway    EDGE_INGRESS_MODE=core-proxy\n'
      printf 'Suggested restart commands:\n'
      printf '  core:    %s\n' "$CORE_START_CORE_PROXY_CMD"
      printf '  gateway: %s\n' "$GATEWAY_START_CORE_PROXY_CMD"
      ;;
    core-to-edge-public)
      printf '  controller EDGE_INGRESS_MODE=core-to-edge-public\n'
      printf '  gateway    usually stays core-proxy\n'
      printf 'Suggested restart commands:\n'
      printf '  core:    %s\n' "$CORE_START_PUBLIC_CMD"
      printf '  gateway: %s\n' "$GATEWAY_START_CORE_PROXY_CMD"
      ;;
    edge-local)
      printf '  controller EDGE_INGRESS_MODE=edge-local and AE_ROUTE_BUNDLE_ENABLED=1\n'
      printf '  gateway    EDGE_INGRESS_MODE=edge-local, AE_EDGE_LOCAL_UPSTREAM_MODE=%s, and AE_EDGE_LOCAL_INGRESS_CONFIG_DIR set\n' "$REQUIRED_EDGE_LOCAL_UPSTREAM_MODE"
      printf 'Suggested restart commands:\n'
      printf '  core:    %s\n' "$CORE_START_EDGE_LOCAL_CMD"
      printf '  gateway: %s\n' "$GATEWAY_START_EDGE_LOCAL_CMD"
      ;;

  esac
  printf '============================================================\n'
}

preflight_error() {
  printf '[ingress-lanes] preflight: %s\n' "$*" >&2
}

verify_lane_preconditions() {
  local lane="$1"
  local controller_pid gateway_pid
  controller_pid="$(find_controller_pid)"
  gateway_pid="$(find_gateway_pid)"

  local controller_mode gateway_mode bundle_enabled edge_local_dir gateway_upstream_mode
  controller_mode="$(proc_env_get "$controller_pid" EDGE_INGRESS_MODE || true)"
  gateway_mode="$(proc_env_get "$gateway_pid" EDGE_INGRESS_MODE || true)"
  bundle_enabled="$(proc_env_get "$controller_pid" AE_ROUTE_BUNDLE_ENABLED || true)"
  edge_local_dir="$(proc_env_get "$gateway_pid" AE_EDGE_LOCAL_INGRESS_CONFIG_DIR || true)"
  gateway_upstream_mode="$(proc_env_get "$gateway_pid" AE_EDGE_LOCAL_UPSTREAM_MODE || true)"

  local ok=1

  if [[ -z "$controller_mode" ]]; then
    preflight_error "controller missing EDGE_INGRESS_MODE"
    ok=0
  fi
  if [[ -z "$gateway_mode" ]]; then
    preflight_error "gateway missing EDGE_INGRESS_MODE"
    ok=0
  fi

  case "$lane" in
    core-proxy)
      if [[ "$controller_mode" != "core-proxy" ]]; then
        preflight_error "controller EDGE_INGRESS_MODE=$controller_mode (expected core-proxy)"
        ok=0
      fi
      if [[ "$gateway_mode" != "core-proxy" ]]; then
        preflight_error "gateway EDGE_INGRESS_MODE=$gateway_mode (expected core-proxy)"
        ok=0
      fi
      if ! ss_has_port 10080; then
        preflight_error "missing core ingress listener 10080"
        ok=0
      fi
      if ! ss_has_port 10443; then
        preflight_error "missing core ingress listener 10443"
        ok=0
      fi
      ;;
    core-to-edge-public)
      if [[ "$controller_mode" != "core-to-edge-public" ]]; then
        preflight_error "controller EDGE_INGRESS_MODE=$controller_mode (expected core-to-edge-public)"
        ok=0
      fi
      if [[ "$gateway_mode" != "core-proxy" && "$gateway_mode" != "core-to-edge-public" ]]; then
        preflight_error "gateway EDGE_INGRESS_MODE=$gateway_mode (expected core-proxy or core-to-edge-public)"
        ok=0
      fi
      if ! ss_has_port 10080; then
        preflight_error "missing core ingress listener 10080"
        ok=0
      fi
      if ! ss_has_port 10443; then
        preflight_error "missing core ingress listener 10443"
        ok=0
      fi
      ;;
    edge-local)
      if [[ "$controller_mode" != "edge-local" ]]; then
        preflight_error "controller EDGE_INGRESS_MODE=$controller_mode (expected edge-local)"
        ok=0
      fi
      if [[ "$bundle_enabled" != "1" ]]; then
        preflight_error "controller AE_ROUTE_BUNDLE_ENABLED=$bundle_enabled (expected 1)"
        ok=0
      fi
      if [[ "$gateway_mode" != "edge-local" ]]; then
        preflight_error "gateway EDGE_INGRESS_MODE=$gateway_mode (expected edge-local)"
        ok=0
      fi
      if [[ -z "$edge_local_dir" ]]; then
        preflight_error "gateway missing AE_EDGE_LOCAL_INGRESS_CONFIG_DIR"
        ok=0
      elif [[ ! -d "$edge_local_dir" ]]; then
        preflight_error "gateway AE_EDGE_LOCAL_INGRESS_CONFIG_DIR does not exist: $edge_local_dir"
        ok=0
      fi
      if [[ -z "$gateway_upstream_mode" ]]; then
        preflight_error "gateway missing AE_EDGE_LOCAL_UPSTREAM_MODE"
        ok=0
      elif [[ "$gateway_upstream_mode" != "$REQUIRED_EDGE_LOCAL_UPSTREAM_MODE" ]]; then
        preflight_error "gateway AE_EDGE_LOCAL_UPSTREAM_MODE=$gateway_upstream_mode (expected $REQUIRED_EDGE_LOCAL_UPSTREAM_MODE)"
        ok=0
      fi
      ;;
  esac

  if (( ok == 0 )); then
    return 1
  fi

  log "preflight OK lane=$lane controller_mode=$controller_mode gateway_mode=$gateway_mode"
  return 0
}

run_env_validate() {
  local lane="$1"
  if (( SKIP_ENV_VALIDATE == 1 )); then
    log "skipping ingress environment validation"
    return 0
  fi

  [[ -x "$VALIDATE_SCRIPT" ]] || die "missing executable validator: $VALIDATE_SCRIPT"
  "$VALIDATE_SCRIPT" \
    --lane "$lane" \
    --site-id "$SITE_ID" \
    --node-id "$NODE_ID" \
    --skip-etcd
}

run_etcd_watchdog() {
  if (( SKIP_ETCD_WATCHDOG == 1 )); then
    log "skipping etcd watchdog preflight"
    return 0
  fi
  [[ -x "$ETCD_MAINTENANCE_SCRIPT" ]] || die "missing executable etcd helper: $ETCD_MAINTENANCE_SCRIPT"
  "$ETCD_MAINTENANCE_SCRIPT" watchdog
}

run_security_checks() {
  local lane="$1"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"

  if (( RUN_SECURITY_BASELINE == 1 )); then
    [[ -x "$SECURITY_BASELINE_SCRIPT" ]] || die "missing executable security baseline script: $SECURITY_BASELINE_SCRIPT"
    local baseline_json="$ROOT_DIR/state/test-results/security-baseline-${lane}-${stamp}.json"
    log "running security baseline lane=$lane result_json=$baseline_json"
    "$SECURITY_BASELINE_SCRIPT" --result-json "$baseline_json"
  fi

  if (( RUN_SECURITY_ACTIVE == 1 )); then
    [[ -x "$SECURITY_ACTIVE_SCRIPT" ]] || die "missing executable security active script: $SECURITY_ACTIVE_SCRIPT"
    local active_json="$ROOT_DIR/state/test-results/security-active-${lane}-${stamp}.json"
    log "running security active lane=$lane result_json=$active_json"
    "$SECURITY_ACTIVE_SCRIPT" --result-json "$active_json"
  fi
}

run_lane_test() {
  local lane="$1"

  case "$lane" in
    core-proxy)
      CORE_PROXY_FORCE_RATHOLE_RESTART=0 "$MATRIX_SCRIPT" \
        --modes core-proxy \
        --archetypes "$CORE_PROXY_ARCHETYPES" \
        --tier "$CORE_PROXY_TIER" \
        --validation-profile "$CORE_PROXY_VALIDATION_PROFILE" \
        --perf-profile "$CORE_PROXY_PERF_PROFILE" \
        --lb-proof-scope "$CORE_PROXY_LB_PROOF_SCOPE"
      ;;
    core-to-edge-public)
      "$MATRIX_SCRIPT" \
        --modes core-to-edge-public \
        --archetypes "$PUBLIC_ARCHETYPES" \
        --tier "$PUBLIC_TIER" \
        --validation-profile "$PUBLIC_VALIDATION_PROFILE"
      ;;
    edge-local)
      ensure_edge_local_listener_url
      local edge_local_cmd=("$MATRIX_SCRIPT" \
        --modes edge-local \
        --archetypes "$EDGE_LOCAL_ARCHETYPES" \
        --tier "$EDGE_LOCAL_TIER" \
        --validation-profile "$EDGE_LOCAL_VALIDATION_PROFILE" \
        --lb-proof-scope "$EDGE_LOCAL_LB_PROOF_SCOPE" \
        --lb-sample-requests "$EDGE_LOCAL_LB_SAMPLE_REQUESTS" \
        --lb-min-backends "$EDGE_LOCAL_LB_MIN_BACKENDS" \
        --lb-max-skew-ratio "$EDGE_LOCAL_LB_MAX_SKEW_RATIO")
      if [[ -n "$EDGE_LOCAL_LISTENER_URL" ]]; then
        edge_local_cmd+=(--edge-local-listener-url "$EDGE_LOCAL_LISTENER_URL")
      fi
      "${edge_local_cmd[@]}"
      ;;
    *)
      die "unknown lane: $lane"
      ;;
  esac
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lanes)
        LANES_CSV="$2"
        shift 2
        ;;
      --site-id)
        SITE_ID="$2"
        shift 2
        ;;
      --node-id)
        NODE_ID="$2"
        shift 2
        ;;
      --yes)
        PROMPT_CHECKPOINTS=0
        shift
        ;;
      --keep-going)
        KEEP_GOING=1
        shift
        ;;
      --skip-etcd-watchdog)
        SKIP_ETCD_WATCHDOG=1
        shift
        ;;
      --skip-env-validate)
        SKIP_ENV_VALIDATE=1
        shift
        ;;
      --security-baseline)
        RUN_SECURITY_BASELINE=1
        shift
        ;;
      --security-active)
        RUN_SECURITY_ACTIVE=1
        shift
        ;;
      --security-all)
        RUN_SECURITY_BASELINE=1
        RUN_SECURITY_ACTIVE=1
        shift
        ;;
      --validate-script)
        VALIDATE_SCRIPT="$2"
        shift 2
        ;;
      --security-baseline-script)
        SECURITY_BASELINE_SCRIPT="$2"
        shift 2
        ;;
      --security-active-script)
        SECURITY_ACTIVE_SCRIPT="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done
}

main() {
  parse_args "$@"

  need_cmd ss
  need_cmd rg
  [[ -x "$MATRIX_SCRIPT" ]] || die "missing executable matrix script: $MATRIX_SCRIPT"

  local lanes=()
  split_csv "$LANES_CSV" lanes
  (( ${#lanes[@]} > 0 )) || die "no lanes selected"

  if (( ${#lanes[@]} == 1 )) && [[ "${lanes[0]}" == "all" ]]; then
    lanes=(core-proxy core-to-edge-public edge-local)
  fi

  local lane
  for lane in "${lanes[@]}"; do
    validate_lane "$lane"
  done

  local failed=0
  for lane in "${lanes[@]}"; do
    print_checkpoint "$lane"
    checkpoint_prompt "$lane"

    if ! run_env_validate "$lane"; then
      if (( KEEP_GOING == 1 )); then
        failed=$((failed + 1))
        continue
      fi
      exit 1
    fi

    if ! verify_lane_preconditions "$lane"; then
      if (( KEEP_GOING == 1 )); then
        failed=$((failed + 1))
        continue
      fi
      exit 1
    fi

    if ! run_etcd_watchdog; then
      if (( KEEP_GOING == 1 )); then
        failed=$((failed + 1))
        continue
      fi
      exit 1
    fi

    log "running lane test: $lane"
    if run_lane_test "$lane"; then
      log "lane PASS: $lane"
    else
      log "lane FAIL: $lane"
      if (( KEEP_GOING == 1 )); then
        failed=$((failed + 1))
        continue
      fi
      exit 1
    fi

    if ! run_security_checks "$lane"; then
      log "security checks FAIL: lane=$lane"
      if (( KEEP_GOING == 1 )); then
        failed=$((failed + 1))
        continue
      fi
      exit 1
    fi
  done

  if (( failed > 0 )); then
    die "completed with failures: $failed lane(s)"
  fi

  log "all requested lanes passed"
}

main "$@"
