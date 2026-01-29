#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '\033[1;32m[init-demo]\033[0m %s\n' "$1"
}

require_root_or_sudo() {
  if [[ "$EUID" -ne 0 ]]; then
    echo "sudo"
  else
    echo ""
  fi
}

SUDO=$(require_root_or_sudo)

# Static demo hosts we can safely add to /etc/hosts when requested.
# Note: echo-storage and echo-stateful do not expose ingress by default.
HOSTS=(blue.home.arpa green.home.arpa docs.home.arpa api.home.arpa dash.home.arpa echo.home.arpa echo-mr.home.arpa echo-multi.home.arpa echo-resources.home.arpa echo-sec.home.arpa echo-tcp.home.arpa echo-exec.home.arpa echo-hardened.home.arpa)
AUTO_HOSTS=""  # set by -y/--yes or -n/--no to auto answer host prompts

# Summarized stop helper: log PIDs before killing by pattern
stop_by_pattern() {
  # $1 = regex pattern (pgrep/pkill -f), $2 = label
  local pat="$1"; local label="${2:-processes}"
  local found
  found=$(pgrep -f -- "$pat" 2>/dev/null || true)
  if [[ -n "$found" ]]; then
    # Flatten PIDs onto one line for readability
    local flat; flat=$(echo "$found" | tr '\n' ' ' | sed 's/  */ /g')
    log "Stopping ${label}: ${flat}"
    pkill -f -- "$pat" 2>/dev/null || true
  fi
}

apishim_health_code() {
  # Emit "code|body" for an apishim /healthz probe using the provided token.
  local port="$1"
  local token="$2"
  if ! command -v curl >/dev/null 2>&1; then
    echo "000|"
    return 0
  fi
  local url="https://127.0.0.1:${port}/healthz"
  local out
  out="$(curl -sk -H "Authorization: Bearer ${token}" "${url}" -w "\n%{http_code}" 2>/dev/null || true)"
  local code="${out##*$'\n'}"
  local body="${out%$'\n'*}"
  if [[ -z "${code}" ]]; then
    code="000"
  fi
  echo "${code}|${body}"
}

start_apishim() {
  if [[ ${LABS_ENABLE:-0} -ne 1 ]]; then
    return 0
  fi
  if [[ "${AE_APISHIM_AUTOSTART:-1}" != "1" ]]; then
    log "Skipping apishim autostart (AE_APISHIM_AUTOSTART=${AE_APISHIM_AUTOSTART})"
    return 0
  fi
  APISHIM_PORT=${APISHIM_PORT:-8445}
  APISHIM_HOST=${APISHIM_HOST:-0.0.0.0}
  local env_file="state/labs/apishim.env"
  if [[ ! -f "${env_file}" ]]; then
    ./scripts/ensure_apishim_env.sh
  fi
  if [[ -f "${env_file}" ]]; then
    export AE_APISHIM_TOKEN="${AE_APISHIM_TOKEN:-$(read_env_file_var "AE_APISHIM_TOKEN" "${env_file}" || true)}"
    export AE_APISHIM_READ_TOKEN="${AE_APISHIM_READ_TOKEN:-$(read_env_file_var "AE_APISHIM_READ_TOKEN" "${env_file}" || true)}"
    export AE_APISHIM_SESSION_SECRET="${AE_APISHIM_SESSION_SECRET:-$(read_env_file_var "AE_APISHIM_SESSION_SECRET" "${env_file}" || true)}"
  fi
  if port_open "127.0.0.1" "${APISHIM_PORT}"; then
    local pid=""
    local restart_reason=""
    local probe=""
    local code=""
    local body=""
    if [[ -f state/apishim.pid ]]; then
      pid=$(cat state/apishim.pid || true)
    fi
    if [[ -n "$pid" && -r "/proc/${pid}/environ" ]]; then
      local expected=""
      local running=""
      local key=""
      for key in AE_APISHIM_SESSION_SECRET AE_APISHIM_TOKEN AE_APISHIM_READ_TOKEN; do
        expected="${!key:-}"
        if [[ -z "$expected" && -f "${env_file}" ]]; then
          expected="$(read_env_file_var "$key" "${env_file}" || true)"
        fi
        running="$(read_proc_env_var "$pid" "$key" || true)"
        if [[ -n "$expected" && -n "$running" && "$expected" != "$running" ]]; then
          restart_reason="${key} mismatch"
          break
        fi
      done
    elif [[ -n "$pid" ]]; then
      log "Apishim already running on 127.0.0.1:${APISHIM_PORT} (env not readable)."
      return 0
    else
      # No pid file: probe healthz with current token to avoid mismatched apishim instances.
      if [[ -n "${AE_APISHIM_TOKEN:-}" ]]; then
        probe="$(apishim_health_code "${APISHIM_PORT}" "${AE_APISHIM_TOKEN}")"
        code="${probe%%|*}"
        body="${probe#*|}"
        if [[ "${code}" == "200" ]]; then
          log "Apishim already running on 127.0.0.1:${APISHIM_PORT} (token ok)."
          return 0
        fi
        if [[ "${code}" == "401" || "${code}" == "403" ]]; then
          if echo "${body}" | grep -q "missing/invalid bearer token"; then
            restart_reason="token mismatch"
          else
            log "Apishim already running on 127.0.0.1:${APISHIM_PORT} (auth ${code}); reusing."
            return 0
          fi
        else
          log "Apishim already running on 127.0.0.1:${APISHIM_PORT} (status ${code}); reusing."
          return 0
        fi
      else
        log "Apishim already running on 127.0.0.1:${APISHIM_PORT} (no pid file)."
        return 0
      fi
    fi
    if [[ -n "$restart_reason" ]]; then
      log "Apishim already running but ${restart_reason}; restarting."
      stop_apishim
      if port_open "127.0.0.1" "${APISHIM_PORT}"; then
        log "Apishim port ${APISHIM_PORT} still in use. Stop other stacks (e.g., labs-aio) or set AE_APISHIM_AUTOSTART=0."
        return 1
      fi
    else
      log "Apishim already running on 127.0.0.1:${APISHIM_PORT}"
      return 0
    fi
  fi
  export AE_APISHIM_RUNTIME="${AE_APISHIM_RUNTIME:-${AE_RUNTIME_BACKEND}}"
  export AE_APISHIM_ENABLE=1
  export AE_APISHIM_ALLOW_ANON=0
  export AE_APISHIM_RBAC=1
  export AE_APISHIM_RBAC_EVAL=0
  export AE_APISHIM_TLS_CERT="${AE_APISHIM_TLS_CERT:-state/labs/apishim.crt}"
  export AE_APISHIM_TLS_KEY="${AE_APISHIM_TLS_KEY:-state/labs/apishim.key}"
  log "Starting apishim (runtime=${AE_APISHIM_RUNTIME}) on https://${APISHIM_HOST}:${APISHIM_PORT}"
  mkdir -p state
  nohup "$PY_BIN" -m ae.apishim serve --host "${APISHIM_HOST}" --port "${APISHIM_PORT}" --tls \
    > state/apishim.log 2>&1 &
  echo $! > state/apishim.pid
}

stop_apishim() {
  if [[ -f state/apishim.pid ]]; then
    local pid
    pid=$(cat state/apishim.pid || true)
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      log "Stopping apishim (pid ${pid})"
      kill "${pid}" || true
    fi
    rm -f state/apishim.pid
  fi
  stop_by_pattern '[p]ython.* -m ae\\.apishim' 'apishim(s)'
}

cleanup_demo_containers() {
  local bin="$1"
  if [[ -z "${bin}" ]] || ! command -v "$bin" >/dev/null 2>&1; then
    return 0
  fi
  local apps_regex="blue|green|shell-demo|echo|echo-mr|echo-multi|echo-sec|echo-tcp|echo-exec|echo-hardened|echo-resources|echo-storage"
  local names to_remove=()
  names=$("$bin" ps -a --format '{{.Names}}' 2>/dev/null || true)
  if [[ -z "$names" ]]; then
    return 0
  fi
  while read -r name; do
    if [[ "$name" =~ ^ae-(${apps_regex})-rev[0-9]+- ]]; then
      to_remove+=("$name")
    fi
  done <<<"$names"
  if [[ ${#to_remove[@]} -gt 0 ]]; then
    log "Removing stale demo containers (${bin}): ${to_remove[*]}"
    "$bin" rm -f "${to_remove[@]}" >/dev/null 2>&1 || true
  fi
}

usage() {
  cat <<USAGE
Usage:
  ./scripts/init_demo.sh [OPTIONS]      # Set up the demo environment
  ./scripts/init_demo.sh --down [OPTS]  # Tear the demo down (and optionally clean hosts)
  ./scripts/init_demo.sh --reset        # Reset controller state/cache and exit
  ./scripts/init_demo.sh --help         # Show this help

Options:
  -y, --yes    Automatically accept /etc/hosts modification prompts (setup/teardown)
  -n, --no     Automatically decline /etc/hosts modification prompts (setup/teardown)
  --no-controller  Do not auto-start the controller daemon
  --no-supervisor  Start controller once (no restart loop)
  -d, --debug  Attach logs to console for troubleshooting (blocks; Ctrl-C to exit)
  --reset      Delete controller state DB and projections (clean slate)
  --reset-registry-cache  Clear the local registry cache (state/registry) before start
  --bind-all   Bind local helpers (docs server) to 0.0.0.0 instead of 127.0.0.1 for LAN access
  --with-secrets-env  Export AE_ALLOW_PLAINTEXT_SECRETS=1 and SOPS_AGE_KEY_FILE=$HOME/.config/ae/keys.txt for this demo run
  --demo-configs   Apply the configs/secrets demo (echo) and enable plaintext secrets for local run
  --demo-standard  Apply the standard demo (blue, green)
  --demo-echo-mr   Apply the multi-replica echo demo (echo-mr)
  --demo-echo-multi Apply the multi-port echo demo (echo-multi)
  --demo-security  Apply security-hardened demo (echo-sec)
  --demo-tcp       Apply TCP-probe demo (echo-tcp)
  --demo-exec      Apply exec-probe demo (echo-exec)
  --demo-hardened  Apply hardened echo demo (echo-hardened)
  --docs-only      Start docs + API only (no apps)
  --demo-rollout   Apply a two-step ordered rollout for echo
  --demo-storage   Apply a storage (PV-lite) demo for echo and list volumes
  --hosts-ip IP    Use this IP for /etc/hosts entries (default 127.0.0.1)
  --labs           Enable Labs API on the controller (playground actions)
  --labs-token T   Enable Labs with bearer token T (use with --labs)

What this does (setup):
  1) Ensures required system packages (python3, venv, pip, sqlite3, age, sops) are present
  2) Creates a Python virtualenv (.venv-demo) and installs project deps
  3) Prepares demo images: pre-pulls a multi‑arch echo image and builds the local green image under samples/servers/
  4) Starts the dev stack (Caddy on :8888 and Prometheus on :9090)
  5) Optionally appends hosts entries for: ${HOSTS[*]}
  6) Applies example manifests (blue, green) via the ae CLI
  7) Builds static docs and serves them locally on DOCS_PORT (default 9109)
  8) Prints ready URLs and a command to run the controller loop with API

What this does (down):
  - Stops the docs server (PID file: state/docs_server.pid)
  - docker compose down for the dev stack (Caddy, Prometheus)
  - Removes demo app containers (any with label ae.app)
  - Optionally removes hosts entries for: ${HOSTS[*]}

Prerequisites:
  - Ubuntu/Debian with sudo access
  - Docker Engine installed and enabled (for dev stack), or Podman for OCI runtime

Environment variables you can override:
  - VENV_DIR (default .venv-demo)
  - DOCS_PORT (default 9109)
  - AE_STATE_DB, AE_SPECS_DIR, AE_CADDY_* (see docs/ops/runbook.md)
  - AE_USE_REGISTRY_CACHE (default 1) to enable local pull-through cache for dev stack
  - AE_REGISTRY_PORT (default 5001), AE_REGISTRY_HOST (default localhost:${AE_REGISTRY_PORT})
  - AE_REGISTRY_IMAGE (default registry:2) to override the registry cache image
  - AE_REGISTRY_USERNAME/AE_REGISTRY_PASSWORD/AE_REGISTRY_REMOTEURL for upstream registry auth

Endpoints after setup:
  - Apps via Caddy: https://blue.home.arpa:8443/ (multi‑arch echo) and https://green.home.arpa:8443/ (local build)
  - Docs via Caddy: https://docs.home.arpa:8443/
  - API via Caddy:  https://api.home.arpa:8443/ (Swagger /swagger, ReDoc /redoc, Dashboard /dashboard)
  - Docs direct:    http://127.0.0.1:9109/

USAGE
}

# Parse flags (supports combining with --down)
DOWN_FLAG=0
RESET_FLAG=0
RESET_REGISTRY_CACHE=0
NO_CONTROLLER=0
API_PORT=${API_PORT:-9108}
# Runtime backend (default to podman/OCI if not set)
AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND:-podman}
# Convenience: register a local node for demo/labs unless explicitly disabled
AE_REGISTER_LOCAL_NODE=${AE_REGISTER_LOCAL_NODE:-1}
AE_USE_REGISTRY_CACHE=${AE_USE_REGISTRY_CACHE:-1}
NO_SUPERVISOR=0
DEBUG_ATTACH=0
DEMO_CONFIGS=0
DEMO_STANDARD=0
DEMO_ECHO_MR=0
DEMO_ECHO_MULTI=0
DEMO_SECURITY=0
DEMO_TCP=0
DEMO_EXEC=0
DEMO_HARDENED=0
DOCS_ONLY=0
DEMO_ROLLOUT=0
DEMO_STORAGE=0
HOSTS_IP=${HOSTS_IP:-127.0.0.1}
# Labs/demo playground flags
LABS_ENABLE=${LABS_ENABLE:-0}
LABS_TOKEN=${LABS_TOKEN:-}
# Secrets env convenience flag (sets AE_ALLOW_PLAINTEXT_SECRETS=1 and SOPS_AGE_KEY_FILE)
WITH_SECRETS_ENV=0
# Default location for the curated demo specs set (controller watches this)
DEMO_SPECS_DIR=${DEMO_SPECS_DIR:-state/demo-specs}

# Compose file list for dev stack (optionally include registry cache override)
DEV_COMPOSE_FILES=(-f ops/dev/docker-compose.yaml)
DEV_COMPOSE_FILES_WITH_CACHE=("${DEV_COMPOSE_FILES[@]}")
if [[ -f ops/dev/docker-compose.cache.override.yml ]]; then
  DEV_COMPOSE_FILES_WITH_CACHE=(-f ops/dev/docker-compose.yaml -f ops/dev/docker-compose.cache.override.yml)
  if [[ "${AE_USE_REGISTRY_CACHE}" == "1" ]]; then
    DEV_COMPOSE_FILES=("${DEV_COMPOSE_FILES_WITH_CACHE[@]}")
  fi
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h|help)
      usage; exit 0 ;;
    --down|down)
      DOWN_FLAG=1 ;;
    --reset|reset)
      RESET_FLAG=1 ;;
    --reset-registry-cache)
      RESET_REGISTRY_CACHE=1 ;;
    -y|--yes)
      AUTO_HOSTS=Y ;;
    -n|--no)
      AUTO_HOSTS=N ;;
    --no-controller)
      NO_CONTROLLER=1 ;;
    --no-supervisor)
      NO_SUPERVISOR=1 ;;
    -d|--debug)
      DEBUG_ATTACH=1 ;;
    --demo-configs)
      DEMO_CONFIGS=1 ;;
    --demo-standard)
      DEMO_STANDARD=1 ;;
    --demo-echo-mr)
      DEMO_ECHO_MR=1 ;;
    --demo-echo-multi)
      DEMO_ECHO_MULTI=1 ;;
    --demo-security)
      DEMO_SECURITY=1 ;;
    --demo-tcp)
      DEMO_TCP=1 ;;
    --demo-exec)
      DEMO_EXEC=1 ;;
    --demo-hardened)
      DEMO_HARDENED=1 ;;
    --docs-only)
      DOCS_ONLY=1 ;;
    --demo-rollout)
      DEMO_ROLLOUT=1 ;;
    --demo-storage)
      DEMO_STORAGE=1 ;;
    --with-secrets-env)
      WITH_SECRETS_ENV=1 ;;
    --labs)
      LABS_ENABLE=1 ;;
    --labs-token)
      LABS_TOKEN=${2:?requires token}; LABS_ENABLE=1; shift ;;
    --hosts-ip)
      HOSTS_IP=${2:?requires IP}; shift ;;
    --bind-all)
      DOCS_BIND=0.0.0.0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2 ;;
  esac
  shift
done

prompt_yes_no() {
  # $1 = prompt message, $2 = default (Y/N)
  local prompt msg default reply
  msg="$1"
  default="${2:-Y}"
  if [[ "$default" == "Y" || "$default" == "y" ]]; then
    prompt="$msg [Y/n]: "
  else
    prompt="$msg [y/N]: "
  fi
  read -r -p "$prompt" reply || reply=""
  if [[ -z "$reply" ]]; then
    reply="$default"
  fi
  case "$reply" in
    Y|y|Yes|yes) return 0 ;;
    *) return 1 ;;
  esac
}

# Wrapper for hosts prompts that honors AUTO_HOSTS override
prompt_yes_no_hosts() {
  # $1 = prompt message, $2 = default (Y/N)
  if [[ -n "$AUTO_HOSTS" ]]; then
    [[ "$AUTO_HOSTS" == "Y" || "$AUTO_HOSTS" == "y" ]]
    return
  fi
  prompt_yes_no "$1" "${2:-Y}"
}

read_env_file_var() {
  # Read key=value from an env file without executing it.
  local key="$1"
  local file="$2"
  if [[ ! -f "$file" ]]; then
    return 1
  fi
  awk -F= -v k="$key" '
    $1 ~ "^[[:space:]]*"k"[[:space:]]*$" {
      sub(/^[[:space:]]*[^=]+[[:space:]]*=[[:space:]]*/, "", $0)
      gsub(/^[[:space:]]*"/, "", $0)
      gsub(/"[[:space:]]*$/, "", $0)
      gsub(/^[[:space:]]*'\''/, "", $0)
      gsub(/'\''[[:space:]]*$/, "", $0)
      print $0
      exit
    }
  ' "$file"
}

read_proc_env_var() {
  # Read key=value from a process environ (best-effort).
  local pid="$1"
  local key="$2"
  local env_file="/proc/${pid}/environ"
  if [[ -z "$pid" || ! -r "$env_file" ]]; then
    return 1
  fi
  tr '\0' '\n' < "$env_file" | awk -v k="$key" '
    index($0, k"=") == 1 {
      sub(/^[^=]*=/, "", $0)
      print $0
      exit
    }
  '
}

gen_token() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
    return 0
  fi
  head -c 32 /dev/urandom | base64 | tr -d '=\n'
}

port_open() {
  local host="$1"
  local port="$2"
  local py="python3"
  if ! command -v "$py" >/dev/null 2>&1; then
    py="python"
  fi
  "$py" - "$host" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(0.3)
try:
    sock.connect((host, port))
    sys.exit(0)
except Exception:
    sys.exit(1)
finally:
    try:
        sock.close()
    except Exception:
        pass
PY
}

warn_insecure_registry() {
  local host_port="$1"
  local engine="$2"
  if [[ "$host_port" != *:* ]]; then
    return 0
  fi
  local host="${host_port%:*}"
  case "$host" in
    localhost|127.0.0.1|::1) ;;
    *) return 0 ;;
  esac
  if [[ "$engine" == "docker" ]]; then
    local daemon_json_sys="/etc/docker/daemon.json"
    local daemon_json_user="${XDG_CONFIG_HOME:-$HOME/.config}/docker/daemon.json"
    for daemon_json in "$daemon_json_user" "$daemon_json_sys"; do
      if [[ -r "$daemon_json" ]]; then
        if grep -q "\"insecure-registries\"" "$daemon_json" && grep -q "$host_port" "$daemon_json"; then
          return 0
        fi
      fi
    done
    log "Warning: Docker insecure registry for ${host_port} not detected."
    log "Add it to /etc/docker/daemon.json (or ${daemon_json_user} for rootless Docker) and restart the Docker daemon."
    return 1
  else
    local conf_user="${XDG_CONFIG_HOME:-$HOME/.config}/containers/registries.conf"
    local conf_sys="/etc/containers/registries.conf"
    local conf_user_dir="${XDG_CONFIG_HOME:-$HOME/.config}/containers/registries.conf.d"
    local conf_sys_dir="/etc/containers/registries.conf.d"
    _conf_has_insecure() {
      local file="$1"
      if [[ -r "$file" ]] && grep -q "location *= *\"${host_port}\"" "$file"; then
        if grep -q "insecure *= *true" "$file"; then
          return 0
        fi
      fi
      return 1
    }
    if _conf_has_insecure "$conf_user" || _conf_has_insecure "$conf_sys"; then
      return 0
    fi
    for conf_dir in "$conf_user_dir" "$conf_sys_dir"; do
      if [[ -d "$conf_dir" ]]; then
        for file in "$conf_dir"/*.conf; do
          [[ -e "$file" ]] || continue
          if _conf_has_insecure "$file"; then
            return 0
          fi
        done
      fi
    done
    log "Warning: Podman registry config for ${host_port} not detected."
    log "Set insecure=true for ${host_port} in ${conf_user} or ${conf_sys} (or a registries.conf.d drop-in)."
    log "If running podman system service, restart it after updating the config."
    return 1
  fi
}

token_valid() {
  local scheme="$1"
  local host="$2"
  local port="$3"
  local token="$4"
  if [[ -z "$token" ]]; then
    return 1
  fi
  local py="python3"
  if ! command -v "$py" >/dev/null 2>&1; then
    py="python"
  fi
  "$py" - "$scheme" "$host" "$port" "$token" <<'PY'
import sys
import urllib.request
import ssl

scheme = sys.argv[1]
host = sys.argv[2]
port = int(sys.argv[3])
token = sys.argv[4]
url = f"{scheme}://{host}:{port}/api/v1/namespaces"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    with urllib.request.urlopen(req, timeout=0.6, context=ctx) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

if [[ $DOWN_FLAG -eq 1 ]]; then
  log "Tearing down demo environment"
  # Stop controller if running
  if [[ -f state/controller.pid ]]; then
    CTRL_PID=$(cat state/controller.pid || true)
    if [[ -n "${CTRL_PID}" ]] && kill -0 "$CTRL_PID" 2>/dev/null; then
      log "Stopping controller (pid ${CTRL_PID})"
      kill "$CTRL_PID" || true
    fi
    rm -f state/controller.pid
  fi
  # Stop supervisor if running
  if [[ -f state/controller_supervisor.pid ]]; then
    SUP_PID=$(cat state/controller_supervisor.pid || true)
    if [[ -n "${SUP_PID}" ]] && kill -0 "$SUP_PID" 2>/dev/null; then
      log "Stopping controller supervisor (pid ${SUP_PID})"
      kill "$SUP_PID" || true
    fi
    rm -f state/controller_supervisor.pid state/controller_supervisor.lock || true
  fi
  # Best-effort: stop stray supervisors/controllers (bench-launched or different venvs) with a visible summary
  stop_by_pattern '[p]ython.* -m ae\.controller' 'controller(s)'
  stop_by_pattern '[s]cripts/supervise_controller\.sh' 'supervisor(s)'
  # Stop apishim if running
  stop_apishim
  # Stop docs server if running
  if [[ -f state/docs_server.pid ]]; then
    DOCS_PID=$(cat state/docs_server.pid || true)
    if [[ -n "${DOCS_PID}" ]] && kill -0 "$DOCS_PID" 2>/dev/null; then
      log "Stopping docs server (pid ${DOCS_PID})"
      kill "$DOCS_PID" || true
    fi
    rm -f state/docs_server.pid
  fi
  # Stop dev stack
  if command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1; then
    # Prefer podman if present to match setup; otherwise docker
    if command -v podman >/dev/null 2>&1; then
      STACK_BIN_DOWN=podman
    else
      STACK_BIN_DOWN=docker
    fi
    STACK_COMPOSE_DOWN=("$STACK_BIN_DOWN" compose)
    log "Stopping dev ${STACK_BIN_DOWN} compose stack (caddy, prometheus)"
    "${STACK_COMPOSE_DOWN[@]}" "${DEV_COMPOSE_FILES_WITH_CACHE[@]}" down || true
    log "Stopping ${STACK_BIN_DOWN} labs stacks (labs-aio, labs-compose)"
    "${STACK_COMPOSE_DOWN[@]}" -f ops/dev/labs-aio.yaml down || true
    "${STACK_COMPOSE_DOWN[@]}" -f ops/dev/labs-compose.yaml down || true
    # Remove demo app containers
    log "Removing demo app containers (label=ae.app)"
    ids=$("$STACK_BIN_DOWN" ps -aq --filter "label=ae.app" || true)
    if [[ -n "$ids" ]]; then
      "$STACK_BIN_DOWN" rm -f $ids || true
    fi
  fi
  # Clear dynamic Caddy site fragments to avoid stale host collisions on next start
  if [[ -d state/caddy ]]; then
    log "Clearing dynamic Caddy sites under state/caddy/*.caddy"
    rm -f state/caddy/*.caddy 2>/dev/null || true
  fi
  if [[ -d state/labs ]]; then
    log "Clearing Labs shim artifacts under state/labs/"
    rm -f state/labs/helm-demo.log state/labs/apishim.env 2>/dev/null || true
  fi
  # Optional full reset of controller state/caches on --down --reset
  if [[ $RESET_FLAG -eq 1 ]]; then
    if [[ -f state/controller.db ]]; then
      log "Removing controller state DB (state/controller.db)"
      rm -f state/controller.db 2>/dev/null || true
    fi
    if [[ -d state/projections ]]; then
      log "Removing projected config/state under state/projections/"
      rm -rf state/projections 2>/dev/null || true
    fi
  fi
  if [[ $RESET_REGISTRY_CACHE -eq 1 ]]; then
    log "Removing local registry cache under state/registry"
    rm -rf state/registry 2>/dev/null || true
  fi
  # Optionally remove hosts entries
  if prompt_yes_no_hosts "Remove hosts entries for ${HOSTS[*]} from /etc/hosts?" N; then
    for host in "${HOSTS[@]}"; do
      $SUDO sed -i.bak "/[[:space:]]$host\$/d" /etc/hosts || true
    done
    log "Hosts entries removed (backup at /etc/hosts.bak)"
  else
    log "Keeping hosts entries"
  fi
  # Optionally remove trusted local CA
  if command -v update-ca-certificates >/dev/null 2>&1; then
    if [[ -f /usr/local/share/ca-certificates/caddy-local-root.crt ]]; then
      if prompt_yes_no_hosts "Remove trusted Caddy local root CA from host trust store?" Y; then
        $SUDO rm -f /usr/local/share/ca-certificates/caddy-local-root.crt || true
        $SUDO update-ca-certificates >/dev/null 2>&1 || true
        log "Removed Caddy local root CA from host trust"
      fi
    fi
  fi
  log "Demo teardown complete."
  exit 0
fi

# Standalone reset without --down: attempt a safe stop and then clear state
if [[ $RESET_FLAG -eq 1 ]]; then
  log "Resetting controller state (DB, projections, dynamic sites)"
  # Best-effort stop like --down to avoid deleting an open DB
  if [[ -f state/controller.pid ]]; then
    CTRL_PID=$(cat state/controller.pid || true)
    if [[ -n "${CTRL_PID}" ]] && kill -0 "$CTRL_PID" 2>/dev/null; then
      log "Stopping controller (pid ${CTRL_PID}) for reset"
      kill "$CTRL_PID" || true
      sleep 0.2
    fi
    rm -f state/controller.pid || true
  fi
  if [[ -f state/controller_supervisor.pid ]]; then
    SUP_PID=$(cat state/controller_supervisor.pid || true)
    if [[ -n "${SUP_PID}" ]] && kill -0 "$SUP_PID" 2>/dev/null; then
      log "Stopping controller supervisor (pid ${SUP_PID}) for reset"
      kill "$SUP_PID" || true
      sleep 0.2
    fi
    rm -f state/controller_supervisor.pid state/controller_supervisor.lock || true
  fi
  # Broad best-effort stop of any strays with summary
  stop_by_pattern '[s]cripts/supervise_controller\.sh' 'supervisor(s)'
  stop_by_pattern '[p]ython.* -m ae\.controller' 'controller(s)'
  stop_apishim
  # Clear state
  if [[ -f state/controller.db ]]; then
    log "Removing controller state DB (state/controller.db)"
    rm -f state/controller.db 2>/dev/null || true
  fi
  if [[ -d state/projections ]]; then
    log "Removing projected config/state under state/projections/"
    rm -rf state/projections 2>/dev/null || true
  fi
  if [[ -d state/caddy ]]; then
    log "Clearing dynamic Caddy sites under state/caddy/*.caddy"
    rm -f state/caddy/*.caddy 2>/dev/null || true
  fi
  if [[ -d state/labs ]]; then
    log "Clearing Labs shim artifacts under state/labs/"
    rm -f state/labs/helm-demo.log state/labs/apishim.env 2>/dev/null || true
  fi
  log "Reset complete. Continuing with setup..."
fi

APT_PACKAGES=(
  python3
  python3-venv
  python3-pip
  sqlite3
  age
)

log "Installing system packages"
$SUDO apt-get update -y
$SUDO apt-get install -y "${APT_PACKAGES[@]}"

install_sops() {
  if $SUDO apt-get install -y sops; then
    return 0
  fi

  log "apt did not provide sops; downloading release binary"
  tmp_dir=$(mktemp -d)
  SOPS_VERSION=${SOPS_VERSION:-v3.8.1}
  arch=$(uname -m)
  case "$arch" in
    x86_64|amd64) sops_arch="amd64" ;;
    aarch64|arm64) sops_arch="arm64" ;;
    *) log "Unsupported architecture for sops: $arch" && return 1 ;;
  esac
  curl -fsSL -o "$tmp_dir/sops.deb" "https://github.com/mozilla/sops/releases/download/${SOPS_VERSION}/sops_${SOPS_VERSION#v}_${sops_arch}.deb"
  $SUDO dpkg -i "$tmp_dir/sops.deb"
  rm -rf "$tmp_dir"
}

install_sops || {
  log "Failed to install sops automatically; please install it and re-run."
  exit 1
}

if [[ "$AE_RUNTIME_BACKEND" == "docker" ]]; then
  log "Ensuring Docker service is running (backend=docker)"
  $SUDO systemctl enable --now docker
else
  # Prefer Podman for dev stack when available; otherwise fall back to Docker.
  if command -v podman >/dev/null 2>&1; then
    STACK_BIN=podman
  else
    STACK_BIN=docker
  fi
  if [[ "$STACK_BIN" == "docker" ]]; then
    if command -v docker >/dev/null 2>&1; then
      log "Ensuring Docker service is running for dev stack (backend=$AE_RUNTIME_BACKEND)"
      $SUDO systemctl enable --now docker || true
    fi
    if ! command -v podman >/dev/null 2>&1; then
      log "Podman not found; fallback backend will be docker. Set AE_RUNTIME_BACKEND=docker or install Podman."
    fi
  fi
fi

# Determine container stack CLI for compose/exec/cp/logs (docker or podman)
if [[ -z "${STACK_BIN:-}" ]]; then
  if command -v docker >/dev/null 2>&1; then
    STACK_BIN=docker
  elif command -v podman >/dev/null 2>&1; then
    STACK_BIN=podman
  else
    STACK_BIN=docker
  fi
fi
STACK_COMPOSE=("$STACK_BIN" compose)

# Runtime CLI for app containers (may differ from dev stack runtime)
if [[ "$AE_RUNTIME_BACKEND" == "docker" ]]; then
  if command -v docker >/dev/null 2>&1; then
    APP_BIN=docker
  else
    APP_BIN=podman
  fi
else
  if command -v podman >/dev/null 2>&1; then
    APP_BIN=podman
  else
    APP_BIN=docker
  fi
fi

# Optional cache reset before (re)starting the dev stack.
if [[ $RESET_REGISTRY_CACHE -eq 1 ]]; then
  if [[ -f ops/dev/docker-compose.cache.override.yml ]]; then
    log "Resetting local registry cache (state/registry)"
    ${STACK_COMPOSE[@]} "${DEV_COMPOSE_FILES_WITH_CACHE[@]}" stop registry >/dev/null 2>&1 || true
    ${STACK_COMPOSE[@]} "${DEV_COMPOSE_FILES_WITH_CACHE[@]}" rm -f -s registry >/dev/null 2>&1 || true
    rm -rf state/registry 2>/dev/null || true
  else
    log "Registry cache override not found; skipping registry cache reset"
  fi
fi

# Select host network alias for container->host routing
if [[ "$STACK_BIN" == "podman" ]]; then
  HOST_ALIAS=host.containers.internal
  # Prefer the stable host alias inside containers; raw gateway IPs (e.g., 169.254.1.2)
  # can be unreachable or vary per setup.
  unset AE_CADDY_HOST_ALIAS
else
  HOST_ALIAS=host.docker.internal
  unset AE_CADDY_HOST_ALIAS
fi

# Export shared network name so runtimes can attach app containers to the ingress network
export AE_DOCKER_NETWORK=${AE_DOCKER_NETWORK:-dev_default}
export AE_PODMAN_NETWORK=${AE_PODMAN_NETWORK:-dev_default}

# Ensure dev env defaults exist for compose-based stacks.
./scripts/ensure_dev_env.sh

VENV_DIR=${VENV_DIR:-.venv-demo}

if [[ ! -d "$VENV_DIR" ]]; then
  log "Creating Python virtual environment at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

PY_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

log "Installing Python dependencies inside virtualenv"
"$PY_BIN" -m pip install --upgrade pip
"$PIP_BIN" install -e .[dev]

log "Preparing demo images (backend=$AE_RUNTIME_BACKEND)"
# Pre-pull multi-arch echo image used by most samples for faster first run
if command -v podman >/dev/null 2>&1; then
  podman pull mendhak/http-https-echo:37 >/dev/null 2>&1 || true
fi
if command -v docker >/dev/null 2>&1; then
  docker pull mendhak/http-https-echo:37 >/dev/null 2>&1 || true
fi
# Build local demo images (green + shell-demo); blue samples use the pre-pulled echo image
if [[ "$AE_RUNTIME_BACKEND" == "podman" || "$AE_RUNTIME_BACKEND" == "oci" ]]; then
  if command -v podman >/dev/null 2>&1; then
    podman build -t localhost/demo-green:latest samples/servers/green || true
    podman build -t localhost/demo-shell:latest samples/servers/shell-demo || true
  else
    log "Podman not available; building images with Docker as a fallback"
    docker build -t demo-green:latest samples/servers/green || true
    docker build -t demo-shell:latest samples/servers/shell-demo || true
  fi
else
  docker build -t demo-green:latest samples/servers/green || true
  docker build -t demo-shell:latest samples/servers/shell-demo || true
fi

log "Starting local dev stack"
# Pick ports (prefer defaults, fall back if busy)
pick_port() {
  local preferred=$1
  local p=$preferred
  for ((i=0;i<50;i++)); do
    if ! ss -ltn 2>/dev/null | awk '{print $4}' | grep -E "[:\.]${p}$" >/dev/null; then
      echo "$p"; return 0
    fi
    p=$((p+1))
  done
  echo "$preferred"
}

# Podman/systemd cgroup pre-flight (avoid libpod-*.scope already loaded)
if [[ "$STACK_BIN" == "podman" ]]; then
  podman rm -f dev-caddy-1 dev-prometheus-1 2>/dev/null || true
  if systemctl --user status >/dev/null 2>&1; then
    systemctl --user reset-failed >/dev/null 2>&1 || true
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
fi

# Allow caller to override; otherwise choose free ports
export CADDY_HTTP_PORT=${CADDY_HTTP_PORT:-$(pick_port 8888)}
export CADDY_HTTPS_PORT=${CADDY_HTTPS_PORT:-$(pick_port 8443)}
export PROMETHEUS_PORT=${PROMETHEUS_PORT:-$(pick_port 9090)}
if [[ "$PROMETHEUS_PORT" != "9090" ]]; then
  log "Prometheus default port 9090 busy; using ${PROMETHEUS_PORT}"
fi
if [[ "$CADDY_HTTP_PORT" != "8888" || "$CADDY_HTTPS_PORT" != "8443" ]]; then
  log "Caddy ports mapped to HTTP=${CADDY_HTTP_PORT}, HTTPS=${CADDY_HTTPS_PORT}"
fi
export AE_APISHIM_SERVER=${AE_APISHIM_SERVER:-https://api.home.arpa:${CADDY_HTTPS_PORT}}
export DOCS_API_BASE=${DOCS_API_BASE:-https://api.home.arpa:${CADDY_HTTPS_PORT}}
export DOCS_DASHBOARD_URL=${DOCS_DASHBOARD_URL:-https://dash.home.arpa:${CADDY_HTTPS_PORT}/dashboard}
if [[ "${AE_USE_REGISTRY_CACHE}" == "1" && -f ops/dev/docker-compose.cache.override.yml ]]; then
  export AE_REGISTRY_PORT=${AE_REGISTRY_PORT:-$(pick_port 5001)}
  export AE_REGISTRY_HOST=${AE_REGISTRY_HOST:-localhost:${AE_REGISTRY_PORT}}
  export AE_REGISTRY_IMAGE=${AE_REGISTRY_IMAGE:-registry:2}
  if [[ "${AE_REGISTRY_PORT}" != "5001" ]]; then
    log "Registry cache default port 5001 busy; using ${AE_REGISTRY_PORT}"
  fi
  if ! warn_insecure_registry "${AE_REGISTRY_HOST}" "${STACK_BIN}"; then
    log "Registry cache requires an insecure registry entry for ${AE_REGISTRY_HOST}."
    if prompt_yes_no "Continue without registry cache? This may hit Docker Hub pull limits." "N"; then
      log "Continuing without registry cache (AE_USE_REGISTRY_CACHE=0)"
      AE_USE_REGISTRY_CACHE=0
    else
      log "Aborting. Configure the insecure registry or rerun with AE_USE_REGISTRY_CACHE=0."
      exit 1
    fi
  else
    log "Using local registry cache at ${AE_REGISTRY_HOST}"
    log "Registry cache image set to ${AE_REGISTRY_IMAGE}"
  fi
fi
# Ensure state directories exist with liberal perms for rootless Podman
mkdir -p state/caddy-data state/caddy docs/site || true
if [[ "${AE_USE_REGISTRY_CACHE}" == "1" && -f ops/dev/docker-compose.cache.override.yml ]]; then
  mkdir -p state/registry || true
  chmod -R 0777 state/registry || true
fi
# Ensure Caddy can write to /data even under rootless runtimes; if the directory is
# not writable (e.g., created by root from a previous run), replace it with a fresh one.
chmod -R 0777 state/caddy-data state/caddy || true
# Guard: prune any root-owned or otherwise unwritable cert subdirs that Caddy created previously
# These can appear after sudo/bench runs and make chmod fail with EPERM
if [[ -d state/caddy-data/caddy/certificates/local ]]; then
  while IFS= read -r -d '' d; do
    if [[ ! -w "$d" ]]; then
      log "Removing unwritable Caddy cert dir: ${d}"
      $SUDO rm -rf "$d" 2>/dev/null || true
    fi
  done < <(find state/caddy-data/caddy/certificates/local -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)
fi
if ! touch state/caddy-data/.write_test 2>/dev/null; then
  log "caddy-data not writable; recreating with liberal permissions"
  ts=$(date +%Y%m%d-%H%M%S)
  mv state/caddy-data "state/caddy-data.bak-${ts}" 2>/dev/null || true
  mkdir -p state/caddy-data
  chmod -R 0777 state/caddy-data || true
fi
rm -f state/caddy-data/.write_test 2>/dev/null || true

# Clean stale static site snippets (keep docs/api)
mkdir -p ops/dev/caddy/sites
find ops/dev/caddy/sites -maxdepth 1 -type f -name '*.caddy' \
  ! -name 'docs.caddy' ! -name 'api.caddy' -print -delete 2>/dev/null || true
# Controller writes dynamic sites under state/caddy (mounted as /etc/caddy/dynsites)
if [[ "${AE_USE_REGISTRY_CACHE}" == "1" && -f ops/dev/docker-compose.cache.override.yml ]]; then
  log "Starting local registry cache"
  ${STACK_COMPOSE[@]} "${DEV_COMPOSE_FILES_WITH_CACHE[@]}" up -d registry || true
  registry_host="${AE_REGISTRY_HOST%:*}"
  registry_port="${AE_REGISTRY_HOST##*:}"
  if [[ "$registry_host" == "$registry_port" ]]; then
    registry_host="127.0.0.1"
    registry_port="${AE_REGISTRY_PORT:-5001}"
  fi
  for _ in {1..25}; do
    if port_open "$registry_host" "$registry_port"; then
      break
    fi
    sleep 0.2
  done
  if ! port_open "$registry_host" "$registry_port"; then
    log "Registry cache not reachable at ${registry_host}:${registry_port} (image pulls may fail)"
  fi
fi
  if ! ${STACK_COMPOSE[@]} "${DEV_COMPOSE_FILES[@]}" up -d; then
    if [[ "$STACK_BIN" == "podman" ]]; then
      log "Compose up failed; retrying after Podman/systemd remedial steps"
      # Reset failed user units and prune any orphaned artifacts
      if systemctl --user status >/dev/null 2>&1; then
        systemctl --user reset-failed >/dev/null 2>&1 || true
        systemctl --user daemon-reload >/dev/null 2>&1 || true
      fi
      podman system prune -f >/dev/null 2>&1 || true
      podman rm -f dev-caddy-1 dev-prometheus-1 >/dev/null 2>&1 || true
      sleep 1
      ${STACK_COMPOSE[@]} "${DEV_COMPOSE_FILES[@]}" up -d
    else
      # Non-Podman path: rethrow
      ${STACK_COMPOSE[@]} "${DEV_COMPOSE_FILES[@]}" up -d
    fi
  fi

  # Trust Caddy's local CA on the host so browsers don't warn on every run.
  # This only applies to Debian/Ubuntu hosts (update-ca-certificates). If -y was
  # passed, do it non-interactively; otherwise still proceed best‑effort.
  if command -v update-ca-certificates >/dev/null 2>&1; then
    # Trigger local CA creation by touching one TLS site once (ignore TLS verify)
    curl -ksS "https://docs.home.arpa:${CADDY_HTTPS_PORT}/" >/dev/null 2>&1 || true
    # Give Caddy a moment to mint the local CA and leaf certs
    sleep 1
    mkdir -p state/certs
    ROOT_CA_HOST="state/caddy-data/pki/authorities/local/root.crt"
    if [[ ! -s "${ROOT_CA_HOST}" ]]; then
      # Fallback: try to copy from the running container (name may vary; prefer AE_CADDY_CONTAINER when set)
      CADDY_CONTAINER=${AE_CADDY_CONTAINER:-}
      if [[ -z "${CADDY_CONTAINER}" ]]; then
        # Best-effort: pick first container with image caddy and port 443 mapped
        CADDY_CONTAINER=$($STACK_BIN ps --format '{{.Names}}\t{{.Image}}' | awk '/caddy/ {print $1; exit}' || true)
      fi
      if [[ -n "${CADDY_CONTAINER}" ]]; then
        if "$STACK_BIN" exec "${CADDY_CONTAINER}" test -f /data/caddy/pki/authorities/local/root.crt; then
          "$STACK_BIN" cp "${CADDY_CONTAINER}":/data/caddy/pki/authorities/local/root.crt state/certs/caddy-local-root.crt >/dev/null 2>&1 || true
        fi
      fi
    else
      cp -f "${ROOT_CA_HOST}" state/certs/caddy-local-root.crt || true
    fi
    if [[ -s state/certs/caddy-local-root.crt ]]; then
      log "Installing Caddy local root CA into host trust store"
      $SUDO cp state/certs/caddy-local-root.crt /usr/local/share/ca-certificates/caddy-local-root.crt
      $SUDO update-ca-certificates >/dev/null 2>&1 || true
      # Build a canonical dev CA bundle (system + local CA) for CLI tools/SDKs
      mkdir -p state/certs
      COMBINED_OUT="state/certs/combined-dev-ca.pem"
      "$PY_BIN" - <<'PY' || true
import os, sys
try:
    import certifi
    src = certifi.where()
    dev = os.path.join('state','certs','caddy-local-root.crt')
    out = os.path.join('state','certs','combined-dev-ca.pem')
    with open(src,'rb') as fsrc, open(out,'wb') as fout:
        data = fsrc.read()
        fout.write(data)
        with open(dev,'rb') as fd:
            d = fd.read()
            if d not in data:
                if not d.endswith(b"\n"): d += b"\n"
                fout.write(d)
    print(out)
except Exception as e:
    print('skip-combined:', e)
PY
      # Best-effort user trust for Chrome/Chromium (NSS) and Firefox profiles
      if command -v certutil >/dev/null 2>&1; then
        mkdir -p "$HOME/.pki/nssdb"
        certutil -d sql:"$HOME/.pki/nssdb" -A -t "C,," -n "Caddy Local Root" -i state/certs/caddy-local-root.crt 2>/dev/null || true
        for prof in "$HOME"/.mozilla/firefox/*.default* "$HOME"/.mozilla/firefox/*.dev*; do
          [ -d "$prof" ] || continue
          certutil -d sql:"$prof" -A -t "C,," -n "Caddy Local Root" -i state/certs/caddy-local-root.crt 2>/dev/null || true
        done
      fi
    else
      # If CA not present yet, fix perms and try once more (Podman rootless often needs this)
      chmod -R 0777 state/caddy-data || true
      sleep 1
      ${STACK_COMPOSE[@]} "${DEV_COMPOSE_FILES[@]}" restart caddy || true
      sleep 1
      if [[ -s "${ROOT_CA_HOST}" ]]; then
        cp -f "${ROOT_CA_HOST}" state/certs/caddy-local-root.crt || true
      fi
      if [[ -s state/certs/caddy-local-root.crt ]]; then
        log "Installing Caddy local root CA into host trust store (retry)"
        $SUDO cp state/certs/caddy-local-root.crt /usr/local/share/ca-certificates/caddy-local-root.crt
        $SUDO update-ca-certificates >/dev/null 2>&1 || true
      else
        log "Local CA not found; skipping trust install (you can import state/certs/caddy-local-root.crt manually later)"
      fi
    fi
else
  log "update-ca-certificates not found; skipping local CA trust (manually import state/certs/caddy-local-root.crt)"
fi

if prompt_yes_no_hosts "Add hosts entries for ${HOSTS[*]} to /etc/hosts?" N; then
  log "Configuring hosts entries"
  for host in "${HOSTS[@]}"; do
    if ! grep -q "[[:space:]]$host$" /etc/hosts; then
      $SUDO sh -c "echo '${HOSTS_IP} ${host}' >> /etc/hosts"
    fi
  done
else
  log "Skipping hosts entries; use direct addresses instead"
fi

export AE_CADDY_SITES=${AE_CADDY_SITES:-state/caddy}
export AE_CADDY_FILE=${AE_CADDY_FILE:-/etc/caddy/Caddyfile}
# When using the dev docker-compose stack, reload Caddy inside the container.
export AE_CADDY_CONTAINER=${AE_CADDY_CONTAINER:-dev-caddy-1}
# Tell the controller which CLI to use to exec inside the Caddy container
export AE_CONTAINER_CLI=${AE_CONTAINER_CLI:-$STACK_BIN}

# Auto-detect the actual engine that owns the Caddy container to avoid
# reload failures when the chosen STACK_BIN doesn't match a previously
# running stack (e.g., after k1nd/k3d benches).
if command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1; then
  DETECTED_ENGINE=""
  # Prefer an exact container name match
  if command -v docker >/dev/null 2>&1; then
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${AE_CADDY_CONTAINER}"; then
      DETECTED_ENGINE="docker"
    fi
  fi
  if [[ -z "${DETECTED_ENGINE}" ]] && command -v podman >/dev/null 2>&1; then
    if podman ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${AE_CADDY_CONTAINER}"; then
      DETECTED_ENGINE="podman"
    fi
  fi
  if [[ -n "${DETECTED_ENGINE}" && "${AE_CONTAINER_CLI}" != "${DETECTED_ENGINE}" ]]; then
    log "Adjusting AE_CONTAINER_CLI=${DETECTED_ENGINE} (found ${AE_CADDY_CONTAINER} under ${DETECTED_ENGINE})"
    export AE_CONTAINER_CLI="${DETECTED_ENGINE}"
  fi
fi
export AE_STATE_DB=${AE_STATE_DB:-state/controller.db}
# Guard: ensure controller DB is writable (bench runs with sudo can leave it root-owned)
DB_DIR="$(dirname -- "${AE_STATE_DB}")"
mkdir -p "${DB_DIR}" || true
if [[ -e "${AE_STATE_DB}" && ! -w "${AE_STATE_DB}" ]]; then
  log "Fixing permissions on controller state DB (${AE_STATE_DB})"
  if ! $SUDO chown "$(id -u):$(id -g)" "${AE_STATE_DB}" 2>/dev/null; then
    # Fallback: copy to a user-owned file to unblock local dev
    tmpdb="${AE_STATE_DB}.usercopy"
    cp -f "${AE_STATE_DB}" "${tmpdb}" 2>/dev/null || true
    if [[ -s "${tmpdb}" ]]; then
      mv -f "${tmpdb}" "${AE_STATE_DB}" 2>/dev/null || true
    fi
  fi
  chmod u+rw "${AE_STATE_DB}" 2>/dev/null || true
fi
# Avoid indefinite hangs on Caddy reload inside docker exec
export AE_CADDY_RELOAD_TIMEOUT=${AE_CADDY_RELOAD_TIMEOUT:-10}
# For local demos, allow plaintext secrets by default unless explicitly disabled
export AE_ALLOW_PLAINTEXT_SECRETS=${AE_ALLOW_PLAINTEXT_SECRETS:-1}
# If requested, enforce demo-friendly secrets env and point SOPS to age key file
if [[ ${WITH_SECRETS_ENV:-0} -eq 1 ]]; then
  export AE_ALLOW_PLAINTEXT_SECRETS=1
  export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/ae/keys.txt}"
  log "Secrets env enabled (AE_ALLOW_PLAINTEXT_SECRETS=1; SOPS_AGE_KEY_FILE=${SOPS_AGE_KEY_FILE})"
fi
# Mark this run as demo-init so components can quiet benign warnings
# Force AE_DEMO_MODE=1 for demos regardless of a pre-set env
export AE_DEMO_MODE=1
export AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND}
export AE_REGISTER_LOCAL_NODE=${AE_REGISTER_LOCAL_NODE}
# Prefer crun for Podman/OCI demos when available, unless user overrode
if [[ "${AE_RUNTIME_BACKEND}" == "podman" || "${AE_RUNTIME_BACKEND}" == "oci" ]]; then
  if [[ -z "${AE_OCI_RUNTIME:-}" ]]; then
    if command -v crun >/dev/null 2>&1; then
      export AE_OCI_RUNTIME=crun
      log "Using AE_OCI_RUNTIME=crun for Podman runs (crun detected)"
    else
      log "WARN: crun not found; Podman will use its configured default OCI runtime (often runc)."
      log "      Install crun or set [engine].runtime=\"crun\" in containers.conf to prefer it."
    fi
  else
    log "Using AE_OCI_RUNTIME=${AE_OCI_RUNTIME} (user override)"
  fi
fi
mkdir -p "${AE_CADDY_SITES}"
if [[ ! -w "${AE_CADDY_SITES}" ]]; then
  log "Adjusting permissions on ${AE_CADDY_SITES} (may require sudo)"
  $SUDO chown -R "$(id -u):$(id -g)" "${AE_CADDY_SITES}" || true
fi

# If still not writable after attempting chown, gracefully disable ingress writes
if [[ ! -w "${AE_CADDY_SITES}" ]]; then
  log "Ingress config dir not writable: ${AE_CADDY_SITES}. Disabling ingress management for this run."
  # An empty AE_CADDY_SITES tells the CLI to skip ingress manager wiring
  export AE_CADDY_SITES=""
fi

# Proactively clear stale dynamic sites to prevent ambiguous host errors from past runs
log "Resetting dynamic Caddy sites at ${AE_CADDY_SITES}"
rm -f "${AE_CADDY_SITES}"/*.caddy 2>/dev/null || true

# Ensure app containers join the dev compose network so Caddy can resolve them by name (docker path)
export AE_DOCKER_NETWORK=${AE_DOCKER_NETWORK:-dev_default}

# If a labs-aio apishim is running locally, wire helm demo to reuse it.
# Guard against reusing an unrelated shim unless the store/endpoint is explicit.
if [[ ${LABS_ENABLE:-0} -eq 1 ]]; then
  if [[ -z "${AE_LABS_HELM_SERVER:-}" ]]; then
    allow_reuse=0
    LABS_APISHIM_ENV="state/labs/apishim.env"
    if [[ -n "${AE_APISHIM_DSN:-}" ]]; then
      allow_reuse=1
    elif [[ -n "${AE_APISHIM_DB:-}" && "${AE_APISHIM_DB}" != "state/apishim.db" ]]; then
      allow_reuse=1
    elif [[ -f "$LABS_APISHIM_ENV" ]]; then
      allow_reuse=1
    fi
    if [[ ${allow_reuse:-0} -eq 1 ]]; then
      if [[ -f "$LABS_APISHIM_ENV" ]]; then
        LABS_HELM_TOKEN="$(read_env_file_var "AE_LABS_HELM_TOKEN" "$LABS_APISHIM_ENV" || true)"
        if [[ -z "$LABS_HELM_TOKEN" ]]; then
          LABS_HELM_TOKEN="$(read_env_file_var "AE_APISHIM_TOKEN" "$LABS_APISHIM_ENV" || true)"
        fi
        APISHIM_PROBE_PORT=${APISHIM_PROBE_PORT:-8455}
        if [[ -n "$LABS_HELM_TOKEN" ]] && port_open "127.0.0.1" "$APISHIM_PROBE_PORT"; then
          if token_valid "https" "127.0.0.1" "$APISHIM_PROBE_PORT" "$LABS_HELM_TOKEN"; then
            export AE_LABS_HELM_SERVER="https://127.0.0.1:${APISHIM_PROBE_PORT}"
            export AE_LABS_HELM_TOKEN="$LABS_HELM_TOKEN"
            log "Detected apishim on ${AE_LABS_HELM_SERVER}; helm demo will reuse it."
          elif token_valid "http" "127.0.0.1" "$APISHIM_PROBE_PORT" "$LABS_HELM_TOKEN"; then
            export AE_LABS_HELM_SERVER="http://127.0.0.1:${APISHIM_PROBE_PORT}"
            export AE_LABS_HELM_TOKEN="$LABS_HELM_TOKEN"
            log "Detected apishim on ${AE_LABS_HELM_SERVER}; helm demo will reuse it."
          else
            log "Found apishim.env but token rejected by ${APISHIM_PROBE_PORT}; helm demo will start a local shim."
          fi
        fi
      fi
    else
      log "Skipping apishim auto-detect; set AE_LABS_HELM_SERVER, AE_APISHIM_DSN, or create state/labs/apishim.env to reuse a running shim."
    fi
  fi
fi

# Preserve optional helm demo overrides for env.sh
LABS_HELM_SERVER_LINE=""
LABS_HELM_TOKEN_LINE=""
if [[ -n "${AE_LABS_HELM_SERVER:-}" ]]; then
  LABS_HELM_SERVER_LINE="export AE_LABS_HELM_SERVER=${AE_LABS_HELM_SERVER}"
fi
if [[ -n "${AE_LABS_HELM_TOKEN:-}" ]]; then
  LABS_HELM_TOKEN_LINE="export AE_LABS_HELM_TOKEN=${AE_LABS_HELM_TOKEN}"
fi
# Default apishim DB to the shared demo DB when no DSN is configured.
if [[ -z "${AE_APISHIM_DSN:-}" ]]; then
  export AE_APISHIM_DB=${AE_APISHIM_DB:-state/apishim.db}
fi
export AE_LABS=${LABS_ENABLE}
export AE_LABS_TOKEN=${LABS_TOKEN}
if [[ ${LABS_ENABLE:-0} -eq 1 ]]; then
  ./scripts/ensure_apishim_env.sh
  LABS_APISHIM_ENV="state/labs/apishim.env"
  if [[ -z "${AE_APISHIM_SESSION_SECRET:-}" ]]; then
    LABS_SESSION_SECRET="$(read_env_file_var "AE_APISHIM_SESSION_SECRET" "$LABS_APISHIM_ENV" || true)"
    if [[ -z "$LABS_SESSION_SECRET" ]]; then
      LABS_SESSION_SECRET="$(gen_token)"
    fi
    export AE_APISHIM_SESSION_SECRET="$LABS_SESSION_SECRET"
  fi
  if [[ -z "${AE_API_ADMIN_TOKEN:-}" ]]; then
    LABS_ADMIN_TOKEN="$(read_env_file_var "AE_API_ADMIN_TOKEN" "$LABS_APISHIM_ENV" || true)"
    if [[ -z "$LABS_ADMIN_TOKEN" ]]; then
      LABS_ADMIN_TOKEN="$(gen_token)"
    fi
    export AE_API_ADMIN_TOKEN="$LABS_ADMIN_TOKEN"
  fi
  export AE_LABS_SESSION_HOSTS=${AE_LABS_SESSION_HOSTS:-1}
  if [[ -z "${AE_APISHIM_MIRROR:-}" ]]; then
    export AE_APISHIM_MIRROR=1
  fi
  if [[ -z "${AE_APISHIM_SOT:-}" ]]; then
    export AE_APISHIM_SOT=1
  fi
elif [[ -n "${AE_APISHIM_MIRROR:-}" ]]; then
  export AE_APISHIM_MIRROR
  if [[ -n "${AE_APISHIM_SOT:-}" ]]; then
    export AE_APISHIM_SOT
  fi
fi

# Write env helper for manual shells (after exports)
mkdir -p state
cat > state/env.sh <<ENV
export AE_CADDY_SITES=${AE_CADDY_SITES}
export AE_CADDY_FILE=${AE_CADDY_FILE}
export AE_CADDY_CONTAINER=${AE_CADDY_CONTAINER}
export AE_DOCKER_NETWORK=${AE_DOCKER_NETWORK}
export AE_CONTAINER_CLI=${AE_CONTAINER_CLI}
export AE_ALLOW_PLAINTEXT_SECRETS=${AE_ALLOW_PLAINTEXT_SECRETS}
export SOPS_AGE_KEY_FILE=${SOPS_AGE_KEY_FILE:-}
# Demo marker for local tooling.
export AE_DEMO_MODE=1
export AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND}
export AE_REGISTER_LOCAL_NODE=${AE_REGISTER_LOCAL_NODE}
export AE_OCI_RUNTIME=${AE_OCI_RUNTIME:-}
export API_PORT=${API_PORT}
export AE_SPECS_DIR=${DEMO_SPECS_DIR}
export AE_APISHIM_DB=${AE_APISHIM_DB:-}
export AE_APISHIM_MIRROR=${AE_APISHIM_MIRROR:-}
export AE_APISHIM_SOT=${AE_APISHIM_SOT:-}
export AE_APISHIM_SESSION_SECRET=${AE_APISHIM_SESSION_SECRET:-}
export AE_API_ADMIN_TOKEN=${AE_API_ADMIN_TOKEN:-}
export AE_APISHIM_SERVER=${AE_APISHIM_SERVER:-}
export DOCS_API_BASE=${DOCS_API_BASE:-}
export DOCS_DASHBOARD_URL=${DOCS_DASHBOARD_URL:-}
# Labs + docs wiring for controller
export AE_LABS=${LABS_ENABLE}
export AE_LABS_TOKEN=${LABS_TOKEN}
if [[ ${LABS_ENABLE:-0} -eq 1 ]]; then
  # Ensure sessionized hosts to avoid Caddy host collisions from multiple echo-* apps
  export AE_LABS_SESSION_HOSTS=${AE_LABS_SESSION_HOSTS:-1}
fi
${LABS_HELM_SERVER_LINE}
${LABS_HELM_TOKEN_LINE}
export AE_DOCS_PORT=${DOCS_PORT:-9109}
# Canonical dev CA bundle for tools/SDKs (if built by init)
if [ -f "state/certs/combined-dev-ca.pem" ]; then
  # These envs are safe for local shells and scripts; they augment trust for dev endpoints
  export CURL_CA_BUNDLE="$(pwd)/state/certs/combined-dev-ca.pem"
  export REQUESTS_CA_BUNDLE="$(pwd)/state/certs/combined-dev-ca.pem"
  export NODE_EXTRA_CA_CERTS="$(pwd)/state/certs/combined-dev-ca.pem"
  export GIT_SSL_CAINFO="$(pwd)/state/certs/combined-dev-ca.pem"
fi
ENV

# Seed dynsites for docs and API so they are always available
DOCS_PORT=${DOCS_PORT:-9109}
APISHIM_PORT=${APISHIM_PORT:-8445}
# Bind docs server to all interfaces by default so Caddy (in a container)
# can reach it via host.docker.internal. Override with --bind-all or env.
DOCS_BIND=${DOCS_BIND:-0.0.0.0}
cat > "${AE_CADDY_SITES}/docs.caddy" <<DOCS
https://docs.home.arpa {
    log {
        output stdout
        format console
    }
    header -Strict-Transport-Security
    tls internal

    # Proxy controller API paths for single-origin labs (no CORS required)
    @apibase path /health /openapi.json
    handle @apibase {
        reverse_proxy ${AE_CADDY_HOST_ALIAS:-$HOST_ALIAS}:${API_PORT}
    }
    # Proxy shim API paths to the apishim on the host
    @apishim path /api/v1 /api/v1/* /apis /apis/*
    handle @apishim {
        reverse_proxy https://${AE_CADDY_HOST_ALIAS:-$HOST_ALIAS}:${APISHIM_PORT} {
            transport http {
                tls_insecure_skip_verify
            }
        }
    }
    # Keep /api/ scoped to the controller to avoid shadowing docs pages like /api-auth.html or /apishim-compatibility-matrix.html
    @apipaths path /api/* /labs* /status* /events* /logs* /swagger* /redoc* /system* /ui/features
    handle @apipaths {
        reverse_proxy ${AE_CADDY_HOST_ALIAS:-$HOST_ALIAS}:${API_PORT}
    }

    # Static docs fallback
    root * /srv/docs
    file_server browse
}
DOCS

cat > "${AE_CADDY_SITES}/api.caddy" <<API
https://api.home.arpa {
    log {
        output stdout
        format console
    }
    header -Strict-Transport-Security
    tls internal

    @ui path /dashboard* /playground*
    handle @ui {
        respond 404
    }

    @apishim path /api/v1 /api/v1/* /apis /apis/*
    handle @apishim {
        reverse_proxy https://${AE_CADDY_HOST_ALIAS:-$HOST_ALIAS}:${APISHIM_PORT} {
            transport http {
                tls_insecure_skip_verify
            }
        }
    }

    @controller path /api/apishim/* /ui/features /health* /status* /events* /logs* /metrics* /system* /nodes* /history* /manifest* /plan* /k8s/preview
    handle @controller {
        reverse_proxy ${AE_CADDY_HOST_ALIAS:-$HOST_ALIAS}:${API_PORT}
    }

    handle {
        reverse_proxy ${AE_CADDY_HOST_ALIAS:-$HOST_ALIAS}:${API_PORT}
    }
}
API

cat > "${AE_CADDY_SITES}/dash.caddy" <<DASH
https://dash.home.arpa {
    log {
        output stdout
        format console
    }
    header -Strict-Transport-Security
    tls internal
    reverse_proxy ${AE_CADDY_HOST_ALIAS:-$HOST_ALIAS}:${API_PORT}
}
DASH

# Start apishim for labs/demo sessions (exec/port-forward)
start_apishim

# Auto-start the controller daemon unless disabled
# Build a temporary specs set for the selected demo so the controller only watches required apps
DEMO_SPECS_DIR="state/demo-specs"
rm -rf "$DEMO_SPECS_DIR" 2>/dev/null || true
mkdir -p "$DEMO_SPECS_DIR"
if [[ $DOCS_ONLY -ne 1 ]]; then
  # Helper to include a spec if the file exists
  add_spec() {
    local f="$1"; if [[ -f "$f" ]]; then cp "$f" "$DEMO_SPECS_DIR/"; fi
  }
  ANY_DEMO=$(( DEMO_CONFIGS | DEMO_STANDARD | DEMO_ECHO_MR | DEMO_ECHO_MULTI | DEMO_SECURITY | DEMO_TCP | DEMO_EXEC | DEMO_ROLLOUT | DEMO_STORAGE ))
  if [[ $DEMO_STANDARD -eq 1 || $ANY_DEMO -eq 0 ]]; then
    add_spec specs/examples/blue.yaml
    add_spec specs/examples/green.yaml
  fi
  if [[ $DEMO_CONFIGS -eq 1 ]]; then
    add_spec specs/examples/echo.yaml
  fi
  if [[ $DEMO_ECHO_MR -eq 1 ]]; then
    add_spec specs/examples/multi-replica-echo.yaml
  fi
  if [[ $DEMO_ECHO_MULTI -eq 1 ]]; then
    add_spec specs/examples/echo-multiport.yaml
  fi
  if [[ $DEMO_SECURITY -eq 1 ]]; then
    add_spec specs/examples/echo-sec.yaml
  fi
  if [[ $DEMO_TCP -eq 1 ]]; then
    add_spec specs/examples/echo-tcp.yaml
  fi
  if [[ $DEMO_EXEC -eq 1 ]]; then
    add_spec specs/examples/echo-exec.yaml
  fi
  if [[ $DEMO_ROLLOUT -eq 1 ]]; then
    add_spec specs/examples/echo.yaml
    add_spec specs/examples/echo-rollout.yaml
  fi
  if [[ $DEMO_STORAGE -eq 1 ]]; then
    add_spec specs/examples/echo-storage.yaml
  fi
fi

if [[ $NO_CONTROLLER -eq 0 ]]; then
  # Always (re)start the controller to ensure it picks up the curated AE_SPECS_DIR and AE_DEMO_MODE.
  # This avoids stale supervisors carrying an old environment (root cause of apps leakage in dashboard).
  if [[ -f state/controller_supervisor.pid ]]; then
    SUP_PID=$(cat state/controller_supervisor.pid || true)
    if [[ -n "${SUP_PID}" ]] && kill -0 "$SUP_PID" 2>/dev/null; then
      log "Restarting controller supervisor to apply fresh demo env"
      kill "$SUP_PID" || true
      # Allow a short grace for child exit and port release
      sleep 0.5
    fi
    rm -f state/controller_supervisor.pid state/controller_supervisor.lock || true
  fi
  if [[ -f state/controller.pid ]]; then
    CTRL_PID=$(cat state/controller.pid || true)
    if [[ -n "${CTRL_PID}" ]] && kill -0 "$CTRL_PID" 2>/dev/null; then
      log "Stopping prior controller (pid ${CTRL_PID})"
      kill "$CTRL_PID" || true
      sleep 0.2
    fi
    rm -f state/controller.pid || true
  fi
  if [[ $NO_SUPERVISOR -eq 0 ]]; then
    log "Starting controller supervisor (port ${API_PORT})"
    nohup bash scripts/supervise_controller.sh "$PY_BIN" "$DEMO_SPECS_DIR" "${API_PORT}" >/dev/null 2>&1 &
    echo $! > state/controller_supervisor.pid
  else
    log "Starting controller once on :${API_PORT} (background)"
    nohup "$PY_BIN" -m ae.controller --loop --specs "$DEMO_SPECS_DIR" --metrics-port "${API_PORT}" --watch \
      >/dev/null 2>&1 &
    echo $! > state/controller.pid
  fi
else
  log "Skipping controller auto-start (--no-controller)"
fi
ls -1 ops/dev/caddy/sites | sed 's/^/  - /' | xargs -r -I{} true >/dev/null 2>&1 || true

log "Applying demo manifests"
APPLY_TIMEOUT=${APPLY_TIMEOUT:-120}

# Heartbeat helper: run an apply with a periodic progress line so users know it's alive.
APPLY_HEARTBEAT_INTERVAL=${APPLY_HEARTBEAT_INTERVAL:-10}
apply_with_heartbeat() {
  # $1=label (blue/green), $2=manifest path
  local label="$1"; local mf="$2"
  local elapsed=0
  log "Applying ${label} from ${mf} (timeout=${APPLY_TIMEOUT}s)"
  (
    timeout --kill-after=5 "$APPLY_TIMEOUT" "$PY_BIN" -m ae.cli --verbose apply -f "$mf"
  ) &
  local apid=$!
  while kill -0 "$apid" 2>/dev/null; do
    sleep "$APPLY_HEARTBEAT_INTERVAL"
    elapsed=$((elapsed+APPLY_HEARTBEAT_INTERVAL))
    log "… still applying ${label} (${elapsed}s elapsed)"
  done
  wait "$apid"; return $?
}

# Wrapper: on failure, keep -d attached instead of exiting immediately
APPLY_FAILED=0
apply_or_diag() {
  # $1=label, $2=manifest path
  local label="$1"; local mf="$2"
  if ! apply_with_heartbeat "$label" "$mf"; then
    log "Apply for ${label} timed out or failed. Diagnostics:"
    "$STACK_BIN" ps || true
    log "Try: $STACK_BIN logs dev-caddy-1; $STACK_BIN exec dev-caddy-1 caddy reload --config /etc/caddy/Caddyfile"
    log "Or re-run with more verbosity: $PY_BIN -m ae.cli --verbose apply -f ${mf}"
    if [[ $DEBUG_ATTACH -eq 1 ]]; then
      APPLY_FAILED=1
      log "Continuing to attach logs due to -d (debug) flag. Press Ctrl-C to exit." 
    else
      exit 1
    fi
  fi
}

# Ensure controller supervisor is running before applies to inherit demo env
ensure_supervisor_running() {
  if [[ $NO_CONTROLLER -ne 0 ]]; then
    return 0
  fi
  local running=0
  if [[ -f state/controller_supervisor.pid ]] && kill -0 "$(cat state/controller_supervisor.pid 2>/dev/null)" 2>/dev/null; then
    running=1
  fi
  if [[ $running -eq 0 ]]; then
    log "Controller supervisor not running; starting it now"
    nohup bash scripts/supervise_controller.sh "$PY_BIN" "$DEMO_SPECS_DIR" "${API_PORT}" >/dev/null 2>&1 &
    echo $! > state/controller_supervisor.pid
  fi
  # Wait briefly for the HTTP API to come up so subsequent steps see a stable controller
  for i in {1..40}; do
    if curl -fsS "http://127.0.0.1:${API_PORT}/openapi.json" >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done
}

ensure_supervisor_running || true

# Wait briefly for Caddy container to accept execs
for i in {1..20}; do
  if "$STACK_BIN" exec "$AE_CADDY_CONTAINER" caddy version >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

# If Labs is enabled, proactively clean up stray session apps that may share the same base ingress host
if [[ ${LABS_ENABLE:-0} -eq 1 ]]; then
  log "Cleaning old Labs session apps (pattern: echo-<hex6>) to prevent ingress host collisions"
  while read -r app; do
    if [[ "$app" =~ ^echo-[0-9a-f]{6}$ ]]; then
      "$PY_BIN" -m ae.cli delete "$app" >/dev/null 2>&1 || true
    fi
  done < <("$PY_BIN" -m ae.cli status | awk -F: '/^echo-/ {print $1}')
fi

if [[ $DOCS_ONLY -ne 1 ]]; then
  # Default: apply standard demo only when explicitly requested OR when no demo flags were provided at all.
  ANY_DEMO=$(( DEMO_CONFIGS | DEMO_STANDARD | DEMO_ECHO_MR | DEMO_ECHO_MULTI | DEMO_SECURITY | DEMO_TCP | DEMO_EXEC | DEMO_ROLLOUT | DEMO_STORAGE ))
  if [[ $DEMO_STANDARD -eq 1 || $ANY_DEMO -eq 0 ]]; then
    cleanup_demo_containers "${APP_BIN}"
    apply_or_diag "blue"  "specs/examples/blue.yaml"
    apply_or_diag "green" "specs/examples/green.yaml"
  fi
fi

# Optional configs/secrets demo
if [[ $DEMO_CONFIGS -eq 1 ]]; then
  export AE_ALLOW_PLAINTEXT_SECRETS=1
  log "Applying configs/secrets demo (echo) with plaintext secrets enabled"
  if apply_with_heartbeat "echo" "specs/examples/echo.yaml"; then
    "$PY_BIN" -m ae.cli status echo --wide || true
    # Print projection location and sample values if present
    APP_ROOT="state/projections/echo-rev1"; for d in "$APP_ROOT"*; do APP_ROOT="$d"; break; done
    if [[ -d "$APP_ROOT" ]]; then
      log "Projection files under $APP_ROOT (mounted at /var/run/ae/config/echo)"
      find "$APP_ROOT" -maxdepth 2 -type f | sed 's/^/[proj] /'
    fi
    # Quick endpoint verification
    echo
    log "Demo endpoint verification (HTTPS via Caddy ${CADDY_HTTPS_PORT})"
    for host in blue.home.arpa green.home.arpa echo-mr.home.arpa docs.home.arpa api.home.arpa; do
      code=$(curl -ksS -o /dev/null -w '%{http_code}' "https://$host:${CADDY_HTTPS_PORT}/" || true)
      printf '[verify] %-20s -> %s\n' "$host" "${code:-fail}"
    done
  else
    log "Echo demo apply failed"
  fi
fi

# Optional multi-replica echo demo
if [[ $DEMO_ECHO_MR -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying multi-replica echo demo (echo-mr)"
  apply_with_heartbeat "echo-mr" "specs/examples/multi-replica-echo.yaml" || true
fi

# Optional multi-port echo demo (http + metrics)
if [[ $DEMO_ECHO_MULTI -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying multi-port echo demo (echo-multi)"
  apply_with_heartbeat "echo-multi" "specs/examples/echo-multiport.yaml" || true
  # Quick endpoint verification
  code=$(curl -ksS -o /dev/null -w '%{http_code}' "https://echo-multi.home.arpa:${CADDY_HTTPS_PORT}/" || true)
  printf '[verify] %-20s -> %s\n' "echo-multi.home.arpa/" "${code:-fail}"
fi

# Optional security demo
if [[ $DEMO_SECURITY -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying security-hardened echo demo (echo-sec)"
  apply_with_heartbeat "echo-sec" "specs/examples/echo-sec.yaml" || true
fi

# Optional TCP probe demo
if [[ $DEMO_TCP -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying TCP-probe echo demo (echo-tcp)"
  apply_with_heartbeat "echo-tcp" "specs/examples/echo-tcp.yaml" || true
fi

# Optional hardened demo
if [[ $DEMO_HARDENED -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying hardened echo demo (echo-hardened)"
  apply_with_heartbeat "echo-hardened" "specs/examples/echo-hardened.yaml" || true
fi

# Optional exec probe demo
if [[ $DEMO_EXEC -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying exec-probe echo demo (echo-exec)"
  apply_with_heartbeat "echo-exec" "specs/examples/echo-exec.yaml" || true
fi

# Optional rollout demo: apply echo, then echo-rollout
if [[ $DEMO_ROLLOUT -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying rollout demo (echo → echo-rollout)"
  apply_with_heartbeat "echo" "specs/examples/echo.yaml" || true
  sleep 2
  apply_with_heartbeat "echo-rollout" "specs/examples/echo-rollout.yaml" || true
  "$PY_BIN" -m ae.cli status echo --events --history 5 || true
fi

# Optional storage demo
if [[ $DEMO_STORAGE -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying storage demo (echo with PV-lite)"
  apply_with_heartbeat "echo-storage" "specs/examples/echo-storage.yaml" || true
  log "Listing storage volumes"
  "$PY_BIN" -m ae.cli volumes list --app echo || true
fi

# Build and serve docs locally
DOCS_PORT=${DOCS_PORT:-9109}
log "Building static docs (docs/site)"
export DOCS_LABS_TOKEN="${LABS_TOKEN}"
"$PY_BIN" docs/build_docs.py || true
log "Starting docs server on http://${DOCS_BIND}:${DOCS_PORT} (background)"
mkdir -p state
nohup "$PY_BIN" -m http.server "${DOCS_PORT}" --bind "${DOCS_BIND}" --directory docs/site >/dev/null 2>&1 &
echo $! > state/docs_server.pid

log "Current status"
"$PY_BIN" -m ae.cli status

# If Labs demo requested, print playground hints
if [[ ${LABS_ENABLE:-0} -eq 1 ]]; then
  echo
  log "Labs enabled for controller sessions (AE_LABS=1)"
  if [[ -n "${LABS_TOKEN:-}" ]]; then
    log "Labs token exported via state/env.sh (AE_LABS_TOKEN). Paste it in the playground or click 'Use Token'."
  fi
  log "Playground: https://docs.home.arpa:${CADDY_HTTPS_PORT}/playground.html"
  log "Dashboard:  https://dash.home.arpa:${CADDY_HTTPS_PORT}/dashboard"
  log "API base:   https://api.home.arpa:${CADDY_HTTPS_PORT}"
fi

# If backend is podman, ensure demo images are available to Podman by importing from Docker when needed
if [[ "$AE_RUNTIME_BACKEND" == "podman" || "$AE_RUNTIME_BACKEND" == "oci" ]]; then
  if command -v podman >/dev/null 2>&1; then
    for img in demo-green:latest demo-shell:latest; do
      if ! podman images --format '{{.Repository}}:{{.Tag}}' | grep -qE "(^|/)${img}$"; then
        if command -v docker >/dev/null 2>&1; then
          if docker image inspect "$img" >/dev/null 2>&1; then
            log "Importing $img from Docker into Podman (docker-daemon:$img)"
            podman pull "docker-daemon:${img}" >/dev/null 2>&1 || true
          fi
        fi
        # also try localhost/<img>
        if ! podman images --format '{{.Repository}}:{{.Tag}}' | grep -qE "(^|/)${img}$"; then
          if podman images --format '{{.Repository}}:{{.Tag}}' | grep -q "localhost/${img}$"; then
            log "Podman has localhost/${img}; runtime will resolve it"
          fi
        fi
      fi
    done
    # Ensure the multi-arch echo image is present for Podman
    if ! podman images --format '{{.Repository}}:{{.Tag}}' | grep -q '^mendhak/http-https-echo:37$'; then
      podman pull mendhak/http-https-echo:37 >/dev/null 2>&1 || true
    fi
  fi
fi

# Report reachability of the controller HTTP API and UIs.
check_api_reachability() {
  API_PORT=${API_PORT:-9108}
  CADDY_HTTPS_PORT=${CADDY_HTTPS_PORT:-8443}
  echo
  log "API reachability checks (expected after you start the controller)"

  # Direct API JSON
  if curl -fsS "http://127.0.0.1:${API_PORT}/openapi.json" >/dev/null 2>&1; then
    log "Direct API OK: http://127.0.0.1:${API_PORT}/openapi.json"
    for path in /swagger /redoc /dashboard; do
      code=$(curl -fsS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${API_PORT}${path}" || true)
      if [[ "$code" == "200" ]]; then
        log "Direct ${path} OK: http://127.0.0.1:${API_PORT}${path}"
      else
        log "Direct ${path} not reachable (HTTP ${code:-fail})"
      fi
    done
  else
    log "Direct API NOT reachable on :${API_PORT}. The controller API is likely not running."
    echo "Next steps:"
    echo "  $ $PY_BIN -m ae.controller --loop --specs specs/ --metrics-port ${API_PORT} --watch"
    echo "  # then visit http://127.0.0.1:${API_PORT}/swagger or /redoc"
  fi

  # Caddy (ingress) reachability — use --resolve to avoid requiring /etc/hosts
  code_api=$(curl -ksS --resolve "api.home.arpa:${CADDY_HTTPS_PORT}:127.0.0.1" -o /dev/null -w '%{http_code}' \
             "https://api.home.arpa:${CADDY_HTTPS_PORT}/openapi.json" || true)
  if [[ "$code_api" == "200" ]]; then
    log "Caddy API OK: https://api.home.arpa:${CADDY_HTTPS_PORT}/openapi.json"
    for path in /swagger /redoc /dashboard; do
      code=$(curl -ksS --resolve "api.home.arpa:${CADDY_HTTPS_PORT}:127.0.0.1" -o /dev/null -w '%{http_code}' \
             "https://api.home.arpa:${CADDY_HTTPS_PORT}${path}" || true)
      if [[ "$code" == "200" ]]; then
        log "Caddy ${path} OK: https://api.home.arpa:${CADDY_HTTPS_PORT}${path}"
      else
        log "Caddy ${path} not reachable (HTTP ${code:-fail})"
      fi
    done
  else
    log "Caddy API not reachable (HTTP ${code_api:-fail})."
    echo "If direct API works, try reloading Caddy:"
    echo "  $ $STACK_BIN exec dev-caddy-1 caddy reload --config /etc/caddy/Caddyfile"
  fi

  # Supervisor status
  if [[ -f state/controller_supervisor.pid ]] && kill -0 "$(cat state/controller_supervisor.pid 2>/dev/null)" 2>/dev/null; then
    restarts=$(cat state/controller_restart_count 2>/dev/null || echo 0)
    last_rc=$(cat state/controller_last_exit 2>/dev/null || echo 0)
    log "Controller supervisor running; restarts=${restarts}, last_exit=${last_rc}"
  else
    log "Controller supervisor not running"
  fi
}

check_api_reachability || true

# Validate caddy upstream reachability and network DNS
check_network_sanity() {
  echo
  log "Ingress/network sanity checks"
  local sites_dir="$AE_CADDY_SITES"
  if [[ ! -d "$sites_dir" ]]; then
    log "No dynamic sites directory at $sites_dir; skipping"
    return 0
  fi

  shopt -s nullglob
  for site in "$sites_dir"/*.caddy; do
    app=$(basename "$site" .caddy)
    # Extract upstream list from reverse_proxy line
    ups_line=$(grep -E "^[[:space:]]*reverse_proxy[[:space:]]+" "$site" | head -n1 || true)
    if [[ -z "$ups_line" ]]; then
      log "[$app] no reverse_proxy line found"
      continue
    fi
    # Remove leading 'reverse_proxy ' and trailing '{'
    ups_line=${ups_line#*reverse_proxy }
    ups_line=${ups_line%%\{}
    # Iterate upstreams (space-separated)
    ok_count=0
    total=0
    for up in $ups_line; do
      total=$((total+1))
      host=${up%%:*}
      port=${up##*:}
      if [[ "$host" == "127.0.0.1" || "$host" == "0.0.0.0" ]]; then
        code=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 3 "http://$up" || true)
        if [[ -n "$code" ]]; then ok_count=$((ok_count+1)); else log "[$app] upstream http://$up not reachable"; fi
      elif [[ "$host" == "host.docker.internal" || "$host" == "host.containers.internal" ]]; then
        # Validate inside caddy container where host alias is mapped
        if "$STACK_BIN" exec "$AE_CADDY_CONTAINER" getent hosts "$host" >/dev/null 2>&1; then
          ok_count=$((ok_count+1))
        else
          log "[$app] caddy cannot resolve $host"
        fi
      else
        # Check DNS from inside caddy container
        if "$STACK_BIN" exec "$AE_CADDY_CONTAINER" getent hosts "$host" >/dev/null 2>&1; then
          ok_count=$((ok_count+1))
        else
          log "[$app] caddy cannot resolve $host on network $AE_DOCKER_NETWORK"
        fi
      fi
    done
    log "[$app] upstream targets detected: $total, basic checks passed: $ok_count"
  done
  shopt -u nullglob
}

check_network_sanity || true

# If debugging requested, attach logs and block
attach_debug_logs() {
  echo
  log "Attaching logs (Ctrl-C to exit)"

  # Runtime summary banner for -d mode
  echo
  log "Runtime summary"
  # Determine the effective runtime the CLI would instantiate under current env
  EFFECTIVE_RUNTIME=$(
    "$PY_BIN" - <<'PY' 2>/dev/null || true
from ae.cli.__main__ import runtime_factory
try:
    r = runtime_factory()
    print(type(r).__name__)
except Exception as e:  # fallback if imports fail in partial env
    print(f"Unavailable ({e})")
PY
  )
  printf '[runtime] requested=%s effective=%s\n' "${AE_RUNTIME_BACKEND:-unset}" "${EFFECTIVE_RUNTIME:-unknown}"
  # Container stack CLI (docker|podman) used for dev services like Caddy
  printf '[runtime] stack_cli=%s host_alias=%s docker_net=%s podman_net=%s\n' \
    "${STACK_BIN:-unknown}" "${HOST_ALIAS:-unknown}" "${AE_DOCKER_NETWORK:-unset}" "${AE_PODMAN_NETWORK:-unset}"
  # Versions (best-effort)
  if command -v podman >/dev/null 2>&1; then
    printf '[version] podman: %s\n' "$(podman --version 2>/dev/null | head -n1)"
  else
    printf '[version] podman: not found on PATH\n'
  fi
  if command -v docker >/dev/null 2>&1; then
    printf '[version] docker: %s\n' "$(docker --version 2>/dev/null | head -n1)"
  else
    printf '[version] docker: not found on PATH\n'
  fi
  # Python used by controller + ae CLI
  printf '[version] python: %s\n' "$("$PY_BIN" --version 2>&1)"

  # TLS/dev CA summary
  echo
  log "Dev TLS bundle"
  ROOT_CA_LOCAL="state/certs/caddy-local-root.crt"
  COMBINED_CA="state/certs/combined-dev-ca.pem"
  SYS_CA="/usr/local/share/ca-certificates/caddy-local-root.crt"
  if [[ -s "$ROOT_CA_LOCAL" ]]; then
    printf '[tls] local root: %s\n' "$(realpath "$ROOT_CA_LOCAL" 2>/dev/null || echo "$ROOT_CA_LOCAL")"
  else
    printf '[tls] local root: missing (will be created after first TLS access)\n'
  fi
  if [[ -s "$COMBINED_CA" ]]; then
    printf '[tls] combined bundle: %s\n' "$(realpath "$COMBINED_CA" 2>/dev/null || echo "$COMBINED_CA")"
  else
    printf '[tls] combined bundle: not built\n'
  fi
  if [[ -s "$SYS_CA" ]]; then
    printf '[tls] system trust: %s (installed)\n' "$SYS_CA"
  else
    printf '[tls] system trust: not installed (import %s)\n' "$ROOT_CA_LOCAL"
  fi
  # Show exported tool envs if present
  if [[ -s "state/env.sh" ]]; then
    # shellcheck disable=SC1091
    . state/env.sh >/dev/null 2>&1 || true
  fi
  printf '[tls] env CURL_CA_BUNDLE=%s\n' "${CURL_CA_BUNDLE:-unset}"
  printf '[tls] env REQUESTS_CA_BUNDLE=%s\n' "${REQUESTS_CA_BUNDLE:-unset}"
  printf '[tls] env NODE_EXTRA_CA_CERTS=%s\n' "${NODE_EXTRA_CA_CERTS:-unset}"
  printf '[tls] env GIT_SSL_CAINFO=%s\n' "${GIT_SSL_CAINFO:-unset}"
  # Tail controller log if present
  touch state/controller.log
  # Filter out known benign demo-mode messages
  grep_ctl='sops metadata not found|watchdog not available'
  TAIL_OPTS=""
  if tail --help 2>&1 | grep -q -- "--disable-inotify"; then
    TAIL_OPTS="--disable-inotify"
  fi
  tail ${TAIL_OPTS} -n 50 -F state/controller.log \
    | grep -Ev "$grep_ctl" \
    | sed -u 's/^/[controller] /' &
  T1=$!
  # Docker logs for dev services
  # Filter noisy TLS trust/OCSP messages which are expected in dev
  grep_caddy='certutil|no OCSP stapling|installing root certificate|admin endpoint started|config is unchanged'
  "$STACK_BIN" logs -f dev-caddy-1 2>&1 \
    | grep -Ev "$grep_caddy" \
    | sed -u 's/^/[caddy] /' &
  T2=$!
  "$STACK_BIN" logs -f dev-prometheus-1 2>&1 | sed -u 's/^/[prometheus] /' &
  T3=$!
  # Stream site changes
  tail ${TAIL_OPTS} -n 0 -F "$AE_CADDY_SITES"/*.caddy 2>/dev/null | sed -u 's/^/[sites] /' &
  T4=$!
  # Clean up on exit
  trap 'kill $T1 $T2 $T3 $T4 2>/dev/null || true; exit 0' INT TERM
  wait
}

if [[ $DEBUG_ATTACH -eq 1 ]]; then
  attach_debug_logs
fi

cat <<EOF

Demo setup complete.

- Blue app:   https://blue.home.arpa:${CADDY_HTTPS_PORT}/
- Green app:  https://green.home.arpa:${CADDY_HTTPS_PORT}/
- Docs site:  https://docs.home.arpa:${CADDY_HTTPS_PORT}/ (via Caddy) and http://${DOCS_BIND}:${DOCS_PORT}/ (direct)
  API UIs:    https://api.home.arpa:${CADDY_HTTPS_PORT}/swagger, https://api.home.arpa:${CADDY_HTTPS_PORT}/redoc, https://api.home.arpa:${CADDY_HTTPS_PORT}/dashboard
  API direct: http://127.0.0.1:9108/swagger, http://127.0.0.1:9108/redoc, http://127.0.0.1:9108/dashboard

If hosts mapping was added, you can also visit:
  - curl -k https://blue.home.arpa:${CADDY_HTTPS_PORT}/
  - curl -k https://green.home.arpa:${CADDY_HTTPS_PORT}/
  - curl -k https://docs.home.arpa:${CADDY_HTTPS_PORT}/

To tear everything down when finished:
  $ ./scripts/init_demo.sh --down

Controller status above shows ready/live replicas. To run the controller loop with API:
  $ $PY_BIN -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch

After starting the controller with --metrics-port 9108, view the API docs:
  - Docs UI:   http://127.0.0.1:9108/docs
  - OpenAPI:   http://127.0.0.1:9108/openapi.json
  - Swagger:   http://127.0.0.1:9108/swagger
  - ReDoc:     http://127.0.0.1:9108/redoc

EOF
