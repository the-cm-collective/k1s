#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${APISHIM_ENV_FILE:-$ROOT_DIR/state/labs/apishim.env}"
ENV_OVERRIDE_FILE="${APISHIM_ENV_OVERRIDE_FILE:-$ROOT_DIR/.env}"

log() {
  printf '[apishim-env] %s\n' "$1"
}

read_env_var() {
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
  # Fallback: 32 bytes -> base64, strip padding
  head -c 32 /dev/urandom | base64 | tr -d '=\n'
}

require_strong() {
  local label="$1"
  local val="$2"
  local min_len="${3:-24}"
  if [[ -z "$val" ]]; then
    return 1
  fi
  if [[ "${#val}" -lt "$min_len" ]]; then
    echo "[apishim-env] $label is too short (${#val} < $min_len). Set a longer value or omit to auto-generate." >&2
    return 2
  fi
  return 0
}

token="${AE_APISHIM_TOKEN:-}"
read_token="${AE_APISHIM_READ_TOKEN:-}"

if [[ -z "$token" ]]; then
  token="$(read_env_var "AE_APISHIM_TOKEN" "$ENV_OVERRIDE_FILE" || true)"
fi
if [[ -z "$read_token" ]]; then
  read_token="$(read_env_var "AE_APISHIM_READ_TOKEN" "$ENV_OVERRIDE_FILE" || true)"
fi

if ! require_strong "AE_APISHIM_TOKEN" "$token"; then
  token="$(gen_token)"
fi
if ! require_strong "AE_APISHIM_READ_TOKEN" "$read_token"; then
  read_token="$(gen_token)"
fi

mkdir -p "$(dirname "$ENV_FILE")"
umask 077
cat > "$ENV_FILE" <<EOF
AE_APISHIM_TOKEN=${token}
AE_APISHIM_READ_TOKEN=${read_token}
AE_LABS_HELM_TOKEN=${token}
EOF
chmod 600 "$ENV_FILE"
log "Wrote $ENV_FILE (tokens generated or sourced securely)."

CERT_FILE="${APISHIM_CERT_FILE:-$ROOT_DIR/state/labs/apishim.crt}"
KEY_FILE="${APISHIM_KEY_FILE:-$ROOT_DIR/state/labs/apishim.key}"
if [[ ! -s "$CERT_FILE" || ! -s "$KEY_FILE" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    mkdir -p "$(dirname "$CERT_FILE")"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3 -nodes \
      -keyout "$KEY_FILE" -out "$CERT_FILE" -subj "/CN=apishim" >/dev/null 2>&1
    chmod 600 "$KEY_FILE" "$CERT_FILE"
    log "Wrote $CERT_FILE and $KEY_FILE (self-signed, dev only)."
  else
    log "openssl not found; skipping apishim TLS cert generation."
  fi
fi
