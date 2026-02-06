#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${APISHIM_ENV_FILE:-$ROOT_DIR/state/profiles/labs/apishim.env}"
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
session_secret="${AE_APISHIM_SESSION_SECRET:-}"
admin_token="${AE_API_ADMIN_TOKEN:-}"
labs_token="${AE_LABS_TOKEN:-}"

if [[ -z "$token" ]]; then
  token="$(read_env_var "AE_APISHIM_TOKEN" "$ENV_OVERRIDE_FILE" || true)"
fi
if [[ -z "$read_token" ]]; then
  read_token="$(read_env_var "AE_APISHIM_READ_TOKEN" "$ENV_OVERRIDE_FILE" || true)"
fi
if [[ -z "$session_secret" ]]; then
  session_secret="$(read_env_var "AE_APISHIM_SESSION_SECRET" "$ENV_OVERRIDE_FILE" || true)"
fi
if [[ -z "$admin_token" ]]; then
  admin_token="$(read_env_var "AE_API_ADMIN_TOKEN" "$ENV_OVERRIDE_FILE" || true)"
fi
if [[ -z "$labs_token" ]]; then
  labs_token="$(read_env_var "AE_LABS_TOKEN" "$ENV_OVERRIDE_FILE" || true)"
fi
if [[ -z "$token" ]]; then
  token="$(read_env_var "AE_APISHIM_TOKEN" "$ENV_FILE" || true)"
fi
if [[ -z "$read_token" ]]; then
  read_token="$(read_env_var "AE_APISHIM_READ_TOKEN" "$ENV_FILE" || true)"
fi
if [[ -z "$session_secret" ]]; then
  session_secret="$(read_env_var "AE_APISHIM_SESSION_SECRET" "$ENV_FILE" || true)"
fi
if [[ -z "$admin_token" ]]; then
  admin_token="$(read_env_var "AE_API_ADMIN_TOKEN" "$ENV_FILE" || true)"
fi
if [[ -z "$labs_token" ]]; then
  labs_token="$(read_env_var "AE_LABS_TOKEN" "$ENV_FILE" || true)"
fi

if ! require_strong "AE_APISHIM_TOKEN" "$token"; then
  token="$(gen_token)"
fi
if ! require_strong "AE_APISHIM_READ_TOKEN" "$read_token"; then
  read_token="$(gen_token)"
fi
if ! require_strong "AE_APISHIM_SESSION_SECRET" "$session_secret" 32; then
  session_secret="$(gen_token)"
fi
if ! require_strong "AE_API_ADMIN_TOKEN" "$admin_token"; then
  admin_token="$(gen_token)"
fi
if [[ -z "$labs_token" ]]; then
  labs_token="$token"
fi

mkdir -p "$(dirname "$ENV_FILE")"
umask 077
cat > "$ENV_FILE" <<EOF
AE_APISHIM_TOKEN=${token}
AE_APISHIM_READ_TOKEN=${read_token}
AE_APISHIM_SESSION_SECRET=${session_secret}
AE_API_ADMIN_TOKEN=${admin_token}
AE_LABS_TOKEN=${labs_token}
AE_LABS_HELM_TOKEN=${token}
EOF
chmod 600 "$ENV_FILE"
log "Wrote $ENV_FILE (tokens generated or sourced securely)."

CERT_FILE="${APISHIM_CERT_FILE:-$ROOT_DIR/state/profiles/labs/apishim.crt}"
KEY_FILE="${APISHIM_KEY_FILE:-$ROOT_DIR/state/profiles/labs/apishim.key}"
if command -v openssl >/dev/null 2>&1; then
  need_regen=0
  if [[ -s "$CERT_FILE" && -s "$KEY_FILE" ]]; then
    cert_text="$(openssl x509 -in "$CERT_FILE" -noout -text 2>/dev/null || true)"
    if ! grep -q "Subject Alternative Name" <<<"$cert_text"; then
      need_regen=1
    elif ! grep -q "DNS:localhost" <<<"$cert_text"; then
      need_regen=1
    elif ! grep -q "IP Address:127.0.0.1" <<<"$cert_text"; then
      need_regen=1
    fi
  fi
  if [[ ! -s "$CERT_FILE" || ! -s "$KEY_FILE" || $need_regen -eq 1 ]]; then
    mkdir -p "$(dirname "$CERT_FILE")"
    subj="/CN=apishim"
    san="${APISHIM_CERT_SANS:-DNS:apishim,DNS:localhost,IP:127.0.0.1,IP:::1}"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3 -nodes \
      -keyout "$KEY_FILE" -out "$CERT_FILE" -subj "$subj" -addext "subjectAltName=${san}" >/dev/null 2>&1
    chmod 600 "$KEY_FILE" "$CERT_FILE"
    log "Wrote $CERT_FILE and $KEY_FILE (self-signed, dev only)."
  fi
else
  log "openssl not found; skipping apishim TLS cert generation."
fi
