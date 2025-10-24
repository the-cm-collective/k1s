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

HOSTS=(blue.home.arpa green.home.arpa docs.home.arpa api.home.arpa)
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

What this does (setup):
  1) Ensures required system packages (python3, venv, pip, sqlite3, age, sops) are present
  2) Creates a Python virtualenv (.venv-demo) and installs project deps
  3) Builds demo Docker images (blue/green) under samples/servers/
  4) Starts the dev stack (Caddy on :8080 and Prometheus on :9090)
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
  - Docker Engine installed and enabled (this script will try to start it)

Environment variables you can override:
  - VENV_DIR (default .venv-demo)
  - DOCS_PORT (default 9109)
  - AE_STATE_DB, AE_SPECS_DIR, AE_CADDY_* (see docs/runbook.md)

Endpoints after setup:
  - Apps via Caddy: https://blue.home.arpa:8443/ and https://green.home.arpa:8443/
  - Docs via Caddy: https://docs.home.arpa:8443/
  - API via Caddy:  https://api.home.arpa:8443/ (Swagger at /swagger, ReDoc at /redoc)
  - Docs direct:    http://127.0.0.1:9109/

USAGE
}

# Parse flags (supports combining with --down)
DOWN_FLAG=0
NO_CONTROLLER=0
API_PORT=${API_PORT:-9108}
NO_SUPERVISOR=0
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
    rm -f state/controller_supervisor.pid
  fi
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
  if command -v docker >/dev/null 2>&1; then
    log "Stopping dev docker compose stack (caddy, prometheus)"
    docker compose -f ops/dev/docker-compose.yaml down || true
    # Remove demo app containers
    log "Removing demo app containers (label=ae.app)"
    ids=$(docker ps -aq --filter "label=ae.app" || true)
    if [[ -n "$ids" ]]; then
      docker rm -f $ids || true
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

log "Ensuring Docker service is running"
$SUDO systemctl enable --now docker

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

log "Building demo Docker images"
docker build -t demo-blue:latest samples/servers/blue

docker build -t demo-green:latest samples/servers/green

log "Starting local Caddy and Prometheus stack"
docker compose -f ops/dev/docker-compose.yaml up -d

if prompt_yes_no_hosts "Add hosts entries for ${HOSTS[*]} to /etc/hosts?" N; then
  log "Configuring hosts entries"
  for host in "${HOSTS[@]}"; do
    if ! grep -q "$host" /etc/hosts; then
      $SUDO sh -c "echo '127.0.0.1 $host' >> /etc/hosts"
    fi
  done
else
  log "Skipping hosts entries; use direct addresses instead"
fi

export AE_CADDY_SITES=${AE_CADDY_SITES:-ops/dev/caddy/sites}
export AE_CADDY_FILE=${AE_CADDY_FILE:-/etc/caddy/Caddyfile}
# When using the dev docker-compose stack, reload Caddy inside the container.
export AE_CADDY_CONTAINER=${AE_CADDY_CONTAINER:-dev-caddy-1}
export AE_STATE_DB=${AE_STATE_DB:-state/controller.db}
# Avoid indefinite hangs on Caddy reload inside docker exec
export AE_CADDY_RELOAD_TIMEOUT=${AE_CADDY_RELOAD_TIMEOUT:-10}
mkdir -p "${AE_CADDY_SITES}"

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

# Wait briefly for Caddy container to accept execs
for i in {1..20}; do
  if docker exec "$AE_CADDY_CONTAINER" caddy version >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! timeout --kill-after=5 "$APPLY_TIMEOUT" "$PY_BIN" -m ae.cli --verbose apply -f specs/examples/blue.yaml; then
  log "Apply for blue timed out or failed. Diagnostics:"
  docker ps || true
  log "Try: docker logs dev-caddy-1; docker exec dev-caddy-1 caddy reload --config /etc/caddy/Caddyfile"
  log "Or re-run with more verbosity: $PY_BIN -m ae.cli --verbose apply -f specs/examples/blue.yaml"
  exit 1
fi
if ! timeout --kill-after=5 "$APPLY_TIMEOUT" "$PY_BIN" -m ae.cli --verbose apply -f specs/examples/green.yaml; then
  log "Apply for green timed out or failed. Diagnostics:"
  docker ps || true
  log "Try: docker logs dev-caddy-1; docker exec dev-caddy-1 caddy reload --config /etc/caddy/Caddyfile"
  log "Or re-run with more verbosity: $PY_BIN -m ae.cli --verbose apply -f specs/examples/green.yaml"
  exit 1
fi

# Build and serve docs locally
DOCS_PORT=${DOCS_PORT:-9109}
log "Building static docs (docs/site)"
"$PY_BIN" docs/build_docs.py || true
log "Starting docs server on http://127.0.0.1:${DOCS_PORT} (background)"
mkdir -p state
nohup "$PY_BIN" -m http.server "${DOCS_PORT}" --directory docs/site >/dev/null 2>&1 &
echo $! > state/docs_server.pid

log "Current status"
"$PY_BIN" -m ae.cli status

# Report reachability of the controller HTTP API and UIs.
check_api_reachability() {
  API_PORT=${API_PORT:-9108}
  CADDY_HTTPS_PORT=${CADDY_HTTPS_PORT:-8443}
  echo
  log "API reachability checks (expected after you start the controller)"

  # Direct API JSON
  if curl -fsS "http://127.0.0.1:${API_PORT}/openapi.json" >/dev/null 2>&1; then
    log "Direct API OK: http://127.0.0.1:${API_PORT}/openapi.json"
    for path in /swagger /redoc; do
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
  else
    log "Caddy API not reachable (HTTP ${code_api:-fail})."
    echo "If direct API works, try reloading Caddy:"
    echo "  $ docker exec dev-caddy-1 caddy reload --config /etc/caddy/Caddyfile"
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

cat <<EOF

Demo setup complete.

- Blue app:   https://blue.home.arpa:8443/
- Green app:  https://green.home.arpa:8443/
- Docs site:  https://docs.home.arpa:8443/ (via Caddy) and http://127.0.0.1:${DOCS_PORT}/ (direct)
  API UIs:    https://api.home.arpa:8443/swagger (Swagger), https://api.home.arpa:8443/redoc (ReDoc)
  API direct: http://127.0.0.1:9108/swagger and http://127.0.0.1:9108/redoc

If hosts mapping was added, you can also visit:
  - curl -k https://blue.home.arpa:8443/
  - curl -k https://green.home.arpa:8443/
  - curl -k https://docs.home.arpa:8443/

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
