#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_ID="${SITE_ID:-${1:-}}"
EDGE_PORT="${EDGE_PORT:-4224}"
EDGE_HTTP_PORT="${EDGE_HTTP_PORT:-8224}"
ENGINE_BIN="${ENGINE_BIN:-}"

if [[ -z "$SITE_ID" ]]; then
  echo "usage: SITE_ID=<site-id> [EDGE_PORT=4224 EDGE_HTTP_PORT=8224] $0" >&2
  exit 1
fi

detect_engine() {
  if [[ -n "${AE_CONTAINER_CLI:-}" ]]; then
    printf '%s' "${AE_CONTAINER_CLI}"
    return 0
  fi
  if [[ -n "${STACK_BIN:-}" ]]; then
    printf '%s' "${STACK_BIN}"
    return 0
  fi
  case "${AE_RUNTIME_BACKEND:-}" in
    docker) printf 'docker'; return 0 ;;
    podman|oci) printf 'podman'; return 0 ;;
  esac
  if command -v podman >/dev/null 2>&1; then
    printf 'podman'
    return 0
  fi
  printf 'docker'
}

ENGINE_BIN="${ENGINE_BIN:-$(detect_engine)}"
HUB_CONF="$ROOT_DIR/ops/dev/nats-hub.conf"
EDGE_TEMPLATE="$ROOT_DIR/ops/dev/nats-edge.conf"
EDGE_CONF="$ROOT_DIR/ops/dev/nats-edge-${SITE_ID}.conf"

if [[ ! -f "$EDGE_TEMPLATE" ]]; then
  echo "missing template: $EDGE_TEMPLATE" >&2
  exit 1
fi

python - <<'PY' "$EDGE_TEMPLATE" "$EDGE_CONF" "$SITE_ID"
from pathlib import Path
import sys

tmpl, out, site_id = sys.argv[1:4]
text = Path(tmpl).read_text(encoding="utf-8")
if "sfo-edge-01" not in text:
    raise SystemExit("template missing sfo-edge-01 marker")
text = text.replace("sfo-edge-01", site_id)
text = text.replace("edge-sfo-01", f"edge-{site_id}")
Path(out).write_text(text, encoding="utf-8")
print(f"[edge-site] wrote {out}")
PY

hub_updated="$(
python - <<'PY' "$HUB_CONF" "$SITE_ID"
from pathlib import Path
import sys

path = Path(sys.argv[1])
site_id = sys.argv[2]
text = path.read_text(encoding="utf-8")
marker = "# --- site uplink users (managed by scripts/dev/add_edge_site.sh)"
user_block = f'''
      {{
        user: "site-{site_id}-uplink"
        password: "dev"
        permissions: {{
          publish: [
            "k1s.v1.site.{site_id}.>",
            "$JS.API.>",
            "$JS.K1S.API.>",
            "$JS.API.CONSUMER.MSG.NEXT.K1S_WORK.WORK_SITE_{site_id}",
            "$JS.ACK.K1S_WORK.>"
          ]
          subscribe: ["_INBOX.>", "k1s.v1.site.{site_id}.routes.bundle"]
        }}
      }}
'''
if f'user: "site-{site_id}-uplink"' in text:
    print("noop")
    raise SystemExit(0)
if marker not in text:
    raise SystemExit("hub config missing marker; rebase ops/dev/nats-hub.conf")
parts = text.split(marker)
if len(parts) != 2:
    raise SystemExit("unexpected hub config marker layout")
text = parts[0] + marker + user_block + parts[1]
path.write_text(text, encoding="utf-8")
print("updated")
PY
)"
if [[ "$hub_updated" == "updated" ]]; then
  echo "[edge-site] updated ${HUB_CONF}"
else
  echo "[edge-site] hub already configured for site"
fi

# Ensure hub is up and reload to pick up new user.
if "$ENGINE_BIN" ps --format '{{.Names}}' 2>/dev/null | grep -q '^dev-nats-hub-1$'; then
  if [[ "$hub_updated" == "updated" ]]; then
    if ! "$ENGINE_BIN" exec -T dev-nats-hub-1 nats-server --signal reload >/dev/null 2>&1; then
      "$ENGINE_BIN" restart dev-nats-hub-1 >/dev/null 2>&1 || true
    fi
  fi
else
  "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml" up -d nats-hub >/dev/null 2>&1 || true
fi

# Ensure compose network exists (dev_default) so edge can reach nats-hub by DNS.
if ! "$ENGINE_BIN" network inspect dev_default >/dev/null 2>&1; then
  "$ENGINE_BIN" network create dev_default >/dev/null 2>&1 || true
fi

EDGE_CONTAINER="dev-nats-edge-${SITE_ID}"
"$ENGINE_BIN" rm -f "$EDGE_CONTAINER" >/dev/null 2>&1 || true
"$ENGINE_BIN" run -d --name "$EDGE_CONTAINER" \
  --network dev_default \
  -p "${EDGE_PORT}:4223" \
  -p "${EDGE_HTTP_PORT}:8223" \
  -v "${EDGE_CONF}:/etc/nats/nats-edge.conf:ro" \
  docker.io/library/nats:2.10 \
  -c /etc/nats/nats-edge.conf >/dev/null

cat <<EOF
[edge-site] started ${EDGE_CONTAINER}
[edge-site] use:
  AE_SITE_ID=${SITE_ID} AE_NODE_ID=edge-node-1 \\
  AE_NATS_URL=nats://gateway:dev@127.0.0.1:${EDGE_PORT} make k1s-edge
EOF
