#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

MODE=""
TIER="tier1"
STRICT=0
EXPECT_BUNDLE_DISABLED=0
SKIP_WORKLOAD_APPLY=0
KEEP_SPECS=0

SITE_ID="${SITE_ID:-sea-edge-02}"
APP_NAME="${APP_NAME:-app-svc}"
APP_MANIFEST="${APP_MANIFEST:-$ROOT_DIR/specs/examples/app-svc-node-sea-edge-02-edge-1.yaml}"

CORE_SPECS_DIR="${CORE_SPECS_DIR:-$ROOT_DIR/state/profiles/k1s-core/specs}"
CORE_ENVOY_CONFIG="${CORE_ENVOY_CONFIG:-$ROOT_DIR/state/profiles/k1s-core/edge-ingress/envoy.yaml}"
EDGE_LOCAL_CADDY_FILE="${EDGE_LOCAL_CADDY_FILE:-$ROOT_DIR/state/profiles/k1s-core/edge-local/edge-local.caddy}"

CORE_INGRESS_URL="${CORE_INGRESS_URL:-http://127.0.0.1:10080/}"
CORE_INGRESS_TLS_URL="${CORE_INGRESS_TLS_URL:-https://127.0.0.1:10443/}"
EDGE_BACKEND_URL="${EDGE_BACKEND_URL:-http://127.0.0.1:18081/}"
EDGE_LOCAL_LISTENER_URL="${EDGE_LOCAL_LISTENER_URL:-}"

CORE_PROXY_ROUTE_SRC="${CORE_PROXY_ROUTE_SRC:-$ROOT_DIR/specs/examples/edge-ingress-route-core-proxy.yaml}"
CORE_TO_EDGE_PUBLIC_ROUTE_SRC="${CORE_TO_EDGE_PUBLIC_ROUTE_SRC:-$ROOT_DIR/specs/examples/edge-ingress-route-core-to-edge-public.yaml}"
EDGE_LOCAL_ROUTE_SRC="${EDGE_LOCAL_ROUTE_SRC:-$ROOT_DIR/specs/examples/edge-ingress-route-edge-local.yaml}"

PUBLIC_GOOD_URL="${PUBLIC_GOOD_URL:-$EDGE_BACKEND_URL}"
PUBLIC_BAD_URL="${PUBLIC_BAD_URL:-http://127.0.0.1:19081/}"

WAIT_TIMEOUT_S="${WAIT_TIMEOUT_S:-90}"
READY_TIMEOUT_S="${READY_TIMEOUT_S:-180}"

declare -A STAGED_BACKUPS=()
declare -a STAGED_FILES=()
BACKUP_DIR=""

usage() {
  cat <<'USAGE'
Usage: scripts/dev/test_ingress_modes_single_host.sh --mode <mode> [options]

Validates a single ingress mode against an already-running single-host core/edge lab.
The script manages EdgeIngressRoute/SiteIngressEndpoint files in the active core specs dir.

Modes:
  core-proxy
  core-to-edge-public
  edge-local

Options:
  --mode <mode>                    Mode to validate (required)
  --tier <tier>                    tier1|tier2|both (default: tier1)
  --strict                         Enable stricter negative checks
  --expect-bundle-disabled         For edge-local strict check: expect bundle mutations NOT to apply
  --skip-workload-apply            Skip applying app workload manifest
  --keep-specs                     Do not restore/remove staged specs on exit

  --site-id <site>                 Site id (default: sea-edge-02)
  --app-name <name>                Workload app name (default: app-svc)
  --app-manifest <path>            Workload manifest path

  --core-specs-dir <path>          Active core specs dir
  --core-envoy-config <path>       Rendered core Envoy config path
  --edge-local-caddy-file <path>   Rendered edge-local Caddy file path

  --core-ingress-url <url>         Core ingress URL (default: http://127.0.0.1:10080/)
  --core-ingress-tls-url <url>     Core ingress TLS URL (default: https://127.0.0.1:10443/)
  --edge-backend-url <url>         Edge backend URL (default: http://127.0.0.1:18081/)
  --public-good-url <url>          Public endpoint URL for mode 2 (default: edge backend URL)
  --public-bad-url <url>           Broken public endpoint URL for strict mode 2
  --edge-local-listener-url <url>  Tier2 edge-local ingress URL (required for tier2/both)
  --core-proxy-route-src <path>    EdgeIngressRoute source file for core-proxy mode
  --core-to-edge-public-route-src <path>
                                  EdgeIngressRoute source file for core-to-edge-public mode
  --edge-local-route-src <path>    EdgeIngressRoute source file for edge-local mode

  --wait-timeout <seconds>         Reconcile wait timeout (default: 90)
  --ready-timeout <seconds>        Workload readiness timeout (default: 180)
  -h, --help                       Show help

Examples:
  scripts/dev/test_ingress_modes_single_host.sh --mode core-proxy
  scripts/dev/test_ingress_modes_single_host.sh --mode core-to-edge-public --strict
  scripts/dev/test_ingress_modes_single_host.sh --mode edge-local --tier tier1
  scripts/dev/test_ingress_modes_single_host.sh --mode edge-local --tier tier2 \
    --edge-local-listener-url https://127.0.0.1:11443/
USAGE
}

log() {
  printf '[ingress-modes] %s\n' "$*"
}

die() {
  printf '[ingress-modes] ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

run_ae() {
  PYTHONPATH="$ROOT_DIR/src" python -m ae.cli "$@"
}

mkbackup_dir() {
  if [[ -z "$BACKUP_DIR" ]]; then
    BACKUP_DIR="$(mktemp -d)"
  fi
}

stage_file() {
  local src="$1"
  local dst="$2"

  [[ -f "$src" ]] || die "missing source file: $src"
  mkdir -p "$(dirname "$dst")"

  if [[ -z "${STAGED_BACKUPS[$dst]+x}" ]]; then
    mkbackup_dir
    if [[ -f "$dst" ]]; then
      local backup_path
      backup_path="$(mktemp "$BACKUP_DIR/backup.XXXXXX")"
      cp "$dst" "$backup_path"
      STAGED_BACKUPS[$dst]="$backup_path"
    else
      STAGED_BACKUPS[$dst]=""
    fi
    STAGED_FILES+=("$dst")
  fi

  cp "$src" "$dst"
  log "staged $(basename "$dst") -> $dst"
}

restore_specs() {
  if [[ "$KEEP_SPECS" -eq 1 ]]; then
    log "keeping staged specs (--keep-specs)"
    return
  fi

  local i
  for ((i=${#STAGED_FILES[@]} - 1; i >= 0; i--)); do
    local dst="${STAGED_FILES[$i]}"
    local backup="${STAGED_BACKUPS[$dst]}"
    if [[ -n "$backup" && -f "$backup" ]]; then
      cp "$backup" "$dst"
    else
      rm -f "$dst"
    fi
  done

  if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
    rm -rf "$BACKUP_DIR"
  fi
}

wait_for_pattern() {
  local file="$1"
  local pattern="$2"
  local timeout="$3"
  local expect="${4:-present}"

  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    local found=0
    if [[ -f "$file" ]] && rg -n --fixed-strings --quiet "$pattern" "$file"; then
      found=1
    fi

    if [[ "$expect" == "present" && "$found" -eq 1 ]]; then
      return 0
    fi
    if [[ "$expect" == "absent" && "$found" -eq 0 ]]; then
      return 0
    fi

    sleep 2
  done

  if [[ "$expect" == "present" ]]; then
    die "timed out waiting for pattern '$pattern' in $file"
  fi
  die "timed out waiting for pattern '$pattern' to disappear from $file"
}

edge_local_recovery_hint() {
  cat >&2 <<EOF
[ingress-modes] recovery:
  1) restart core with:
     AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=edge-local AE_ROUTE_BUNDLE_ENABLED=1 make k1s-core
  2) restart edge gateway with:
     AE_SITE_ID=$SITE_ID AE_NODE_ID=edge-1 EDGE_INGRESS_MODE=edge-local AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=$(dirname "$EDGE_LOCAL_CADDY_FILE") make k1s-edge-core
  3) ensure specs dir is writable:
     CORE_SPECS=state/profiles/k1s-core/specs
     sudo chown -R \$USER:\$(id -gn) "\$CORE_SPECS" && sudo chmod -R g+rwX "\$CORE_SPECS" && sudo find "\$CORE_SPECS" -type d -exec chmod 2775 {} \;
EOF
}

die_edge_local_preflight() {
  local msg="$1"
  printf '[ingress-modes] ERROR: %s\n' "$msg" >&2
  edge_local_recovery_hint
  exit 1
}

read_proc_environ_lines() {
  local pid="$1"
  local proc_env="/proc/$pid/environ"

  if [[ -r "$proc_env" ]]; then
    tr '\0' '\n' < "$proc_env"
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    if sudo -n cat "$proc_env" >/dev/null 2>&1; then
      sudo -n cat "$proc_env" | tr '\0' '\n'
      return 0
    fi
    if sudo cat "$proc_env" >/dev/null 2>&1; then
      sudo cat "$proc_env" | tr '\0' '\n'
      return 0
    fi
  fi

  return 1
}

require_pid_for_pattern() {
  local pattern="$1"
  local name="$2"
  local pid
  pid="$(pgrep -f "$pattern" | head -n1 || true)"
  [[ -n "$pid" ]] || die_edge_local_preflight "edge-local preflight failed: $name process not found (pattern: $pattern)"
  printf '%s\n' "$pid"
}

require_env_value() {
  local env_lines="$1"
  local key="$2"
  local expected="$3"
  local process_name="$4"
  local match="${key}=${expected}"
  if ! printf '%s\n' "$env_lines" | rg -n --fixed-strings --quiet "$match"; then
    die_edge_local_preflight "edge-local preflight failed: expected $process_name env $match"
  fi
}

extract_env_value() {
  local env_lines="$1"
  local key="$2"
  awk -F= -v k="$key" '$1==k {print substr($0, index($0, "=")+1); exit}' <<<"$env_lines"
}

preflight_core_specs_writable() {
  if [[ ! -d "$CORE_SPECS_DIR" ]]; then
    die_edge_local_preflight "edge-local preflight failed: missing core specs dir $CORE_SPECS_DIR"
  fi
  if [[ ! -w "$CORE_SPECS_DIR" ]]; then
    die_edge_local_preflight "edge-local preflight failed: core specs dir is not writable: $CORE_SPECS_DIR"
  fi
}

preflight_edge_local_render_path() {
  local caddy_file="$1"
  local caddy_dir
  caddy_dir="$(dirname "$caddy_file")"

  if [[ ! -d "$caddy_dir" ]]; then
    die_edge_local_preflight "edge-local preflight failed: missing $caddy_dir (restart edge gateway with EDGE_INGRESS_MODE=edge-local and AE_EDGE_LOCAL_INGRESS_CONFIG_DIR=$caddy_dir)"
  fi
  if [[ ! -r "$caddy_dir" || ! -x "$caddy_dir" ]]; then
    die_edge_local_preflight "edge-local preflight failed: cannot read $caddy_dir as user $(id -un); fix ownership/permissions or run checks with sudo"
  fi
  if [[ -f "$caddy_file" && ! -r "$caddy_file" ]]; then
    die_edge_local_preflight "edge-local preflight failed: cannot read $caddy_file as user $(id -un); fix ownership/permissions or run checks with sudo"
  fi

  log "edge-local preflight OK render_path=$caddy_file"
}

preflight_edge_local_runtime() {
  local caddy_dir
  local controller_pid
  local gateway_pid
  local controller_env
  local gateway_env
  local controller_transport
  local controller_nats_url
  local gateway_transport
  local gateway_nats_url
  local edge_code

  need_cmd pgrep
  caddy_dir="$(dirname "$EDGE_LOCAL_CADDY_FILE")"
  preflight_core_specs_writable

  controller_pid="$(require_pid_for_pattern "python -m ae.controller" "controller")"
  if ! controller_env="$(read_proc_environ_lines "$controller_pid")"; then
    die_edge_local_preflight "edge-local preflight failed: cannot read controller env from /proc/$controller_pid/environ (try sudo and verify process is running)"
  fi
  require_env_value "$controller_env" "EDGE_INGRESS_MODE" "edge-local" "controller"
  require_env_value "$controller_env" "AE_ROUTE_BUNDLE_ENABLED" "1" "controller"
  controller_transport="$(extract_env_value "$controller_env" "AE_TRANSPORT_BACKEND")"
  if [[ "$controller_transport" != "nats-core" && "$controller_transport" != "nats-js" ]]; then
    die_edge_local_preflight "edge-local preflight failed: controller AE_TRANSPORT_BACKEND must be nats-core or nats-js (found: ${controller_transport:-<unset>})"
  fi
  controller_nats_url="$(extract_env_value "$controller_env" "AE_NATS_URL")"
  if [[ -z "$controller_nats_url" ]]; then
    die_edge_local_preflight "edge-local preflight failed: controller AE_NATS_URL is unset"
  fi

  gateway_pid="$(require_pid_for_pattern "python -m ae.gateway" "gateway")"
  if ! gateway_env="$(read_proc_environ_lines "$gateway_pid")"; then
    die_edge_local_preflight "edge-local preflight failed: cannot read gateway env from /proc/$gateway_pid/environ (try sudo and verify process is running)"
  fi
  require_env_value "$gateway_env" "EDGE_INGRESS_MODE" "edge-local" "gateway"
  require_env_value "$gateway_env" "AE_EDGE_LOCAL_INGRESS_CONFIG_DIR" "$caddy_dir" "gateway"
  gateway_transport="$(extract_env_value "$gateway_env" "AE_TRANSPORT_BACKEND")"
  if [[ "$gateway_transport" != "nats-core" && "$gateway_transport" != "nats-js" ]]; then
    die_edge_local_preflight "edge-local preflight failed: gateway AE_TRANSPORT_BACKEND must be nats-core or nats-js (found: ${gateway_transport:-<unset>})"
  fi
  gateway_nats_url="$(extract_env_value "$gateway_env" "AE_NATS_URL")"
  if [[ -z "$gateway_nats_url" ]]; then
    die_edge_local_preflight "edge-local preflight failed: gateway AE_NATS_URL is unset"
  fi

  preflight_edge_local_render_path "$EDGE_LOCAL_CADDY_FILE"

  edge_code="$(curl -sS -o /dev/null -w '%{http_code}' "$EDGE_BACKEND_URL" || true)"
  if [[ ! "$edge_code" =~ ^2[0-9][0-9]$ ]]; then
    die_edge_local_preflight "edge-local preflight failed: expected 2xx from edge backend $EDGE_BACKEND_URL, got $edge_code"
  fi
  log "edge-local preflight OK controller_pid=$controller_pid gateway_pid=$gateway_pid edge_backend_code=$edge_code"
}

extract_route_site_from_file() {
  local route_file="$1"
  python - "$route_file" <<'PY'
import re
import sys
path = sys.argv[1]
try:
    text = open(path, encoding="utf-8").read()
except Exception:
    print("")
    raise SystemExit(0)
match = re.search(r"(?m)^[ \t]*site:[ \t]*([^\s#]+)", text)
print(match.group(1) if match else "")
PY
}

extract_route_host_from_file() {
  local route_file="$1"
  python - "$route_file" <<'PY'
import re
import sys
path = sys.argv[1]
try:
    text = open(path, encoding="utf-8").read()
except Exception:
    print("")
    raise SystemExit(0)
match = re.search(r"(?m)^[ \t]*host:[ \t]*([^\s#]+)", text)
print(match.group(1) if match else "")
PY
}

verify_edge_local_route_site() {
  local route_file="$1"
  local site_id
  site_id="$(extract_route_site_from_file "$route_file")"
  if [[ -z "$site_id" ]]; then
    die_edge_local_preflight "edge-local preflight failed: staged route missing exposure.placement.site in $route_file"
  fi
  if [[ "$site_id" != "$SITE_ID" ]]; then
    die_edge_local_preflight "edge-local preflight failed: staged route site '$site_id' does not match requested site '$SITE_ID'"
  fi
  log "edge-local route site OK site_id=$site_id route=$route_file"
}

wait_for_edge_local_render() {
  local pattern="$1"
  local file="$2"
  local timeout="$3"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if [[ -f "$file" ]] && rg -n --fixed-strings --quiet "$pattern" "$file"; then
      return 0
    fi
    sleep 2
  done

  die_edge_local_preflight "timed out waiting for '$pattern' in $file (site=$SITE_ID). check controller logs for route bundle publish and gateway logs for route bundle apply acknowledgements."
}

fetch_code() {
  local url="$1"
  local host="$2"
  local out
  out="$(mktemp)"
  local code="000"

  if code="$(curl -sS -k --connect-timeout 2 --max-time 5 -o "$out" -w '%{http_code}' -H "Host: $host" "$url" 2>/dev/null)"; then
    :
  else
    code="000"
  fi

  rm -f "$out"
  printf '%s\n' "$code"
}

assert_http_2xx() {
  local url="$1"
  local host="$2"
  local code
  code="$(fetch_code "$url" "$host")"
  if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
    log "HTTP OK host=$host url=$url code=$code"
    return
  fi
  die "expected 2xx for host=$host url=$url, got $code"
}

assert_http_2xx_or_3xx() {
  local url="$1"
  local host="$2"
  local code
  code="$(fetch_code "$url" "$host")"
  if [[ "$code" =~ ^[23][0-9][0-9]$ ]]; then
    log "HTTP OK (2xx/3xx) host=$host url=$url code=$code"
    return
  fi
  die "expected 2xx/3xx for host=$host url=$url, got $code"
}

assert_http_non_2xx() {
  local url="$1"
  local host="$2"
  local code
  code="$(fetch_code "$url" "$host")"
  if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
    log "HTTP non-2xx as expected host=$host url=$url code=$code"
    return
  fi
  die "expected non-2xx for host=$host url=$url, got $code"
}

assert_http_5xx_or_000() {
  local url="$1"
  local host="$2"
  local code
  code="$(fetch_code "$url" "$host")"
  if [[ "$code" == "000" || "$code" =~ ^5[0-9][0-9]$ ]]; then
    log "HTTP failure as expected host=$host url=$url code=$code"
    return
  fi
  die "expected 5xx/000 for host=$host url=$url, got $code"
}

render_public_endpoint() {
  local url="$1"
  local out="$2"

  cat > "$out" <<EOF_ENDPOINT
apiVersion: k1s.io/v1
kind: SiteIngressEndpoint
metadata:
  name: ${SITE_ID}
spec:
  mode: core-to-edge-public
  public:
    urls:
      - url: ${url}
        expectedSANs:
          - pop-${SITE_ID}.home.arpa
EOF_ENDPOINT
}

apply_workload_and_wait() {
  [[ -f "$APP_MANIFEST" ]] || die "workload manifest not found: $APP_MANIFEST"
  log "planning workload manifest: $APP_MANIFEST"
  run_ae plan -f "$APP_MANIFEST" --verbose || true

  if [[ "$SKIP_WORKLOAD_APPLY" -eq 0 ]]; then
    log "applying workload manifest: $APP_MANIFEST"
    run_ae apply -f "$APP_MANIFEST"
  else
    log "skipping workload apply (--skip-workload-apply)"
  fi

  log "waiting for workload readiness: $APP_NAME"
  if ! run_ae status "$APP_NAME" --watch 2 --timeout "$READY_TIMEOUT_S" --events >/tmp/ingress-mode-status.log; then
    log "workload did not become ready; collecting diagnostics"
    run_ae status "$APP_NAME" --events || true
    run_ae nodes || true
    die "workload readiness failed for $APP_NAME"
  fi
  log "workload ready: $APP_NAME"

  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$EDGE_BACKEND_URL" || true)"
  [[ "$code" =~ ^2[0-9][0-9]$ ]] || die "edge backend not healthy at $EDGE_BACKEND_URL (code=$code)"
}

run_core_proxy() {
  local route_dst="$CORE_SPECS_DIR/edge-ingress-route-core-proxy.yaml"
  local route_src="$CORE_PROXY_ROUTE_SRC"
  local route_host

  stage_file "$route_src" "$route_dst"
  route_host="$(extract_route_host_from_file "$route_dst")"
  [[ -n "$route_host" ]] || die "core-proxy route file missing spec.host: $route_dst"

  wait_for_pattern "$CORE_ENVOY_CONFIG" "$route_host" "$WAIT_TIMEOUT_S" present
  # Route fixture enables redirectHttpToHttps, so HTTP may return 301.
  assert_http_2xx_or_3xx "$CORE_INGRESS_URL" "$route_host"
  assert_http_2xx "$CORE_INGRESS_TLS_URL" "$route_host"

  if [[ "$STRICT" -eq 1 ]]; then
    log "strict check: removing route to verify non-2xx"
    rm -f "$route_dst"
    wait_for_pattern "$CORE_ENVOY_CONFIG" "$route_host" "$WAIT_TIMEOUT_S" absent
    assert_http_non_2xx "$CORE_INGRESS_URL" "$route_host"

    stage_file "$route_src" "$route_dst"
    wait_for_pattern "$CORE_ENVOY_CONFIG" "$route_host" "$WAIT_TIMEOUT_S" present
    assert_http_2xx_or_3xx "$CORE_INGRESS_URL" "$route_host"
    assert_http_2xx "$CORE_INGRESS_TLS_URL" "$route_host"
  fi
}

run_core_to_edge_public() {
  local route_dst="$CORE_SPECS_DIR/edge-ingress-route-core-to-edge-public.yaml"
  local route_src="$CORE_TO_EDGE_PUBLIC_ROUTE_SRC"
  local route_host
  local endpoint_dst="$CORE_SPECS_DIR/site-ingress-endpoint-${SITE_ID}-public.yaml"
  local endpoint_good
  endpoint_good="$(mktemp)"

  render_public_endpoint "$PUBLIC_GOOD_URL" "$endpoint_good"
  stage_file "$endpoint_good" "$endpoint_dst"
  stage_file "$route_src" "$route_dst"
  route_host="$(extract_route_host_from_file "$route_dst")"
  [[ -n "$route_host" ]] || die "core-to-edge-public route file missing spec.host: $route_dst"

  wait_for_pattern "$CORE_ENVOY_CONFIG" "$route_host" "$WAIT_TIMEOUT_S" present
  assert_http_2xx "$CORE_INGRESS_URL" "$route_host"

  if [[ "$STRICT" -eq 1 ]]; then
    local endpoint_bad
    endpoint_bad="$(mktemp)"
    render_public_endpoint "$PUBLIC_BAD_URL" "$endpoint_bad"

    log "strict check: staging broken public endpoint URL"
    stage_file "$endpoint_bad" "$endpoint_dst"
    sleep 5
    assert_http_5xx_or_000 "$CORE_INGRESS_URL" "$route_host"

    stage_file "$endpoint_good" "$endpoint_dst"
    sleep 5
    assert_http_2xx "$CORE_INGRESS_URL" "$route_host"

    rm -f "$endpoint_bad"
  fi

  rm -f "$endpoint_good"
}

run_edge_local_tier1() {
  local route_dst="$CORE_SPECS_DIR/edge-ingress-route-edge-local.yaml"
  local route_src="$EDGE_LOCAL_ROUTE_SRC"
  local route_host
  local code

  preflight_edge_local_runtime
  stage_file "$route_src" "$route_dst"
  verify_edge_local_route_site "$route_dst"
  route_host="$(extract_route_host_from_file "$route_dst")"
  [[ -n "$route_host" ]] || die_edge_local_preflight "edge-local preflight failed: staged route missing spec.host in $route_dst"

  wait_for_edge_local_render "$route_host" "$EDGE_LOCAL_CADDY_FILE" "$WAIT_TIMEOUT_S"
  wait_for_pattern "$CORE_ENVOY_CONFIG" "$route_host" 10 absent

  code="$(curl -sS -o /dev/null -w '%{http_code}' "$EDGE_BACKEND_URL" || true)"
  [[ "$code" =~ ^2[0-9][0-9]$ ]] || die "edge backend check failed at $EDGE_BACKEND_URL (code=$code)"
  log "edge-local tier1 checks passed"

  if [[ "$STRICT" -eq 1 ]]; then
    local alt_host
    local mutated
    local route_host_base
    route_host_base="${route_host%.home.arpa}"
    if [[ "$route_host_base" == "$route_host" ]]; then
      route_host_base="$route_host"
    fi
    alt_host="${route_host_base}-alt-${RANDOM}.home.arpa"
    mutated="$(mktemp)"
    python - "$route_src" "$alt_host" > "$mutated" <<'PY'
import re
import sys
path = sys.argv[1]
alt_host = sys.argv[2]
text = open(path, encoding="utf-8").read()
updated, count = re.subn(r"(?m)^([ \t]*host:[ \t]*)([^\s#]+)(.*)$", rf"\1{alt_host}\3", text, count=1)
if count != 1:
    raise SystemExit("failed to rewrite route host for strict edge-local check")
sys.stdout.write(updated)
PY

    log "strict check: mutating edge-local host to $alt_host"
    stage_file "$mutated" "$route_dst"

    if [[ "$EXPECT_BUNDLE_DISABLED" -eq 1 ]]; then
      wait_for_pattern "$EDGE_LOCAL_CADDY_FILE" "$alt_host" 20 absent
      log "bundle-disabled expectation satisfied (no edge-local config update)"
    else
      wait_for_pattern "$EDGE_LOCAL_CADDY_FILE" "$alt_host" "$WAIT_TIMEOUT_S" present
      log "bundle update observed for mutated host"
    fi

    stage_file "$route_src" "$route_dst"
    wait_for_pattern "$EDGE_LOCAL_CADDY_FILE" "$route_host" "$WAIT_TIMEOUT_S" present
    rm -f "$mutated"
  fi
}

run_edge_local_tier2() {
  [[ -n "$EDGE_LOCAL_LISTENER_URL" ]] || die "--edge-local-listener-url is required for tier2"
  assert_http_2xx "$EDGE_LOCAL_LISTENER_URL" "app-edge-local.home.arpa"
  log "edge-local tier2 passed"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --tier)
      TIER="${2:-}"
      shift 2
      ;;
    --strict)
      STRICT=1
      shift
      ;;
    --expect-bundle-disabled)
      EXPECT_BUNDLE_DISABLED=1
      shift
      ;;
    --skip-workload-apply)
      SKIP_WORKLOAD_APPLY=1
      shift
      ;;
    --keep-specs)
      KEEP_SPECS=1
      shift
      ;;
    --site-id)
      SITE_ID="${2:-}"
      shift 2
      ;;
    --app-name)
      APP_NAME="${2:-}"
      shift 2
      ;;
    --app-manifest)
      APP_MANIFEST="${2:-}"
      shift 2
      ;;
    --core-specs-dir)
      CORE_SPECS_DIR="${2:-}"
      shift 2
      ;;
    --core-envoy-config)
      CORE_ENVOY_CONFIG="${2:-}"
      shift 2
      ;;
    --edge-local-caddy-file)
      EDGE_LOCAL_CADDY_FILE="${2:-}"
      shift 2
      ;;
    --core-ingress-url)
      CORE_INGRESS_URL="${2:-}"
      shift 2
      ;;
    --core-ingress-tls-url)
      CORE_INGRESS_TLS_URL="${2:-}"
      shift 2
      ;;
    --edge-backend-url)
      EDGE_BACKEND_URL="${2:-}"
      shift 2
      ;;
    --public-good-url)
      PUBLIC_GOOD_URL="${2:-}"
      shift 2
      ;;
    --public-bad-url)
      PUBLIC_BAD_URL="${2:-}"
      shift 2
      ;;
    --edge-local-listener-url)
      EDGE_LOCAL_LISTENER_URL="${2:-}"
      shift 2
      ;;
    --core-proxy-route-src)
      CORE_PROXY_ROUTE_SRC="${2:-}"
      shift 2
      ;;
    --core-to-edge-public-route-src)
      CORE_TO_EDGE_PUBLIC_ROUTE_SRC="${2:-}"
      shift 2
      ;;
    --edge-local-route-src)
      EDGE_LOCAL_ROUTE_SRC="${2:-}"
      shift 2
      ;;
    --wait-timeout)
      WAIT_TIMEOUT_S="${2:-}"
      shift 2
      ;;
    --ready-timeout)
      READY_TIMEOUT_S="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

case "$MODE" in
  core-proxy|core-to-edge-public|edge-local) ;;
  *) die "--mode is required and must be one of: core-proxy|core-to-edge-public|edge-local" ;;
esac

case "$TIER" in
  tier1|tier2|both) ;;
  *) die "--tier must be one of: tier1|tier2|both" ;;
esac

need_cmd rg
need_cmd curl
need_cmd python

if ! mkdir -p "$CORE_SPECS_DIR"; then
  die "failed to create core specs dir: $CORE_SPECS_DIR (fix ownership/permissions)"
fi
trap restore_specs EXIT

log "mode=$MODE tier=$TIER strict=$STRICT site_id=$SITE_ID"
log "core_specs_dir=$CORE_SPECS_DIR"

apply_workload_and_wait

case "$MODE" in
  core-proxy)
    run_core_proxy
    ;;
  core-to-edge-public)
    run_core_to_edge_public
    ;;
  edge-local)
    if [[ "$TIER" == "tier1" || "$TIER" == "both" ]]; then
      run_edge_local_tier1
    fi
    if [[ "$TIER" == "tier2" || "$TIER" == "both" ]]; then
      run_edge_local_tier2
    fi
    ;;
esac

log "PASS mode=$MODE tier=$TIER"
