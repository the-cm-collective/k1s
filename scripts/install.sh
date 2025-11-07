#!/usr/bin/env bash
set -euo pipefail

usage(){
  cat <<EOF
Install/Uninstall ae controller as a systemd service.

Usage:
  $0 install         [--enable]
  $0 uninstall       [--disable]
  $0 docs-install    [--enable]
  $0 docs-uninstall  [--disable]
  $0 caddy-install   [--enable] [--tls-sample]
  $0 caddy-uninstall [--disable]

Actions:
  install   - copies unit/env files, creates dirs, and (optionally) enables/starts the service
  uninstall - stops/disables the service and removes installed unit/env files

Environment:
  PREFIX_SYSTEMD=/etc/systemd/system (override target units dir)
  PREFIX_ETC=/etc/ae                (override config root)
EOF
}

ACTION="${1:-}"
ENABLE=0
DISABLE=0
TLS_SAMPLE=0
shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable) ENABLE=1 ;;
    --disable) DISABLE=1 ;;
    --tls-sample) TLS_SAMPLE=1 ;;
    -h|--help) usage; exit 0 ;;
  esac
  shift || true
done

SYSTEMD_DIR="${PREFIX_SYSTEMD:-/etc/systemd/system}"
ETC_DIR="${PREFIX_ETC:-/etc/ae}"

install_unit(){
  mkdir -p "$ETC_DIR/specs"
  mkdir -p "$SYSTEMD_DIR"
  # env
  install -m 0644 ops/systemd/ae.env "$ETC_DIR/ae.env"
  # unit
  install -m 0644 ops/systemd/ae-controller.service "$SYSTEMD_DIR/ae-controller.service"
  # Optional hardening drop-in
  if [[ "${AE_SYSTEMD_HARDEN:-}" == "1" ]]; then
    mkdir -p "$SYSTEMD_DIR/ae-controller.service.d"
    cat >"$SYSTEMD_DIR/ae-controller.service.d/hardening.conf" <<'EOF'
[Service]
# Hardened defaults (opt-in)
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
LockPersonality=true
RestrictSUIDSGID=true
ProtectControlGroups=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectClock=yes
RestrictRealtime=yes
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
SystemCallFilter=@system-service
EOF
  fi
  # sample spec (echo)
  if [[ -f specs/examples/echo.yaml && ! -f "$ETC_DIR/specs/echo.yaml" ]]; then
    install -m 0644 specs/examples/echo.yaml "$ETC_DIR/specs/echo.yaml"
  fi
  systemctl daemon-reload || true
  if [[ "$ENABLE" -eq 1 ]]; then
    systemctl enable --now ae-controller.service || true
  fi
  echo "Installed ae-controller.service. Edit $ETC_DIR/ae.env and run: systemctl restart ae-controller"
}

uninstall_unit(){
  if [[ "$DISABLE" -eq 1 ]]; then
    systemctl disable --now ae-controller.service || true
  fi
  rm -f "$SYSTEMD_DIR/ae-controller.service"
  systemctl daemon-reload || true
  echo "Removed ae-controller.service. Config remains at $ETC_DIR (remove manually if desired)."
}

docs_install(){
  mkdir -p "$SYSTEMD_DIR"
  # Copy docs (if built) to /usr/share/ae/docs
  local dst="${AE_DOCS_DIR:-/usr/share/ae/docs}"
  mkdir -p "$dst"
  if [[ -d docs/site ]]; then
    rsync -a --delete docs/site/ "$dst/"
  else
    echo "warning: docs/site not found; serving empty directory $dst" >&2
  fi
  install -m 0644 ops/systemd/ae-docs.service "$SYSTEMD_DIR/ae-docs.service"
  if [[ "${AE_SYSTEMD_HARDEN:-}" == "1" ]]; then
    mkdir -p "$SYSTEMD_DIR/ae-docs.service.d"
    cat >"$SYSTEMD_DIR/ae-docs.service.d/hardening.conf" <<'EOF'
[Service]
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
LockPersonality=true
RestrictSUIDSGID=true
ProtectControlGroups=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectClock=yes
RestrictRealtime=yes
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
SystemCallFilter=@system-service
EOF
  fi
  systemctl daemon-reload || true
  if [[ "$ENABLE" -eq 1 ]]; then
    systemctl enable --now ae-docs.service || true
  fi
  echo "Installed ae-docs.service serving ${dst} (port from AE_DOCS_PORT or 9109)"
}

docs_uninstall(){
  if [[ "$DISABLE" -eq 1 ]]; then
    systemctl disable --now ae-docs.service || true
  fi
  rm -f "$SYSTEMD_DIR/ae-docs.service"
  systemctl daemon-reload || true
  echo "Removed ae-docs.service"
}

# Caddy reverse proxy (optional)
caddy_install(){
  mkdir -p /etc/caddy
  if [[ ! -f /etc/caddy/Caddyfile ]]; then
    if [[ "$TLS_SAMPLE" -eq 1 ]]; then
      install -m 0644 ops/caddy/Caddyfile.tls.sample /etc/caddy/Caddyfile
      echo "Installed TLS sample Caddyfile at /etc/caddy/Caddyfile (hosts: docs.home.arpa, api.home.arpa on :8443)" >&2
      echo "Hint: add to /etc/hosts => 127.0.0.1 docs.home.arpa api.home.arpa" >&2
    else
      install -m 0644 ops/caddy/Caddyfile.sample /etc/caddy/Caddyfile
    fi
  else
    echo "Caddyfile already exists at /etc/caddy/Caddyfile; leaving in place" >&2
  fi
  install -m 0644 ops/systemd/caddy.service "$SYSTEMD_DIR/caddy.service"
  systemctl daemon-reload || true
  if [[ "$ENABLE" -eq 1 ]]; then
    systemctl enable --now caddy.service || true
  fi
  echo "Installed caddy.service; edit /etc/caddy/Caddyfile and run: systemctl reload caddy"
  if [[ "${AE_SYSTEMD_HARDEN:-}" == "1" ]]; then
    mkdir -p "$SYSTEMD_DIR/caddy.service.d"
    cat >"$SYSTEMD_DIR/caddy.service.d/hardening.conf" <<'EOF'
[Service]
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
LockPersonality=true
RestrictSUIDSGID=true
ProtectControlGroups=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectClock=yes
RestrictRealtime=yes
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
SystemCallFilter=@system-service
EOF
    systemctl daemon-reload || true
  fi
}

caddy_uninstall(){
  if [[ "$DISABLE" -eq 1 ]]; then
    systemctl disable --now caddy.service || true
  fi
  rm -f "$SYSTEMD_DIR/caddy.service"
  systemctl daemon-reload || true
  echo "Removed caddy.service (left /etc/caddy/Caddyfile in place)"
}

case "$ACTION" in
  install) install_unit ;;
  uninstall) uninstall_unit ;;
  docs-install) docs_install ;;
  docs-uninstall) docs_uninstall ;;
  caddy-install) caddy_install ;;
  caddy-uninstall) caddy_uninstall ;;
  *) usage; exit 2 ;;
esac
