#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${CONTROLLER_ENV_FILE:-$ROOT_DIR/state/env.sh}"
APISHIM_ENV_FILE="${APISHIM_ENV_FILE:-}"
PROFILE_DIR="${PROFILE_DIR:-}"

log() {
  printf '[controller-env] %s\n' "$1"
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

require_strong() {
  local label="$1"
  local val="$2"
  local min_len="${3:-24}"
  if [[ -z "$val" ]]; then
    return 1
  fi
  if [[ "${#val}" -lt "$min_len" ]]; then
    echo "[controller-env] $label is too short (${#val} < $min_len). Set a longer value or omit to auto-generate." >&2
    return 2
  fi
  return 0
}

default_profile_dir() {
  if [[ -n "$PROFILE_DIR" ]]; then
    printf '%s' "$PROFILE_DIR"
    return 0
  fi
  local env_dir
  env_dir="$(dirname "$ENV_FILE")"
  if [[ "$(basename "$(dirname "$env_dir")")" == "profiles" ]]; then
    printf '%s' "$env_dir"
    return 0
  fi
  printf '%s/state' "$ROOT_DIR"
}

profile_dir="$(default_profile_dir)"

admin_token="$(read_env_var "AE_API_ADMIN_TOKEN" "$APISHIM_ENV_FILE" || true)"
if [[ -z "$admin_token" ]]; then
  admin_token="${AE_API_ADMIN_TOKEN:-}"
fi
if [[ -z "$admin_token" ]]; then
  admin_token="$(read_env_var "AE_API_ADMIN_TOKEN" "$ENV_FILE" || true)"
fi

scaler_token="${AE_API_SCALER_TOKEN:-}"
if [[ -z "$scaler_token" ]]; then
  scaler_token="$(read_env_var "AE_API_SCALER_TOKEN" "$ENV_FILE" || true)"
fi

read_token="${AE_API_READ_TOKEN:-}"
if [[ -z "$read_token" ]]; then
  read_token="$(read_env_var "AE_API_READ_TOKEN" "$ENV_FILE" || true)"
fi

labs_token="${AE_LABS_TOKEN:-}"
if [[ -z "$labs_token" ]]; then
  labs_token="$(read_env_var "AE_LABS_TOKEN" "$APISHIM_ENV_FILE" || true)"
fi
if [[ -z "$labs_token" ]]; then
  labs_token="$(read_env_var "AE_LABS_TOKEN" "$ENV_FILE" || true)"
fi

state_db="${AE_STATE_DB:-}"
if [[ -z "$state_db" ]]; then
  state_db="$(read_env_var "AE_STATE_DB" "$ENV_FILE" || true)"
fi
if [[ -z "$state_db" ]]; then
  state_db="${profile_dir}/controller.db"
fi

etcd_endpoints="${AE_ETCD_ENDPOINTS:-}"
if [[ -z "$etcd_endpoints" ]]; then
  etcd_endpoints="$(read_env_var "AE_ETCD_ENDPOINTS" "$ENV_FILE" || true)"
fi

etcd_prefix="${AE_ETCD_PREFIX:-}"
if [[ -z "$etcd_prefix" ]]; then
  etcd_prefix="$(read_env_var "AE_ETCD_PREFIX" "$ENV_FILE" || true)"
fi

state_backend="${AE_STATE_BACKEND:-}"
if [[ -z "$state_backend" ]]; then
  state_backend="$(read_env_var "AE_STATE_BACKEND" "$ENV_FILE" || true)"
fi
if [[ -z "$state_backend" ]]; then
  if [[ -n "$etcd_endpoints" ]]; then
    state_backend="etcd"
  else
    state_backend="sqlite"
  fi
fi

apishim_server="${AE_APISHIM_SERVER:-}"
if [[ -z "$apishim_server" ]]; then
  apishim_server="$(read_env_var "AE_APISHIM_SERVER" "$ENV_FILE" || true)"
fi
if [[ -z "$apishim_server" ]]; then
  apishim_server="https://127.0.0.1:${APISHIM_PORT:-8445}"
fi

if ! require_strong "AE_API_ADMIN_TOKEN" "$admin_token"; then
  admin_token="$(gen_token_hex)"
fi
if ! require_strong "AE_API_SCALER_TOKEN" "$scaler_token"; then
  scaler_token="$(gen_token_hex)"
fi
if ! require_strong "AE_API_READ_TOKEN" "$read_token"; then
  read_token="$(gen_token_hex)"
fi

mkdir -p "$(dirname "$ENV_FILE")"
umask 077
{
  printf 'AE_API_ADMIN_TOKEN=%s\n' "$admin_token"
  printf 'AE_API_SCALER_TOKEN=%s\n' "$scaler_token"
  printf 'AE_API_READ_TOKEN=%s\n' "$read_token"
  printf 'AE_STATE_DB=%s\n' "$state_db"
  printf 'AE_STATE_BACKEND=%s\n' "$state_backend"
  if [[ -n "$etcd_endpoints" ]]; then
    printf 'AE_ETCD_ENDPOINTS=%s\n' "$etcd_endpoints"
  fi
  if [[ -n "$etcd_prefix" ]]; then
    printf 'AE_ETCD_PREFIX=%s\n' "$etcd_prefix"
  fi
  if [[ -n "$labs_token" ]]; then
    printf 'AE_LABS_TOKEN=%s\n' "$labs_token"
  fi
  if [[ -n "$apishim_server" ]]; then
    printf 'AE_APISHIM_SERVER=%s\n' "$apishim_server"
  fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

log "Wrote $ENV_FILE (controller auth env)."
