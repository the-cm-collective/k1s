#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
NSC_BIN="${NSC_BIN:-}"

if [ -z "$NSC_BIN" ] && [ -x "$ROOT_DIR/ops/dev/bin/nsc" ]; then
  NSC_BIN="$ROOT_DIR/ops/dev/bin/nsc"
fi
if [ -z "$NSC_BIN" ]; then
  NSC_BIN="$(command -v nsc || true)"
fi

STATE_DIR="${STATE_DIR:-$ROOT_DIR/.local/nats-jwt}"
NSC_DIR="${NSC_DIR:-$STATE_DIR/nsc}"
CREDS_DIR="$STATE_DIR/creds"
ACCOUNTS_DIR="$STATE_DIR/accounts"
HUB_CONF="$STATE_DIR/nats-hub.conf"
EDGE_CONF="$STATE_DIR/nats-edge.conf"

OPERATOR="${OPERATOR:-K1S_DEV}"
ACCOUNT="${ACCOUNT:-K1S}"
SYS_ACCOUNT="${SYS_ACCOUNT:-SYS}"
SITE_ID="${SITE_ID:-sfo-edge-01}"
NODE_ID="${NODE_ID:-worker-1}"
RESET="${RESET:-1}"

if [ -z "$NSC_BIN" ] || [ ! -x "$NSC_BIN" ]; then
  echo "nsc not found; run ops/dev/install-nsc.sh or set NSC_BIN." >&2
  exit 1
fi

NSC_HELP="$("$NSC_BIN" --help 2>/dev/null || true)"
DIR_FLAG=""
if echo "$NSC_HELP" | grep -q -- "--all-dirs"; then
  DIR_FLAG="--all-dirs"
elif echo "$NSC_HELP" | grep -q -- "--dir"; then
  DIR_FLAG="--dir"
fi
DIR_ARGS=()
if [ -n "$DIR_FLAG" ]; then
  DIR_ARGS=("$DIR_FLAG" "$NSC_DIR")
fi
_account_pub() {
  local name="$1"
  local out=""
  out="$("$NSC_BIN" describe account --name "$name" --field sub "${DIR_ARGS[@]}" 2>/dev/null || true)"
  out="${out//\"/}"
  if [[ "$out" == A* ]]; then
    echo "$out"
    return 0
  fi
  out="$("$NSC_BIN" describe account --name "$name" "${DIR_ARGS[@]}" 2>/dev/null | awk -F': ' '/Public Key/ {print $2; exit}' || true)"
  echo "$out"
}

if [ "$RESET" = "1" ]; then
  rm -rf "$STATE_DIR"
fi
mkdir -p "$STATE_DIR" "$NSC_DIR" "$CREDS_DIR" "$ACCOUNTS_DIR"

PUB_FLAG="--pub"
SUB_FLAG="--sub"
if ! nsc add user --help 2>/dev/null | grep -q -- "--pub"; then
  PUB_FLAG="--allow-pub"
  SUB_FLAG="--allow-sub"
fi

echo "Initializing NATS operator/account store in $NSC_DIR"
"$NSC_BIN" add operator --name "$OPERATOR" "${DIR_ARGS[@]}"
"$NSC_BIN" env --operator "$OPERATOR" --store "$NSC_DIR" "${DIR_ARGS[@]}"
"$NSC_BIN" add account --name "$SYS_ACCOUNT" "${DIR_ARGS[@]}"
"$NSC_BIN" add account --name "$ACCOUNT" "${DIR_ARGS[@]}"
"$NSC_BIN" edit account --name "$ACCOUNT" --js-enable 1 "${DIR_ARGS[@]}"
"$NSC_BIN" edit account --name "$ACCOUNT" --js-tier 1 --js-mem-storage 256M --js-disk-storage 2G "${DIR_ARGS[@]}"
"$NSC_BIN" edit operator --system-account "$SYS_ACCOUNT" "${DIR_ARGS[@]}"

echo "Creating users (permissions are dev-focused; tighten for production)."
"$NSC_BIN" add user --name sys --account "$SYS_ACCOUNT" "${DIR_ARGS[@]}" \
  "$PUB_FLAG" ">" "$SUB_FLAG" ">"

"$NSC_BIN" add user --name hub-controller --account "$ACCOUNT" "${DIR_ARGS[@]}" \
  "$PUB_FLAG" "k1s.v1.work.site.>" \
  "$PUB_FLAG" "\$JS.API.STREAM.>" \
  "$PUB_FLAG" "\$JS.API.CONSUMER.>" \
  "$PUB_FLAG" "\$JS.K1S.API.>" \
  "$PUB_FLAG" "_INBOX.>" \
  "$SUB_FLAG" "k1s.v1.site.>" \
  "$SUB_FLAG" "_INBOX.>"

"$NSC_BIN" add user --name "site-${SITE_ID}-uplink" --account "$ACCOUNT" "${DIR_ARGS[@]}" \
  "$PUB_FLAG" "k1s.v1.site.${SITE_ID}.>" \
  "$PUB_FLAG" "\$JS.API.CONSUMER.MSG.NEXT.K1S_WORK.WORK_SITE_${SITE_ID}" \
  "$PUB_FLAG" "\$JS.K1S.API.CONSUMER.MSG.NEXT.K1S_WORK.WORK_SITE_${SITE_ID}" \
  "$PUB_FLAG" "\$JS.ACK.K1S_WORK.>" \
  "$SUB_FLAG" "_INBOX.>" \
  "$SUB_FLAG" "k1s.v1.site.${SITE_ID}.routes.bundle"

"$NSC_BIN" add user --name gateway --account "$ACCOUNT" "${DIR_ARGS[@]}" \
  "$PUB_FLAG" "k1s.v1.local.work.>" \
  "$PUB_FLAG" "k1s.v1.site.${SITE_ID}.>" \
  "$PUB_FLAG" "\$JS.API.CONSUMER.MSG.NEXT.K1S_WORK.WORK_SITE_${SITE_ID}" \
  "$PUB_FLAG" "\$JS.K1S.API.CONSUMER.MSG.NEXT.K1S_WORK.WORK_SITE_${SITE_ID}" \
  "$PUB_FLAG" "\$JS.ACK.K1S_WORK.>" \
  "$SUB_FLAG" "k1s.v1.local.result" \
  "$SUB_FLAG" "k1s.v1.local.work.progress" \
  "$SUB_FLAG" "k1s.v1.local.status.>" \
  "$SUB_FLAG" "k1s.v1.local.logs.>" \
  "$SUB_FLAG" "k1s.v1.site.${SITE_ID}.routes.bundle" \
  "$SUB_FLAG" "_INBOX.>"

"$NSC_BIN" add user --name worker --account "$ACCOUNT" "${DIR_ARGS[@]}" \
  "$PUB_FLAG" "k1s.v1.local.result" \
  "$PUB_FLAG" "k1s.v1.local.work.progress" \
  "$PUB_FLAG" "k1s.v1.local.status.${NODE_ID}" \
  "$PUB_FLAG" "k1s.v1.local.logs.${NODE_ID}" \
  "$PUB_FLAG" "k1s.v1.local.node.announce.${NODE_ID}" \
  "$SUB_FLAG" "k1s.v1.local.work.${NODE_ID}"

echo "Exporting user creds."
"$NSC_BIN" generate creds --account "$SYS_ACCOUNT" --name sys "${DIR_ARGS[@]}" > "$CREDS_DIR/sys.creds"
"$NSC_BIN" generate creds --account "$ACCOUNT" --name hub-controller "${DIR_ARGS[@]}" > "$CREDS_DIR/hub-controller.creds"
"$NSC_BIN" generate creds --account "$ACCOUNT" --name "site-${SITE_ID}-uplink" "${DIR_ARGS[@]}" > "$CREDS_DIR/site-${SITE_ID}-uplink.creds"
"$NSC_BIN" generate creds --account "$ACCOUNT" --name gateway "${DIR_ARGS[@]}" > "$CREDS_DIR/gateway.creds"
"$NSC_BIN" generate creds --account "$ACCOUNT" --name worker "${DIR_ARGS[@]}" > "$CREDS_DIR/worker.creds"

echo "Copying operator/account JWTs into resolver directory."
OP_JWT_SRC="$(find "$NSC_DIR" -name "${OPERATOR}.jwt" -o -name operator.jwt | head -n1 || true)"
if [ -z "$OP_JWT_SRC" ]; then
  echo "operator.jwt not found in $NSC_DIR" >&2
  exit 1
fi
cp "$OP_JWT_SRC" "$STATE_DIR/operator.jwt"

SYS_PUB="$(_account_pub "$SYS_ACCOUNT")"
ACC_PUB="$(_account_pub "$ACCOUNT")"

SYS_JWT_SRC="$(find "$NSC_DIR" -name "${SYS_ACCOUNT}.jwt" | head -n1 || true)"
ACC_JWT_SRC="$(find "$NSC_DIR" -name "${ACCOUNT}.jwt" | head -n1 || true)"

if [ -n "$SYS_JWT_SRC" ]; then
  if [ -n "$SYS_PUB" ]; then
    cp "$SYS_JWT_SRC" "$ACCOUNTS_DIR/${SYS_PUB}.jwt"
  else
    cp "$SYS_JWT_SRC" "$ACCOUNTS_DIR/${SYS_ACCOUNT}.jwt"
  fi
fi
if [ -n "$ACC_JWT_SRC" ]; then
  if [ -n "$ACC_PUB" ]; then
    cp "$ACC_JWT_SRC" "$ACCOUNTS_DIR/${ACC_PUB}.jwt"
  else
    cp "$ACC_JWT_SRC" "$ACCOUNTS_DIR/${ACCOUNT}.jwt"
  fi
fi

SYS_LINE=""
if [ -n "$SYS_PUB" ]; then
  SYS_LINE="system_account: \"$SYS_PUB\""
fi

cat > "$HUB_CONF" <<EOF
# Hub NATS server (JetStream enabled, JWT/operator auth).
server_name: "hub"
port: 4222
http: 8222

jetstream {
  store_dir: /data/jetstream
  domain: "K1S"
  max_mem_store: 256MB
  max_file_store: 2GB
}

operator: /etc/nats/jwt/operator.jwt
resolver {
  type: full
  dir: /etc/nats/jwt/accounts
}
$SYS_LINE

leafnodes {
  port: 7422
}
EOF

cat > "$EDGE_CONF" <<EOF
# Edge Leader NATS server (JWT/operator auth).
server_name: "edge-${SITE_ID}"
port: 4223
http: 8223

operator: /etc/nats/jwt/operator.jwt
resolver {
  type: full
  dir: /etc/nats/jwt/accounts
}

leafnodes {
  remotes = [
    {
      url: "nats://nats-hub:7422"
      credentials: "/etc/nats/creds/site-${SITE_ID}-uplink.creds"
      account: "${ACC_PUB}"
    }
  ]
}
EOF

echo "JWT artifacts written to $STATE_DIR"
echo "  operator jwt: $STATE_DIR/operator.jwt"
echo "  account jwt dir: $ACCOUNTS_DIR"
echo "  creds dir: $CREDS_DIR"
echo "  hub config: $HUB_CONF"
echo "  edge config: $EDGE_CONF"
