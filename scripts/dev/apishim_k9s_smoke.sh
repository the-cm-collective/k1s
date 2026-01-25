#!/usr/bin/env bash
set -euo pipefail

KUBECONFIG_PATH="${K9S_KUBECONFIG:-${KUBECONFIG:-}}"
NAMESPACE="${K9S_NAMESPACE:-default}"
POD="${K9S_POD:-}"
TOKEN="${K9S_TOKEN:-}"
ALLOW_FAIL="${K9S_SMOKE_ALLOW_FAIL:-0}"
TIMEOUT="${K9S_TIMEOUT:-30}"
EXEC_KEY="${K9S_EXEC_KEY:-s}"
PF_KEY="${K9S_PORT_FORWARD_KEY:-p}"
PF_PORT="${K9S_PORT_FORWARD_PORT:-8080}"
PF_SPEC="${K9S_PORT_FORWARD_SPEC:-${PF_PORT}}"
PF_LOCAL="${K9S_PORT_FORWARD_LOCAL:-${PF_PORT}}"
EXEC_TEXT="${K9S_EXEC_TEXT:-k9s-exec-ok}"
LOG_FILE="${K9S_LOG:-/tmp/k9s-smoke.log}"
SCREEN_DIR="${K9S_SCREEN_DIR:-/tmp/k9s-smoke-screens}"

if [[ -z "${KUBECONFIG_PATH}" ]]; then
  echo "error: KUBECONFIG not set (use K9S_KUBECONFIG or KUBECONFIG)" >&2
  exit 1
fi

if ! command -v k9s >/dev/null 2>&1; then
  msg="k9s not installed; skipping k9s smoke"
  if [[ "${ALLOW_FAIL}" == "1" ]]; then
    echo "${msg}"
    exit 0
  fi
  echo "error: ${msg}" >&2
  exit 1
fi

if ! command -v expect >/dev/null 2>&1; then
  msg="expect not installed; cannot automate k9s smoke"
  if [[ "${ALLOW_FAIL}" == "1" ]]; then
    echo "${msg}"
    exit 0
  fi
  echo "error: ${msg}" >&2
  exit 1
fi

if [[ "${PF_SPEC}" == *:* ]]; then
  PF_LOCAL="${PF_SPEC%%:*}"
  PF_PORT="${PF_SPEC##*:}"
elif [[ -n "${PF_SPEC}" ]]; then
  PF_PORT="${PF_SPEC}"
  PF_LOCAL="${PF_LOCAL:-${PF_PORT}}"
fi

if [[ -z "${POD}" ]]; then
  POD="$(kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${NAMESPACE}" \
    get pods -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi

if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(python - <<'PY' "${KUBECONFIG_PATH}"
import re
import sys
path = sys.argv[1]
token = ""
try:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"\\s*token:\\s*(\\S+)", line)
            if m:
                token = m.group(1)
                break
except Exception:
    token = ""
print(token)
PY
)"
fi

if [[ -z "${POD}" ]]; then
  msg="no pod found for k9s smoke in namespace ${NAMESPACE}"
  if [[ "${ALLOW_FAIL}" == "1" ]]; then
    echo "${msg}"
    exit 0
  fi
  echo "error: ${msg}" >&2
  exit 1
fi

mkdir -p "${SCREEN_DIR}"
 : > "${LOG_FILE}"

export TIMEOUT POD EXEC_KEY PF_KEY PF_PORT PF_LOCAL EXEC_TEXT

cmd=(
  k9s
  --kubeconfig "${KUBECONFIG_PATH}"
  --namespace "${NAMESPACE}"
  --command pods
  --headless
  --logLevel info
  --logFile "${LOG_FILE}"
  --screen-dump-dir "${SCREEN_DIR}"
)
if [[ -n "${TOKEN}" ]]; then
  cmd+=(--token "${TOKEN}")
fi

set +e
timeout "${TIMEOUT}s" expect -f - -- "${cmd[@]}" <<'EXP'
  set timeout $env(TIMEOUT)
  spawn -noecho {*}$argv
  # Ensure pods view and target selection.
  sleep 2
  send ":pods\r"
  sleep 1
  send "/$env(POD)\r"
  sleep 1
  # Exec shell (best-effort).
  set exec_ok 0
  send "$env(EXEC_KEY)"
  expect {
    -re {K9s-Shell} {}
    -re {/app \$} {}
    timeout {exit 2}
  }
  send "echo $env(EXEC_TEXT)\r"
  expect {
    -re $env(EXEC_TEXT) {set exec_ok 1}
    timeout {exit 2}
  }
  send "exit\r"
  expect {
    -re {Pods\(} {}
    timeout {exit 2}
  }
  send "\033"
  sleep 1
  # Port-forward (best-effort).
  send "$env(PF_KEY)"
  sleep 2
  send "$env(PF_LOCAL)\t$env(PF_PORT)\r"
  sleep 2
  set pf_local $env(PF_LOCAL)
  if {[catch {exec ss -ltnp} ssout] == 0} {
    foreach line [split $ssout "\n"] {
      if {[regexp {LISTEN\s+\d+\s+\d+\s+.*:(\d+)\s+.*k9s} $line -> port]} {
        set pf_local $port
        break
      }
    }
  }
  set curl_ok 0
  for {set i 0} {$i < 5} {incr i} {
    if {[catch {exec curl -fsS --max-time 2 http://127.0.0.1:$pf_local/} out] == 0} {
      set curl_ok 1
      break
    }
    after 500
  }
  if {!$curl_ok} {
    exit 3
  }
  send "\033"
  sleep 1
  send ":quit\r"
  sleep 1
  send "q"
  sleep 1
  send "\003"
  expect eof
EXP
rc=$?
set -e

if [[ "${rc}" -ne 0 ]]; then
  msg="k9s smoke failed (exit ${rc})"
  if [[ "${ALLOW_FAIL}" == "1" ]]; then
    echo "${msg}; continuing"
    exit 0
  fi
  echo "error: ${msg}" >&2
  exit "${rc}"
fi

if [[ -f "${LOG_FILE}" ]]; then
  if grep -E -q "access denied|panic" "${LOG_FILE}"; then
    msg="k9s smoke failed (see ${LOG_FILE})"
    if [[ "${ALLOW_FAIL}" == "1" ]]; then
      echo "${msg}; continuing"
      exit 0
    fi
    echo "error: ${msg}" >&2
    exit 1
  fi
fi

echo "k9s smoke completed (pod=${POD})"
