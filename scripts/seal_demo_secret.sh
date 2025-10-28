#!/usr/bin/env bash
set -euo pipefail

# Seal the sample demo secret with SOPS/age for local testing.
#
# - Picks the first recipient from ~/.config/ae/keys.txt or AE_AGE_RECIPIENT.
# - Reads token from AE_DEMO_SECRET_TOKEN or defaults to dev-token-123.
# - Produces specs/examples/demo-secret.sops.yaml (overwrites in place).
#
# Usage:
#   scripts/seal_demo_secret.sh            # uses defaults
#   AE_DEMO_SECRET_TOKEN=abc123 scripts/seal_demo_secret.sh
#   AE_AGE_RECIPIENT=age1... scripts/seal_demo_secret.sh

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
out="$root/specs/examples/demo-secret.sops.yaml"

recipient="${AE_AGE_RECIPIENT:-}"
if [[ -z "$recipient" && -f "$HOME/.config/ae/keys.txt" ]]; then
  # If the file contains a private identity (AGE-SECRET-KEY-1...), derive the recipient.
  if grep -q '^AGE-SECRET-KEY-1' "$HOME/.config/ae/keys.txt"; then
    if command -v age-keygen >/dev/null 2>&1; then
      recipient=$(age-keygen -y "$HOME/.config/ae/keys.txt" | tr -d '\r\n')
    else
      echo "error: age-keygen not found to derive recipient from identity file. Install 'age'." >&2
      exit 2
    fi
  else
    # Assume file already holds a public recipient (age1...)
    recipient=$(head -n1 "$HOME/.config/ae/keys.txt" | tr -d '\r\n')
  fi
fi
if [[ -z "$recipient" ]]; then
  echo "error: no age recipient configured." >&2
  echo "- Set AE_AGE_RECIPIENT=age1... or place a key in ~/.config/ae/keys.txt (first line)." >&2
  exit 2
fi

token="${AE_DEMO_SECRET_TOKEN:-dev-token-123}"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
printf 'token: %s\n' "$token" > "$tmp"

if ! command -v sops >/dev/null 2>&1; then
  echo "error: sops is not installed. Please install sops first." >&2
  exit 2
fi

echo "[seal] recipient: $recipient" >&2
echo "[seal] writing sealed secret → $out" >&2
cp "$tmp" "$out"
sops --encrypt --age "$recipient" -i "$out"

# Quick verify (non-fatal if recipient is remote-only and no key available locally)
if sops --decrypt "$out" >/dev/null 2>&1; then
  echo "[seal] verify ok: sops --decrypt works locally" >&2
else
  echo "[seal] note: decrypt verify skipped/failed (no private key?). File is sealed with metadata." >&2
fi

echo "$out"
