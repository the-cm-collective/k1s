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
HOSTS=(blue.home.arpa green.home.arpa docs.home.arpa api.home.arpa echo.home.arpa echo-mr.home.arpa echo-resources.home.arpa echo-sec.home.arpa echo-tcp.home.arpa echo-exec.home.arpa)
AUTO_HOSTS=""  # set by -y/--yes or -n/--no to auto answer host prompts

usage() {
  cat <<USAGE
Usage:
  ./scripts/init_demo.sh [OPTIONS]      # Set up the demo environment
  ./scripts/init_demo.sh --down [OPTS]  # Tear the demo down (and optionally clean hosts)
  ./scripts/init_demo.sh --help         # Show this help

Options:
  -y, --yes    Automatically accept /etc/hosts modification prompts (setup/teardown)
  -n, --no     Automatically decline /etc/hosts modification prompts (setup/teardown)
  --no-controller  Do not auto-start the controller daemon
  --no-supervisor  Start controller once (no restart loop)
  -d, --debug  Attach logs to console for troubleshooting (blocks; Ctrl-C to exit)
  --bind-all   Bind local helpers (docs server) to 0.0.0.0 instead of 127.0.0.1 for LAN access
  --demo-configs   Apply the configs/secrets demo (echo) and enable plaintext secrets for local run
  --demo-standard  Apply the standard demo (blue, green)
  --demo-echo-mr   Apply the multi-replica echo demo (echo-mr)
  --demo-security  Apply security-hardened demo (echo-sec)
  --demo-tcp       Apply TCP-probe demo (echo-tcp)
  --demo-exec      Apply exec-probe demo (echo-exec)
  --docs-only      Start docs + API only (no apps)
  --demo-rollout   Apply a two-step ordered rollout for echo
  --demo-storage   Apply a storage (PV-lite) demo for echo and list volumes
  --hosts-ip IP    Use this IP for /etc/hosts entries (default 127.0.0.1)

What this does (setup):
  1) Ensures required system packages (python3, venv, pip, sqlite3, age, sops) are present
  2) Creates a Python virtualenv (.venv-demo) and installs project deps
  3) Builds demo Docker images (blue/green) under samples/servers/
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
  - AE_STATE_DB, AE_SPECS_DIR, AE_CADDY_* (see docs/runbook.md)

Endpoints after setup:
  - Apps via Caddy: https://blue.home.arpa:8443/ and https://green.home.arpa:8443/
  - Docs via Caddy: https://docs.home.arpa:8443/
  - API via Caddy:  https://api.home.arpa:8443/ (Swagger /swagger, ReDoc /redoc, Dashboard /dashboard)
  - Docs direct:    http://127.0.0.1:9109/

USAGE
}

# Parse flags (supports combining with --down)
DOWN_FLAG=0
NO_CONTROLLER=0
API_PORT=${API_PORT:-9108}
# Runtime backend (default to podman/OCI if not set)
AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND:-podman}
NO_SUPERVISOR=0
DEBUG_ATTACH=0
DEMO_CONFIGS=0
DEMO_STANDARD=0
DEMO_ECHO_MR=0
DEMO_SECURITY=0
DEMO_TCP=0
DEMO_EXEC=0
DOCS_ONLY=0
DEMO_ROLLOUT=0
DEMO_STORAGE=0
HOSTS_IP=${HOSTS_IP:-127.0.0.1}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h|help)
      usage; exit 0 ;;
    --down|down)
      DOWN_FLAG=1 ;;
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
    --demo-security)
      DEMO_SECURITY=1 ;;
    --demo-tcp)
      DEMO_TCP=1 ;;
    --demo-exec)
      DEMO_EXEC=1 ;;
    --docs-only)
      DOCS_ONLY=1 ;;
    --demo-rollout)
      DEMO_ROLLOUT=1 ;;
    --demo-storage)
      DEMO_STORAGE=1 ;;
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
  # Best-effort: kill any stray supervisors/controllers on the same port
  pkill -f 'scripts/supervise_controller\.sh .*\s[0-9]{4,5}$' 2>/dev/null || true
  pkill -f 'python(.venv-demo)?/bin/python -m ae\.controller' 2>/dev/null || true
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
    "${STACK_COMPOSE_DOWN[@]}" -f ops/dev/docker-compose.yaml down || true
    # Remove demo app containers
    log "Removing demo app containers (label=ae.app)"
    ids=$("$STACK_BIN_DOWN" ps -aq --filter "label=ae.app" || true)
    if [[ -n "$ids" ]]; then
      "$STACK_BIN_DOWN" rm -f $ids || true
    fi
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

# Select host network alias for container->host routing
if [[ "$STACK_BIN" == "podman" ]]; then
  HOST_ALIAS=host.containers.internal
else
  HOST_ALIAS=host.docker.internal
fi

# Export shared network name so runtimes can attach app containers to the ingress network
export AE_DOCKER_NETWORK=${AE_DOCKER_NETWORK:-dev_default}
export AE_PODMAN_NETWORK=${AE_PODMAN_NETWORK:-dev_default}

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

log "Building demo images (backend=$AE_RUNTIME_BACKEND)"
if [[ "$AE_RUNTIME_BACKEND" == "podman" || "$AE_RUNTIME_BACKEND" == "oci" ]]; then
  if command -v podman >/dev/null 2>&1; then
    podman build -t localhost/demo-blue:latest samples/servers/blue || true
    podman build -t localhost/demo-green:latest samples/servers/green || true
  else
    log "Podman not available; building images with Docker as a fallback"
    docker build -t demo-blue:latest samples/servers/blue || true
    docker build -t demo-green:latest samples/servers/green || true
  fi
else
  docker build -t demo-blue:latest samples/servers/blue || true
  docker build -t demo-green:latest samples/servers/green || true
fi

log "Starting local Caddy and Prometheus stack"
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
# Ensure state directories exist with liberal perms for rootless Podman
mkdir -p state/caddy-data state/caddy docs/site || true
# Ensure Caddy can write to /data even under rootless runtimes; if the directory is
# not writable (e.g., created by root from a previous run), replace it with a fresh one.
chmod -R 0777 state/caddy-data state/caddy || true
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
  if ! ${STACK_COMPOSE[@]} -f ops/dev/docker-compose.yaml up -d; then
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
      ${STACK_COMPOSE[@]} -f ops/dev/docker-compose.yaml up -d
    else
      # Non-Podman path: rethrow
      ${STACK_COMPOSE[@]} -f ops/dev/docker-compose.yaml up -d
    fi
  fi

  # Trust Caddy's local CA on the host so browsers don't warn on every run.
  # This only applies to Debian/Ubuntu hosts.
  if command -v update-ca-certificates >/dev/null 2>&1; then
    # Trigger local CA creation by touching one TLS site once (ignore TLS verify)
    curl -ksS "https://docs.home.arpa:${CADDY_HTTPS_PORT}/" >/dev/null 2>&1 || true
    # Give Caddy a moment to mint the local CA and leaf certs
    sleep 1
    # Export Caddy local root from the container if present
    if "$STACK_BIN" exec dev-caddy-1 test -f /data/caddy/pki/authorities/local/root.crt; then
      mkdir -p state/certs
      "$STACK_BIN" cp dev-caddy-1:/data/caddy/pki/authorities/local/root.crt state/certs/caddy-local-root.crt >/dev/null 2>&1 || true
      if [[ -s state/certs/caddy-local-root.crt ]]; then
        log "Installing Caddy local root CA into host trust store"
        $SUDO cp state/certs/caddy-local-root.crt /usr/local/share/ca-certificates/caddy-local-root.crt
        $SUDO update-ca-certificates >/dev/null 2>&1 || true
      fi
    else
      # If CA not present yet, fix perms and try one more cycle (Podman rootless often needs this)
      chmod -R 0777 state/caddy-data || true
      sleep 1
      ${STACK_COMPOSE[@]} -f ops/dev/docker-compose.yaml restart caddy || true
      sleep 1
      if "$STACK_BIN" exec dev-caddy-1 test -f /data/caddy/pki/authorities/local/root.crt; then
        mkdir -p state/certs
        "$STACK_BIN" cp dev-caddy-1:/data/caddy/pki/authorities/local/root.crt state/certs/caddy-local-root.crt >/dev/null 2>&1 || true
        if [[ -s state/certs/caddy-local-root.crt ]]; then
          log "Installing Caddy local root CA into host trust store (retry)"
          $SUDO cp state/certs/caddy-local-root.crt /usr/local/share/ca-certificates/caddy-local-root.crt
          $SUDO update-ca-certificates >/dev/null 2>&1 || true
        fi
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
export AE_STATE_DB=${AE_STATE_DB:-state/controller.db}
# Avoid indefinite hangs on Caddy reload inside docker exec
export AE_CADDY_RELOAD_TIMEOUT=${AE_CADDY_RELOAD_TIMEOUT:-10}
# For local demos, allow plaintext secrets by default unless explicitly disabled
export AE_ALLOW_PLAINTEXT_SECRETS=${AE_ALLOW_PLAINTEXT_SECRETS:-1}
# Mark this run as demo-init so components can quiet benign warnings
export AE_DEMO_MODE=${AE_DEMO_MODE:-1}
export AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND}
mkdir -p "${AE_CADDY_SITES}"
if [[ ! -w "${AE_CADDY_SITES}" ]]; then
  log "Adjusting permissions on ${AE_CADDY_SITES} (may require sudo)"
  $SUDO chown -R "$(id -u):$(id -g)" "${AE_CADDY_SITES}" || true
fi

# Ensure app containers join the dev compose network so Caddy can resolve them by name (docker path)
export AE_DOCKER_NETWORK=${AE_DOCKER_NETWORK:-dev_default}

# Write env helper for manual shells (after exports)
mkdir -p state
cat > state/env.sh <<ENV
export AE_CADDY_SITES=${AE_CADDY_SITES}
export AE_CADDY_FILE=${AE_CADDY_FILE}
export AE_CADDY_CONTAINER=${AE_CADDY_CONTAINER}
export AE_DOCKER_NETWORK=${AE_DOCKER_NETWORK}
export AE_CONTAINER_CLI=${AE_CONTAINER_CLI}
export AE_ALLOW_PLAINTEXT_SECRETS=${AE_ALLOW_PLAINTEXT_SECRETS}
export AE_DEMO_MODE=${AE_DEMO_MODE}
export AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND}
export API_PORT=${API_PORT}
ENV

# Seed dynsites for docs and API so they are always available
DOCS_PORT=${DOCS_PORT:-9109}
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
    reverse_proxy ${HOST_ALIAS}:${API_PORT}
}
API

# Auto-start the controller daemon unless disabled
if [[ $NO_CONTROLLER -eq 0 ]]; then
  if [[ -f state/controller.pid ]]; then
    CTRL_PID=$(cat state/controller.pid || true)
  else
    CTRL_PID=""
  fi
  if [[ -n "${CTRL_PID}" ]] && kill -0 "$CTRL_PID" 2>/dev/null; then
    log "Controller already running (pid ${CTRL_PID})"
  else
    if [[ $NO_SUPERVISOR -eq 0 ]]; then
      log "Starting controller supervisor (port ${API_PORT})"
      nohup bash scripts/supervise_controller.sh "$PY_BIN" specs "${API_PORT}" >/dev/null 2>&1 &
      echo $! > state/controller_supervisor.pid
    else
      log "Starting controller once on :${API_PORT} (background)"
      nohup "$PY_BIN" -m ae.controller --loop --specs specs/ --metrics-port "${API_PORT}" --watch \
        >/dev/null 2>&1 &
      echo $! > state/controller.pid
    fi
  fi
else
  log "Skipping controller auto-start (--no-controller)"
fi
ls -1 ops/dev/caddy/sites | sed 's/^/  - /' | xargs -r -I{} true >/dev/null 2>&1 || true

log "Applying demo manifests"
APPLY_TIMEOUT=${APPLY_TIMEOUT:-120}

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
    nohup bash scripts/supervise_controller.sh "$PY_BIN" specs "${API_PORT}" >/dev/null 2>&1 &
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

if [[ $DOCS_ONLY -ne 1 ]]; then
  # Default: apply standard demo unless explicitly disabled. If no flags specified, treat as standard.
  if [[ $DEMO_STANDARD -eq 1 ]] || { [[ $DEMO_STANDARD -eq 0 && $DEMO_ECHO_MR -eq 0 && $DEMO_CONFIGS -eq 0 ]]; }; then
    if ! timeout --kill-after=5 "$APPLY_TIMEOUT" "$PY_BIN" -m ae.cli --verbose apply -f specs/examples/blue.yaml; then
      log "Apply for blue timed out or failed. Diagnostics:"
      "$STACK_BIN" ps || true
      log "Try: $STACK_BIN logs dev-caddy-1; $STACK_BIN exec dev-caddy-1 caddy reload --config /etc/caddy/Caddyfile"
      log "Or re-run with more verbosity: $PY_BIN -m ae.cli --verbose apply -f specs/examples/blue.yaml"
      exit 1
    fi
    if ! timeout --kill-after=5 "$APPLY_TIMEOUT" "$PY_BIN" -m ae.cli --verbose apply -f specs/examples/green.yaml; then
      log "Apply for green timed out or failed. Diagnostics:"
      "$STACK_BIN" ps || true
      log "Try: $STACK_BIN logs dev-caddy-1; $STACK_BIN exec dev-caddy-1 caddy reload --config /etc/caddy/Caddyfile"
      log "Or re-run with more verbosity: $PY_BIN -m ae.cli --verbose apply -f specs/examples/green.yaml"
      exit 1
    fi
  fi
fi

# Optional configs/secrets demo
if [[ $DEMO_CONFIGS -eq 1 ]]; then
  export AE_ALLOW_PLAINTEXT_SECRETS=1
  log "Applying configs/secrets demo (echo) with plaintext secrets enabled"
  if "$PY_BIN" -m ae.cli apply -f specs/examples/echo.yaml; then
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
  "$PY_BIN" -m ae.cli apply -f specs/examples/multi-replica-echo.yaml || true
fi

# Optional security demo
if [[ $DEMO_SECURITY -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying security-hardened echo demo (echo-sec)"
  "$PY_BIN" -m ae.cli apply -f specs/examples/echo-sec.yaml || true
fi

# Optional TCP probe demo
if [[ $DEMO_TCP -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying TCP-probe echo demo (echo-tcp)"
  "$PY_BIN" -m ae.cli apply -f specs/examples/echo-tcp.yaml || true
fi

# Optional exec probe demo
if [[ $DEMO_EXEC -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying exec-probe echo demo (echo-exec)"
  "$PY_BIN" -m ae.cli apply -f specs/examples/echo-exec.yaml || true
fi

# Optional rollout demo: apply echo, then echo-rollout
if [[ $DEMO_ROLLOUT -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying rollout demo (echo → echo-rollout)"
  "$PY_BIN" -m ae.cli apply -f specs/examples/echo.yaml || true
  sleep 2
  "$PY_BIN" -m ae.cli apply -f specs/examples/echo-rollout.yaml || true
  "$PY_BIN" -m ae.cli status echo --events --history 5 || true
fi

# Optional storage demo
if [[ $DEMO_STORAGE -eq 1 && $DOCS_ONLY -ne 1 ]]; then
  log "Applying storage demo (echo with PV-lite)"
  "$PY_BIN" -m ae.cli apply -f specs/examples/echo-storage.yaml || true
  log "Listing storage volumes"
  "$PY_BIN" -m ae.cli volumes list --app echo || true
fi

# Build and serve docs locally
DOCS_PORT=${DOCS_PORT:-9109}
log "Building static docs (docs/site)"
"$PY_BIN" docs/build_docs.py || true
log "Starting docs server on http://${DOCS_BIND}:${DOCS_PORT} (background)"
mkdir -p state
nohup "$PY_BIN" -m http.server "${DOCS_PORT}" --bind "${DOCS_BIND}" --directory docs/site >/dev/null 2>&1 &
echo $! > state/docs_server.pid

log "Current status"
"$PY_BIN" -m ae.cli status

# If backend is podman, ensure demo images are available to Podman by importing from Docker when needed
if [[ "$AE_RUNTIME_BACKEND" == "podman" || "$AE_RUNTIME_BACKEND" == "oci" ]]; then
  if command -v podman >/dev/null 2>&1; then
    for img in demo-blue:latest demo-green:latest; do
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
  # Tail controller log if present
  touch state/controller.log
  # Filter out known benign demo-mode messages
  grep_ctl='sops metadata not found|watchdog not available'
  tail -n 50 -F state/controller.log \
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
  tail -n 0 -F "$AE_CADDY_SITES"/*.caddy 2>/dev/null | sed -u 's/^/[sites] /' &
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
