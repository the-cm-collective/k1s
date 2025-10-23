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

APT_PACKAGES=(
  python3
  python3-venv
  python3-pip
  sqlite3
  caddy
  age
  sops
)

log "Installing system packages"
$SUDO apt-get update -y
$SUDO apt-get install -y "${APT_PACKAGES[@]}"

log "Ensuring Docker service is running"
$SUDO systemctl enable --now docker

log "Installing Python dependencies"
python3 -m pip install --upgrade pip
python3 -m pip install -e .[dev]

log "Building demo Docker images"
docker build -t demo-blue:latest samples/servers/blue

docker build -t demo-green:latest samples/servers/green

log "Starting local Caddy and Prometheus stack"
docker compose -f ops/dev/docker-compose.yaml up -d

log "Configuring hosts entries"
for host in blue.home.arpa green.home.arpa; do
  if ! grep -q "$host" /etc/hosts; then
    $SUDO sh -c "echo '127.0.0.1 $host' >> /etc/hosts"
  fi
done

export AE_CADDY_SITES=${AE_CADDY_SITES:-ops/dev/caddy/sites}
export AE_STATE_DB=${AE_STATE_DB:-state/controller.db}
mkdir -p "${AE_CADDY_SITES}"

log "Applying demo manifests"
python3 -m ae.cli apply -f specs/examples/blue.yaml
python3 -m ae.cli apply -f specs/examples/green.yaml

log "Current status"
python3 -m ae.cli status

log "Demo setup complete. Test with: curl http://blue.home.arpa/"
