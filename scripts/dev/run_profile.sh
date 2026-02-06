#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROFILE="${1:-}"

if [[ -z "$PROFILE" ]]; then
  echo "usage: $0 <dev-min|dev-etcd|k1s-core|k1s-edge>" >&2
  exit 1
fi

detect_python() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf '%s' "$ROOT_DIR/.venv/bin/python"
  else
    printf '%s' "python"
  fi
}

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

normalize_ingress_mode() {
  case "${1:-}" in
    ""|core-proxy|core_proxy|core) printf 'core-proxy' ;;
    core-to-edge-public|core_to_edge_public|public) printf 'core-to-edge-public' ;;
    edge-local|edge_local|local) printf 'edge-local' ;;
    *) printf 'core-proxy' ;;
  esac
}

compose() {
  local engine="$1"; shift
  "$engine" compose "$@"
}

ensure_specs_dir() {
  local dir="$1"
  mkdir -p "$dir"
}

start_docs_server() {
  local docs_port="${AE_DOCS_PORT:-9109}"
  local docs_bind="${DOCS_BIND:-127.0.0.1}"
  local pid_file="$ROOT_DIR/state/docs_server.pid"
  local docs_dir="$ROOT_DIR/docs/site"
  local api_base="${DOCS_API_BASE:-http://127.0.0.1:${METRICS_PORT:-9108}}"
  local dash_url="${DOCS_DASHBOARD_URL:-http://127.0.0.1:${METRICS_PORT:-9108}/dashboard}"

  mkdir -p "$ROOT_DIR/state"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    rm -f "$pid_file" || true
  fi

  DOCS_API_BASE="$api_base" DOCS_DASHBOARD_URL="$dash_url" "$PYTHON_BIN" docs/build_docs.py >/dev/null 2>&1 || true
  nohup "$PYTHON_BIN" -m http.server "$docs_port" --bind "$docs_bind" --directory "$docs_dir" >/dev/null 2>&1 &
  echo $! > "$pid_file"
}

start_caddy() {
  local https_port="${CADDY_HTTPS_PORT:-8443}"
  local api_base="https://api.home.arpa:${https_port}"
  local dash_url="https://dash.home.arpa:${https_port}/dashboard"
  local docs_env="$ROOT_DIR/state/dev.env"
  local caddy_sites="$ROOT_DIR/state/caddy"

  mkdir -p "$caddy_sites"
  if [[ -x "$ROOT_DIR/scripts/ensure_dev_env.sh" ]]; then
    AE_CONTAINER_CLI="$ENGINE_BIN" "$ROOT_DIR/scripts/ensure_dev_env.sh" >/dev/null 2>&1 || true
  fi

  DOCS_API_BASE="$api_base" DOCS_DASHBOARD_URL="$dash_url" "$PYTHON_BIN" docs/build_docs.py >/dev/null 2>&1 || true

  local host_alias="host.docker.internal"
  if [[ "$ENGINE_BIN" == "podman" ]]; then
    host_alias="host.containers.internal"
  fi
  cat > "${caddy_sites}/dash.caddy" <<EOF
https://dash.home.arpa {
    log {
        output stdout
        format console
    }
    header -Strict-Transport-Security
    tls internal
    reverse_proxy ${host_alias}:${METRICS_PORT:-9108}
}
EOF

  "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.yaml" up -d caddy >/dev/null 2>&1 || true
  "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.yaml" exec -T caddy \
    caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
}

write_envoy_bootstrap() {
  local path="$1"
  PYTHONPATH=src "$PYTHON_BIN" - <<PY
from pathlib import Path
from ae.ingress.envoy_core_proxy import render_envoy_config, EnvoyRenderConfig

cfg_path = Path("${path}")
cfg_path.parent.mkdir(parents=True, exist_ok=True)
cfg_path.write_text(render_envoy_config([], [], EnvoyRenderConfig()), encoding="utf-8")
PY
}

write_rathole_server_bootstrap() {
  local path="$1"
  local bind_addr="$2"
  local token="$3"
  PYTHONPATH=src "$PYTHON_BIN" - <<PY
from pathlib import Path
from ae.ingress.rathole import write_rathole_server, RatholeServerConfig

cfg_path = Path("${path}")
write_rathole_server(
    cfg_path,
    RatholeServerConfig(bind_addr="${bind_addr}", default_token="${token}", services=[]),
)
PY
}

write_rathole_client_config() {
  local path="$1"
  local remote_addr="$2"
  local token="$3"
  local site_id="$4"
  local local_addr="$5"
  PYTHONPATH=src "$PYTHON_BIN" - <<PY
from pathlib import Path
from ae.ingress.rathole import write_rathole_client, RatholeClientConfig, RatholeClientService

cfg_path = Path("${path}")
write_rathole_client(
    cfg_path,
    RatholeClientConfig(
        remote_addr="${remote_addr}",
        default_token="${token}",
        services=[RatholeClientService(name="${site_id}", local_addr="${local_addr}")],
    ),
)
PY
}

start_envoy_container() {
  local name="$1"
  local config_path="$2"
  local image="${AE_ENVOY_IMAGE:-envoyproxy/envoy:v1.29-latest}"
  "$ENGINE_BIN" rm -f "$name" >/dev/null 2>&1 || true
  "$ENGINE_BIN" run -d --name "$name" --network host \
    -v "${config_path}:/etc/envoy/envoy.yaml:ro" \
    "$image" -c /etc/envoy/envoy.yaml --log-level info >/dev/null
}

start_rathole_server_container() {
  local name="$1"
  local config_path="$2"
  local image="${AE_RATHOLE_IMAGE:-ghcr.io/rapiz1/rathole:v0.5.0}"
  "$ENGINE_BIN" rm -f "$name" >/dev/null 2>&1 || true
  "$ENGINE_BIN" run -d --name "$name" --network host \
    -v "${config_path}:/etc/rathole/server.toml:ro" \
    "$image" --server /etc/rathole/server.toml >/dev/null
}

start_rathole_client_container() {
  local name="$1"
  local config_path="$2"
  local image="${AE_RATHOLE_IMAGE:-ghcr.io/rapiz1/rathole:v0.5.0}"
  "$ENGINE_BIN" rm -f "$name" >/dev/null 2>&1 || true
  "$ENGINE_BIN" run -d --name "$name" --network host \
    -v "${config_path}:/etc/rathole/client.toml:ro" \
    "$image" --client /etc/rathole/client.toml >/dev/null
}

PYTHON_BIN="$(detect_python)"
ENGINE_BIN="$(detect_engine)"

case "$PROFILE" in
  dev-min)
    PROFILE_DIR="${PROFILE_DIR:-$ROOT_DIR/state/profiles/dev-min}"
    SPECS_DIR="${SPECS_DIR:-$PROFILE_DIR/specs}"
    ensure_specs_dir "$SPECS_DIR"
    export AE_SPECS_DIR="$SPECS_DIR"
    export AE_PROJECTION_ROOT="${AE_PROJECTION_ROOT:-$PROFILE_DIR/projections}"
    export AE_STATE_DB="${AE_STATE_DB:-$PROFILE_DIR/controller.db}"
    export AE_STATE_BACKEND="${AE_STATE_BACKEND:-sqlite}"
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-http}"
    METRICS_PORT="${METRICS_PORT:-9108}"
    if [[ "${CORE_CADDY:-0}" == "1" ]]; then
      start_caddy
    fi
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.controller --loop --metrics-port "$METRICS_PORT" --watch
    ;;
  dev-etcd)
    COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml}"
    compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d etcd
    PROFILE_DIR="${PROFILE_DIR:-$ROOT_DIR/state/profiles/dev-etcd}"
    SPECS_DIR="${SPECS_DIR:-$PROFILE_DIR/specs}"
    ensure_specs_dir "$SPECS_DIR"
    export AE_SPECS_DIR="$SPECS_DIR"
    export AE_PROJECTION_ROOT="${AE_PROJECTION_ROOT:-$PROFILE_DIR/projections}"
    export AE_STATE_BACKEND="${AE_STATE_BACKEND:-etcd}"
    export AE_ETCD_ENDPOINTS="${AE_ETCD_ENDPOINTS:-http://127.0.0.1:2379}"
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-http}"
    METRICS_PORT="${METRICS_PORT:-9108}"
    if [[ "${CORE_CADDY:-0}" == "1" ]]; then
      start_caddy
    fi
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.controller --loop --metrics-port "$METRICS_PORT" --watch
    ;;
  k1s-core)
    COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml}"
    compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d etcd nats-hub
    PROFILE_DIR="${PROFILE_DIR:-$ROOT_DIR/state/profiles/k1s-core}"
    SPECS_DIR="${SPECS_DIR:-$PROFILE_DIR/specs}"
    ensure_specs_dir "$SPECS_DIR"
    export AE_SPECS_DIR="$SPECS_DIR"
    export AE_PROJECTION_ROOT="${AE_PROJECTION_ROOT:-$PROFILE_DIR/projections}"
    export AE_STATE_BACKEND="${AE_STATE_BACKEND:-etcd}"
    export AE_ETCD_ENDPOINTS="${AE_ETCD_ENDPOINTS:-http://127.0.0.1:2379}"
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-nats-js}"
    export AE_NATS_URL="${AE_NATS_URL:-nats://hub-controller:dev@127.0.0.1:4222}"
    export AE_JS_DOMAIN="${AE_JS_DOMAIN:-K1S}"
    INGRESS_MODE="$(normalize_ingress_mode "${EDGE_INGRESS_MODE:-${AE_EDGE_INGRESS_MODE:-core-proxy}}")"
    EDGE_INGRESS_START="${EDGE_INGRESS_START:-1}"
    EDGE_INGRESS_DIR="${EDGE_INGRESS_DIR:-$PROFILE_DIR/edge-ingress}"
    EDGE_ENVOY_CONFIG="${EDGE_ENVOY_CONFIG:-$EDGE_INGRESS_DIR/envoy.yaml}"
    EDGE_RATHOLE_SERVER="${EDGE_RATHOLE_SERVER:-$EDGE_INGRESS_DIR/rathole-server.toml}"
    export AE_EDGE_INGRESS_CONFIG_DIR="${AE_EDGE_INGRESS_CONFIG_DIR:-$EDGE_INGRESS_DIR}"
    export AE_EDGE_INGRESS_ENVOY_CONFIG="${AE_EDGE_INGRESS_ENVOY_CONFIG:-$EDGE_ENVOY_CONFIG}"
    export AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX="${AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX:-edge.local}"
    export AE_EDGE_INGRESS_LOCAL_ADDR="${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081}"
    export AE_RATHOLE_BIND_ADDR="${AE_RATHOLE_BIND_ADDR:-0.0.0.0:2333}"
    export AE_RATHOLE_DEFAULT_TOKEN="${AE_RATHOLE_DEFAULT_TOKEN:-dev}"
    export AE_RATHOLE_SERVER_ADDR="${AE_RATHOLE_SERVER_ADDR:-127.0.0.1:2333}"
    if [[ "$INGRESS_MODE" == "core-proxy" ]]; then
      export AE_EDGE_INGRESS_CORE_PROXY=1
      export AE_RATHOLE_CLIENT_DIR="${AE_RATHOLE_CLIENT_DIR:-$EDGE_INGRESS_DIR/clients}"
    else
      export AE_EDGE_INGRESS_CORE_PROXY=0
    fi
    if [[ "$EDGE_INGRESS_START" == "1" ]]; then
      write_envoy_bootstrap "$EDGE_ENVOY_CONFIG"
      write_rathole_server_bootstrap "$EDGE_RATHOLE_SERVER" "$AE_RATHOLE_BIND_ADDR" "$AE_RATHOLE_DEFAULT_TOKEN"
      ENVOY_CONTAINER="${ENVOY_CONTAINER:-k1s-core-envoy}"
      RATHOLE_SERVER_CONTAINER="${RATHOLE_SERVER_CONTAINER:-k1s-core-rathole}"
      if [[ "$INGRESS_MODE" == "core-proxy" ]]; then
        export AE_EDGE_INGRESS_RELOAD_CMD="${AE_EDGE_INGRESS_RELOAD_CMD:-$ENGINE_BIN restart $ENVOY_CONTAINER $RATHOLE_SERVER_CONTAINER}"
        start_envoy_container "$ENVOY_CONTAINER" "$EDGE_ENVOY_CONFIG"
        start_rathole_server_container "$RATHOLE_SERVER_CONTAINER" "$EDGE_RATHOLE_SERVER"
      elif [[ "$INGRESS_MODE" == "core-to-edge-public" ]]; then
        export AE_EDGE_INGRESS_RELOAD_CMD="${AE_EDGE_INGRESS_RELOAD_CMD:-$ENGINE_BIN restart $ENVOY_CONTAINER}"
        start_envoy_container "$ENVOY_CONTAINER" "$EDGE_ENVOY_CONFIG"
      fi
    fi
    METRICS_PORT="${METRICS_PORT:-9108}"
    if [[ "${CORE_CADDY:-0}" == "1" ]]; then
      start_caddy
    fi
    if [[ "${CORE_DOCS:-0}" == "1" ]]; then
      start_docs_server
    fi
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.controller --loop --metrics-port "$METRICS_PORT" --watch
    ;;
  k1s-edge)
    COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml}"
    compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d nats-edge
    EDGE_PROFILE="${EDGE_PROFILE:-k1s-edge}"
    PROFILE_DIR="${PROFILE_DIR:-$ROOT_DIR/state/profiles/$EDGE_PROFILE}"
    mkdir -p "$PROFILE_DIR"
    INGRESS_MODE="$(normalize_ingress_mode "${EDGE_INGRESS_MODE:-${AE_EDGE_INGRESS_MODE:-core-proxy}}")"
    EDGE_INGRESS_START="${EDGE_INGRESS_START:-1}"
    EDGE_INGRESS_DIR="${EDGE_INGRESS_DIR:-$PROFILE_DIR/edge-ingress}"
    if [[ "$EDGE_PROFILE" == "k1s-core" || "$EDGE_PROFILE" == "core" ]]; then
      DEFAULT_EDGE_BACKEND="nats-js"
    else
      DEFAULT_EDGE_BACKEND="nats-core"
    fi
    EDGE_TRANSPORT_BACKEND="${EDGE_TRANSPORT_BACKEND:-$DEFAULT_EDGE_BACKEND}"
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-$EDGE_TRANSPORT_BACKEND}"
    export AE_SITE_ID="${AE_SITE_ID:-sfo-edge-01}"
    export AE_NODE_ID="${AE_NODE_ID:-edge-node-1}"
    EDGE_RATHOLE_CLIENT="${EDGE_RATHOLE_CLIENT:-$EDGE_INGRESS_DIR/rathole-client-${AE_SITE_ID}.toml}"
    export AE_NATS_URL="${AE_NATS_URL:-nats://gateway:dev@127.0.0.1:4223}"
    export AE_JS_DOMAIN="${AE_JS_DOMAIN:-K1S}"
    export AE_GATEWAY_SPOOL_PATH="${AE_GATEWAY_SPOOL_PATH:-$PROFILE_DIR/gateway-${AE_SITE_ID}-${AE_NODE_ID}.db}"
    export AE_EDGE_INGRESS_LOCAL_ADDR="${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081}"
    export AE_RATHOLE_DEFAULT_TOKEN="${AE_RATHOLE_DEFAULT_TOKEN:-dev}"
    export AE_RATHOLE_SERVER_ADDR="${AE_RATHOLE_SERVER_ADDR:-127.0.0.1:2333}"
    if [[ "$INGRESS_MODE" == "edge-local" ]]; then
      export AE_EDGE_LOCAL_INGRESS_CONFIG_DIR="${AE_EDGE_LOCAL_INGRESS_CONFIG_DIR:-$PROFILE_DIR/edge-local}"
    fi
    if [[ "$INGRESS_MODE" == "core-proxy" && "$EDGE_INGRESS_START" == "1" ]]; then
      write_rathole_client_config "$EDGE_RATHOLE_CLIENT" "$AE_RATHOLE_SERVER_ADDR" "$AE_RATHOLE_DEFAULT_TOKEN" "$AE_SITE_ID" "$AE_EDGE_INGRESS_LOCAL_ADDR"
      RATHOLE_CLIENT_CONTAINER="${RATHOLE_CLIENT_CONTAINER:-k1s-edge-${AE_SITE_ID}-${AE_NODE_ID}-rathole}"
      start_rathole_client_container "$RATHOLE_CLIENT_CONTAINER" "$EDGE_RATHOLE_CLIENT"
    fi
    EDGE_START_WORKER="${EDGE_START_WORKER:-1}"
    if [[ "$EDGE_START_WORKER" == "1" ]]; then
      WORKER_NODE_ID="${EDGE_WORKER_NODE_ID:-$AE_NODE_ID}"
      WORKER_NATS_URL="${EDGE_WORKER_NATS_URL:-nats://worker:dev@127.0.0.1:4223}"
      WORKER_DELAY_MS="${EDGE_WORKER_DELAY_MS:-50}"
      WORKER_PROGRESS="${EDGE_WORKER_PROGRESS:-5}"
      PYTHONPATH=src "$PYTHON_BIN" -m ae.worker_stub \
        --node-id "$WORKER_NODE_ID" \
        --nats-url "$WORKER_NATS_URL" \
        --delay-ms "$WORKER_DELAY_MS" \
        --progress-interval "$WORKER_PROGRESS" &
      worker_pid=$!
      trap 'kill "$worker_pid" >/dev/null 2>&1 || true' EXIT
    fi
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.gateway
    ;;
  *)
    echo "unknown profile: $PROFILE" >&2
    exit 1
    ;;
esac
