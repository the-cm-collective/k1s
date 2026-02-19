#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

FAULT=""
ACTION=""
DRY_RUN=0

CORE_SPECS_DIR="${CORE_SPECS_DIR:-$ROOT_DIR/state/profiles/k1s-core/specs}"
APP_NAME="${APP_NAME:-ingress-matrix-static}"
APP_MANIFEST="${APP_MANIFEST:-$ROOT_DIR/specs/examples/ingress-matrix/http-static.yaml}"
ROUTE_BUNDLE_CONFIG="${ROUTE_BUNDLE_CONFIG:-$ROOT_DIR/ops/dev/nats-hub.conf}"
NATS_RELOAD_CMD="${NATS_RELOAD_CMD:-}"
CONTROLLER_START_CMD="${CONTROLLER_START_CMD:-}"
GATEWAY_START_CMD="${GATEWAY_START_CMD:-}"
CONTROLLER_PATTERN="${CONTROLLER_PATTERN:-python -m ae.controller}"
GATEWAY_PATTERN="${GATEWAY_PATTERN:-python -m ae.gateway}"
STATE_DIR="${STATE_DIR:-$ROOT_DIR/state/test-results/fault-state}"

usage() {
  cat <<'USAGE'
Usage: scripts/dev/ingress_fault_injection.sh --fault <name> --action <inject|recover|cycle> [options]

Faults:
  controller-restart
  gateway-restart
  specs-permission-drift
  backend-unavailable
  nats-route-bundle-permission

Options:
  --fault <name>                   Fault name (required)
  --action <inject|recover|cycle>  Action to run (required)
  --dry-run                        Print actions without mutating

  --core-specs-dir <path>          Specs directory for permission drift fault
  --app-name <name>                App name for backend-unavailable fault
  --app-manifest <path>            App manifest to restore backend
  --route-bundle-config <path>     NATS config path for route-bundle permission fault
  --nats-reload-cmd <cmd>          Command to reload/restart NATS after config edit

  --controller-start-cmd <cmd>     Restart command for controller recovery
  --gateway-start-cmd <cmd>        Restart command for gateway recovery
  --controller-pattern <pattern>   Process pattern for controller (default: python -m ae.controller)
  --gateway-pattern <pattern>      Process pattern for gateway (default: python -m ae.gateway)
  --state-dir <path>               State directory for fault metadata
  -h, --help                       Show help
USAGE
}

log() {
  printf '[fault-inject] %s\n' "$*"
}

die() {
  printf '[fault-inject] ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

run_cmd() {
  local cmd="$1"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN: $cmd"
    return 0
  fi
  bash -lc "$cmd"
}

state_file_for_fault() {
  local fault="$1"
  printf '%s/%s.state' "$STATE_DIR" "$fault"
}

write_state_value() {
  local fault="$1"
  local key="$2"
  local value="$3"
  mkdir -p "$STATE_DIR"
  local state_file
  state_file="$(state_file_for_fault "$fault")"
  {
    if [[ -f "$state_file" ]]; then
      grep -v "^${key}=" "$state_file" || true
    fi
    printf '%s=%s\n' "$key" "$value"
  } > "${state_file}.tmp"
  mv "${state_file}.tmp" "$state_file"
}

read_state_value() {
  local fault="$1"
  local key="$2"
  local state_file
  state_file="$(state_file_for_fault "$fault")"
  [[ -f "$state_file" ]] || return 1
  awk -F= -v k="$key" '$1==k {print substr($0, index($0, "=")+1); exit}' "$state_file"
}

run_optional_reload() {
  if [[ -n "$NATS_RELOAD_CMD" ]]; then
    log "running NATS reload command"
    run_cmd "$NATS_RELOAD_CMD"
  fi
}

fault_controller_restart_inject() {
  need_cmd pgrep
  local pid
  pid="$(pgrep -f "$CONTROLLER_PATTERN" | head -n1 || true)"
  [[ -n "$pid" ]] || die "controller process not found (pattern: $CONTROLLER_PATTERN)"
  log "stopping controller pid=$pid"
  run_cmd "kill $pid"
}

fault_controller_restart_recover() {
  [[ -n "$CONTROLLER_START_CMD" ]] || die "controller recovery requires --controller-start-cmd"
  log "starting controller using provided command"
  run_cmd "$CONTROLLER_START_CMD"
}

fault_gateway_restart_inject() {
  need_cmd pgrep
  local pid
  pid="$(pgrep -f "$GATEWAY_PATTERN" | head -n1 || true)"
  [[ -n "$pid" ]] || die "gateway process not found (pattern: $GATEWAY_PATTERN)"
  log "stopping gateway pid=$pid"
  run_cmd "kill $pid"
}

fault_gateway_restart_recover() {
  [[ -n "$GATEWAY_START_CMD" ]] || die "gateway recovery requires --gateway-start-cmd"
  log "starting gateway using provided command"
  run_cmd "$GATEWAY_START_CMD"
}

fault_specs_permission_drift_inject() {
  if [[ ! -d "$CORE_SPECS_DIR" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: specs dir not present, skipping existence check: $CORE_SPECS_DIR"
      return 0
    fi
    die "specs dir not found: $CORE_SPECS_DIR"
  fi
  log "injecting non-writable specs dir drift at $CORE_SPECS_DIR"
  run_cmd "sudo chown -R root:root \"$CORE_SPECS_DIR\""
  run_cmd "sudo chmod -R a-w \"$CORE_SPECS_DIR\""
  run_cmd "sudo find \"$CORE_SPECS_DIR\" -type d -exec chmod a+rx {} \\;"
}

fault_specs_permission_drift_recover() {
  if [[ ! -d "$CORE_SPECS_DIR" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: specs dir not present, skipping existence check: $CORE_SPECS_DIR"
      return 0
    fi
    die "specs dir not found: $CORE_SPECS_DIR"
  fi
  local user_name group_name
  user_name="$(id -un)"
  group_name="$(id -gn)"
  log "recovering specs dir ownership/perms at $CORE_SPECS_DIR (owner=$user_name group=$group_name)"
  run_cmd "sudo chown -R \"$user_name:$group_name\" \"$CORE_SPECS_DIR\""
  run_cmd "sudo chmod -R g+rwX \"$CORE_SPECS_DIR\""
  run_cmd "sudo find \"$CORE_SPECS_DIR\" -type d -exec chmod 2775 {} \\;"
}

fault_backend_unavailable_inject() {
  [[ -n "$APP_NAME" ]] || die "--app-name is required"
  log "deleting app '$APP_NAME' to simulate backend unavailable"
  run_cmd "PYTHONPATH=\"$ROOT_DIR/src\" python -m ae.cli delete \"$APP_NAME\" || true"
}

fault_backend_unavailable_recover() {
  if [[ ! -f "$APP_MANIFEST" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: app manifest not present, skipping existence check: $APP_MANIFEST"
      return 0
    fi
    die "app manifest not found: $APP_MANIFEST"
  fi
  log "re-applying app manifest '$APP_MANIFEST'"
  run_cmd "PYTHONPATH=\"$ROOT_DIR/src\" python -m ae.cli apply -f \"$APP_MANIFEST\""
}

fault_route_bundle_permission_inject() {
  if [[ ! -f "$ROUTE_BUNDLE_CONFIG" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: route bundle config not present, skipping existence check: $ROUTE_BUNDLE_CONFIG"
      return 0
    fi
    die "route bundle config not found: $ROUTE_BUNDLE_CONFIG"
  fi
  if ! rg -n --fixed-strings --quiet "k1s.v1.site.*.routes.bundle" "$ROUTE_BUNDLE_CONFIG"; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: route bundle permission string not found in $ROUTE_BUNDLE_CONFIG"
      return 0
    fi
    die "route bundle permission string not found in $ROUTE_BUNDLE_CONFIG"
  fi
  mkdir -p "$STATE_DIR"
  local backup_path="$STATE_DIR/nats-route-bundle-permission.conf.bak"
  log "backing up $ROUTE_BUNDLE_CONFIG to $backup_path"
  run_cmd "cp \"$ROUTE_BUNDLE_CONFIG\" \"$backup_path\""
  write_state_value "nats-route-bundle-permission" "backup_path" "$backup_path"

  log "disabling route-bundle publish permission in $ROUTE_BUNDLE_CONFIG"
  run_cmd "python - \"$ROUTE_BUNDLE_CONFIG\" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8')
needle = 'k1s.v1.site.*.routes.bundle'
if needle not in text:
    raise SystemExit('missing permission needle')
path.write_text(text.replace(needle, needle + '.disabled', 1), encoding='utf-8')
PY"
  run_optional_reload
}

fault_route_bundle_permission_recover() {
  local backup_path
  backup_path="$(read_state_value "nats-route-bundle-permission" "backup_path" || true)"
  if [[ -z "$backup_path" || ! -f "$backup_path" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: no backup state found for nats-route-bundle-permission"
      return 0
    fi
    [[ -n "$backup_path" ]] || die "missing backup state for nats-route-bundle-permission"
    [[ -f "$backup_path" ]] || die "backup file not found: $backup_path"
  fi
  log "restoring route-bundle permission config from $backup_path"
  run_cmd "cp \"$backup_path\" \"$ROUTE_BUNDLE_CONFIG\""
  run_optional_reload
}

run_fault_action() {
  local fault="$1"
  local action="$2"
  case "$fault:$action" in
    controller-restart:inject) fault_controller_restart_inject ;;
    controller-restart:recover) fault_controller_restart_recover ;;
    gateway-restart:inject) fault_gateway_restart_inject ;;
    gateway-restart:recover) fault_gateway_restart_recover ;;
    specs-permission-drift:inject) fault_specs_permission_drift_inject ;;
    specs-permission-drift:recover) fault_specs_permission_drift_recover ;;
    backend-unavailable:inject) fault_backend_unavailable_inject ;;
    backend-unavailable:recover) fault_backend_unavailable_recover ;;
    nats-route-bundle-permission:inject) fault_route_bundle_permission_inject ;;
    nats-route-bundle-permission:recover) fault_route_bundle_permission_recover ;;
    *) die "unsupported fault/action: $fault/$action" ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fault)
      FAULT="${2:-}"
      shift 2
      ;;
    --action)
      ACTION="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --core-specs-dir)
      CORE_SPECS_DIR="${2:-}"
      shift 2
      ;;
    --app-name)
      APP_NAME="${2:-}"
      shift 2
      ;;
    --app-manifest)
      APP_MANIFEST="${2:-}"
      shift 2
      ;;
    --route-bundle-config)
      ROUTE_BUNDLE_CONFIG="${2:-}"
      shift 2
      ;;
    --nats-reload-cmd)
      NATS_RELOAD_CMD="${2:-}"
      shift 2
      ;;
    --controller-start-cmd)
      CONTROLLER_START_CMD="${2:-}"
      shift 2
      ;;
    --gateway-start-cmd)
      GATEWAY_START_CMD="${2:-}"
      shift 2
      ;;
    --controller-pattern)
      CONTROLLER_PATTERN="${2:-}"
      shift 2
      ;;
    --gateway-pattern)
      GATEWAY_PATTERN="${2:-}"
      shift 2
      ;;
    --state-dir)
      STATE_DIR="${2:-}"
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

[[ -n "$FAULT" ]] || die "--fault is required"
case "$ACTION" in
  inject|recover|cycle) ;;
  *) die "--action must be inject, recover, or cycle" ;;
esac

if [[ "$ACTION" == "cycle" ]]; then
  log "cycle start fault=$FAULT"
  run_fault_action "$FAULT" "inject"
  run_fault_action "$FAULT" "recover"
  log "cycle complete fault=$FAULT"
else
  run_fault_action "$FAULT" "$ACTION"
  log "completed fault=$FAULT action=$ACTION"
fi
