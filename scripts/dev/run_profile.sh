#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROFILE="${1:-}"

if [[ -z "$PROFILE" ]]; then
  echo "usage: $0 <dev-min|dev-etcd|k1s-core|k1s-edge>" >&2
  exit 1
fi

if [[ -z "${AE_RUNTIME_BACKEND:-}" ]]; then
  export AE_RUNTIME_BACKEND=podman
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

abs_path() {
  local path="$1"
  if [[ -z "$path" ]]; then
    printf '%s' "$path"
    return 0
  fi
  if [[ "$path" == /* ]]; then
    printf '%s' "$path"
    return 0
  fi
  printf '%s/%s' "$ROOT_DIR" "$path"
}

seed_demo_specs() {
  local dir="$1"
  local wipe="${2:-1}"
  local blue_src="$ROOT_DIR/specs/examples/blue.yaml"
  local green_src="$ROOT_DIR/specs/examples/green.yaml"
  if [[ ! -f "$blue_src" || ! -f "$green_src" ]]; then
    return 0
  fi
  if [[ "$wipe" == "1" ]]; then
    rm -rf "$dir" 2>/dev/null || true
    mkdir -p "$dir"
  fi
  local wrote=0
  if [[ ! -f "$dir/blue.yaml" ]]; then
    cp "$blue_src" "$dir/blue.yaml" && wrote=1
  fi
  if [[ ! -f "$dir/green.yaml" ]]; then
    cp "$green_src" "$dir/green.yaml" && wrote=1
  fi
  if [[ "$wrote" -eq 1 ]]; then
    echo "[demo-seed] added blue/green specs to $dir"
  fi
}

ensure_demo_green_image() {
  local engine="$1"
  local image="demo-green:latest"
  local sample_dir="$ROOT_DIR/samples/servers/green"
  if [[ ! -d "$sample_dir" ]]; then
    return 0
  fi
  if "$engine" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -qE "(^|/)${image}$"; then
    return 0
  fi
  if [[ "$engine" == "podman" ]]; then
    if "$engine" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -q "localhost/${image}$"; then
      "$engine" tag "localhost/${image}" "${image}" >/dev/null 2>&1 || true
      return 0
    fi
    echo "[demo-seed] building localhost/${image} (podman)"
    "$engine" build -t "localhost/${image}" "$sample_dir" >/dev/null 2>&1 || true
    "$engine" tag "localhost/${image}" "${image}" >/dev/null 2>&1 || true
  else
    echo "[demo-seed] building ${image} (${engine})"
    "$engine" build -t "${image}" "$sample_dir" >/dev/null 2>&1 || true
  fi
}

ensure_demo_echo_image() {
  local engine="$1"
  local image="mendhak/http-https-echo:37"
  if "$engine" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -q '^mendhak/http-https-echo:37$'; then
    return 0
  fi
  echo "[demo-seed] pulling ${image} (${engine})"
  "$engine" pull "$image" >/dev/null 2>&1 || true
}

resolve_docs_labs_token() {
  if [[ "${AE_LABS:-0}" != "1" ]]; then
    return 0
  fi
  if [[ -n "${DOCS_LABS_TOKEN:-}" ]]; then
    return 0
  fi
  local env_file="${AE_APISHIM_ENV_FILE:-}"
  if [[ -z "$env_file" || ! -f "$env_file" ]]; then
    return 0
  fi
  local token=""
  token="$(awk -F= '/^AE_LABS_TOKEN=/{print $2}' "$env_file" 2>/dev/null || true)"
  if [[ -n "$token" ]]; then
    export DOCS_LABS_TOKEN="$token"
  fi
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

  resolve_docs_labs_token
  DOCS_API_BASE="$api_base" DOCS_DASHBOARD_URL="$dash_url" "$PYTHON_BIN" docs/build_docs.py >/dev/null 2>&1 || true
  nohup "$PYTHON_BIN" -m http.server "$docs_port" --bind "$docs_bind" --directory "$docs_dir" >/dev/null 2>&1 &
  echo $! > "$pid_file"
}

start_caddy() {
  local https_port="${CADDY_HTTPS_PORT:-8443}"
  local http_port="${CADDY_HTTP_PORT:-8888}"
  local api_base="https://api.home.arpa:${https_port}"
  local dash_url="https://dash.home.arpa:${https_port}/dashboard"
  local docs_env="$ROOT_DIR/state/dev.env"
  local caddy_sites="$ROOT_DIR/state/caddy"
  local caddy_data="$ROOT_DIR/state/caddy-data"
  local caddy_config="$ROOT_DIR/ops/dev/caddy"
  local docs_dir="$ROOT_DIR/docs/site"
  local caddy_container="${AE_CADDY_CONTAINER:-dev-caddy-1}"
  local apishim_upstream=""
  local caddy_network=""

  mkdir -p "$caddy_sites"
  resolve_docs_labs_token
  DOCS_API_BASE="$api_base" DOCS_DASHBOARD_URL="$dash_url" "$PYTHON_BIN" docs/build_docs.py >/dev/null 2>&1 || true

  local host_alias="host.docker.internal"
  if [[ "$ENGINE_BIN" == "podman" ]]; then
    host_alias="host.containers.internal"
  fi
  if [[ -x "$ROOT_DIR/scripts/ensure_dev_env.sh" ]]; then
    AE_CONTAINER_CLI="$ENGINE_BIN" "$ROOT_DIR/scripts/ensure_dev_env.sh" >/dev/null 2>&1 || true
  fi
  if [[ "${AE_APISHIM_MODE:-}" == "container" && "$ENGINE_BIN" == "podman" ]]; then
    if "$ENGINE_BIN" network inspect dev_default >/dev/null 2>&1; then
      apishim_upstream="apishim:${APISHIM_PORT:-8445}"
      caddy_network="dev_default"
    fi
  fi
  if [[ -z "$apishim_upstream" ]]; then
    apishim_upstream="${host_alias}:${APISHIM_PORT:-8445}"
  fi
  export APISHIM_ENV_FILE="${APISHIM_ENV_FILE:-$docs_env}"
  if [[ -f "$docs_env" ]]; then
    sed -i '/^APISHIM_UPSTREAM=/d' "$docs_env" >/dev/null 2>&1 || true
    printf 'APISHIM_UPSTREAM=%s\n' "$apishim_upstream" >>"$docs_env"
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

  if [[ "$ENGINE_BIN" == "podman" ]]; then
    mkdir -p "$caddy_data"
    "$ENGINE_BIN" rm -f "$caddy_container" >/dev/null 2>&1 || true
    local caddy_started=0
    "$ENGINE_BIN" run -d --name "$caddy_container" \
      -p "${http_port}:80" \
      -p "${https_port}:443" \
      --env-file "$docs_env" \
      -v "${caddy_config}:/etc/caddy:ro" \
      -v "${caddy_data}:/data" \
      -v "${caddy_sites}:/etc/caddy/dynsites:ro" \
      -v "${docs_dir}:/srv/docs:ro" \
      ${caddy_network:+--network "$caddy_network"} \
      --add-host "host.docker.internal:host-gateway" \
      --add-host "host.containers.internal:host-gateway" \
      docker.io/library/caddy:2.8 >/dev/null 2>&1 && caddy_started=1 || true
    if [[ "$caddy_started" -ne 1 ]]; then
      "$ENGINE_BIN" rm -f "$caddy_container" >/dev/null 2>&1 || true
      "$ENGINE_BIN" run -d --name "$caddy_container" \
        -p "${http_port}:80" \
        -p "${https_port}:443" \
        --env-file "$docs_env" \
        -v "${caddy_config}:/etc/caddy:ro" \
        -v "${caddy_data}:/data" \
        -v "${caddy_sites}:/etc/caddy/dynsites:ro" \
        -v "${docs_dir}:/srv/docs:ro" \
        ${caddy_network:+--network "$caddy_network"} \
        docker.io/library/caddy:2.8 >/dev/null 2>&1 || true
    fi
    "$ENGINE_BIN" exec -T "$caddy_container" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
  else
    "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.yaml" up -d caddy >/dev/null 2>&1 || true
    "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.yaml" exec -T caddy \
      caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
  fi
}

is_truthy() {
  case "${1:-}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

port_open() {
  local host="$1"
  local port="$2"
  "$PYTHON_BIN" - <<'PY' "$host" "$port"
import socket, sys
host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket()
sock.settimeout(0.4)
try:
    sock.connect((host, port))
    sys.exit(0)
except Exception:
    sys.exit(1)
finally:
    try:
        sock.close()
    except Exception:
        pass
PY
}

start_apishim() {
  local profile_dir="$1"
  local host="${APISHIM_HOST:-127.0.0.1}"
  local port="${APISHIM_PORT:-8445}"
  local pid_file="${APISHIM_PID_FILE:-$ROOT_DIR/state/apishim.pid}"
  local env_file="${APISHIM_ENV_FILE:-$profile_dir/apishim.env}"
  local cert_file="${APISHIM_CERT_FILE:-$profile_dir/apishim.crt}"
  local key_file="${APISHIM_KEY_FILE:-$profile_dir/apishim.key}"
  local mode="${AE_APISHIM_MODE:-container}"
  local already_running=0
  export AE_APISHIM_ENV_FILE="${AE_APISHIM_ENV_FILE:-$env_file}"

  if ! is_truthy "${AE_APISHIM_AUTOSTART:-1}"; then
    return 0
  fi
  if port_open "$host" "$port"; then
    already_running=1
  fi

  mkdir -p "$profile_dir"
  APISHIM_ENV_FILE="$env_file" APISHIM_CERT_FILE="$cert_file" APISHIM_KEY_FILE="$key_file" \
    "$ROOT_DIR/scripts/ensure_apishim_env.sh" >/dev/null 2>&1 || true
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi

  export AE_APISHIM_RUNTIME="${AE_APISHIM_RUNTIME:-${AE_RUNTIME_BACKEND:-docker}}"
  export AE_APISHIM_ENABLE=1
  export AE_APISHIM_ALLOW_ANON="${AE_APISHIM_ALLOW_ANON:-0}"
  export AE_APISHIM_RBAC="${AE_APISHIM_RBAC:-1}"
  export AE_APISHIM_RBAC_EVAL="${AE_APISHIM_RBAC_EVAL:-0}"
  export AE_APISHIM_DB="${AE_APISHIM_DB:-$profile_dir/apishim.db}"
  export AE_APISHIM_TLS_CERT="${AE_APISHIM_TLS_CERT:-$cert_file}"
  export AE_APISHIM_TLS_KEY="${AE_APISHIM_TLS_KEY:-$key_file}"
  export AE_APISHIM_SERVER="${AE_APISHIM_SERVER:-https://127.0.0.1:${port}}"
  # Ensure controller can mint shim session tokens (dashboard exec/port-forward).
  if [[ -n "${AE_APISHIM_SESSION_SECRET:-}" ]]; then
    export AE_APISHIM_SESSION_SECRET
  fi
  if [[ -n "${AE_LABS_TOKEN:-}" ]]; then
    export AE_LABS_TOKEN
  fi
  if [[ -n "${AE_API_ADMIN_TOKEN:-}" ]]; then
    export AE_API_ADMIN_TOKEN
  fi

  if [[ "$already_running" -eq 1 ]]; then
    if [[ "$mode" == "container" ]]; then
      local profile_rel="$profile_dir"
      if [[ "$profile_dir" == "$ROOT_DIR/"* ]]; then
        profile_rel="${profile_dir#"$ROOT_DIR/"}"
      fi
      export APISHIM_ENV_FILE="$env_file"
      export APISHIM_PROFILE_DIR="${APISHIM_PROFILE_DIR:-$profile_rel}"
      export APISHIM_PORT="$port"
      export APISHIM_CONTAINER=1
      AE_CONTAINER_CLI="$ENGINE_BIN" APISHIM_CONTAINER=1 "$ROOT_DIR/scripts/ensure_dev_env.sh" >/dev/null 2>&1 || true
    fi
    return 0
  fi

  if [[ "$mode" == "host" ]]; then
    nohup "$PYTHON_BIN" -m ae.apishim serve --host "$host" --port "$port" --tls \
      >"$profile_dir/apishim.log" 2>&1 &
    echo $! > "$pid_file"
    return 0
  fi

  local host_alias="host.docker.internal"
  if [[ "$ENGINE_BIN" == "podman" ]]; then
    host_alias="host.containers.internal"
  fi
  if [[ -z "${APISHIM_NODE_ADVERTISE_IP:-}" ]]; then
    export APISHIM_NODE_ADVERTISE_IP="$host_alias"
  fi

  connect_apishim_network() {
    local engine="$ENGINE_BIN"
    local net_name=""
    local container="${AE_APISHIM_CONTAINER_NAME:-}"
    if [[ -n "${AE_PODMAN_NETWORK:-}" ]]; then
      net_name="${AE_PODMAN_NETWORK}"
    elif [[ -n "${AE_NETWORK_NAME:-}" ]]; then
      net_name="${AE_NETWORK_NAME}"
    elif [[ "$engine" == "podman" ]]; then
      net_name="podman"
    elif [[ "$engine" == "docker" ]]; then
      net_name="bridge"
    fi
    if [[ -z "$net_name" ]]; then
      return 0
    fi
    if [[ -z "$container" ]]; then
      container="$($engine ps --format '{{.Names}}' 2>/dev/null | awk '/apishim/ {print $1; exit}' || true)"
    fi
    if [[ -z "$container" ]]; then
      return 0
    fi
    if ! "$engine" network inspect "$net_name" >/dev/null 2>&1; then
      return 0
    fi
    "$engine" network connect "$net_name" "$container" >/dev/null 2>&1 || true
  }

  local profile_rel="$profile_dir"
  if [[ "$profile_dir" == "$ROOT_DIR/"* ]]; then
    profile_rel="${profile_dir#"$ROOT_DIR/"}"
  fi
  export APISHIM_ENV_FILE="$env_file"
  export APISHIM_PROFILE_DIR="${APISHIM_PROFILE_DIR:-$profile_rel}"
  export APISHIM_PORT="$port"
  export APISHIM_CONTAINER=1
  AE_CONTAINER_CLI="$ENGINE_BIN" APISHIM_CONTAINER=1 "$ROOT_DIR/scripts/ensure_dev_env.sh" >/dev/null 2>&1 || true
  "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.yaml" up -d apishim >/dev/null 2>&1 || true
  connect_apishim_network
}

ensure_dev_local() {
  if [[ "${AE_DEV_LOCAL:-0}" == "1" ]]; then
    DEV_PROFILE_DIR="${DEV_PROFILE_DIR:-${1:-}}" \
      AE_RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-}" \
      AE_CONTAINER_CLI="${AE_CONTAINER_CLI:-}" \
      STACK_BIN="${STACK_BIN:-}" \
      CADDY_HTTPS_PORT="${CADDY_HTTPS_PORT:-}" \
      AE_APISHIM_TLS_CERT="${AE_APISHIM_TLS_CERT:-}" \
      AE_TLS_DIR="${AE_TLS_DIR:-}" \
      "$ROOT_DIR/scripts/dev/ensure_dev_local.sh" || true
  fi
}

build_docs_with_labs_token() {
  if [[ "${AE_LABS:-0}" != "1" ]]; then
    return 0
  fi
  if [[ "${CORE_CADDY:-0}" != "1" && "${CORE_DOCS:-0}" != "1" ]]; then
    return 0
  fi
  if [[ -n "${DOCS_LABS_TOKEN:-}" ]]; then
    return 0
  fi
  local env_file="${AE_APISHIM_ENV_FILE:-}"
  if [[ -z "$env_file" || ! -f "$env_file" ]]; then
    return 0
  fi
  local token=""
  token="$(awk -F= '/^AE_LABS_TOKEN=/{print $2}' "$env_file" 2>/dev/null || true)"
  if [[ -z "$token" ]]; then
    return 0
  fi
  DOCS_LABS_TOKEN="$token" python docs/build_docs.py || true
  if [[ -f "docs/site/playground.html" ]]; then
    python - <<'PY' "$token" || true
from pathlib import Path
import sys

token = sys.argv[1]
path = Path("docs/site/playground.html")
text = path.read_text(encoding="utf-8")
needle = "window.DOCS_LABS_TOKEN='"
idx = text.find(needle)
if idx == -1:
    raise SystemExit(0)
start = idx + len(needle)
end = text.find("'", start)
if end == -1:
    raise SystemExit(0)
current = text[start:end]
if current == token:
    raise SystemExit(0)
patched = text[:start] + token + text[end:]
path.write_text(patched, encoding="utf-8")
PY
  fi
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
  local image="${AE_ENVOY_IMAGE:-docker.io/envoyproxy/envoy:v1.29-latest}"
  "$ENGINE_BIN" rm -f "$name" >/dev/null 2>&1 || true
  "$ENGINE_BIN" run -d --name "$name" --network host \
    -v "${config_path}:/etc/envoy/envoy.yaml:ro" \
    "$image" -c /etc/envoy/envoy.yaml --log-level info >/dev/null
}

start_rathole_server_container() {
  local name="$1"
  local config_path="$2"
  local image="${AE_RATHOLE_IMAGE:-docker.io/rapiz1/rathole:v0.5.0}"
  "$ENGINE_BIN" rm -f "$name" >/dev/null 2>&1 || true
  "$ENGINE_BIN" run -d --name "$name" --network host \
    -v "${config_path}:/etc/rathole/server.toml:ro" \
    "$image" --server /etc/rathole/server.toml >/dev/null
}

start_rathole_client_container() {
  local name="$1"
  local config_path="$2"
  local image="${AE_RATHOLE_IMAGE:-docker.io/rapiz1/rathole:v0.5.0}"
  "$ENGINE_BIN" rm -f "$name" >/dev/null 2>&1 || true
  "$ENGINE_BIN" run -d --name "$name" --network host \
    -v "${config_path}:/etc/rathole/client.toml:ro" \
    "$image" --client /etc/rathole/client.toml >/dev/null
}

PYTHON_BIN="$(detect_python)"
ENGINE_BIN="$(detect_engine)"

if [[ "${BENCH_MODE:-0}" == "1" ]]; then
  export AE_APISHIM_AUTOSTART="${AE_APISHIM_AUTOSTART:-0}"
  export AE_LABS="${AE_LABS:-0}"
  export CORE_CADDY="${CORE_CADDY:-0}"
  export CORE_DOCS="${CORE_DOCS:-0}"
  export AE_DEV_LOCAL="${AE_DEV_LOCAL:-0}"
fi

if [[ -z "${AE_APISHIM_MODE:-}" ]]; then
  if [[ "$ENGINE_BIN" == "podman" ]]; then
    AE_APISHIM_MODE="container"
  else
    AE_APISHIM_MODE="host"
  fi
  export AE_APISHIM_MODE
fi

if [[ "$ENGINE_BIN" == "podman" ]]; then
  if [[ -z "${APISHIM_CONTAINER_SOCKET:-}" ]]; then
    APISHIM_CONTAINER_SOCKET="/run/user/$(id -u)/podman/podman.sock"
  fi
  if [[ -z "${APISHIM_CONTAINER_HOST:-}" ]]; then
    APISHIM_CONTAINER_HOST="unix:///run/podman/podman.sock"
  fi
  export APISHIM_CONTAINER_SOCKET
  export APISHIM_CONTAINER_HOST
fi

case "$PROFILE" in
  dev-min)
    PROFILE_DIR="$(abs_path "${PROFILE_DIR:-state/profiles/dev-min}")"
    SPECS_DIR="$(abs_path "${SPECS_DIR:-$PROFILE_DIR/specs}")"
    ensure_specs_dir "$SPECS_DIR"
    export DEV_PROFILE_DIR="$PROFILE_DIR"
    export AE_SPECS_DIR="$SPECS_DIR"
    export AE_PROJECTION_ROOT="${AE_PROJECTION_ROOT:-$PROFILE_DIR/projections}"
    export AE_STATE_DB="${AE_STATE_DB:-$PROFILE_DIR/controller.db}"
    export AE_STATE_BACKEND="${AE_STATE_BACKEND:-sqlite}"
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-http}"
    if [[ "${AE_DEMO_SEED:-0}" == "1" ]]; then
      seed_demo_specs "$SPECS_DIR" "${AE_DEMO_SEED_WIPE:-1}"
      ensure_demo_green_image "$ENGINE_BIN"
      ensure_demo_echo_image "$ENGINE_BIN"
    fi
    export AE_REGISTER_LOCAL_NODE="${AE_REGISTER_LOCAL_NODE:-1}"
    export AE_LABS="${AE_LABS:-1}"
    export APISHIM_PORT="${APISHIM_PORT:-8445}"
    METRICS_PORT="${METRICS_PORT:-9108}"
    start_apishim "$PROFILE_DIR"
    build_docs_with_labs_token
    if [[ "${CORE_CADDY:-0}" == "1" ]]; then
      export AE_CADDY_CONTAINER="${AE_CADDY_CONTAINER:-dev-caddy-1}"
      export AE_CONTAINER_CLI="${AE_CONTAINER_CLI:-$ENGINE_BIN}"
      export AE_CADDY_FILE="${AE_CADDY_FILE:-/etc/caddy/Caddyfile}"
      export AE_CADDY_SITES="${AE_CADDY_SITES:-$ROOT_DIR/state/caddy}"
      start_caddy
    fi
    ensure_dev_local "$PROFILE_DIR"
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.controller --loop --metrics-port "$METRICS_PORT" --watch
    ;;
  dev-etcd)
    COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml}"
    compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d etcd
    PROFILE_DIR="$(abs_path "${PROFILE_DIR:-state/profiles/dev-etcd}")"
    SPECS_DIR="$(abs_path "${SPECS_DIR:-$PROFILE_DIR/specs}")"
    ensure_specs_dir "$SPECS_DIR"
    export DEV_PROFILE_DIR="$PROFILE_DIR"
    export AE_SPECS_DIR="$SPECS_DIR"
    export AE_PROJECTION_ROOT="${AE_PROJECTION_ROOT:-$PROFILE_DIR/projections}"
    export AE_STATE_BACKEND="${AE_STATE_BACKEND:-etcd}"
    export AE_ETCD_ENDPOINTS="${AE_ETCD_ENDPOINTS:-http://127.0.0.1:2379}"
    export AE_ETCD_PREFIX="${AE_ETCD_PREFIX:-k1s/profiles/dev-etcd}"
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-http}"
    export AE_REGISTER_LOCAL_NODE="${AE_REGISTER_LOCAL_NODE:-1}"
    export AE_LABS="${AE_LABS:-1}"
    export APISHIM_PORT="${APISHIM_PORT:-8445}"
    METRICS_PORT="${METRICS_PORT:-9108}"
    start_apishim "$PROFILE_DIR"
    build_docs_with_labs_token
    if [[ "${CORE_CADDY:-0}" == "1" ]]; then
      export AE_CADDY_CONTAINER="${AE_CADDY_CONTAINER:-dev-caddy-1}"
      export AE_CONTAINER_CLI="${AE_CONTAINER_CLI:-$ENGINE_BIN}"
      export AE_CADDY_FILE="${AE_CADDY_FILE:-/etc/caddy/Caddyfile}"
      export AE_CADDY_SITES="${AE_CADDY_SITES:-$ROOT_DIR/state/caddy}"
      start_caddy
    fi
    ensure_dev_local "$PROFILE_DIR"
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.controller --loop --metrics-port "$METRICS_PORT" --watch
    ;;
  k1s-core)
    COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml}"
    compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d etcd nats-hub
    PROFILE_DIR="$(abs_path "${PROFILE_DIR:-state/profiles/k1s-core}")"
    SPECS_DIR="$(abs_path "${SPECS_DIR:-$PROFILE_DIR/specs}")"
    ensure_specs_dir "$SPECS_DIR"
    export DEV_PROFILE_DIR="$PROFILE_DIR"
    export AE_SPECS_DIR="$SPECS_DIR"
    export AE_PROJECTION_ROOT="${AE_PROJECTION_ROOT:-$PROFILE_DIR/projections}"
    export AE_STATE_BACKEND="${AE_STATE_BACKEND:-etcd}"
    export AE_ETCD_ENDPOINTS="${AE_ETCD_ENDPOINTS:-http://127.0.0.1:2379}"
    export AE_ETCD_PREFIX="${AE_ETCD_PREFIX:-k1s/profiles/k1s-core}"
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-nats-js}"
    export AE_NATS_URL="${AE_NATS_URL:-nats://hub-controller:dev@127.0.0.1:4222}"
    export AE_JS_DOMAIN="${AE_JS_DOMAIN:-K1S}"
    if [[ "${AE_DEV_LOCAL:-0}" == "1" ]]; then
      export AE_REGISTER_LOCAL_NODE="${AE_REGISTER_LOCAL_NODE:-1}"
      export AE_LABS="${AE_LABS:-1}"
      export CORE_CADDY="${CORE_CADDY:-1}"
      export CORE_DOCS="${CORE_DOCS:-1}"
    fi
    INGRESS_MODE="$(normalize_ingress_mode "${EDGE_INGRESS_MODE:-${AE_EDGE_INGRESS_MODE:-core-proxy}}")"
    EDGE_INGRESS_START="${EDGE_INGRESS_START:-1}"
    EDGE_INGRESS_DIR="${EDGE_INGRESS_DIR:-$PROFILE_DIR/edge-ingress}"
    EDGE_ENVOY_CONFIG="${EDGE_ENVOY_CONFIG:-$EDGE_INGRESS_DIR/envoy.yaml}"
    EDGE_RATHOLE_SERVER="${EDGE_RATHOLE_SERVER:-$EDGE_INGRESS_DIR/rathole-server.toml}"
    export AE_EDGE_INGRESS_CONFIG_DIR="${AE_EDGE_INGRESS_CONFIG_DIR:-$EDGE_INGRESS_DIR}"
    export AE_EDGE_INGRESS_ENVOY_CONFIG="${AE_EDGE_INGRESS_ENVOY_CONFIG:-$EDGE_ENVOY_CONFIG}"
    export AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX="${AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX:-edge.local}"
    export AE_EDGE_INGRESS_LOCAL_ADDR="${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081}"
    export AE_EDGE_INGRESS_HTTP_PORT="${AE_EDGE_INGRESS_HTTP_PORT:-10080}"
    export AE_EDGE_INGRESS_TLS_PORT="${AE_EDGE_INGRESS_TLS_PORT:-10443}"
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
    export APISHIM_PORT="${APISHIM_PORT:-8445}"
    if [[ "${AE_LABS:-0}" == "1" ]]; then
      start_apishim "$PROFILE_DIR"
      build_docs_with_labs_token
    fi
    if [[ "${CORE_CADDY:-0}" == "1" ]]; then
      export AE_CADDY_CONTAINER="${AE_CADDY_CONTAINER:-dev-caddy-1}"
      export AE_CONTAINER_CLI="${AE_CONTAINER_CLI:-$ENGINE_BIN}"
      export AE_CADDY_FILE="${AE_CADDY_FILE:-/etc/caddy/Caddyfile}"
      export AE_CADDY_SITES="${AE_CADDY_SITES:-$ROOT_DIR/state/caddy}"
      start_caddy
    fi
    if [[ "${CORE_DOCS:-0}" == "1" ]]; then
      start_docs_server
    fi
    ensure_dev_local "$PROFILE_DIR"
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.controller --loop --metrics-port "$METRICS_PORT" --watch
    ;;
  k1s-edge)
    COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml}"
    compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d nats-edge
    EDGE_PROFILE="${EDGE_PROFILE:-k1s-edge}"
    PROFILE_DIR="$(abs_path "${PROFILE_DIR:-state/profiles/$EDGE_PROFILE}")"
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
