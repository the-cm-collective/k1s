#!/usr/bin/env bash
set -euo pipefail

usage(){
  cat <<EOF
Install/Uninstall ae controller as a systemd service.

Usage:
  $0 install         [--enable]
  $0 uninstall       [--disable]
  $0 ha-core-install [--enable]
  $0 ha-core-uninstall [--disable]
  $0 docs-install    [--enable]
  $0 docs-uninstall  [--disable]
  $0 caddy-install   [--enable] [--tls-sample]
  $0 caddy-uninstall [--disable]

Actions:
  install   - copies unit/env files, creates dirs, and (optionally) enables/starts the service
  ha-core-install - copies HA core unit/env files and wrapper, and (optionally) enables/starts the service
  uninstall - stops/disables the service and removes installed unit/env files

Environment:
  PREFIX_SYSTEMD=/etc/systemd/system (override target units dir)
  PREFIX_ETC=/etc/ae                (override config root)
  PREFIX_BIN=/usr/local/bin         (override installed helper/wrapper dir)
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
BIN_DIR="${PREFIX_BIN:-/usr/local/bin}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

write_hardening_dropin() {
  local service_name="$1"
  mkdir -p "$SYSTEMD_DIR/${service_name}.d"
  cat >"$SYSTEMD_DIR/${service_name}.d/hardening.conf" <<'EOF'
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
}

install_unit(){
  mkdir -p "$ETC_DIR/specs"
  mkdir -p "$SYSTEMD_DIR"
  # env
  install -m 0644 ops/systemd/ae.env "$ETC_DIR/ae.env"
  # unit
  install -m 0644 ops/systemd/ae-controller.service "$SYSTEMD_DIR/ae-controller.service"
  # Optional hardening drop-in
  if [[ "${AE_SYSTEMD_HARDEN:-}" == "1" ]]; then
    write_hardening_dropin "ae-controller.service"
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

install_ha_core_unit(){
  local env_path="$ETC_DIR/ha-core.env"
  local wrapper_path="$BIN_DIR/ae-ha-core-service"
  mkdir -p "$ETC_DIR" "$SYSTEMD_DIR" "$BIN_DIR"

  sed "s|__REPO_ROOT__|$REPO_ROOT|g" ops/systemd/ae-ha-core.env >"$env_path"
  sed \
    -e "s|__ENV_FILE__|$env_path|g" \
    -e "s|__WRAPPER__|$wrapper_path|g" \
    ops/systemd/ae-ha-core.service >"$SYSTEMD_DIR/ae-ha-core.service"

  cat >"$wrapper_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="\${AE_HA_CORE_ENV_FILE:-$env_path}"
if [[ -f "\$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "\$ENV_FILE"
  set +a
fi

if [[ -z "\${AE_REPO_ROOT:-}" ]]; then
  echo "error: AE_REPO_ROOT must point to the k1s checkout for ae-ha-core-service" >&2
  exit 1
fi
if [[ ! -x "\${AE_REPO_ROOT}/scripts/dev/run_profile.sh" ]]; then
  echo "error: missing run_profile.sh under AE_REPO_ROOT=\${AE_REPO_ROOT}" >&2
  exit 1
fi

export AE_DEV_LOCAL="\${AE_DEV_LOCAL:-0}"
export AE_LABS="\${AE_LABS:-0}"
export CORE_CADDY="\${CORE_CADDY:-0}"
export CORE_DOCS="\${CORE_DOCS:-0}"

cd "\${AE_REPO_ROOT}"
exec bash "\${AE_REPO_ROOT}/scripts/dev/run_profile.sh" k1s-ha-core
EOF
  chmod 0755 "$wrapper_path"

  if [[ "${AE_SYSTEMD_HARDEN:-}" == "1" ]]; then
    write_hardening_dropin "ae-ha-core.service"
  fi
  systemctl daemon-reload || true
  if [[ "$ENABLE" -eq 1 ]]; then
    systemctl enable --now ae-ha-core.service || true
  fi
  echo "Installed ae-ha-core.service. Edit $env_path and run: systemctl restart ae-ha-core"
}

uninstall_ha_core_unit(){
  local env_path="$ETC_DIR/ha-core.env"
  local wrapper_path="$BIN_DIR/ae-ha-core-service"
  if [[ "$DISABLE" -eq 1 ]]; then
    systemctl disable --now ae-ha-core.service || true
  fi
  rm -f "$SYSTEMD_DIR/ae-ha-core.service" "$wrapper_path"
  systemctl daemon-reload || true
  echo "Removed ae-ha-core.service. Config remains at $env_path (remove manually if desired)."
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
    write_hardening_dropin "ae-docs.service"
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
    write_hardening_dropin "caddy.service"
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
  ha-core-install) install_ha_core_unit ;;
  ha-core-uninstall) uninstall_ha_core_unit ;;
  docs-install) docs_install ;;
  docs-uninstall) docs_uninstall ;;
  caddy-install) caddy_install ;;
  caddy-uninstall) caddy_uninstall ;;
  *) usage; exit 2 ;;
esac
