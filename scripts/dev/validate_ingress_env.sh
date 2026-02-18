#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

LANE="${LANE:-any}"
SITE_ID="${SITE_ID:-sea-edge-02}"
NODE_ID="${NODE_ID:-edge-1}"
CORE_SPECS_DIR="${CORE_SPECS_DIR:-$ROOT_DIR/state/profiles/k1s-core/specs}"
ETCD_MAINTENANCE_SCRIPT="${ETCD_MAINTENANCE_SCRIPT:-$ROOT_DIR/scripts/dev/etcd_maintenance.sh}"
MIN_INOTIFY_FREE_PCT="${MIN_INOTIFY_FREE_PCT:-5}"
REQUIRED_EDGE_LOCAL_UPSTREAM_MODE="${REQUIRED_EDGE_LOCAL_UPSTREAM_MODE:-bundle-endpoints}"
RESULT_JSON="${RESULT_JSON:-}"
RUN_ETCD_WATCHDOG=0
SKIP_MODE=0
SKIP_SPECS=0
SKIP_INOTIFY=0
SKIP_ETCD=0

FAILED=0
CHECK_FILE="$(mktemp)"
trap 'rm -f "$CHECK_FILE"' EXIT

usage() {
  cat <<'USAGE'
Usage: scripts/dev/validate_ingress_env.sh [options]

Runs ingress preflight checks before matrix/lane execution.

Options:
  --lane <lane>                  any|core-proxy|core-to-edge-public|edge-local (default: any)
  --site-id <id>                 Site id for gateway targeting (default: sea-edge-02)
  --node-id <id>                 Node id for gateway targeting (default: edge-1)
  --core-specs-dir <path>        Core specs dir to validate writable (default: state/profiles/k1s-core/specs)
  --min-inotify-free-pct <n>     Minimum free inotify watch percentage (default: 5)
  --result-json <path>           Write machine-readable result JSON
  --watchdog                     Run etcd watchdog before status check
  --skip-mode                    Skip process mode/env checks
  --skip-specs                   Skip core specs dir checks
  --skip-inotify                 Skip inotify capacity checks
  --skip-etcd                    Skip etcd status/alarm checks
  -h, --help                     Show help
USAGE
}

log() {
  printf '[ingress-preflight] %s\n' "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

record_check() {
  local status="$1"
  local severity="$2"
  local code="$3"
  local message="$4"
  printf '%s\t%s\t%s\t%s\n' "$status" "$severity" "$code" "$message" >>"$CHECK_FILE"
  case "$status" in
    fail|error) FAILED=1 ;;
  esac
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

find_controller_pid() {
  pgrep -f 'python -m ae.controller' | head -n1 || true
}

find_gateway_pid() {
  local pids=()
  mapfile -t pids < <(pgrep -f 'python -m ae.gateway' || true)
  if (( ${#pids[@]} == 0 )); then
    return 1
  fi

  local pid cmdline
  for pid in "${pids[@]}"; do
    if [[ -r "/proc/${pid}/cmdline" ]]; then
      cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
    elif sudo -n true >/dev/null 2>&1; then
      cmdline="$(sudo -n cat "/proc/${pid}/cmdline" 2>/dev/null | tr '\0' ' ' || true)"
    else
      cmdline=""
    fi
    if [[ "$cmdline" == *"${SITE_ID}"* ]] || [[ "$cmdline" == *"${NODE_ID}"* ]]; then
      printf '%s\n' "$pid"
      return 0
    fi
  done

  printf '%s\n' "${pids[0]}"
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
  return 1
}

proc_env_get() {
  local pid="$1"
  local key="$2"
  local line

  while IFS= read -r line; do
    [[ "$line" == "${key}="* ]] || continue
    printf '%s' "${line#*=}"
    return 0
  done < <(read_environ_raw "$pid" 2>/dev/null | tr '\0' '\n')

  return 1
}

listener_present() {
  local port="$1"
  ss -ltn 2>/dev/null | rg -q ":${port}\\b"
}

check_mode_requirements() {
  local controller_pid gateway_pid
  controller_pid="$(find_controller_pid)"
  if [[ -z "$controller_pid" ]]; then
    record_check fail high controller_missing "controller process not found"
    return
  fi
  record_check pass info controller_present "controller pid=${controller_pid}"

  gateway_pid="$(find_gateway_pid || true)"
  if [[ -z "$gateway_pid" ]]; then
    record_check fail high gateway_missing "gateway process not found"
    return
  fi
  record_check pass info gateway_present "gateway pid=${gateway_pid}"

  local controller_mode gateway_mode bundle_enabled edge_local_dir gateway_upstream_mode
  controller_mode="$(proc_env_get "$controller_pid" EDGE_INGRESS_MODE || true)"
  gateway_mode="$(proc_env_get "$gateway_pid" EDGE_INGRESS_MODE || true)"
  bundle_enabled="$(proc_env_get "$controller_pid" AE_ROUTE_BUNDLE_ENABLED || true)"
  gateway_upstream_mode="$(proc_env_get "$gateway_pid" AE_EDGE_LOCAL_UPSTREAM_MODE || true)"
  edge_local_dir="$(proc_env_get "$gateway_pid" AE_EDGE_LOCAL_INGRESS_CONFIG_DIR || true)"

  if [[ -z "$controller_mode" ]]; then
    record_check fail high controller_mode_missing "controller missing EDGE_INGRESS_MODE"
  else
    record_check pass info controller_mode "controller EDGE_INGRESS_MODE=${controller_mode}"
  fi

  if [[ -z "$gateway_mode" ]]; then
    record_check fail high gateway_mode_missing "gateway missing EDGE_INGRESS_MODE"
  else
    record_check pass info gateway_mode "gateway EDGE_INGRESS_MODE=${gateway_mode}"
  fi

  case "$LANE" in
    any)
      ;;
    core-proxy)
      if [[ "$controller_mode" != "core-proxy" ]]; then
        record_check fail high lane_controller_mode "controller mode must be core-proxy for lane core-proxy"
      else
        record_check pass info lane_controller_mode "controller mode matches lane"
      fi
      if [[ "$gateway_mode" != "core-proxy" ]]; then
        record_check fail high lane_gateway_mode "gateway mode must be core-proxy for lane core-proxy"
      else
        record_check pass info lane_gateway_mode "gateway mode matches lane"
      fi
      if listener_present 10080; then
        record_check pass info listener_10080 "listener 10080 present"
      else
        record_check fail high listener_10080 "listener 10080 missing"
      fi
      if listener_present 2333; then
        record_check pass info listener_2333 "listener 2333 present"
      else
        record_check fail high listener_2333 "listener 2333 missing"
      fi
      if listener_present 10443; then
        record_check pass info listener_10443 "listener 10443 present"
      else
        record_check warn medium listener_10443 "listener 10443 missing (TLS ingress may be disabled)"
      fi
      ;;
    core-to-edge-public)
      if [[ "$controller_mode" != "core-to-edge-public" ]]; then
        record_check fail high lane_controller_mode "controller mode must be core-to-edge-public for lane core-to-edge-public"
      else
        record_check pass info lane_controller_mode "controller mode matches lane"
      fi
      if [[ "$gateway_mode" != "core-proxy" && "$gateway_mode" != "core-to-edge-public" ]]; then
        record_check fail medium lane_gateway_mode "gateway mode should be core-proxy or core-to-edge-public for public lane"
      else
        record_check pass info lane_gateway_mode "gateway mode accepted for lane"
      fi
      if listener_present 10080; then
        record_check pass info listener_10080 "listener 10080 present"
      else
        record_check fail high listener_10080 "listener 10080 missing"
      fi
      if listener_present 10443; then
        record_check pass info listener_10443 "listener 10443 present"
      else
        record_check warn medium listener_10443 "listener 10443 missing (TLS listener may activate after route staging)"
      fi
      ;;
    edge-local)
      if [[ "$controller_mode" != "edge-local" ]]; then
        record_check fail high lane_controller_mode "controller mode must be edge-local for lane edge-local"
      else
        record_check pass info lane_controller_mode "controller mode matches lane"
      fi
      if [[ "$bundle_enabled" != "1" ]]; then
        record_check fail high route_bundle_missing "AE_ROUTE_BUNDLE_ENABLED must be 1 for edge-local lane"
      else
        record_check pass info route_bundle_missing "AE_ROUTE_BUNDLE_ENABLED=1"
      fi
      if [[ "$gateway_mode" != "edge-local" ]]; then
        record_check fail high lane_gateway_mode "gateway mode must be edge-local for lane edge-local"
      else
        record_check pass info lane_gateway_mode "gateway mode matches lane"
      fi
      if [[ -z "$edge_local_dir" ]]; then
        record_check fail high edge_local_dir_missing "gateway missing AE_EDGE_LOCAL_INGRESS_CONFIG_DIR"
      elif [[ ! -d "$edge_local_dir" ]]; then
        record_check fail high edge_local_dir_missing "AE_EDGE_LOCAL_INGRESS_CONFIG_DIR path does not exist: $edge_local_dir"
      else
        record_check pass info edge_local_dir_missing "AE_EDGE_LOCAL_INGRESS_CONFIG_DIR exists: $edge_local_dir"
      fi
      if [[ -z "$gateway_upstream_mode" ]]; then
        record_check fail high edge_local_upstream_mode "gateway missing AE_EDGE_LOCAL_UPSTREAM_MODE"
      elif [[ "$gateway_upstream_mode" != "$REQUIRED_EDGE_LOCAL_UPSTREAM_MODE" ]]; then
        record_check fail high edge_local_upstream_mode "AE_EDGE_LOCAL_UPSTREAM_MODE must be $REQUIRED_EDGE_LOCAL_UPSTREAM_MODE for edge-local lane (found: $gateway_upstream_mode)"
      else
        record_check pass info edge_local_upstream_mode "AE_EDGE_LOCAL_UPSTREAM_MODE=$gateway_upstream_mode"
      fi
      ;;
    *)
      die "invalid --lane value: $LANE"
      ;;
  esac
}

check_specs_writable() {
  if [[ ! -d "$CORE_SPECS_DIR" ]]; then
    record_check fail high core_specs_missing "core specs dir missing: $CORE_SPECS_DIR"
    return
  fi
  if [[ -w "$CORE_SPECS_DIR" ]]; then
    record_check pass info core_specs_writable "core specs dir is writable: $CORE_SPECS_DIR"
  else
    record_check fail high core_specs_writable "core specs dir is not writable: $CORE_SPECS_DIR"
  fi
}

check_inotify_headroom() {
  local max used free free_pct
  if ! max="$(sysctl -n fs.inotify.max_user_watches 2>/dev/null)"; then
    record_check warn low inotify_unavailable "unable to read fs.inotify.max_user_watches"
    return
  fi
  if [[ ! "$max" =~ ^[0-9]+$ ]] || (( max <= 0 )); then
    record_check warn low inotify_unavailable "invalid fs.inotify.max_user_watches value: ${max:-<empty>}"
    return
  fi

  used=0
  local pid c
  while IFS= read -r pid; do
    pid="${pid//[[:space:]]/}"
    [[ -n "$pid" ]] || continue
    [[ -d "/proc/$pid/fdinfo" ]] || continue
    c="$(grep -h '^inotify' /proc/$pid/fdinfo/* 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
    [[ "$c" =~ ^[0-9]+$ ]] || c=0
    used=$((used + c))
  done < <(ps -u "$USER" -o pid=)

  free=$((max - used))
  (( free < 0 )) && free=0
  free_pct=$(( (free * 100) / max ))

  if (( free_pct < MIN_INOTIFY_FREE_PCT )); then
    record_check fail high inotify_pressure "inotify headroom too low: used=${used} free=${free} max=${max} free_pct=${free_pct} threshold=${MIN_INOTIFY_FREE_PCT}"
  else
    record_check pass info inotify_pressure "inotify headroom OK: used=${used} free=${free} max=${max} free_pct=${free_pct} threshold=${MIN_INOTIFY_FREE_PCT}"
  fi
}

parse_etcd_status_field() {
  local line="$1"
  local key="$2"
  printf '%s' "$line" | sed -nE "s/.*${key}=([0-9]+).*/\\1/p" | head -n1
}

check_etcd_status() {
  if [[ ! -x "$ETCD_MAINTENANCE_SCRIPT" ]]; then
    record_check warn low etcd_helper_missing "etcd helper not executable: $ETCD_MAINTENANCE_SCRIPT"
    return
  fi

  if (( RUN_ETCD_WATCHDOG == 1 )); then
    if "$ETCD_MAINTENANCE_SCRIPT" watchdog >/dev/null 2>&1; then
      record_check pass info etcd_watchdog "etcd watchdog completed"
    else
      record_check fail medium etcd_watchdog "etcd watchdog failed"
    fi
  fi

  local status_out
  if ! status_out="$("$ETCD_MAINTENANCE_SCRIPT" status 2>&1)"; then
    record_check fail high etcd_status "unable to query etcd status: ${status_out:-<empty>}"
    return
  fi

  local line usage nospace threshold
  line="$(printf '%s\n' "$status_out" | rg 'usage_pct=' | tail -n1 || true)"
  if [[ -z "$line" ]]; then
    record_check warn low etcd_status_parse "etcd status output missing usage/alarm fields"
    return
  fi

  usage="$(parse_etcd_status_field "$line" 'usage_pct')"
  nospace="$(parse_etcd_status_field "$line" 'nospace_alarm')"
  threshold="${AE_ETCD_MAINTENANCE_THRESHOLD_PCT:-80}"
  [[ "$threshold" =~ ^[0-9]+$ ]] || threshold=80
  [[ "$usage" =~ ^[0-9]+$ ]] || usage=0
  [[ "$nospace" =~ ^[0-9]+$ ]] || nospace=0

  if (( nospace == 1 )); then
    record_check fail high etcd_nospace "etcd NOSPACE alarm is active"
  else
    record_check pass info etcd_nospace "etcd NOSPACE alarm is clear"
  fi

  if (( usage >= threshold )); then
    record_check fail medium etcd_usage "etcd usage_pct=${usage} exceeds threshold=${threshold}"
  else
    record_check pass info etcd_usage "etcd usage_pct=${usage} below threshold=${threshold}"
  fi
}

write_result_json() {
  local out="$1"
  python - "$CHECK_FILE" "$out" "$LANE" <<'PY'
import json
import sys
from pathlib import Path

check_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
lane = sys.argv[3]

rows = []
for line in check_path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    parts = line.split("\t", 3)
    if len(parts) != 4:
        continue
    status, severity, code, message = parts
    rows.append(
        {
            "status": status,
            "severity": severity,
            "code": code,
            "message": message,
        }
    )

summary = {
    "total": len(rows),
    "failures": sum(1 for r in rows if r["status"] in {"fail", "error"}),
    "warnings": sum(1 for r in rows if r["status"] == "warn"),
    "passes": sum(1 for r in rows if r["status"] == "pass"),
    "lane": lane,
}

payload = {"summary": summary, "checks": rows}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lane)
        LANE="${2:-}"
        shift 2
        ;;
      --site-id)
        SITE_ID="${2:-}"
        shift 2
        ;;
      --node-id)
        NODE_ID="${2:-}"
        shift 2
        ;;
      --core-specs-dir)
        CORE_SPECS_DIR="${2:-}"
        shift 2
        ;;
      --min-inotify-free-pct)
        MIN_INOTIFY_FREE_PCT="${2:-}"
        shift 2
        ;;
      --result-json)
        RESULT_JSON="${2:-}"
        shift 2
        ;;
      --watchdog)
        RUN_ETCD_WATCHDOG=1
        shift
        ;;
      --skip-mode)
        SKIP_MODE=1
        shift
        ;;
      --skip-specs)
        SKIP_SPECS=1
        shift
        ;;
      --skip-inotify)
        SKIP_INOTIFY=1
        shift
        ;;
      --skip-etcd)
        SKIP_ETCD=1
        shift
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
  [[ "$MIN_INOTIFY_FREE_PCT" =~ ^[0-9]+$ ]] || die "--min-inotify-free-pct must be an integer"
  case "$LANE" in
    any|core-proxy|core-to-edge-public|edge-local) ;;
    *) die "--lane must be one of: any|core-proxy|core-to-edge-public|edge-local" ;;
  esac

  need_cmd rg
  need_cmd ss
  need_cmd python

  (( SKIP_MODE == 1 )) || check_mode_requirements
  (( SKIP_SPECS == 1 )) || check_specs_writable
  (( SKIP_INOTIFY == 1 )) || check_inotify_headroom
  (( SKIP_ETCD == 1 )) || check_etcd_status

  while IFS=$'\t' read -r status severity code message; do
    case "$status" in
      pass) log "PASS [$code] $message" ;;
      warn) log "WARN [$code] $message" ;;
      fail|error) log "FAIL [$code] $message" ;;
      *) log "INFO [$code] $message" ;;
    esac
  done < "$CHECK_FILE"

  if [[ -n "$RESULT_JSON" ]]; then
    write_result_json "$RESULT_JSON"
    log "result_json=$RESULT_JSON"
  fi

  if (( FAILED == 1 )); then
    die "preflight failed"
  fi
  log "preflight passed"
}

main "$@"
