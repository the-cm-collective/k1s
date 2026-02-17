#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PROFILE="${PROFILE:-k1s-core}"
CORE_SPECS_DIR="${CORE_SPECS_DIR:-$ROOT_DIR/state/profiles/${PROFILE}/specs}"
API_URL="${API_URL:-http://127.0.0.1:9108}"
FAIL_ON="${FAIL_ON:-high}"
RESULT_JSON="${RESULT_JSON:-$ROOT_DIR/state/test-results/security-baseline-$(date -u +%Y%m%dT%H%M%SZ).json}"

CHECK_FILE="$(mktemp)"
trap 'rm -f "$CHECK_FILE"' EXIT

FAIL_BUILD=0

usage() {
  cat <<'USAGE'
Usage: scripts/dev/security_baseline_check.sh [options]

Runs production-hardening baseline checks against the live dev stack.

Options:
  --profile <name>               Profile name (default: k1s-core)
  --core-specs-dir <path>        Specs dir to inspect security defaults
  --api-url <url>                Controller API base URL (default: http://127.0.0.1:9108)
  --fail-on <level>              none|low|medium|high|critical (default: high)
  --result-json <path>           Output JSON path
  -h, --help                     Show help
USAGE
}

log() {
  printf '[security-baseline] %s\n' "$*"
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

record_check() {
  local status="$1"
  local severity="$2"
  local code="$3"
  local message="$4"
  printf '%s\t%s\t%s\t%s\n' "$status" "$severity" "$code" "$message" >>"$CHECK_FILE"

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

listener_present() {
  local port="$1"
  ss -ltn 2>/dev/null | rg -q ":${port}\\b"
}

check_workload_security_defaults() {
  local result
  result="$(python - "$CORE_SPECS_DIR" <<'PY'
import json
import sys
from pathlib import Path

try:
    import yaml
except Exception:
    print(json.dumps({"error": "missing-pyyaml"}))
    raise SystemExit(0)

specs_dir = Path(sys.argv[1])
if not specs_dir.exists():
    print(json.dumps({"error": "missing-specs-dir"}))
    raise SystemExit(0)

files = sorted(specs_dir.rglob("*.yaml")) + sorted(specs_dir.rglob("*.yml"))

total = 0
missing = 0
missing_items = []

for path in files:
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except Exception:
        continue
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if str(doc.get("kind") or "") != "Deployment":
            continue
        spec = doc.get("spec") or {}
        if not isinstance(spec, dict):
            continue
        total += 1
        sec = spec.get("security") or {}
        if not isinstance(sec, dict):
            sec = {}
        missing_fields = []
        if sec.get("runAsUser") is None:
            missing_fields.append("runAsUser")
        if sec.get("readOnlyRootFilesystem") is not True:
            missing_fields.append("readOnlyRootFilesystem=true")
        seccomp = sec.get("seccompProfileType")
        if seccomp not in {"RuntimeDefault", "Localhost"}:
            missing_fields.append("seccompProfileType")
        drop_caps = sec.get("dropCapabilities") or []
        if isinstance(drop_caps, list) and "NET_RAW" not in [str(x) for x in drop_caps]:
            missing_fields.append("dropCapabilities(NET_RAW)")

        if missing_fields:
            missing += 1
            missing_items.append(
                {
                    "file": str(path),
                    "name": ((doc.get("metadata") or {}).get("name") or "<unknown>"),
                    "missing": missing_fields,
                }
            )

print(json.dumps({"total": total, "missing": missing, "items": missing_items[:30]}))
PY
)"

  local err total missing
  err="$(python - "$result" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
print(payload.get("error", ""))
PY
)"

  if [[ -n "$err" ]]; then
    case "$err" in
      missing-pyyaml)
        record_check fail medium security_parse "python yaml module unavailable; cannot inspect workload security defaults"
        ;;
      missing-specs-dir)
        record_check fail medium security_specs_missing "core specs dir missing: $CORE_SPECS_DIR"
        ;;
      *)
        record_check fail medium security_parse "unable to inspect workload security defaults: $err"
        ;;
    esac
    return
  fi

  total="$(python - "$result" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
print(payload.get("total", 0))
PY
)"
  missing="$(python - "$result" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
print(payload.get("missing", 0))
PY
)"

  if [[ "$total" -eq 0 ]]; then
    record_check warn low security_specs_empty "no Deployment manifests found under $CORE_SPECS_DIR"
  elif [[ "$missing" -gt 0 ]]; then
    record_check fail medium security_defaults "${missing}/${total} Deployment manifest(s) missing recommended security defaults"
  else
    record_check pass info security_defaults "all ${total} Deployment manifests include recommended security defaults"
  fi
}

check_controller_and_env() {
  local pid env_raw
  pid="$(find_controller_pid)"
  if [[ -z "$pid" ]]; then
    record_check fail critical controller_missing "controller process not found"
    return
  fi
  record_check pass info controller_present "controller pid=${pid}"

  env_raw="$(read_environ_raw "$pid" || true)"
  if [[ -z "$env_raw" ]]; then
    record_check fail high controller_env_unreadable "cannot read controller environment from /proc/${pid}/environ"
    return
  fi

  local ingress_mode api_mutations
  local api_read api_scaler api_admin
  local shim_anon plain_secrets registry_insecure
  local shim_token shim_read shim_exec shim_pf shim_mint shim_secret

  ingress_mode="$(proc_env_get "$env_raw" EDGE_INGRESS_MODE || true)"
  api_mutations="$(proc_env_get "$env_raw" AE_API_MUTATIONS || true)"
  api_read="$(proc_env_get "$env_raw" AE_API_READ_TOKEN || true)"
  api_scaler="$(proc_env_get "$env_raw" AE_API_SCALER_TOKEN || true)"
  api_admin="$(proc_env_get "$env_raw" AE_API_ADMIN_TOKEN || true)"

  shim_anon="$(proc_env_get "$env_raw" AE_APISHIM_ALLOW_ANON || true)"
  plain_secrets="$(proc_env_get "$env_raw" AE_ALLOW_PLAINTEXT_SECRETS || true)"
  registry_insecure="$(proc_env_get "$env_raw" AE_CRI_REGISTRY_INSECURE || true)"

  shim_token="$(proc_env_get "$env_raw" AE_APISHIM_TOKEN || true)"
  shim_read="$(proc_env_get "$env_raw" AE_APISHIM_READ_TOKEN || true)"
  shim_exec="$(proc_env_get "$env_raw" AE_APISHIM_EXEC_TOKEN || true)"
  shim_pf="$(proc_env_get "$env_raw" AE_APISHIM_PORTFORWARD_TOKEN || true)"
  shim_mint="$(proc_env_get "$env_raw" AE_APISHIM_MINT_TOKEN || true)"
  shim_secret="$(proc_env_get "$env_raw" AE_APISHIM_SESSION_SECRET || true)"

  if truthy "$shim_anon"; then
    record_check fail critical apishim_allow_anon "AE_APISHIM_ALLOW_ANON is enabled"
  else
    record_check pass info apishim_allow_anon "AE_APISHIM_ALLOW_ANON is disabled"
  fi

  if truthy "$plain_secrets"; then
    record_check fail high plaintext_secrets_enabled "AE_ALLOW_PLAINTEXT_SECRETS is enabled"
  else
    record_check pass info plaintext_secrets_enabled "AE_ALLOW_PLAINTEXT_SECRETS is disabled"
  fi

  if truthy "$registry_insecure"; then
    record_check fail high cri_registry_insecure "AE_CRI_REGISTRY_INSECURE is enabled"
  else
    record_check pass info cri_registry_insecure "AE_CRI_REGISTRY_INSECURE is disabled"
  fi

  if truthy "$api_mutations"; then
    record_check pass info api_mutations_gate "AE_API_MUTATIONS is enabled"
    if [[ -z "$api_scaler" || -z "$api_admin" ]]; then
      record_check fail high api_mutation_tokens "AE_API_MUTATIONS enabled but scaler/admin token missing"
    else
      record_check pass info api_mutation_tokens "scaler/admin tokens present for mutation endpoints"
    fi
  else
    record_check warn low api_mutations_gate "AE_API_MUTATIONS is disabled"
  fi

  if [[ -n "$api_read" || -n "$api_scaler" || -n "$api_admin" ]]; then
    if [[ -z "$api_read" ]]; then
      record_check fail high api_read_missing "token auth appears enabled but AE_API_READ_TOKEN is missing"
    else
      record_check pass info api_read_present "AE_API_READ_TOKEN present"
    fi
  else
    record_check warn low api_tokens_missing "AE_API_* tokens are not configured"
  fi

  local token_var token_val token_len
  for token_var in AE_API_READ_TOKEN AE_API_SCALER_TOKEN AE_API_ADMIN_TOKEN AE_APISHIM_TOKEN AE_APISHIM_READ_TOKEN AE_APISHIM_EXEC_TOKEN AE_APISHIM_PORTFORWARD_TOKEN AE_APISHIM_MINT_TOKEN; do
    case "$token_var" in
      AE_API_READ_TOKEN) token_val="$api_read" ;;
      AE_API_SCALER_TOKEN) token_val="$api_scaler" ;;
      AE_API_ADMIN_TOKEN) token_val="$api_admin" ;;
      AE_APISHIM_TOKEN) token_val="$shim_token" ;;
      AE_APISHIM_READ_TOKEN) token_val="$shim_read" ;;
      AE_APISHIM_EXEC_TOKEN) token_val="$shim_exec" ;;
      AE_APISHIM_PORTFORWARD_TOKEN) token_val="$shim_pf" ;;
      AE_APISHIM_MINT_TOKEN) token_val="$shim_mint" ;;
    esac
    if [[ -n "$token_val" ]]; then
      token_len="${#token_val}"
      if (( token_len < 16 )); then
        record_check fail medium "token_strength_${token_var}" "${token_var} length=${token_len} (<16)"
      else
        record_check pass info "token_strength_${token_var}" "${token_var} length is acceptable"
      fi
    fi
  done

  if [[ -n "$shim_secret" ]]; then
    if (( ${#shim_secret} < 32 )); then
      record_check fail medium apishim_session_secret "AE_APISHIM_SESSION_SECRET is shorter than 32 chars"
    else
      record_check pass info apishim_session_secret "AE_APISHIM_SESSION_SECRET length is acceptable"
    fi
  else
    record_check fail medium apishim_session_secret "AE_APISHIM_SESSION_SECRET is missing"
  fi

  if [[ -n "$ingress_mode" ]]; then
    record_check pass info ingress_mode "EDGE_INGRESS_MODE=${ingress_mode}"
  else
    record_check warn low ingress_mode "EDGE_INGRESS_MODE is not set"
  fi

  case "$ingress_mode" in
    core-proxy)
      listener_present 10080 && record_check pass info listener_10080 "listener 10080 present" || record_check fail high listener_10080 "listener 10080 missing"
      listener_present 2333 && record_check pass info listener_2333 "listener 2333 present" || record_check fail high listener_2333 "listener 2333 missing"
      listener_present 10443 && record_check pass info listener_10443 "listener 10443 present" || record_check warn low listener_10443 "listener 10443 missing"
      ;;
    core-to-edge-public)
      listener_present 10080 && record_check pass info listener_10080 "listener 10080 present" || record_check fail high listener_10080 "listener 10080 missing"
      listener_present 10443 && record_check pass info listener_10443 "listener 10443 present" || record_check fail high listener_10443 "listener 10443 missing"
      ;;
    edge-local)
      record_check pass info listener_mode "edge-local mode listener checks skipped"
      ;;
  esac
}

check_exposed_sensitive_ports() {
  if ss -ltn 2>/dev/null | rg -q '0\.0\.0\.0:2379\b'; then
    record_check fail critical etcd_exposed "etcd is exposed on 0.0.0.0:2379"
  else
    record_check pass info etcd_exposed "etcd is not exposed on 0.0.0.0:2379"
  fi
}

check_api_read_auth_gate() {
  local pid env_raw read_token code
  pid="$(find_controller_pid)"
  if [[ -z "$pid" ]]; then return 0; fi
  env_raw="$(read_environ_raw "$pid" || true)"
  if [[ -z "$env_raw" ]]; then return 0; fi
  read_token="$(proc_env_get "$env_raw" AE_API_READ_TOKEN || true)"

  if [[ -z "$read_token" ]]; then
    record_check warn low api_read_gate "AE_API_READ_TOKEN unset; read auth gate check skipped"
    return
  fi

  code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 8 "${API_URL%/}/status" || true)"
  case "$code" in
    401|403)
      record_check pass info api_read_gate "unauthenticated GET /status denied (code=$code)"
      ;;
    000)
      record_check fail medium api_read_gate "unable to reach controller API at ${API_URL%/}/status"
      ;;
    *)
      record_check fail critical api_read_gate "unauthenticated GET /status unexpectedly allowed (code=$code)"
      ;;
  esac
}

write_json() {
  local out="$1"
  python - "$CHECK_FILE" "$out" "$PROFILE" "$FAIL_ON" "$API_URL" <<'PY'
import json
import sys
from pathlib import Path

check_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
profile = sys.argv[3]
fail_on = sys.argv[4]
api_url = sys.argv[5]

rows = []
for line in check_path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    status, severity, code, message = line.split("\t", 3)
    rows.append(
        {
            "status": status,
            "severity": severity,
            "code": code,
            "message": message,
        }
    )

summary = {
    "total_checks": len(rows),
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
    "fail_on": fail_on,
    "summary": summary,
    "checks": rows,
}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profile)
        PROFILE="${2:-}"
        shift 2
        ;;
      --core-specs-dir)
        CORE_SPECS_DIR="${2:-}"
        shift 2
        ;;
      --api-url)
        API_URL="${2:-}"
        shift 2
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

  need_cmd rg
  need_cmd ss
  need_cmd curl
  need_cmd python

  check_controller_and_env
  check_exposed_sensitive_ports
  check_api_read_auth_gate
  check_workload_security_defaults

  while IFS=$'\t' read -r status severity code message; do
    case "$status" in
      pass) log "PASS [$severity] [$code] $message" ;;
      warn) log "WARN [$severity] [$code] $message" ;;
      fail) log "FAIL [$severity] [$code] $message" ;;
      *) log "INFO [$severity] [$code] $message" ;;
    esac
  done < "$CHECK_FILE"

  write_json "$RESULT_JSON"
  log "result_json=$RESULT_JSON"

  if (( FAIL_BUILD == 1 )); then
    die "baseline failed for fail-on=$FAIL_ON"
  fi
  log "baseline passed"
}

main "$@"
