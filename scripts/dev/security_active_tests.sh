#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PROFILE="${PROFILE:-k1s-core}"
API_URL="${API_URL:-http://127.0.0.1:9108}"
APISHIM_SERVER="${APISHIM_SERVER:-}"
APISHIM_INSECURE="${APISHIM_INSECURE:-1}"
FAIL_ON="${FAIL_ON:-high}"
RESULT_JSON="${RESULT_JSON:-$ROOT_DIR/state/test-results/security-active-$(date -u +%Y%m%dT%H%M%SZ).json}"

TEST_FILE="$(mktemp)"
trap 'rm -f "$TEST_FILE"' EXIT
FAIL_BUILD=0

usage() {
  cat <<'USAGE'
Usage: scripts/dev/security_active_tests.sh [options]

Runs staged active authn/authz probes against controller API and API shim.

Options:
  --profile <name>               Profile name for default shim env discovery (default: k1s-core)
  --api-url <url>                Controller API URL (default: http://127.0.0.1:9108)
  --apishim-server <url>         Override API shim server URL
  --apishim-insecure             Use -k for API shim HTTPS checks (default: enabled)
  --apishim-strict-tls           Require valid TLS chain for API shim checks
  --fail-on <level>              none|low|medium|high|critical (default: high)
  --result-json <path>           Output JSON path
  -h, --help                     Show help
USAGE
}

log() {
  printf '[security-active] %s\n' "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

severity_rank() {
  case "$1" in
    critical) printf '4' ;;
    high) printf '3' ;;
    medium) printf '2' ;;
    low) printf '1' ;;
    info) printf '0' ;;
    *) printf '0' ;;
  esac
}

should_fail() {
  local severity="$1"
  local threshold="$2"
  local sr tr
  sr="$(severity_rank "$severity")"
  tr="$(severity_rank "$threshold")"
  (( sr >= tr ))
}

record_test() {
  local status="$1"
  local severity="$2"
  local name="$3"
  local expected="$4"
  local actual="$5"
  local message="$6"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$status" "$severity" "$name" "$expected" "$actual" "$message" >>"$TEST_FILE"

  if [[ "$status" == "fail" && "$FAIL_ON" != "none" ]]; then
    if should_fail "$severity" "$FAIL_ON"; then
      FAIL_BUILD=1
    fi
  fi
}

find_controller_pid() {
  pgrep -f 'python -m ae.controller' | head -n1 || true
}

read_environ_raw() {
  local pid="$1"
  if [[ -r "/proc/${pid}/environ" ]]; then
    cat "/proc/${pid}/environ"
    return 0
  fi
  if sudo -n true >/dev/null 2>&1; then
    sudo -n cat "/proc/${pid}/environ"
    return 0
  fi
  return 1
}

proc_env_get() {
  local raw="$1"
  local key="$2"
  local line
  while IFS= read -r line; do
    [[ "$line" == "${key}="* ]] || continue
    printf '%s' "${line#*=}"
    return 0
  done < <(printf '%s' "$raw" | tr '\0' '\n')
  return 1
}

curl_code() {
  local url="$1"
  shift
  curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 10 "$@" "$url" 2>/dev/null || true
}

discover_apishim_server() {
  if [[ -n "$APISHIM_SERVER" ]]; then
    return 0
  fi

  local env_file="$ROOT_DIR/state/profiles/${PROFILE}/apishim.cli.env"
  if [[ -f "$env_file" ]]; then
    APISHIM_SERVER="$(awk -F= '/^AE_APISHIM_SERVER=/{print $2}' "$env_file" | tail -n1)"
  fi
}

run_controller_auth_tests() {
  local pid env_raw
  pid="$(find_controller_pid)"
  if [[ -z "$pid" ]]; then
    record_test fail critical controller_missing "controller running" "absent" "controller process not found"
    return
  fi

  env_raw="$(read_environ_raw "$pid" || true)"
  if [[ -z "$env_raw" ]]; then
    record_test fail high controller_env_unreadable "read controller env" "failed" "cannot read /proc/${pid}/environ"
    return
  fi

  local api_mut read_token scaler_token admin_token
  api_mut="$(proc_env_get "$env_raw" AE_API_MUTATIONS || true)"
  read_token="$(proc_env_get "$env_raw" AE_API_READ_TOKEN || true)"
  scaler_token="$(proc_env_get "$env_raw" AE_API_SCALER_TOKEN || true)"
  admin_token="$(proc_env_get "$env_raw" AE_API_ADMIN_TOKEN || true)"

  local code

  if [[ -n "$read_token" ]]; then
    code="$(curl_code "${API_URL%/}/status")"
    case "$code" in
      401|403)
        record_test pass high unauth_status_denied "401/403" "$code" "unauthenticated /status denied"
        ;;
      000)
        record_test fail medium unauth_status_denied "reachable" "$code" "controller API not reachable"
        ;;
      *)
        record_test fail critical unauth_status_denied "401/403" "$code" "unauthenticated /status unexpectedly allowed"
        ;;
    esac

    code="$(curl_code "${API_URL%/}/status" -H 'Authorization: Bearer invalid-token')"
    case "$code" in
      401|403)
        record_test pass high invalid_token_status_denied "401/403" "$code" "invalid token rejected for /status"
        ;;
      *)
        record_test fail high invalid_token_status_denied "401/403" "$code" "invalid token unexpectedly accepted for /status"
        ;;
    esac
  else
    record_test warn low read_token_missing "configured AE_API_READ_TOKEN" "missing" "read-token checks skipped"
  fi

  if truthy "$api_mut"; then
    code="$(curl_code "${API_URL%/}/scale/security-probe-app" -X POST -H 'Content-Type: application/json' -d '{"replicas":1}')"
    case "$code" in
      401|403)
        record_test pass critical unauth_scale_denied "401/403" "$code" "unauthenticated mutation denied"
        ;;
      *)
        record_test fail critical unauth_scale_denied "401/403" "$code" "unauthenticated mutation unexpectedly allowed"
        ;;
    esac

    code="$(curl_code "${API_URL%/}/scale/security-probe-app" -X POST -H 'Authorization: Bearer invalid-token' -H 'Content-Type: application/json' -d '{"replicas":1}')"
    case "$code" in
      401|403)
        record_test pass high invalid_token_scale_denied "401/403" "$code" "invalid token rejected for mutation"
        ;;
      *)
        record_test fail high invalid_token_scale_denied "401/403" "$code" "invalid token accepted for mutation"
        ;;
    esac

    if [[ -n "$read_token" ]]; then
      code="$(curl_code "${API_URL%/}/scale/security-probe-app" -X POST -H "Authorization: Bearer ${read_token}" -H 'Content-Type: application/json' -d '{"replicas":1}')"
      case "$code" in
        401|403)
          record_test pass high read_token_scale_denied "401/403" "$code" "read token cannot scale"
          ;;
        *)
          record_test fail high read_token_scale_denied "401/403" "$code" "read token can scale"
          ;;
      esac
    else
      record_test warn low read_token_scale_denied "read token present" "missing" "read-token mutation test skipped"
    fi

    if [[ -n "$scaler_token" && -n "$admin_token" ]]; then
      code="$(curl_code "${API_URL%/}/delete/security-probe-app?purge=1" -X POST -H "Authorization: Bearer ${scaler_token}")"
      case "$code" in
        401|403)
          record_test pass high scaler_delete_denied "401/403" "$code" "scaler token cannot delete"
          ;;
        *)
          record_test fail high scaler_delete_denied "401/403" "$code" "scaler token can delete"
          ;;
      esac

      code="$(curl_code "${API_URL%/}/scale/security-probe-app" -X POST -H "Authorization: Bearer ${admin_token}" -H 'Content-Type: application/json' -d '{"replicas":1}')"
      case "$code" in
        401|403|000)
          record_test fail medium admin_scale_authorized "non-401/403" "$code" "admin token did not authenticate for scale"
          ;;
        *)
          record_test pass info admin_scale_authorized "non-401/403" "$code" "admin token authenticated for scale"
          ;;
      esac
    else
      record_test warn low scaler_admin_missing "scaler+admin tokens present" "missing" "scaler/admin authorization checks skipped"
    fi
  else
    record_test warn low mutations_disabled "AE_API_MUTATIONS=1" "disabled" "mutation authz tests skipped"
  fi
}

run_apishim_auth_tests() {
  discover_apishim_server
  if [[ -z "$APISHIM_SERVER" ]]; then
    record_test warn low apishim_server_missing "configured apishim server" "missing" "apishim auth checks skipped"
    return
  fi

  local pid env_raw shim_anon="" shim_token=""
  pid="$(find_controller_pid)"
  if [[ -n "$pid" ]]; then
    env_raw="$(read_environ_raw "$pid" || true)"
    if [[ -n "$env_raw" ]]; then
      shim_anon="$(proc_env_get "$env_raw" AE_APISHIM_ALLOW_ANON || true)"
      shim_token="$(proc_env_get "$env_raw" AE_APISHIM_TOKEN || true)"
    fi
  fi

  local -a shim_tls_args=()
  if truthy "$APISHIM_INSECURE"; then
    shim_tls_args=(-k)
  fi

  local code
  code="$(curl_code "${APISHIM_SERVER%/}/openapi/v2" "${shim_tls_args[@]}")"

  if truthy "$shim_anon"; then
    case "$code" in
      2??)
        record_test pass medium apishim_anon_expected "2xx" "$code" "apishim anonymous mode is enabled"
        ;;
      *)
        record_test fail medium apishim_anon_expected "2xx" "$code" "apishim anon enabled but unauth request did not succeed"
        ;;
    esac
    return
  fi

  case "$code" in
    401|403)
      record_test pass high apishim_unauth_denied "401/403" "$code" "apishim unauthenticated access denied"
      ;;
    000)
      record_test fail medium apishim_unauth_denied "reachable" "$code" "apishim endpoint not reachable"
      ;;
    *)
      record_test fail high apishim_unauth_denied "401/403" "$code" "apishim unauthenticated access unexpectedly allowed"
      ;;
  esac

  if [[ -n "$shim_token" ]]; then
    code="$(curl_code "${APISHIM_SERVER%/}/openapi/v2" "${shim_tls_args[@]}" -H 'Authorization: Bearer invalid-token')"
    case "$code" in
      401|403)
        record_test pass high apishim_invalid_token_denied "401/403" "$code" "apishim invalid token rejected"
        ;;
      *)
        record_test fail high apishim_invalid_token_denied "401/403" "$code" "apishim invalid token accepted"
        ;;
    esac
  else
    record_test warn low apishim_token_missing "AE_APISHIM_TOKEN present" "missing" "invalid-token shim check skipped"
  fi
}

write_json() {
  local out="$1"
  python - "$TEST_FILE" "$out" "$PROFILE" "$API_URL" "$APISHIM_SERVER" "$FAIL_ON" "$APISHIM_INSECURE" <<'PY'
import json
import sys
from pathlib import Path

src = Path(sys.argv[1])
out = Path(sys.argv[2])
profile = sys.argv[3]
api_url = sys.argv[4]
apishim_server = sys.argv[5]
fail_on = sys.argv[6]
apishim_insecure = sys.argv[7]

rows = []
for line in src.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    status, severity, name, expected, actual, message = line.split("\t", 5)
    rows.append(
        {
            "status": status,
            "severity": severity,
            "name": name,
            "expected": expected,
            "actual": actual,
            "message": message,
        }
    )

summary = {
    "total_tests": len(rows),
    "passes": sum(1 for r in rows if r["status"] == "pass"),
    "warnings": sum(1 for r in rows if r["status"] == "warn"),
    "failures": sum(1 for r in rows if r["status"] == "fail"),
    "failures_by_severity": {
        sev: sum(1 for r in rows if r["status"] == "fail" and r["severity"] == sev)
        for sev in ["critical", "high", "medium", "low", "info"]
    },
}

payload = {
    "profile": profile,
    "api_url": api_url,
    "apishim_server": apishim_server,
    "apishim_insecure": apishim_insecure,
    "fail_on": fail_on,
    "summary": summary,
    "tests": rows,
}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profile)
        PROFILE="${2:-}"
        shift 2
        ;;
      --api-url)
        API_URL="${2:-}"
        shift 2
        ;;
      --apishim-server)
        APISHIM_SERVER="${2:-}"
        shift 2
        ;;
      --apishim-insecure)
        APISHIM_INSECURE=1
        shift
        ;;
      --apishim-strict-tls)
        APISHIM_INSECURE=0
        shift
        ;;
      --fail-on)
        FAIL_ON="${2:-}"
        shift 2
        ;;
      --result-json)
        RESULT_JSON="${2:-}"
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
  case "$FAIL_ON" in
    none|low|medium|high|critical) ;;
    *) die "--fail-on must be one of: none|low|medium|high|critical" ;;
  esac

  need_cmd curl
  need_cmd python

  run_controller_auth_tests
  run_apishim_auth_tests

  while IFS=$'\t' read -r status severity name expected actual message; do
    case "$status" in
      pass) log "PASS [$severity] [$name] expected=${expected} actual=${actual} ${message}" ;;
      warn) log "WARN [$severity] [$name] expected=${expected} actual=${actual} ${message}" ;;
      fail) log "FAIL [$severity] [$name] expected=${expected} actual=${actual} ${message}" ;;
      *) log "INFO [$severity] [$name] expected=${expected} actual=${actual} ${message}" ;;
    esac
  done < "$TEST_FILE"

  write_json "$RESULT_JSON"
  log "result_json=$RESULT_JSON"

  if (( FAIL_BUILD == 1 )); then
    die "active tests failed for fail-on=${FAIL_ON}"
  fi
  log "active tests passed"
}

main "$@"
