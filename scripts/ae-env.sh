#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APISHIM_ENV_FILE="${APISHIM_ENV_FILE:-$ROOT_DIR/state/labs/apishim.env}"
DEV_ENV_FILE="${DEV_ENV_FILE:-$ROOT_DIR/state/dev.env}"
CONTROLLER_ENV_FILE="${CONTROLLER_ENV_FILE:-$ROOT_DIR/state/env.sh}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/ae-env.sh local [--output FILE]
  ./scripts/ae-env.sh generate [--output FILE]

local:   Print export lines from local state env files (apishim/controller).
generate: Generate fresh tokens and print export lines (for remote setup).
EOF
}

read_env_var() {
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

gen_token_urlsafe() {
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

gen_token_hex() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    python - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
    return 0
  fi
  head -c 16 /dev/urandom | xxd -p -c 32
}

mode="${1:-}"
shift || true
out_file=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output|-o)
      out_file="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

emit() {
  local line="$1"
  if [[ -n "$out_file" ]]; then
    printf '%s\n' "$line" >> "$out_file"
  else
    printf '%s\n' "$line"
  fi
}

case "$mode" in
  local)
    if [[ -n "$out_file" ]]; then
      : > "$out_file"
    fi

    apishim_token="$(read_env_var "AE_APISHIM_TOKEN" "$APISHIM_ENV_FILE" || true)"
    apishim_read="$(read_env_var "AE_APISHIM_READ_TOKEN" "$APISHIM_ENV_FILE" || true)"
    apishim_secret="$(read_env_var "AE_APISHIM_SESSION_SECRET" "$APISHIM_ENV_FILE" || true)"
    admin_token="$(read_env_var "AE_API_ADMIN_TOKEN" "$APISHIM_ENV_FILE" || true)"
    labs_token="$(read_env_var "AE_LABS_TOKEN" "$APISHIM_ENV_FILE" || true)"
    if [[ -z "$admin_token" ]]; then
      admin_token="$(read_env_var "AE_API_ADMIN_TOKEN" "$CONTROLLER_ENV_FILE" || true)"
    fi
    scaler_token="$(read_env_var "AE_API_SCALER_TOKEN" "$CONTROLLER_ENV_FILE" || true)"
    read_token="$(read_env_var "AE_API_READ_TOKEN" "$CONTROLLER_ENV_FILE" || true)"

    if [[ -n "$apishim_token" ]]; then emit "export AE_APISHIM_TOKEN=${apishim_token}"; fi
    if [[ -n "$apishim_read" ]]; then emit "export AE_APISHIM_READ_TOKEN=${apishim_read}"; fi
    if [[ -n "$apishim_secret" ]]; then emit "export AE_APISHIM_SESSION_SECRET=${apishim_secret}"; fi
    if [[ -n "$admin_token" ]]; then emit "export AE_API_ADMIN_TOKEN=${admin_token}"; fi
    if [[ -n "$labs_token" ]]; then emit "export AE_LABS_TOKEN=${labs_token}"; fi
    if [[ -n "$scaler_token" ]]; then emit "export AE_API_SCALER_TOKEN=${scaler_token}"; fi
    if [[ -n "$read_token" ]]; then emit "export AE_API_READ_TOKEN=${read_token}"; fi

    server="${AE_APISHIM_SERVER:-}"
    if [[ -z "$server" ]]; then
      upstream="${APISHIM_UPSTREAM:-}"
      port="${APISHIM_PORT:-}"
      if [[ -z "$upstream" ]]; then
        upstream="$(read_env_var "APISHIM_UPSTREAM" "$DEV_ENV_FILE" || true)"
      fi
      if [[ -z "$port" ]]; then
        port="$(read_env_var "APISHIM_PORT" "$DEV_ENV_FILE" || true)"
      fi
      if [[ -n "$upstream" ]]; then
        if [[ "$upstream" == *"://"* ]]; then
          server="$upstream"
        else
          server="https://${upstream}"
        fi
      elif [[ -n "$port" ]]; then
        server="https://127.0.0.1:${port}"
      else
        server="https://127.0.0.1:8445"
      fi
    fi
    emit "export AE_APISHIM_SERVER=${server}"
    ;;
  generate)
    if [[ -n "$out_file" ]]; then
      : > "$out_file"
    fi
    apishim_token="$(gen_token_urlsafe)"
    apishim_read="$(gen_token_urlsafe)"
    apishim_secret="$(gen_token_urlsafe)"
    admin_token="$(gen_token_hex)"
    scaler_token="$(gen_token_hex)"
    read_token="$(gen_token_hex)"

    emit "export AE_APISHIM_TOKEN=${apishim_token}"
    emit "export AE_APISHIM_READ_TOKEN=${apishim_read}"
    emit "export AE_APISHIM_SESSION_SECRET=${apishim_secret}"
    emit "export AE_API_ADMIN_TOKEN=${admin_token}"
    emit "export AE_API_SCALER_TOKEN=${scaler_token}"
    emit "export AE_API_READ_TOKEN=${read_token}"
    emit "export AE_API_MUTATIONS=1"
    ;;
  *)
    usage
    exit 2
    ;;
esac
