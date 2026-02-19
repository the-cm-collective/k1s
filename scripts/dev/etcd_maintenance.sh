#!/usr/bin/env bash
set -euo pipefail

CMD="${1:-status}"

AE_ETCD_ENDPOINTS="${AE_ETCD_ENDPOINTS:-http://127.0.0.1:2379}"
AE_ETCD_MAINTENANCE_THRESHOLD_PCT="${AE_ETCD_MAINTENANCE_THRESHOLD_PCT:-80}"
AE_ETCD_QUOTA_BACKEND_BYTES="${AE_ETCD_QUOTA_BACKEND_BYTES:-2147483648}"
AE_ETCD_MAINTENANCE_WAIT_S="${AE_ETCD_MAINTENANCE_WAIT_S:-12}"
AE_ETCD_MAINTENANCE_TIMEOUT_S="${AE_ETCD_MAINTENANCE_TIMEOUT_S:-5}"

usage() {
  cat <<'USAGE'
Usage: scripts/dev/etcd_maintenance.sh <status|compact-defrag|watchdog>

Commands:
  status          Print current endpoint, revision, db size, quota usage, alarms.
  compact-defrag  Run compaction + defragment + alarm disarm sequence.
  watchdog        Run compact-defrag only when NOSPACE alarm is active or db usage
                  exceeds AE_ETCD_MAINTENANCE_THRESHOLD_PCT.

Environment:
  AE_ETCD_ENDPOINTS                  Comma-separated endpoints (default: http://127.0.0.1:2379)
  AE_ETCD_MAINTENANCE_THRESHOLD_PCT  Watchdog threshold (default: 80)
  AE_ETCD_QUOTA_BACKEND_BYTES        Quota for percent math (default: 2147483648)
  AE_ETCD_MAINTENANCE_WAIT_S         Endpoint readiness wait (default: 12)
  AE_ETCD_MAINTENANCE_TIMEOUT_S      Per-request timeout seconds (default: 5)
USAGE
}

log() {
  printf '[etcd-maint] %s\n' "$*"
}

die() {
  printf '[etcd-maint] ERROR: %s\n' "$*" >&2
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

json_post() {
  local endpoint="$1"
  local path="$2"
  local payload="$3"
  local body
  if ! body="$(curl -fsS \
    --max-time "$AE_ETCD_MAINTENANCE_TIMEOUT_S" \
    -H 'Content-Type: application/json' \
    -X POST \
    "${endpoint}${path}" \
    -d "$payload" 2>/dev/null)"; then
    return 1
  fi
  printf '%s' "$body"
}

health_ok() {
  local endpoint="$1"
  local body
  if ! body="$(curl -fsS --max-time 2 "${endpoint}/health" 2>/dev/null || true)"; then
    return 1
  fi
  python - "$body" <<'PY' >/dev/null 2>&1
import json
import sys

raw = sys.argv[1]
try:
    payload = json.loads(raw or "{}")
except Exception:
    raise SystemExit(1)

health = str(payload.get("health", "")).lower()
if health in {"true", "1", "ok"}:
    raise SystemExit(0)
raise SystemExit(1)
PY
}

parse_snapshot() {
  local status_json="$1"
  local alarms_json="$2"
  local quota_bytes="$3"
  python - "$status_json" "$alarms_json" "$quota_bytes" <<'PY'
import json
import sys

status_raw, alarms_raw, quota_raw = sys.argv[1:4]
try:
    status = json.loads(status_raw or "{}")
except Exception:
    status = {}
try:
    alarms = json.loads(alarms_raw or "{}")
except Exception:
    alarms = {}

def to_int(value):
    try:
        return int(value)
    except Exception:
        return 0

header = status.get("header") or {}
revision = to_int(header.get("revision"))
db_size = to_int(status.get("dbSize"))
db_size_in_use = to_int(status.get("dbSizeInUse"))
quota = max(1, to_int(quota_raw))
pct = int((db_size * 100) / quota) if quota else 0

alarm_items = alarms.get("alarms") or []
nospace = 0
for item in alarm_items:
    if str((item or {}).get("alarm") or "").upper() == "NOSPACE":
        nospace = 1
        break

print(
    "\t".join(
        [
            str(revision),
            str(db_size),
            str(db_size_in_use),
            str(quota),
            str(pct),
            str(nospace),
            str(len(alarm_items)),
        ]
    )
)
PY
}

alarm_deactivate_payloads() {
  local alarms_json="$1"
  python - "$alarms_json" <<'PY'
import json
import sys

raw = sys.argv[1]
try:
    payload = json.loads(raw or "{}")
except Exception:
    payload = {}

for item in payload.get("alarms") or []:
    alarm = str((item or {}).get("alarm") or "").upper()
    member_id = str((item or {}).get("memberID") or (item or {}).get("memberId") or "0")
    if not alarm:
        continue
    print(json.dumps({"action": "DEACTIVATE", "memberID": member_id, "alarm": alarm}))
PY
}

join_endpoints() {
  local raw="$1"
  local -n out_ref="$2"
  out_ref=()
  local part
  IFS=',' read -r -a parts <<<"$raw"
  for part in "${parts[@]}"; do
    part="$(trim "$part")"
    [[ -n "$part" ]] || continue
    part="${part%/}"
    out_ref+=("$part")
  done
}

wait_for_endpoint() {
  local -n endpoints_ref="$1"
  local deadline=$((SECONDS + AE_ETCD_MAINTENANCE_WAIT_S))
  while (( SECONDS < deadline )); do
    local ep
    for ep in "${endpoints_ref[@]}"; do
      if health_ok "$ep"; then
        printf '%s\n' "$ep"
        return 0
      fi
    done
    sleep 1
  done
  printf '%s\n' "${endpoints_ref[0]}"
  return 1
}

snapshot() {
  local endpoint="$1"
  local status_json alarms_json fields
  status_json="$(json_post "$endpoint" "/v3/maintenance/status" '{}')"
  alarms_json="$(json_post "$endpoint" "/v3/maintenance/alarm" '{"action":"GET"}')"
  fields="$(parse_snapshot "$status_json" "$alarms_json" "$AE_ETCD_QUOTA_BACKEND_BYTES")"
  printf '%s\n' "$status_json"
  printf '%s\n' "$alarms_json"
  printf '%s\n' "$fields"
}

load_snapshot() {
  local endpoint="$1"
  local -n out_status_ref="$2"
  local -n out_alarms_ref="$3"
  local -n out_fields_ref="$4"
  local snap=()

  if ! mapfile -t snap < <(snapshot "$endpoint"); then
    die "failed to query etcd snapshot at ${endpoint}; ensure etcd is reachable"
  fi
  if (( ${#snap[@]} < 3 )); then
    die "incomplete etcd snapshot at ${endpoint}"
  fi

  out_status_ref="${snap[0]}"
  out_alarms_ref="${snap[1]}"
  out_fields_ref="${snap[2]}"
}

print_snapshot_line() {
  local endpoint="$1"
  local fields_tsv="$2"
  local revision db_size db_size_in_use quota pct nospace alarm_count
  IFS=$'\t' read -r revision db_size db_size_in_use quota pct nospace alarm_count <<<"$fields_tsv"
  log "endpoint=${endpoint} revision=${revision} db_size=${db_size} db_size_in_use=${db_size_in_use} quota=${quota} usage_pct=${pct} nospace_alarm=${nospace} alarms=${alarm_count}"
}

run_compact_defrag() {
  local endpoint="$1"
  local status_json alarms_json fields
  load_snapshot "$endpoint" status_json alarms_json fields
  print_snapshot_line "$endpoint" "$fields"

  local revision db_size db_size_in_use quota pct nospace alarm_count
  IFS=$'\t' read -r revision db_size db_size_in_use quota pct nospace alarm_count <<<"$fields"
  [[ "$revision" =~ ^[0-9]+$ ]] || die "invalid etcd revision: $revision"
  (( revision > 0 )) || die "etcd revision is zero; refusing compaction"

  log "running compaction revision=${revision}"
  json_post "$endpoint" "/v3/kv/compaction" "{\"revision\":\"${revision}\",\"physical\":true}" >/dev/null

  log "running defragment endpoint=${endpoint}"
  json_post "$endpoint" "/v3/maintenance/defragment" '{}' >/dev/null

  local payload
  while IFS= read -r payload; do
    [[ -n "$payload" ]] || continue
    json_post "$endpoint" "/v3/maintenance/alarm" "$payload" >/dev/null || true
  done < <(alarm_deactivate_payloads "$alarms_json")

  load_snapshot "$endpoint" status_json alarms_json fields
  print_snapshot_line "$endpoint" "$fields"
}

main() {
  case "$CMD" in
    -h|--help|help)
      usage
      exit 0
      ;;
    status|compact-defrag|watchdog)
      ;;
    *)
      usage
      die "unknown command: $CMD"
      ;;
  esac

  need_cmd curl
  need_cmd python

  [[ "$AE_ETCD_MAINTENANCE_THRESHOLD_PCT" =~ ^[0-9]+$ ]] || die "AE_ETCD_MAINTENANCE_THRESHOLD_PCT must be an integer"
  [[ "$AE_ETCD_QUOTA_BACKEND_BYTES" =~ ^[0-9]+$ ]] || die "AE_ETCD_QUOTA_BACKEND_BYTES must be an integer"
  [[ "$AE_ETCD_MAINTENANCE_WAIT_S" =~ ^[0-9]+$ ]] || die "AE_ETCD_MAINTENANCE_WAIT_S must be an integer"
  [[ "$AE_ETCD_MAINTENANCE_TIMEOUT_S" =~ ^[0-9]+$ ]] || die "AE_ETCD_MAINTENANCE_TIMEOUT_S must be an integer"

  local endpoints=()
  join_endpoints "$AE_ETCD_ENDPOINTS" endpoints
  (( ${#endpoints[@]} > 0 )) || die "no endpoints parsed from AE_ETCD_ENDPOINTS"

  local endpoint
  if endpoint="$(wait_for_endpoint endpoints)"; then
    :
  else
    log "warning: etcd /health not ready yet; using endpoint=${endpoint}"
  fi

  case "$CMD" in
    status)
      local status_json alarms_json fields
      load_snapshot "$endpoint" status_json alarms_json fields
      print_snapshot_line "$endpoint" "$fields"
      ;;
    compact-defrag)
      run_compact_defrag "$endpoint"
      ;;
    watchdog)
      local status_json alarms_json fields
      load_snapshot "$endpoint" status_json alarms_json fields
      print_snapshot_line "$endpoint" "$fields"

      local revision db_size db_size_in_use quota pct nospace alarm_count
      IFS=$'\t' read -r revision db_size db_size_in_use quota pct nospace alarm_count <<<"$fields"
      if (( nospace == 1 || pct >= AE_ETCD_MAINTENANCE_THRESHOLD_PCT )); then
        log "watchdog trigger: pct=${pct} threshold=${AE_ETCD_MAINTENANCE_THRESHOLD_PCT} nospace=${nospace}"
        run_compact_defrag "$endpoint"
      else
        log "watchdog no-op: pct=${pct} threshold=${AE_ETCD_MAINTENANCE_THRESHOLD_PCT} nospace=${nospace}"
      fi
      ;;
  esac
}

main "$@"
