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

HOSTS=(blue.home.arpa green.home.arpa docs.home.arpa)

usage() {
  cat <<USAGE
Usage:
  ./scripts/init_demo.sh           # Set up the demo environment
  ./scripts/init_demo.sh --down    # Tear the demo down and optionally clean hosts
  ./scripts/init_demo.sh --help    # Show this help

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
  - Apps via Caddy: http://blue.home.arpa:8080/ and http://green.home.arpa:8080/
  - Docs via Caddy: http://docs.home.arpa:8080/
  - Docs direct:   http://127.0.0.1:9109/

USAGE
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || "${1:-}" == "help" ]]; then
  usage
  exit 0
fi

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

DOWN_MODE=${1:-}
if [[ "$DOWN_MODE" == "--down" || "$DOWN_MODE" == "down" ]]; then
  log "Tearing down demo environment"
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
  if prompt_yes_no "Remove hosts entries for ${HOSTS[*]} from /etc/hosts?" N; then
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

if prompt_yes_no "Add hosts entries for ${HOSTS[*]} to /etc/hosts?" N; then
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

cat <<EOF

Demo setup complete.

- Blue app:   http://blue.home.arpa:8080/  (or http://127.0.0.1:8080 for Caddy)
- Green app:  http://green.home.arpa:8080/ (or http://127.0.0.1:8080)
- Docs site:  http://docs.home.arpa:8080/ (via Caddy) and http://127.0.0.1:${DOCS_PORT}/ (direct)

If hosts mapping was added, you can also visit:
  - curl http://blue.home.arpa:8080/
  - curl http://green.home.arpa:8080/
  - curl http://docs.home.arpa:8080/

To tear everything down when finished:
  $ ./scripts/init_demo.sh --down

Controller status above shows ready/live replicas. To run the controller loop with API:
  $ "$PY_BIN" -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch

EOF
