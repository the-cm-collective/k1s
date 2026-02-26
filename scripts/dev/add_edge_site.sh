#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_ID="${SITE_ID:-${1:-}}"
EDGE_PORT="${EDGE_PORT:-4224}"
EDGE_HTTP_PORT="${EDGE_HTTP_PORT:-8224}"
REGISTER_ONLY="${REGISTER_ONLY:-0}"
HUB_PROFILE="${HUB_PROFILE:-k1s-core}"
ENGINE_BIN="${ENGINE_BIN:-}"
RUNTIME_BACKEND_EXPLICIT=1
if [[ -z "${AE_RUNTIME_BACKEND:-}" ]]; then
  RUNTIME_BACKEND_EXPLICIT=0
  export AE_RUNTIME_BACKEND=podman
fi
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    PYTHON_BIN="python"
  fi
fi

if [[ -z "$SITE_ID" ]]; then
  cat >&2 <<USAGE
usage: SITE_ID=<site-id> [EDGE_PORT=4224 EDGE_HTTP_PORT=8224] [REGISTER_ONLY=0|1] $0
USAGE
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

resolve_infra_backend() {
  if [[ -n "${AE_INFRA_BACKEND:-}" ]]; then
    printf '%s' "${AE_INFRA_BACKEND}"
    return 0
  fi
  case "${AE_RUNTIME_BACKEND:-}" in
    cri|containerd) printf 'cri'; return 0 ;;
  esac
  if detect_running_core_cri; then
    printf 'cri'
    return 0
  fi
  printf 'compose'
  return 0
}

detect_running_core_cri() {
  local endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
  local crictl_bin="${CRICTL_BIN:-crictl}"
  local pods_json
  if ! command -v "$crictl_bin" >/dev/null 2>&1; then
    return 1
  fi
  if ! pods_json="$("$crictl_bin" --runtime-endpoint "$endpoint" pods -o json 2>/dev/null)"; then
    return 1
  fi
  PODS_JSON="$pods_json" "$PYTHON_BIN" - <<'PY'
import json
import os

try:
    payload = json.loads(os.environ.get("PODS_JSON", "") or "{}")
except Exception:
    raise SystemExit(1)

for item in payload.get("items") or []:
    if not isinstance(item, dict):
        continue
    labels = item.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    labels = {str(k): str(v) for k, v in labels.items()}
    meta = item.get("metadata") or {}
    if isinstance(meta, dict):
        meta_labels = meta.get("labels") or {}
        if isinstance(meta_labels, dict):
            labels.update({str(k): str(v) for k, v in meta_labels.items()})
        name = str(meta.get("name") or "")
        namespace = str(meta.get("namespace") or "")
        if name.startswith("k1s-core-") and namespace in {"", "k1s-dev"}:
            raise SystemExit(0)
    if labels.get("ae.stack.profile") != "k1s-core":
        continue
    if labels.get("ae.stack.backend") == "cri":
        raise SystemExit(0)
raise SystemExit(1)
PY
}

run_cri_stack() {
  PYTHONPATH=src "$PYTHON_BIN" "$ROOT_DIR/scripts/dev/cri_stack.py" "$@"
}

resolve_hub_leaf_host() {
  if [[ -n "${AE_NATS_HUB_LEAF_HOST:-}" ]]; then
    printf '%s' "${AE_NATS_HUB_LEAF_HOST}"
    return 0
  fi
  if [[ -n "${AE_CONTROLLER_URL:-}" ]]; then
    local controller_host
    controller_host="$($PYTHON_BIN - <<'PY' "${AE_CONTROLLER_URL}"
from urllib.parse import urlparse
import sys

url = (sys.argv[1] or "").strip()
try:
    parsed = urlparse(url)
except Exception:
    print("")
    raise SystemExit(0)
print(parsed.hostname or "")
PY
    )"
    if [[ -n "$controller_host" ]]; then
      printf '%s' "$controller_host"
      return 0
    fi
  fi
  printf '127.0.0.1'
}

resolve_hub_leaf_port() {
  if [[ -n "${AE_NATS_HUB_LEAF_PORT:-}" ]]; then
    printf '%s' "${AE_NATS_HUB_LEAF_PORT}"
    return 0
  fi
  printf '7422'
}

ENGINE_BIN="${ENGINE_BIN:-$(detect_engine)}"
INFRA_BACKEND="$(resolve_infra_backend)"
if [[ "$INFRA_BACKEND" == "cri" ]]; then
  if [[ "$RUNTIME_BACKEND_EXPLICIT" -eq 0 ]]; then
    export AE_RUNTIME_BACKEND="cri"
  fi
  export AE_CRI_RUNTIME_HANDLER="${AE_CRI_RUNTIME_HANDLER:-runc}"
  export AE_CRI_IMAGE_POLICY="${AE_CRI_IMAGE_POLICY:-pull}"
  export AE_CRI_SET_HOSTNAME="${AE_CRI_SET_HOSTNAME:-0}"
fi

HUB_CONF_TEMPLATE="$ROOT_DIR/ops/dev/nats-hub.conf"
HUB_GENERATED_DIR="${AE_HUB_CONFIG_DIR:-$ROOT_DIR/state/profiles/${HUB_PROFILE}/generated}"
HUB_SITE_REGISTRY="$HUB_GENERATED_DIR/nats-sites.txt"
HUB_CONF_GENERATED="$HUB_GENERATED_DIR/nats-hub.generated.conf"
EDGE_TEMPLATE="$ROOT_DIR/ops/dev/nats-edge.conf"
EDGE_CONF="$ROOT_DIR/ops/dev/nats-edge-${SITE_ID}.conf"
HUB_LEAF_HOST="$(resolve_hub_leaf_host)"
HUB_LEAF_PORT="$(resolve_hub_leaf_port)"

mkdir -p "$HUB_GENERATED_DIR"

if [[ ! -f "$EDGE_TEMPLATE" ]]; then
  echo "missing template: $EDGE_TEMPLATE" >&2
  exit 1
fi
if [[ ! -f "$HUB_CONF_TEMPLATE" ]]; then
  echo "missing hub template: $HUB_CONF_TEMPLATE" >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY' "$EDGE_TEMPLATE" "$EDGE_CONF" "$SITE_ID" "$EDGE_PORT" "$EDGE_HTTP_PORT" "$INFRA_BACKEND" "$HUB_LEAF_HOST" "$HUB_LEAF_PORT"
from pathlib import Path
import re
import sys

tmpl, out, site_id, edge_port, edge_http_port, infra_backend, hub_host, hub_port = sys.argv[1:9]
text = Path(tmpl).read_text(encoding="utf-8")
if "sfo-edge-01" not in text:
    raise SystemExit("template missing sfo-edge-01 marker")
text = text.replace("sfo-edge-01", site_id)
text = text.replace("edge-sfo-01", f"edge-{site_id}")
if infra_backend == "cri":
    text = re.sub(r'@nats-hub:\d+"', f'@{hub_host}:{hub_port}"', text)
    text = re.sub(r"(?m)^port:\s*\d+\s*$", f"port: {edge_port}", text)
    text = re.sub(r"(?m)^http:\s*\d+\s*$", f"http: {edge_http_port}", text)
Path(out).write_text(text, encoding="utf-8")
print(f"[edge-site] wrote {out}")
PY

hub_updated="$($PYTHON_BIN - <<'PY' "$HUB_CONF_TEMPLATE" "$HUB_CONF_GENERATED" "$HUB_SITE_REGISTRY" "$SITE_ID"
from pathlib import Path
import hashlib
import sys

def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()

def user_block(site_id: str) -> str:
    return f'''
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

def load_sites(path: Path) -> list[str]:
    if not path.exists():
        return []
    items: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        site = raw.strip()
        if not site or site.startswith("#"):
            continue
        items.append(site)
    return sorted(set(items))

template_path, out_path, registry_path, site_id = [Path(arg) if i < 3 else arg for i, arg in enumerate(sys.argv[1:5])]
text = template_path.read_text(encoding="utf-8")
marker = "# --- site uplink users (managed by scripts/dev/add_edge_site.sh)"
if marker not in text:
    raise SystemExit("hub config missing marker; rebase ops/dev/nats-hub.conf")

sites = load_sites(registry_path)
sites.append(site_id)
sites = sorted(set(sites))

rendered = text
for site in sites:
    needle = f'user: "site-{site}-uplink"'
    if needle in rendered:
        continue
    rendered = rendered.replace(marker, marker + user_block(site), 1)

prev_hash = file_hash(out_path)
out_path.write_text(rendered, encoding="utf-8")
registry_path.write_text("\n".join(sites) + "\n", encoding="utf-8")
next_hash = file_hash(out_path)
print("updated" if prev_hash != next_hash else "noop")
PY
)"

if [[ "$hub_updated" == "updated" ]]; then
  echo "[edge-site] rendered ${HUB_CONF_GENERATED}"
else
  echo "[edge-site] hub runtime config unchanged: ${HUB_CONF_GENERATED}"
fi

if [[ "$INFRA_BACKEND" == "cri" ]]; then
  CRI_EDGE_PROFILE="${EDGE_PROFILE:-k1s-core}"
  if [[ "$hub_updated" == "updated" ]]; then
    run_cri_stack up-nats-hub --profile "$HUB_PROFILE" --config "$HUB_CONF_GENERATED" --recreate
  else
    run_cri_stack up-nats-hub --profile "$HUB_PROFILE" --config "$HUB_CONF_GENERATED"
  fi

  if [[ "$REGISTER_ONLY" == "1" ]]; then
    EDGE_CONTAINER="(register-only)"
  else
    run_cri_stack up-edge-nats --profile "$CRI_EDGE_PROFILE" --site-id "$SITE_ID" --config "$EDGE_CONF" --recreate
    EDGE_CONTAINER="k1s-edge-nats-${SITE_ID}"
  fi
else
  if ! "$ENGINE_BIN" ps --format '{{.Names}}' 2>/dev/null | grep -q '^dev-nats-hub-1$'; then
    "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml" up -d nats-hub >/dev/null 2>&1 || true
  fi

  "$ENGINE_BIN" cp "$HUB_CONF_GENERATED" dev-nats-hub-1:/etc/nats/nats-hub.conf >/dev/null
  if ! "$ENGINE_BIN" exec -T dev-nats-hub-1 nats-server --signal reload >/dev/null 2>&1; then
    "$ENGINE_BIN" restart dev-nats-hub-1 >/dev/null 2>&1 || true
  fi

  if [[ "$REGISTER_ONLY" == "1" ]]; then
    EDGE_CONTAINER="(register-only)"
  else
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
  fi
fi

if [[ "$REGISTER_ONLY" == "1" ]]; then
  cat <<EOF
[edge-site] registered site credentials for ${SITE_ID}
[edge-site] hub config: ${HUB_CONF_GENERATED}
EOF
elif [[ "$INFRA_BACKEND" == "cri" ]]; then
  EDGE_TARGET_HINT="k1s-edge-core-cri"
  if [[ "${EDGE_PROFILE:-k1s-core}" != "k1s-core" ]]; then
    EDGE_TARGET_HINT="k1s-edge-cri"
  fi
  cat <<EOF
[edge-site] started ${EDGE_CONTAINER}
[edge-site] use:
  AE_SITE_ID=${SITE_ID} AE_NODE_ID=edge-node-1 \\
  AE_NATS_URL=nats://gateway:dev@127.0.0.1:${EDGE_PORT} make ${EDGE_TARGET_HINT}
EOF
else
  cat <<EOF
[edge-site] started ${EDGE_CONTAINER}
[edge-site] use:
  AE_SITE_ID=${SITE_ID} AE_NODE_ID=edge-node-1 \\
  AE_NATS_URL=nats://gateway:dev@127.0.0.1:${EDGE_PORT} make k1s-edge
EOF
fi
