#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

MODES_CSV="${MODES_CSV:-core-proxy,core-to-edge-public,edge-local}"
ARCHETYPES_CSV="${ARCHETYPES_CSV:-http-static,http-path-routing,http-multi-replica,http-multiport,http-redirect,http-large-payload,http2-unary}"
TIER="${TIER:-tier1}"
STRICT=0
KEEP_SPECS=0
FAIL_FAST=0
VALIDATION_PROFILE="${VALIDATION_PROFILE:-standard}"
PERF_PROFILE="${PERF_PROFILE:-off}"

SITE_ID="${SITE_ID:-sea-edge-02}"
NODE_ID="${NODE_ID:-edge-1}"

CORE_SPECS_DIR="${CORE_SPECS_DIR:-$ROOT_DIR/state/profiles/k1s-core/specs}"
CORE_ENVOY_CONFIG="${CORE_ENVOY_CONFIG:-$ROOT_DIR/state/profiles/k1s-core/edge-ingress/envoy.yaml}"
EDGE_LOCAL_CADDY_FILE="${EDGE_LOCAL_CADDY_FILE:-$ROOT_DIR/state/profiles/k1s-core/edge-local/edge-local.caddy}"

CORE_INGRESS_URL="${CORE_INGRESS_URL:-http://127.0.0.1:10080/}"
CORE_INGRESS_TLS_URL="${CORE_INGRESS_TLS_URL:-https://127.0.0.1:10443/}"
CORE_PUBLIC_INGRESS_URL="${CORE_PUBLIC_INGRESS_URL:-$CORE_INGRESS_TLS_URL}"
EDGE_LOCAL_LISTENER_URL="${EDGE_LOCAL_LISTENER_URL:-}"
EDGE_BACKEND_HOST="${EDGE_BACKEND_HOST:-127.0.0.1}"
EDGE_BACKEND_SCHEME="${EDGE_BACKEND_SCHEME:-http}"
CORE_PROXY_LOCAL_ADDR="${CORE_PROXY_LOCAL_ADDR:-${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081}}"

WAIT_TIMEOUT_S="${WAIT_TIMEOUT_S:-90}"
READY_TIMEOUT_S="${READY_TIMEOUT_S:-180}"
STABILITY_REQUESTS="${STABILITY_REQUESTS:-30}"
LARGE_PAYLOAD_PATH="${LARGE_PAYLOAD_PATH:-/}"
LARGE_PAYLOAD_MIN_BYTES="${LARGE_PAYLOAD_MIN_BYTES:-65536}"
HTTP2_ENFORCE_DOWNSTREAM_H2="${HTTP2_ENFORCE_DOWNSTREAM_H2:-0}"
WS_DURATION_SECONDS="${WS_DURATION_SECONDS:-600}"
WS_CONNECTIONS="${WS_CONNECTIONS:-50}"
WS_HEARTBEAT_SECONDS="${WS_HEARTBEAT_SECONDS:-5}"
WS_MIN_CONNECTED_RATIO="${WS_MIN_CONNECTED_RATIO:-0.98}"
WS_MAX_CONNECT_FAILURE_RATE="${WS_MAX_CONNECT_FAILURE_RATE:-0.02}"
WS_MAX_MESSAGE_LOSS="${WS_MAX_MESSAGE_LOSS:-0}"
LB_SAMPLE_REQUESTS="${LB_SAMPLE_REQUESTS:-5000}"
LB_MIN_BACKENDS="${LB_MIN_BACKENDS:-2}"
LB_MAX_SKEW_RATIO="${LB_MAX_SKEW_RATIO:-0.35}"
LB_PROOF_SCOPE="${LB_PROOF_SCOPE:-auto}"
STICKY_REQUESTS_PER_CLIENT="${STICKY_REQUESTS_PER_CLIENT:-100}"
PERF_DURATION_SECONDS="${PERF_DURATION_SECONDS:-180}"
PERF_CONCURRENCY="${PERF_CONCURRENCY:-50}"
PERF_RPS_TARGET="${PERF_RPS_TARGET:-0}"
PERF_WARMUP_SECONDS="${PERF_WARMUP_SECONDS:-20}"
PERF_MIN_RPS="${PERF_MIN_RPS:-0}"
PERF_MAX_P95_MS="${PERF_MAX_P95_MS:-0}"
PERF_MAX_P99_MS="${PERF_MAX_P99_MS:-0}"
PERF_MAX_ERROR_RATE="${PERF_MAX_ERROR_RATE:-1}"

RESULTS_DIR="${RESULTS_DIR:-$ROOT_DIR/state/test-results}"
RUN_STAMP="${RUN_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULT_JSON="${RESULT_JSON:-$RESULTS_DIR/ingress-matrix-${RUN_STAMP}.json}"
RESULT_TSV=""
FAILURES_DIR=""
DEEP_PROBE_SCRIPT="${DEEP_PROBE_SCRIPT:-$ROOT_DIR/scripts/dev/ingress_deep_probe.py}"
ETCD_MAINTENANCE_SCRIPT="${ETCD_MAINTENANCE_SCRIPT:-$ROOT_DIR/scripts/dev/etcd_maintenance.sh}"
POSTCHECK_EVIDENCE_JSON="{}"
POSTCHECK_PERF_JSON="{}"

usage() {
  cat <<'USAGE'
Usage: scripts/dev/test_ingress_matrix_single_host.sh [options]

Runs ingress mode validation as a mode x workload-archetype matrix on single-host
multisite CRI ops patterns.

Options:
  --modes <csv>                    Modes to run (default: core-proxy,core-to-edge-public,edge-local)
  --archetypes <csv>               Archetypes to run (default includes protocol variants)
  --tier <tier>                    tier1|tier2|both (default: tier1)
  --strict                         Pass --strict to single-mode checker
  --keep-specs                     Keep staged specs after each row
  --fail-fast                      Stop on first failed row
  --validation-profile <profile>   standard|deep|deep+perf (default: standard)
  --perf-profile <profile>         off|sample|full (default: off; deep+perf defaults to sample)

  --site-id <id>                   Site id placement (default: sea-edge-02)
  --node-id <id>                   Node id label for report context (default: edge-1)
  --core-specs-dir <path>          Core specs dir for staged route files
  --core-envoy-config <path>       Rendered Envoy config file
  --edge-local-caddy-file <path>   Edge-local rendered Caddy file

  --core-ingress-url <url>         Core ingress URL (HTTP)
  --core-ingress-tls-url <url>     Core ingress TLS URL
  --core-public-ingress-url <url>  Core ingress URL for core-to-edge-public (default: core ingress TLS URL)
  --edge-local-listener-url <url>  Optional edge-local listener URL for HTTP checks
  --edge-backend-host <host>       Backend host used for direct edge probes (default: 127.0.0.1)
  --edge-backend-scheme <scheme>   Backend URL scheme (default: http)
  --core-proxy-local-addr <addr>   Core-proxy fixed tunnel local target (default: ${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081})

  --wait-timeout <seconds>         Reconcile wait timeout per row (default: 90)
  --ready-timeout <seconds>        Workload ready timeout per row (default: 180)
  --stability-requests <n>         Requests for stability check (default: 30)
  --large-payload-path <path>      Path used for large-payload assertion (default: /)
  --large-payload-min-bytes <n>    Minimum bytes expected for large-payload assertion (default: 65536)
  --http2-enforce-downstream-h2    Require HTTP/2 in postcheck for all modes (default: off)
  --ws-duration-seconds <n>        WebSocket soak duration for deep checks (default: 600)
  --ws-connections <n>             WebSocket connection count for deep checks (default: 50)
  --ws-heartbeat-seconds <n>       WebSocket heartbeat period (default: 5)
  --ws-min-connected-ratio <f>     Minimum connected_ratio for ws soak (0..1, default: 0.98)
  --ws-max-connect-failure-rate <f> Maximum connect failure rate for ws soak (0..1, default: 0.02)
  --ws-max-message-loss <n>        Maximum websocket message_loss (default: 0)
  --lb-sample-requests <n>         Requests used for lb distribution probe (default: 5000)
  --lb-min-backends <n>            Minimum distinct backends for lb probe (default: 2)
  --lb-max-skew-ratio <ratio>      Max allowed lb skew ratio (default: 0.35)
  --lb-proof-scope <scope>         LB distribution enforcement scope: auto|strict-all|edge-only|off (default: auto)
  --sticky-requests-per-client <n> Requests per sticky client (default: 100)
  --perf-duration-seconds <n>      Perf probe duration (default: 180)
  --perf-concurrency <n>           Perf probe concurrency (default: 50)
  --perf-rps-target <n>            Reserved perf target knob (default: 0 = unlimited)
  --perf-warmup-seconds <n>        Perf warmup duration before sampling (default: 20)
  --perf-min-rps <n>               Full-profile minimum requests/sec threshold (default: 0 disabled)
  --perf-max-p95-ms <n>            Full-profile maximum p95 latency threshold in ms (default: 0 disabled)
  --perf-max-p99-ms <n>            Full-profile maximum p99 latency threshold in ms (default: 0 disabled)
  --perf-max-error-rate <n>        Full-profile maximum error rate (0..1, default: 1)

  --results-dir <path>             Output directory for matrix artifacts
  --result-json <path>             Result JSON path
  -h, --help                       Show help
USAGE
}

log() {
  printf '[ingress-matrix] %s\n' "$*"
}

die() {
  printf '[ingress-matrix] ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

trim() {
  local s="${1:-}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

validate_mode() {
  case "$1" in
    core-proxy|core-to-edge-public|edge-local) ;;
    *) die "invalid mode '$1'" ;;
  esac
}

validate_archetype() {
  case "$1" in
    http-static|http-path-routing|http-multi-replica|http-multiport|http-redirect|http-large-payload|http2-unary|ws-echo|lb-distribution|sticky-cookie) ;;
    *) die "invalid archetype '$1'" ;;
  esac
}

split_csv() {
  local csv="$1"
  local -n out_ref="$2"
  local raw
  IFS=',' read -r -a raw <<<"$csv"
  out_ref=()
  local item trimmed
  for item in "${raw[@]}"; do
    trimmed="$(trim "$item")"
    [[ -n "$trimmed" ]] && out_ref+=("$trimmed")
  done
}

archetype_manifest() {
  case "$1" in
    http-static) printf '%s/specs/examples/ingress-matrix/http-static.yaml' "$ROOT_DIR" ;;
    http-path-routing) printf '%s/specs/examples/ingress-matrix/http-path-routing.yaml' "$ROOT_DIR" ;;
    http-multi-replica) printf '%s/specs/examples/ingress-matrix/http-multi-replica.yaml' "$ROOT_DIR" ;;
    http-multiport) printf '%s/specs/examples/ingress-matrix/http-multiport.yaml' "$ROOT_DIR" ;;
    http-redirect) printf '%s/specs/examples/ingress-matrix/http-redirect.yaml' "$ROOT_DIR" ;;
    http-large-payload) printf '%s/specs/examples/ingress-matrix/http-large-payload.yaml' "$ROOT_DIR" ;;
    http2-unary) printf '%s/specs/examples/ingress-matrix/http2-unary.yaml' "$ROOT_DIR" ;;
    ws-echo) printf '%s/specs/examples/ingress-matrix/ws-echo.yaml' "$ROOT_DIR" ;;
    lb-distribution) printf '%s/specs/examples/ingress-matrix/lb-distribution.yaml' "$ROOT_DIR" ;;
    sticky-cookie) printf '%s/specs/examples/ingress-matrix/sticky-cookie.yaml' "$ROOT_DIR" ;;
  esac
}

archetype_app_name() {
  case "$1" in
    http-static) printf 'ingress-matrix-static' ;;
    http-path-routing) printf 'ingress-matrix-path' ;;
    http-multi-replica) printf 'ingress-matrix-replicas' ;;
    http-multiport) printf 'ingress-matrix-multiport' ;;
    http-redirect) printf 'ingress-matrix-redirect' ;;
    http-large-payload) printf 'ingress-matrix-large' ;;
    http2-unary) printf 'ingress-matrix-http2' ;;
    ws-echo) printf 'ingress-matrix-ws' ;;
    lb-distribution) printf 'ingress-matrix-lb' ;;
    sticky-cookie) printf 'ingress-matrix-sticky' ;;
  esac
}

archetype_backend_port() {
  case "$1" in
    http-static) printf '18111' ;;
    http-path-routing) printf '18112' ;;
    http-multi-replica) printf '18113' ;;
    http-multiport) printf '18114' ;;
    http-redirect) printf '18115' ;;
    http-large-payload) printf '18116' ;;
    http2-unary) printf '18117' ;;
    ws-echo) printf '18118' ;;
    lb-distribution) printf '18119' ;;
    sticky-cookie) printf '18120' ;;
  esac
}

archetype_service_port() {
  printf '8080'
}

archetype_assertion_profile() {
  case "$1" in
    http-path-routing) printf 'path' ;;
    http-redirect) printf 'redirect' ;;
    http-large-payload) printf 'large-payload' ;;
    http2-unary) printf 'http2' ;;
    ws-echo) printf 'ws' ;;
    lb-distribution) printf 'lb' ;;
    sticky-cookie) printf 'sticky' ;;
    *) printf 'baseline' ;;
  esac
}

host_for_row() {
  local mode="$1"
  local archetype="$2"
  case "$mode" in
    core-proxy) printf '%s-core-proxy.home.arpa' "$archetype" ;;
    core-to-edge-public) printf '%s-public.home.arpa' "$archetype" ;;
    edge-local) printf '%s-edge-local.home.arpa' "$archetype" ;;
  esac
}

route_name_for_row() {
  local mode="$1"
  local archetype="$2"
  local mode_slug="${mode//[^a-z0-9-]/-}"
  printf 'ingress-%s-%s' "$archetype" "$mode_slug"
}

policy_name_for_archetype() {
  case "$1" in
    ws-echo) printf 'ingress-matrix-ws-policy' ;;
    lb-distribution) printf 'ingress-matrix-lb-policy' ;;
    sticky-cookie) printf 'ingress-matrix-sticky-policy' ;;
    *) printf '' ;;
  esac
}

policy_manifest_for_archetype() {
  local archetype="$1"
  local variant="${2:-base}"
  case "$archetype:$variant" in
    ws-echo:base) printf '%s/specs/examples/ingress-matrix/policies/ws-enabled.yaml' "$ROOT_DIR" ;;
    lb-distribution:base) printf '%s/specs/examples/ingress-matrix/policies/lb-round-robin.yaml' "$ROOT_DIR" ;;
    lb-distribution:least_request) printf '%s/specs/examples/ingress-matrix/policies/lb-least-request.yaml' "$ROOT_DIR" ;;
    sticky-cookie:base) printf '%s/specs/examples/ingress-matrix/policies/sticky-cookie.yaml' "$ROOT_DIR" ;;
    *) printf '' ;;
  esac
}

render_route_file() {
  local mode="$1"
  local archetype="$2"
  local app_name="$3"
  local host="$4"
  local out="$5"
  local route_name
  local service_port
  local policy_name
  route_name="$(route_name_for_row "$mode" "$archetype")"
  service_port="$(archetype_service_port "$archetype")"
  policy_name="$(policy_name_for_archetype "$archetype")"

  {
    cat <<EOF
apiVersion: k1s.io/v1
kind: EdgeIngressRoute
metadata:
  name: ${route_name}
  namespace: default
spec:
  host: ${host}
  paths:
EOF
    if [[ "$archetype" == "http-path-routing" ]]; then
      cat <<EOF
    - path: /api
      serviceRef:
        namespace: default
        name: ${app_name}
        port: ${service_port}
    - path: /healthz
      serviceRef:
        namespace: default
        name: ${app_name}
        port: ${service_port}
EOF
    else
      cat <<EOF
    - path: /
      serviceRef:
        namespace: default
        name: ${app_name}
        port: ${service_port}
EOF
    fi
    cat <<EOF
  exposure:
    mode: ${mode}
    placement:
      site: ${SITE_ID}
    tls:
      mode: terminate-core
      terminateCore:
        redirectHttpToHttps: true
EOF
    if [[ -n "$policy_name" ]]; then
      cat <<EOF
  policyRef:
    name: ${policy_name}
    namespace: default
EOF
    fi
  } > "$out"
}

core_proxy_target_port() {
  local addr="$1"
  local port="${addr##*:}"
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  (( port >= 1 && port <= 65535 )) || return 1
  printf '%s\n' "$port"
}

rewrite_manifest_for_core_proxy() {
  local input_manifest="$1"
  local output_manifest="$2"
  local target_port="$3"
  python - "$input_manifest" "$output_manifest" "$target_port" <<'PY'
import sys
from pathlib import Path

import yaml

in_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
target_port = int(sys.argv[3])

doc = yaml.safe_load(in_path.read_text(encoding="utf-8")) or {}
spec = doc.get("spec")
if not isinstance(spec, dict):
    raise SystemExit(f"manifest missing spec: {in_path}")
service = spec.get("service")
if not isinstance(service, dict):
    raise SystemExit(f"manifest missing spec.service: {in_path}")

if isinstance(service.get("port"), int):
    service["port"] = target_port
elif isinstance(service.get("ports"), list):
    ports = service["ports"]
    updated = False
    for item in ports:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        if name in {"http", "web", ""}:
            item["port"] = target_port
            updated = True
            break
    if not updated:
        for item in ports:
            if isinstance(item, dict):
                item["port"] = target_port
                updated = True
                break
    if not updated:
        raise SystemExit(f"manifest spec.service.ports is empty/unusable: {in_path}")
else:
    raise SystemExit(f"manifest has unsupported spec.service shape: {in_path}")

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(
    yaml.safe_dump(doc, sort_keys=False),
    encoding="utf-8",
)
PY
}

fetch_code() {
  local url="$1"
  local host="$2"
  local code="000"
  if code="$(curl -sS -k --connect-timeout 2 --max-time 5 -o /dev/null -w '%{http_code}' -H "Host: $host" "$url" 2>/dev/null)"; then
    :
  else
    code="000"
  fi
  printf '%s\n' "$code"
}

mode_base_url() {
  local mode="$1"
  case "$mode" in
    core-proxy) printf '%s' "$CORE_INGRESS_URL" ;;
    core-to-edge-public) printf '%s' "$CORE_PUBLIC_INGRESS_URL" ;;
    edge-local) printf '%s' "$EDGE_LOCAL_LISTENER_URL" ;;
    *) printf '' ;;
  esac
}

mode_tls_url() {
  local mode="$1"
  case "$mode" in
    core-proxy|core-to-edge-public) printf '%s' "$CORE_INGRESS_TLS_URL" ;;
    edge-local) printf '%s' "$EDGE_LOCAL_LISTENER_URL" ;;
    *) printf '' ;;
  esac
}

assert_code_2xx_or_3xx() {
  local url="$1"
  local host="$2"
  local code
  code="$(fetch_code "$url" "$host")"
  [[ "$code" =~ ^[23][0-9][0-9]$ ]] || return 1
  return 0
}

stability_check() {
  local url="$1"
  local host="$2"
  local count="$3"
  local i code
  local failures=0
  for ((i = 1; i <= count; i++)); do
    code="$(fetch_code "$url" "$host")"
    if [[ ! "$code" =~ ^[23][0-9][0-9]$ ]]; then
      failures=$((failures + 1))
    fi
  done
  if (( failures > 0 )); then
    return 1
  fi
  return 0
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
  return 1
}

mode_selected() {
  local target="$1"
  local mode
  for mode in "${MODES[@]}"; do
    [[ "$mode" == "$target" ]] && return 0
  done
  return 1
}

listener_present() {
  local port="$1"
  ss -ltn 2>/dev/null | rg -q ":${port}\b"
}

core_proxy_transport_preflight() {
  if ! mode_selected "core-proxy"; then
    return 0
  fi

  need_cmd ss

  # Bootstrap-only preflight: full core-proxy transport (10443/2333/18080)
  # may be materialized during row staging/reconcile.
  if ! listener_present "10080"; then
    die "core-proxy bootstrap preflight failed: missing listener 10080"
  fi

  log "core-proxy bootstrap preflight passed listener=10080"
}

etcd_watchdog_preflight() {
  local planned_rows=$(( ${#MODES[@]} * ${#ARCHETYPES[@]} ))
  local run_watchdog=0

  if [[ "$VALIDATION_PROFILE" == "deep" || "$VALIDATION_PROFILE" == "deep+perf" ]]; then
    run_watchdog=1
  elif (( planned_rows > 1 )); then
    run_watchdog=1
  fi

  if (( run_watchdog == 0 )); then
    return 0
  fi

  case "${AE_ETCD_MAINTENANCE_ENABLE:-1}" in
    1|true|yes|on) ;;
    *)
      log "etcd watchdog preflight skipped (AE_ETCD_MAINTENANCE_ENABLE=${AE_ETCD_MAINTENANCE_ENABLE:-0})"
      return 0
      ;;
  esac

  if [[ ! -x "$ETCD_MAINTENANCE_SCRIPT" ]]; then
    log "etcd watchdog preflight skipped (helper missing: $ETCD_MAINTENANCE_SCRIPT)"
    return 0
  fi

  log "running etcd watchdog preflight"
  "$ETCD_MAINTENANCE_SCRIPT" watchdog || die "etcd watchdog preflight failed"
}

lb_distribution_required() {
  local mode="$1"
  case "$LB_PROOF_SCOPE" in
    strict-all)
      return 0
      ;;
    off)
      return 1
      ;;
    edge-only|auto)
      if [[ "$mode" == "edge-local" ]]; then
        return 0
      fi
      return 1
      ;;
    *)
      return 1
      ;;
  esac
}

lb_effective_endpoint_count() {
  local mode="$1"
  local host="$2"
  if [[ "$mode" != "core-proxy" || ! -f "$CORE_ENVOY_CONFIG" ]]; then
    printf '0\n'
    return 0
  fi
  python - "$CORE_ENVOY_CONFIG" "$host" <<'PY'
import sys
from pathlib import Path
import yaml

cfg_path = Path(sys.argv[1])
host = sys.argv[2]

try:
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
except Exception:
    print("0")
    raise SystemExit(0)

listeners = (data.get("static_resources") or {}).get("listeners") or []
cluster_name = ""

for listener in listeners:
    for chain in listener.get("filter_chains") or []:
        for flt in chain.get("filters") or []:
            tc = flt.get("typed_config") or {}
            rc = tc.get("route_config") or {}
            for vh in rc.get("virtual_hosts") or []:
                domains = vh.get("domains") or []
                if host not in domains and "*" not in domains:
                    continue
                for route in vh.get("routes") or []:
                    r = route.get("route") or {}
                    name = str(r.get("cluster") or "").strip()
                    if name:
                        cluster_name = name
                        break
                if cluster_name:
                    break
            if cluster_name:
                break
        if cluster_name:
            break
    if cluster_name:
        break

if not cluster_name:
    print("0")
    raise SystemExit(0)

clusters = (data.get("static_resources") or {}).get("clusters") or []
for cluster in clusters:
    if str(cluster.get("name") or "").strip() != cluster_name:
        continue
    endpoints = (
        (cluster.get("load_assignment") or {})
        .get("endpoints", [{}])[0]
        .get("lb_endpoints", [])
    )
    print(str(len(endpoints)))
    raise SystemExit(0)

print("0")
PY
}

edge_local_lb_dns_fallback_present() {
  local caddy_file="$1"
  [[ -f "$caddy_file" ]] || return 1
  rg -n --quiet "reverse_proxy[[:space:]]+ingress-matrix-lb\.default[^[:space:]]*:8080" "$caddy_file"
}

json_merge_objects() {
  local left="${1-}"
  local right="${2-}"
  [[ -n "$left" ]] || left="{}"
  [[ -n "$right" ]] || right="{}"
  python - "$left" "$right" <<'PY'
import json
import sys

def as_dict(raw: str):
    try:
        value = json.loads(raw or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}

left = as_dict(sys.argv[1])
right = as_dict(sys.argv[2])
for key, value in right.items():
    if key in left and isinstance(left[key], dict) and isinstance(value, dict):
        left[key].update(value)
    else:
        left[key] = value
print(json.dumps(left, separators=(",", ":"), sort_keys=True))
PY
}

write_postcheck_outputs() {
  local row_tmp_dir="${1:-}"
  if [[ -z "$row_tmp_dir" || ! -d "$row_tmp_dir" ]]; then
    return 0
  fi
  printf '%s' "$POSTCHECK_EVIDENCE_JSON" > "$row_tmp_dir/postcheck-evidence.json"
  printf '%s' "$POSTCHECK_PERF_JSON" > "$row_tmp_dir/postcheck-perf.json"
}

run_deep_probe() {
  local row_log="$1"
  shift
  python "$DEEP_PROBE_SCRIPT" "$@" 2>>"$row_log"
}

perf_full_verdict() {
  local perf_json="$1"
  local min_rps="$2"
  local max_p95="$3"
  local max_p99="$4"
  local max_error_rate="$5"
  python - "$perf_json" "$min_rps" "$max_p95" "$max_p99" "$max_error_rate" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1] or "{}")
min_rps = float(sys.argv[2])
max_p95 = float(sys.argv[3])
max_p99 = float(sys.argv[4])
max_error_rate = float(sys.argv[5])
rps = float(payload.get("rps", 0.0))
p95 = float((payload.get("latency") or {}).get("p95_ms", 0.0))
p99 = float((payload.get("latency") or {}).get("p99_ms", 0.0))
error_rate = float(payload.get("error_rate", 1.0))
ok = True
if min_rps > 0:
    ok = ok and rps >= min_rps
if max_p95 > 0:
    ok = ok and p95 <= max_p95
if max_p99 > 0:
    ok = ok and p99 <= max_p99
if max_error_rate >= 0:
    ok = ok and error_rate <= max_error_rate
print("1" if ok else "0")
PY
}

ws_deep_verdict() {
  local ws_json="$1"
  local min_connected_ratio="$2"
  local max_connect_failure_rate="$3"
  local max_message_loss="$4"
  python - "$ws_json" "$min_connected_ratio" "$max_connect_failure_rate" "$max_message_loss" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1] or "{}")
min_connected_ratio = float(sys.argv[2])
max_connect_failure_rate = float(sys.argv[3])
max_message_loss = int(float(sys.argv[4]))

attempted = int(payload.get("attempted_connections") or 0)
failures = int(payload.get("connect_failures") or 0)
connected_ratio = float(payload.get("connected_ratio") or 0.0)
message_loss = int(payload.get("message_loss") or 0)
failure_rate = (float(failures) / float(attempted)) if attempted > 0 else 1.0

ok = True
ok = ok and connected_ratio >= min_connected_ratio
ok = ok and failure_rate <= max_connect_failure_rate
ok = ok and message_loss <= max_message_loss
print("1" if ok else "0")
PY
}

postcheck_archetype() {
  local mode="$1"
  local archetype="$2"
  local host="$3"
  local row_tmp_dir="${4:-}"
  local row_log="${5:-/dev/null}"
  local profile
  profile="$(archetype_assertion_profile "$archetype")"
  POSTCHECK_EVIDENCE_JSON="{}"
  POSTCHECK_PERF_JSON="{}"
  write_postcheck_outputs "$row_tmp_dir"

  if [[ "$mode" == "edge-local" && -z "$EDGE_LOCAL_LISTENER_URL" ]]; then
    [[ -f "$EDGE_LOCAL_CADDY_FILE" ]] || return 1
    rg -n --fixed-strings --quiet "$host" "$EDGE_LOCAL_CADDY_FILE" || return 1
    if [[ "$profile" == "path" ]]; then
      rg -n --fixed-strings --quiet "/api" "$EDGE_LOCAL_CADDY_FILE" || return 1
      rg -n --fixed-strings --quiet "/healthz" "$EDGE_LOCAL_CADDY_FILE" || return 1
    fi
    return 0
  fi

  local base_url
  base_url="$(mode_base_url "$mode")"

  [[ -n "$base_url" ]] || return 1

  case "$profile" in
    baseline)
      stability_check "$base_url" "$host" "$STABILITY_REQUESTS" || return 1
      ;;
    path)
      local api_url
      api_url="${base_url%/}/api"
      stability_check "$api_url" "$host" "$STABILITY_REQUESTS" || return 1
      local path
      for path in "/api" "/healthz"; do
        assert_code_2xx_or_3xx "${base_url%/}${path}" "$host" || return 1
      done
      ;;
    redirect)
      local code redirect_url
      redirect_url="$base_url"
      if [[ "$mode" != "edge-local" ]]; then
        redirect_url="$CORE_INGRESS_URL"
      fi
      code="$(fetch_code "$redirect_url" "$host")"
      [[ "$code" =~ ^3[0-9][0-9]$ ]] || return 1
      ;;
    large-payload)
      local payload_url tls_url size
      tls_url="$(mode_tls_url "$mode")"
      payload_url="${tls_url:-$base_url}"
      local body_seed
      body_seed="$(mktemp)"
      head -c 131072 /dev/zero | tr '\0' 'A' > "$body_seed"
      local tmp_payload
      tmp_payload="$(mktemp)"
      if ! curl -sS -k --connect-timeout 2 --max-time 20 -X POST \
        -H "Host: $host" \
        --data-binary "@$body_seed" \
        "${payload_url%/}${LARGE_PAYLOAD_PATH}" \
        -o "$tmp_payload" >/dev/null 2>&1; then
        rm -f "$body_seed"
        rm -f "$tmp_payload"
        return 1
      fi
      rm -f "$body_seed"
      size="$(wc -c < "$tmp_payload" | tr -d ' ')"
      rm -f "$tmp_payload"
      [[ "$size" =~ ^[0-9]+$ ]] || return 1
      (( size >= LARGE_PAYLOAD_MIN_BYTES )) || return 1
      ;;
    http2)
      local tls_url
      tls_url="$(mode_tls_url "$mode")"
      [[ -n "$tls_url" ]] || return 1
      local meta code version
      meta="$(curl -sS -k --http2 --connect-timeout 2 --max-time 10 -o /dev/null -w '%{http_code} %{http_version}' -H "Host: $host" "$tls_url" 2>/dev/null || true)"
      if [[ -z "$meta" ]]; then
        log "http2 postcheck probe failed host=$host mode=$mode url=$tls_url (empty curl metadata)"
        return 1
      fi
      code="$(awk '{print $1}' <<<"$meta")"
      version="$(awk '{print $2}' <<<"$meta")"
      if [[ ! "$code" =~ ^[23][0-9][0-9]$ ]]; then
        log "http2 postcheck failed host=$host mode=$mode url=$tls_url code=$code version=${version:-unknown} expected=2xx/3xx"
        return 1
      fi
      if [[ "$mode" == "core-proxy" && "$HTTP2_ENFORCE_DOWNSTREAM_H2" -ne 1 ]]; then
        if [[ "$version" != "2" && "$version" != "1.1" ]]; then
          log "http2 postcheck failed host=$host mode=$mode url=$tls_url code=$code version=${version:-unknown} expected=2|1.1 enforce_downstream_h2=$HTTP2_ENFORCE_DOWNSTREAM_H2"
          return 1
        fi
      else
        if [[ "$version" != "2" ]]; then
          log "http2 postcheck failed host=$host mode=$mode url=$tls_url code=$code version=${version:-unknown} expected=2 enforce_downstream_h2=$HTTP2_ENFORCE_DOWNSTREAM_H2"
          return 1
        fi
      fi
      ;;
    ws)
      local ws_probe_base
      ws_probe_base="$(mode_tls_url "$mode")"
      [[ -n "$ws_probe_base" ]] || ws_probe_base="$base_url"
      assert_code_2xx_or_3xx "${ws_probe_base%/}/healthz" "$host" || return 1
      ;;
    lb|sticky)
      local deep_probe_base
      deep_probe_base="$(mode_tls_url "$mode")"
      [[ -n "$deep_probe_base" ]] || deep_probe_base="$base_url"
      assert_code_2xx_or_3xx "${deep_probe_base%/}/id" "$host" || return 1
      ;;
    *)
      return 1
      ;;
  esac

  if [[ "$VALIDATION_PROFILE" != "standard" ]]; then
    local probe_base_url probe_tls_url
    probe_base_url="$(mode_base_url "$mode")"
    probe_tls_url="$(mode_tls_url "$mode")"
    local probe_url="${probe_tls_url:-$probe_base_url}"

    case "$profile" in
      ws)
        local ws_json ws_rc=0
        ws_json="$(run_deep_probe "$row_log" ws_soak \
          --url "${probe_url%/}/ws" \
          --host "$host" \
          --duration-seconds "$WS_DURATION_SECONDS" \
          --connections "$WS_CONNECTIONS" \
          --heartbeat-seconds "$WS_HEARTBEAT_SECONDS")" || ws_rc=$?
        if [[ -n "$ws_json" ]]; then
          POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "{\"ws\":$ws_json}")"
          write_postcheck_outputs "$row_tmp_dir"
        fi
        if [[ "$ws_rc" -ne 0 ]]; then
          log "deep ws probe failed host=$host mode=$mode output=${ws_json:-<empty>}"
          return 1
        fi
        local ws_ok
        ws_ok="$(ws_deep_verdict "$ws_json" "$WS_MIN_CONNECTED_RATIO" "$WS_MAX_CONNECT_FAILURE_RATE" "$WS_MAX_MESSAGE_LOSS")"
        if [[ "$ws_ok" != "1" ]]; then
          log "deep ws threshold check failed host=$host mode=$mode min_connected_ratio=$WS_MIN_CONNECTED_RATIO max_connect_failure_rate=$WS_MAX_CONNECT_FAILURE_RATE max_message_loss=$WS_MAX_MESSAGE_LOSS output=${ws_json:-<empty>}"
          return 1
        fi
        ;;
      lb)
        local strict_distribution_required=0
        if lb_distribution_required "$mode"; then
          strict_distribution_required=1
        fi
        if [[ "$mode" == "edge-local" && "$strict_distribution_required" -eq 1 ]]; then
          if edge_local_lb_dns_fallback_present "$EDGE_LOCAL_CADDY_FILE"; then
            POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "{\"lb\":{\"fail_fast_reason\":\"edge_local_dns_fallback_upstream\",\"edge_local_caddy_file\":\"$EDGE_LOCAL_CADDY_FILE\"}}")"
            write_postcheck_outputs "$row_tmp_dir"
            log "deep lb precheck failed host=$host mode=$mode: rendered edge-local Caddy uses DNS fallback upstream (ingress-matrix-lb.default:8080); expected bundle endpoint fanout"
            return 1
          fi
        fi
        local effective_endpoint_count
        effective_endpoint_count="$(lb_effective_endpoint_count "$mode" "$host" | tr -d '[:space:]')"
        [[ "$effective_endpoint_count" =~ ^[0-9]+$ ]] || effective_endpoint_count="0"
        local lb_meta_json
        lb_meta_json="$(python - "$LB_PROOF_SCOPE" "$strict_distribution_required" "$effective_endpoint_count" "$mode" <<'PY'
import json
import sys
scope = sys.argv[1]
required = bool(int(sys.argv[2]))
endpoint_count = int(sys.argv[3])
mode = sys.argv[4]
assertion_level = "strict_distribution" if required else "policy_switch_only"
print(
    json.dumps(
        {
            "lb": {
                "proof_scope": scope,
                "strict_distribution_required": required,
                "effective_endpoint_count": endpoint_count,
                "mode": mode,
                "assertion_level": assertion_level,
                "observed_via_core": mode == "core-proxy",
                "observed_backend_count_from_core": 0,
                "observed_distribution_ok_from_core": False,
                "observed_backend_identity_header_hits": 0,
                "observed_backend_identity_body_hits": 0,
                "observed_from_header": False,
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    )
)
PY
)"
        POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "$lb_meta_json")"
        write_postcheck_outputs "$row_tmp_dir"

        local rr_min_backends="$LB_MIN_BACKENDS"
        local rr_require_distribution=0
        if [[ "$strict_distribution_required" -eq 0 ]]; then
          rr_min_backends=1
        else
          rr_require_distribution=1
        fi
        local lb_rr_json
        local lb_rr_rc=0
        local -a rr_probe_args=(
          lb_sample
          --url "${probe_url%/}/id"
          --host "$host"
          --strategy round_robin
          --requests "$LB_SAMPLE_REQUESTS"
          --min-backends "$rr_min_backends"
          --max-skew-ratio "$LB_MAX_SKEW_RATIO"
        )
        if [[ "$rr_require_distribution" -eq 1 ]]; then
          rr_probe_args+=(--require-distribution)
        fi
        lb_rr_json="$(run_deep_probe "$row_log" "${rr_probe_args[@]}")" || lb_rr_rc=$?
        local lb_wrap=""
        if [[ -n "$lb_rr_json" ]]; then
          lb_wrap="$(python - "$lb_rr_json" <<'PY'
import json
import sys
print(json.dumps({"lb": {"round_robin": json.loads(sys.argv[1])}}, separators=(",", ":"), sort_keys=True))
PY
)"
          POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "$lb_wrap")"
          local lb_observability_json=""
          lb_observability_json="$(python - "$lb_rr_json" "$mode" <<'PY'
import json
import sys
rr = json.loads(sys.argv[1] or "{}")
mode = str(sys.argv[2] or "")
identity = rr.get("backend_identity")
header_hits = 0
body_hits = 0
if isinstance(identity, dict):
    try:
        header_hits = int(identity.get("header_hits") or 0)
    except Exception:
        header_hits = 0
    try:
        body_hits = int(identity.get("body_hits") or 0)
    except Exception:
        body_hits = 0
payload = {
    "lb": {
        "observed_via_core": mode == "core-proxy",
        "observed_backend_count_from_core": int(rr.get("backend_count") or 0) if mode == "core-proxy" else 0,
        "observed_distribution_ok_from_core": bool(rr.get("distribution_ok")) if mode == "core-proxy" else False,
        "observed_backend_identity_header_hits": header_hits if mode == "core-proxy" else 0,
        "observed_backend_identity_body_hits": body_hits if mode == "core-proxy" else 0,
        "observed_from_header": (header_hits > 0) if mode == "core-proxy" else False,
    }
}
print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
PY
)"
          POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "$lb_observability_json")"
          write_postcheck_outputs "$row_tmp_dir"
        fi
        if [[ "$lb_rr_rc" -ne 0 ]]; then
          log "deep lb round_robin probe failed host=$host mode=$mode output=${lb_rr_json:-<empty>}"
          return 1
        fi

        if [[ "$mode" == "core-proxy" ]]; then
          local base_manifest
          local least_manifest
          base_manifest="$(policy_manifest_for_archetype "$archetype" base)"
          least_manifest="$(policy_manifest_for_archetype "$archetype" least_request)"
          if [[ -n "$least_manifest" && -f "$least_manifest" && -n "$base_manifest" && -f "$base_manifest" ]]; then
            local policy_dst
            policy_dst="$CORE_SPECS_DIR/$(basename "$base_manifest")"
            log "deep lb check: staging least_request policy -> $policy_dst"
            cp "$least_manifest" "$policy_dst"
            if ! wait_for_pattern "$CORE_ENVOY_CONFIG" "lb_policy: LEAST_REQUEST" "$WAIT_TIMEOUT_S" present; then
              POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "{\"lb\":{\"policy_render_check\":{\"least_request\":false}}}")"
              write_postcheck_outputs "$row_tmp_dir"
              log "deep lb check: timed out waiting for LEAST_REQUEST policy in $CORE_ENVOY_CONFIG"
              cp "$base_manifest" "$policy_dst" || true
              return 1
            fi
            POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "{\"lb\":{\"policy_render_check\":{\"least_request\":true}}}")"
            write_postcheck_outputs "$row_tmp_dir"
            local lb_least_json=""
            local lb_least_rc=0
            if [[ "$strict_distribution_required" -eq 1 ]]; then
              local -a least_probe_args=(
                lb_sample
                --url "${probe_url%/}/id"
                --host "$host"
                --strategy least_request
                --requests "$LB_SAMPLE_REQUESTS"
                --min-backends "$rr_min_backends"
                --max-skew-ratio "$LB_MAX_SKEW_RATIO"
              )
              if [[ "$rr_require_distribution" -eq 1 ]]; then
                least_probe_args+=(--require-distribution)
              fi
              lb_least_json="$(run_deep_probe "$row_log" "${least_probe_args[@]}")" || lb_least_rc=$?
            else
              lb_least_json="$(python <<'PY'
import json
print(json.dumps({
    "probe": "lb_sample",
    "strategy": "least_request",
    "probe_skipped": True,
    "reason": "strict_distribution_not_required",
    "pass": True,
}, separators=(",", ":"), sort_keys=True))
PY
)"
            fi
            if [[ -n "$lb_least_json" ]]; then
              local lb_merge
              lb_merge="$(python - "$lb_least_json" <<'PY'
import json
import sys
print(json.dumps({"lb": {"least_request": json.loads(sys.argv[1])}}, separators=(",", ":"), sort_keys=True))
PY
)"
              POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "$lb_merge")"
              write_postcheck_outputs "$row_tmp_dir"
            fi
            cp "$base_manifest" "$policy_dst" || true
            wait_for_pattern "$CORE_ENVOY_CONFIG" "lb_policy: ROUND_ROBIN" "$WAIT_TIMEOUT_S" present || true
            if [[ "$lb_least_rc" -ne 0 ]]; then
              log "deep lb least_request probe failed host=$host mode=$mode output=${lb_least_json:-<empty>}"
              return 1
            fi
            local lb_compare_json
            lb_compare_json="$(python - "$lb_rr_json" "$lb_least_json" "$strict_distribution_required" <<'PY'
import json
import sys
rr = json.loads(sys.argv[1] or "{}")
lr = json.loads(sys.argv[2] or "{}")
strict_required = bool(int(sys.argv[3]))
payload = {
    "lb": {
        "policy_switch": {
            "strict_distribution_required": strict_required,
            "round_robin_backend_count": int(rr.get("backend_count") or 0),
            "least_request_backend_count": int(lr.get("backend_count") or 0),
            "round_robin_distribution_ok": bool(rr.get("distribution_ok")),
            "least_request_distribution_ok": bool(lr.get("distribution_ok")),
            "round_robin_max_skew_ratio": float(rr.get("max_skew_ratio") or 0.0),
            "least_request_max_skew_ratio": float(lr.get("max_skew_ratio") or 0.0),
            "pass": bool(rr.get("pass")) and bool(lr.get("pass")) if strict_required else bool(rr.get("pass")),
        }
    }
}
print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
PY
)"
            POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "$lb_compare_json")"
            write_postcheck_outputs "$row_tmp_dir"
          fi
        fi
        ;;
      sticky)
        local sticky_json sticky_rc=0
        sticky_json="$(run_deep_probe "$row_log" sticky_probe \
          --url "${probe_url%/}/id" \
          --host "$host" \
          --requests-per-client "$STICKY_REQUESTS_PER_CLIENT")" || sticky_rc=$?
        if [[ -n "$sticky_json" ]]; then
          POSTCHECK_EVIDENCE_JSON="$(json_merge_objects "$POSTCHECK_EVIDENCE_JSON" "{\"sticky\":$sticky_json}")"
          write_postcheck_outputs "$row_tmp_dir"
        fi
        if [[ "$sticky_rc" -ne 0 ]]; then
          log "deep sticky probe failed host=$host mode=$mode output=${sticky_json:-<empty>}"
          return 1
        fi
        ;;
    esac
  fi

  if [[ "$VALIDATION_PROFILE" == "deep+perf" && "$PERF_PROFILE" != "off" ]]; then
    local perf_probe_url perf_json
    perf_probe_url="$(mode_tls_url "$mode")"
    [[ -n "$perf_probe_url" ]] || perf_probe_url="$(mode_base_url "$mode")"
    local perf_rc=0
    perf_json="$(run_deep_probe "$row_log" http_bench \
      --url "${perf_probe_url%/}/id" \
      --host "$host" \
      --duration-seconds "$PERF_DURATION_SECONDS" \
      --warmup-seconds "$PERF_WARMUP_SECONDS" \
      --concurrency "$PERF_CONCURRENCY")" || perf_rc=$?
    if [[ "$perf_rc" -ne 0 ]]; then
      log "deep perf probe failed host=$host mode=$mode output=${perf_json:-<empty>}"
      return 1
    fi
    POSTCHECK_PERF_JSON="$(json_merge_objects "$POSTCHECK_PERF_JSON" "{\"http\":$perf_json}")"
    write_postcheck_outputs "$row_tmp_dir"
    if [[ "$PERF_PROFILE" == "full" ]]; then
      local perf_ok
      perf_ok="$(perf_full_verdict "$perf_json" "$PERF_MIN_RPS" "$PERF_MAX_P95_MS" "$PERF_MAX_P99_MS" "$PERF_MAX_ERROR_RATE")"
      if [[ "$perf_ok" != "1" ]]; then
        log "perf full-profile threshold check failed host=$host mode=$mode archetype=$archetype"
        return 1
      fi
    fi
  fi

  return 0
}

collect_failure_diagnostics() {
  local mode="$1"
  local archetype="$2"
  local host="$3"
  local row_dir="$4"
  local row_log="$5"
  local row_tmp_dir="$6"

  mkdir -p "$row_dir"
  cp "$row_log" "$row_dir/row.log"

  {
    echo "mode=$mode"
    echo "archetype=$archetype"
    echo "site_id=$SITE_ID"
    echo "node_id=$NODE_ID"
    echo "host=$host"
    echo "core_specs_dir=$CORE_SPECS_DIR"
    echo "core_envoy_config=$CORE_ENVOY_CONFIG"
    echo "edge_local_caddy_file=$EDGE_LOCAL_CADDY_FILE"
  } > "$row_dir/context.txt"

  if command -v crictl >/dev/null 2>&1; then
    if sudo -n true >/dev/null 2>&1; then
      sudo -n crictl pods > "$row_dir/crictl-pods.txt" 2>&1 || true
      sudo -n crictl ps -a > "$row_dir/crictl-ps.txt" 2>&1 || true
    else
      crictl pods > "$row_dir/crictl-pods.txt" 2>&1 || true
      crictl ps -a > "$row_dir/crictl-ps.txt" 2>&1 || true
    fi
  fi

  [[ -f "$CORE_ENVOY_CONFIG" ]] && cp "$CORE_ENVOY_CONFIG" "$row_dir/envoy.yaml" || true
  [[ -f "$EDGE_LOCAL_CADDY_FILE" ]] && cp "$EDGE_LOCAL_CADDY_FILE" "$row_dir/edge-local.caddy" || true
  if [[ -n "$row_tmp_dir" && -d "$row_tmp_dir" ]]; then
    cp -a "$row_tmp_dir/." "$row_dir/" 2>/dev/null || true
  fi

  local log_file
  for log_file in \
    "$ROOT_DIR/state/profiles/k1s-core/controller.log" \
    "$ROOT_DIR/state/profiles/k1s-core/gateway-sea-edge-02-edge-1.log" \
    "$ROOT_DIR/state/profiles/k1s-edge/gateway-sea-edge-02-edge-1.log"; do
    if [[ -f "$log_file" ]]; then
      cp "$log_file" "$row_dir/$(basename "$log_file")"
    fi
  done
}

append_result_row() {
  local mode="$1"
  local archetype="$2"
  local app_name="$3"
  local manifest="$4"
  local host="$5"
  local backend_port="$6"
  local status="$7"
  local duration_s="$8"
  local note="$9"
  local evidence_json="${10-}"
  local perf_json="${11-}"
  [[ -n "$evidence_json" ]] || evidence_json="{}"
  [[ -n "$perf_json" ]] || perf_json="{}"
  local evidence_b64 perf_b64
  evidence_b64="$(python - "$evidence_json" <<'PY'
import base64
import sys
raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
print(base64.b64encode(raw.encode("utf-8")).decode("ascii"))
PY
)"
  perf_b64="$(python - "$perf_json" <<'PY'
import base64
import sys
raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
print(base64.b64encode(raw.encode("utf-8")).decode("ascii"))
PY
)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$mode" "$archetype" "$app_name" "$manifest" "$host" "$backend_port" "$status" "$duration_s" "$note" "$evidence_b64" "$perf_b64" \
    >> "$RESULT_TSV"
}

write_json_result() {
  local out_json="$1"
  local in_tsv="$2"
  python - "$in_tsv" "$out_json" <<'PY'
import json
import sys
import base64
from pathlib import Path

in_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])

rows = []
for raw in in_path.read_text(encoding="utf-8").splitlines():
    if not raw.strip():
        continue
    parts = raw.split("\t")
    if len(parts) < 11:
        raise SystemExit(f"invalid result row (expected 11 columns): {raw!r}")
    mode, archetype, app_name, manifest, host, backend_port, status, duration_s, note, evidence_b64, perf_b64 = parts[:11]
    row = {
        "mode": mode,
        "archetype": archetype,
        "app_name": app_name,
        "manifest": manifest,
        "host": host,
        "backend_port": int(backend_port),
        "status": status,
        "duration_s": float(duration_s),
        "note": note,
    }
    try:
        evidence_json = base64.b64decode((evidence_b64 or "").encode("ascii"), validate=False).decode("utf-8")
    except Exception:
        evidence_json = "{}"
    try:
        evidence = json.loads(evidence_json or "{}")
    except Exception:
        evidence = {}
    if isinstance(evidence, dict) and evidence:
        row["evidence"] = evidence
    try:
        perf_json = base64.b64decode((perf_b64 or "").encode("ascii"), validate=False).decode("utf-8")
    except Exception:
        perf_json = "{}"
    try:
        perf = json.loads(perf_json or "{}")
    except Exception:
        perf = {}
    if isinstance(perf, dict) and perf:
        row["perf"] = perf
    rows.append(row)

def lb_evidence(row: dict) -> dict:
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        return {}
    lb = evidence.get("lb")
    if not isinstance(lb, dict):
        return {}
    return lb


lb_rows = [row for row in rows if row.get("archetype") == "lb-distribution"]
lb_policy_rows = [
    row for row in lb_rows if str(lb_evidence(row).get("assertion_level") or "") == "policy_switch_only"
]
lb_strict_rows = [
    row for row in lb_rows if str(lb_evidence(row).get("assertion_level") or "") == "strict_distribution"
]
lb_core_observability_rows = [
    row for row in lb_rows if str(lb_evidence(row).get("mode") or "") == "core-proxy"
]


def _core_lb_observable(row: dict) -> bool:
    if row.get("status") != "pass":
        return False
    lb = lb_evidence(row)
    if not bool(lb.get("observed_via_core")):
        return False
    try:
        backend_count = int(lb.get("observed_backend_count_from_core") or 0)
    except Exception:
        backend_count = 0
    return backend_count >= 1


summary = {
    "total_rows": len(rows),
    "passed_rows": sum(1 for row in rows if row["status"] == "pass"),
    "failed_rows": sum(1 for row in rows if row["status"] == "fail"),
    "deep_checks_passed": all(row.get("status") == "pass" for row in rows),
    "perf_collected": any("perf" in row for row in rows),
    "perf_passed": all((row.get("status") == "pass") for row in rows if "perf" in row),
    "lb_policy_rows": len(lb_policy_rows),
    "lb_policy_passed": all(row.get("status") == "pass" for row in lb_policy_rows),
    "lb_strict_rows": len(lb_strict_rows),
    "lb_strict_proof_passed": (
        bool(lb_strict_rows)
        and all(row.get("status") == "pass" for row in lb_strict_rows)
    ),
    "lb_observability_rows": len(lb_core_observability_rows),
    "lb_observability_passed": all(_core_lb_observable(row) for row in lb_core_observability_rows),
}

payload = {"summary": summary, "rows": rows}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

run_row() {
  local mode="$1"
  local archetype="$2"
  local app_name="$3"
  local manifest="$4"
  local backend_port="$5"
  local host="$6"
  local row_tmp_dir="$7"
  local row_log="$8"
  POSTCHECK_EVIDENCE_JSON="{}"
  POSTCHECK_PERF_JSON="{}"

  local route_src="$row_tmp_dir/route-${mode}-${archetype}.yaml"
  render_route_file "$mode" "$archetype" "$app_name" "$host" "$route_src"
  local policy_manifest
  policy_manifest="$(policy_manifest_for_archetype "$archetype" base)"

  local manifest_for_row="$manifest"
  local effective_backend_port="$backend_port"
  if [[ "$mode" == "core-proxy" ]]; then
    local target_port
    target_port="$(core_proxy_target_port "$CORE_PROXY_LOCAL_ADDR")" || die "invalid --core-proxy-local-addr '$CORE_PROXY_LOCAL_ADDR' (expected host:port)"
    manifest_for_row="$row_tmp_dir/workload-${mode}-${archetype}.yaml"
    rewrite_manifest_for_core_proxy "$manifest" "$manifest_for_row" "$target_port"
    effective_backend_port="$target_port"
  fi

  local backend_url="${EDGE_BACKEND_SCHEME}://${EDGE_BACKEND_HOST}:${effective_backend_port}/"
  local core_proxy_http_path="/"
  local core_proxy_tls_path="/"
  local core_public_http_path="/"
  if [[ "$archetype" == "http-path-routing" ]]; then
    core_proxy_http_path="/api"
    core_proxy_tls_path="/api"
    core_public_http_path="/api"
  fi
  local -a cmd=(
    "$ROOT_DIR/scripts/dev/test_ingress_modes_single_host.sh"
    --mode "$mode"
    --tier "$TIER"
    --site-id "$SITE_ID"
    --app-name "$app_name"
    --app-manifest "$manifest_for_row"
    --core-specs-dir "$CORE_SPECS_DIR"
    --core-envoy-config "$CORE_ENVOY_CONFIG"
    --edge-local-caddy-file "$EDGE_LOCAL_CADDY_FILE"
    --core-ingress-url "$CORE_INGRESS_URL"
    --core-ingress-tls-url "$CORE_INGRESS_TLS_URL"
    --edge-backend-url "$backend_url"
    --wait-timeout "$WAIT_TIMEOUT_S"
    --ready-timeout "$READY_TIMEOUT_S"
  )
  if [[ -n "$policy_manifest" ]]; then
    [[ -f "$policy_manifest" ]] || die "missing policy manifest: $policy_manifest"
    cmd+=(--policy-manifest "$policy_manifest")
  fi

  case "$mode" in
    core-proxy)
      cmd+=(--core-proxy-route-src "$route_src")
      cmd+=(--core-proxy-http-path "$core_proxy_http_path")
      cmd+=(--core-proxy-tls-path "$core_proxy_tls_path")
      ;;
    core-to-edge-public)
      cmd+=(--core-ingress-url "$CORE_PUBLIC_INGRESS_URL")
      cmd+=(--core-to-edge-public-route-src "$route_src")
      cmd+=(--core-public-http-path "$core_public_http_path")
      cmd+=(--public-good-url "$backend_url")
      ;;
    edge-local)
      cmd+=(--edge-local-route-src "$route_src")
      if [[ -n "$EDGE_LOCAL_LISTENER_URL" ]]; then
        cmd+=(--edge-local-listener-url "$EDGE_LOCAL_LISTENER_URL")
      fi
      ;;
  esac

  (( STRICT == 1 )) && cmd+=(--strict)
  (( KEEP_SPECS == 1 )) && cmd+=(--keep-specs)

  log "row start mode=$mode archetype=$archetype host=$host backend_port=$effective_backend_port"
  if [[ "$mode" == "core-proxy" && "$effective_backend_port" != "$backend_port" ]]; then
    log "core-proxy row normalized service port from $backend_port to fixed local target $effective_backend_port"
  fi
  if "${cmd[@]}" 2>&1 | tee "$row_log"; then
    local postcheck_rc=0
    postcheck_archetype "$mode" "$archetype" "$host" "$row_tmp_dir" "$row_log" || postcheck_rc=$?
    if [[ -f "$row_tmp_dir/postcheck-evidence.json" ]]; then
      POSTCHECK_EVIDENCE_JSON="$(cat "$row_tmp_dir/postcheck-evidence.json")"
    fi
    if [[ -f "$row_tmp_dir/postcheck-perf.json" ]]; then
      POSTCHECK_PERF_JSON="$(cat "$row_tmp_dir/postcheck-perf.json")"
    fi
    if [[ "$postcheck_rc" -eq 0 ]]; then
      log "row pass mode=$mode archetype=$archetype"
      return 0
    fi
    echo "[ingress-matrix] postcheck failed mode=$mode archetype=$archetype host=$host" | tee -a "$row_log"
    return 1
  fi
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --modes)
      MODES_CSV="${2:-}"
      shift 2
      ;;
    --archetypes)
      ARCHETYPES_CSV="${2:-}"
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
    --keep-specs)
      KEEP_SPECS=1
      shift
      ;;
    --fail-fast)
      FAIL_FAST=1
      shift
      ;;
    --validation-profile)
      VALIDATION_PROFILE="${2:-}"
      shift 2
      ;;
    --perf-profile)
      PERF_PROFILE="${2:-}"
      shift 2
      ;;
    --site-id)
      SITE_ID="${2:-}"
      shift 2
      ;;
    --node-id)
      NODE_ID="${2:-}"
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
    --core-public-ingress-url)
      CORE_PUBLIC_INGRESS_URL="${2:-}"
      shift 2
      ;;
    --edge-local-listener-url)
      EDGE_LOCAL_LISTENER_URL="${2:-}"
      shift 2
      ;;
    --edge-backend-host)
      EDGE_BACKEND_HOST="${2:-}"
      shift 2
      ;;
    --edge-backend-scheme)
      EDGE_BACKEND_SCHEME="${2:-}"
      shift 2
      ;;
    --core-proxy-local-addr)
      CORE_PROXY_LOCAL_ADDR="${2:-}"
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
    --stability-requests)
      STABILITY_REQUESTS="${2:-}"
      shift 2
      ;;
    --large-payload-path)
      LARGE_PAYLOAD_PATH="${2:-}"
      shift 2
      ;;
    --large-payload-min-bytes)
      LARGE_PAYLOAD_MIN_BYTES="${2:-}"
      shift 2
      ;;
    --http2-enforce-downstream-h2)
      HTTP2_ENFORCE_DOWNSTREAM_H2=1
      shift
      ;;
    --ws-duration-seconds)
      WS_DURATION_SECONDS="${2:-}"
      shift 2
      ;;
    --ws-connections)
      WS_CONNECTIONS="${2:-}"
      shift 2
      ;;
    --ws-heartbeat-seconds)
      WS_HEARTBEAT_SECONDS="${2:-}"
      shift 2
      ;;
    --ws-min-connected-ratio)
      WS_MIN_CONNECTED_RATIO="${2:-}"
      shift 2
      ;;
    --ws-max-connect-failure-rate)
      WS_MAX_CONNECT_FAILURE_RATE="${2:-}"
      shift 2
      ;;
    --ws-max-message-loss)
      WS_MAX_MESSAGE_LOSS="${2:-}"
      shift 2
      ;;
    --lb-sample-requests)
      LB_SAMPLE_REQUESTS="${2:-}"
      shift 2
      ;;
    --lb-min-backends)
      LB_MIN_BACKENDS="${2:-}"
      shift 2
      ;;
    --lb-max-skew-ratio)
      LB_MAX_SKEW_RATIO="${2:-}"
      shift 2
      ;;
    --lb-proof-scope)
      LB_PROOF_SCOPE="${2:-}"
      shift 2
      ;;
    --sticky-requests-per-client)
      STICKY_REQUESTS_PER_CLIENT="${2:-}"
      shift 2
      ;;
    --perf-duration-seconds)
      PERF_DURATION_SECONDS="${2:-}"
      shift 2
      ;;
    --perf-concurrency)
      PERF_CONCURRENCY="${2:-}"
      shift 2
      ;;
    --perf-rps-target)
      PERF_RPS_TARGET="${2:-}"
      shift 2
      ;;
    --perf-warmup-seconds)
      PERF_WARMUP_SECONDS="${2:-}"
      shift 2
      ;;
    --perf-min-rps)
      PERF_MIN_RPS="${2:-}"
      shift 2
      ;;
    --perf-max-p95-ms)
      PERF_MAX_P95_MS="${2:-}"
      shift 2
      ;;
    --perf-max-p99-ms)
      PERF_MAX_P99_MS="${2:-}"
      shift 2
      ;;
    --perf-max-error-rate)
      PERF_MAX_ERROR_RATE="${2:-}"
      shift 2
      ;;
    --results-dir)
      RESULTS_DIR="${2:-}"
      shift 2
      ;;
    --result-json)
      RESULT_JSON="${2:-}"
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

case "$TIER" in
  tier1|tier2|both) ;;
  *) die "--tier must be one of: tier1|tier2|both" ;;
esac

case "$EDGE_BACKEND_SCHEME" in
  http|https) ;;
  *) die "--edge-backend-scheme must be http or https" ;;
esac

case "$VALIDATION_PROFILE" in
  standard|deep|deep+perf) ;;
  *) die "--validation-profile must be one of: standard|deep|deep+perf" ;;
esac

case "$PERF_PROFILE" in
  off|sample|full) ;;
  *) die "--perf-profile must be one of: off|sample|full" ;;
esac

case "$LB_PROOF_SCOPE" in
  auto|strict-all|edge-only|off) ;;
  *) die "--lb-proof-scope must be one of: auto|strict-all|edge-only|off" ;;
esac

if [[ "$VALIDATION_PROFILE" == "deep+perf" && "$PERF_PROFILE" == "off" ]]; then
  PERF_PROFILE="sample"
fi

[[ "$LARGE_PAYLOAD_MIN_BYTES" =~ ^[0-9]+$ ]] || die "--large-payload-min-bytes must be an integer"
[[ "$HTTP2_ENFORCE_DOWNSTREAM_H2" =~ ^[01]$ ]] || die "HTTP2_ENFORCE_DOWNSTREAM_H2 must be 0 or 1"
[[ "$WS_DURATION_SECONDS" =~ ^[0-9]+$ ]] || die "--ws-duration-seconds must be an integer"
[[ "$WS_CONNECTIONS" =~ ^[0-9]+$ ]] || die "--ws-connections must be an integer"
[[ "$LB_SAMPLE_REQUESTS" =~ ^[0-9]+$ ]] || die "--lb-sample-requests must be an integer"
[[ "$LB_MIN_BACKENDS" =~ ^[0-9]+$ ]] || die "--lb-min-backends must be an integer"
[[ "$STICKY_REQUESTS_PER_CLIENT" =~ ^[0-9]+$ ]] || die "--sticky-requests-per-client must be an integer"
[[ "$PERF_DURATION_SECONDS" =~ ^[0-9]+$ ]] || die "--perf-duration-seconds must be an integer"
[[ "$PERF_CONCURRENCY" =~ ^[0-9]+$ ]] || die "--perf-concurrency must be an integer"
[[ "$PERF_RPS_TARGET" =~ ^[0-9]+$ ]] || die "--perf-rps-target must be an integer"
[[ "$PERF_WARMUP_SECONDS" =~ ^[0-9]+$ ]] || die "--perf-warmup-seconds must be an integer"
[[ "$PERF_MIN_RPS" =~ ^[0-9]+$ ]] || die "--perf-min-rps must be an integer"
[[ "$PERF_MAX_P95_MS" =~ ^[0-9]+$ ]] || die "--perf-max-p95-ms must be an integer"
[[ "$PERF_MAX_P99_MS" =~ ^[0-9]+$ ]] || die "--perf-max-p99-ms must be an integer"
[[ "$WS_MAX_MESSAGE_LOSS" =~ ^[0-9]+$ ]] || die "--ws-max-message-loss must be an integer"
(( WS_DURATION_SECONDS > 0 )) || die "--ws-duration-seconds must be > 0"
(( WS_CONNECTIONS > 0 )) || die "--ws-connections must be > 0"
(( LB_SAMPLE_REQUESTS > 0 )) || die "--lb-sample-requests must be > 0"
(( LB_MIN_BACKENDS > 0 )) || die "--lb-min-backends must be > 0"
(( STICKY_REQUESTS_PER_CLIENT > 0 )) || die "--sticky-requests-per-client must be > 0"
(( PERF_DURATION_SECONDS > 0 )) || die "--perf-duration-seconds must be > 0"
(( PERF_CONCURRENCY > 0 )) || die "--perf-concurrency must be > 0"

python - "$WS_HEARTBEAT_SECONDS" "$PERF_MAX_ERROR_RATE" "$LB_MAX_SKEW_RATIO" "$WS_MIN_CONNECTED_RATIO" "$WS_MAX_CONNECT_FAILURE_RATE" <<'PY'
import sys
heartbeat = float(sys.argv[1])
max_error = float(sys.argv[2])
lb_max_skew = float(sys.argv[3])
ws_min_connected_ratio = float(sys.argv[4])
ws_max_connect_failure_rate = float(sys.argv[5])
if heartbeat <= 0:
    raise SystemExit("--ws-heartbeat-seconds must be > 0")
if not (0.0 <= max_error <= 1.0):
    raise SystemExit("--perf-max-error-rate must be between 0 and 1")
if lb_max_skew < 0:
    raise SystemExit("--lb-max-skew-ratio must be >= 0")
if not (0.0 <= ws_min_connected_ratio <= 1.0):
    raise SystemExit("--ws-min-connected-ratio must be between 0 and 1")
if not (0.0 <= ws_max_connect_failure_rate <= 1.0):
    raise SystemExit("--ws-max-connect-failure-rate must be between 0 and 1")
PY

need_cmd rg
need_cmd curl
need_cmd python
if [[ "$VALIDATION_PROFILE" != "standard" || "$PERF_PROFILE" != "off" ]]; then
  [[ -f "$DEEP_PROBE_SCRIPT" ]] || die "missing deep probe script: $DEEP_PROBE_SCRIPT"
fi

mkdir -p "$RESULTS_DIR"
RESULT_TSV="$(mktemp)"
FAILURES_DIR="$RESULTS_DIR/failures/ingress-matrix-${RUN_STAMP}"
mkdir -p "$FAILURES_DIR"

declare -a MODES=()
declare -a ARCHETYPES=()
split_csv "$MODES_CSV" MODES
split_csv "$ARCHETYPES_CSV" ARCHETYPES

(( ${#MODES[@]} > 0 )) || die "no modes selected"
(( ${#ARCHETYPES[@]} > 0 )) || die "no archetypes selected"

for mode in "${MODES[@]}"; do
  validate_mode "$mode"
done
for archetype in "${ARCHETYPES[@]}"; do
  validate_archetype "$archetype"
done

log "modes=${MODES[*]}"
log "archetypes=${ARCHETYPES[*]}"
log "tier=$TIER site_id=$SITE_ID node_id=$NODE_ID"
log "core_ingress_url=$CORE_INGRESS_URL core_public_ingress_url=$CORE_PUBLIC_INGRESS_URL core_ingress_tls_url=$CORE_INGRESS_TLS_URL"
log "edge_backend=${EDGE_BACKEND_SCHEME}://${EDGE_BACKEND_HOST}:<dynamic-port>"
log "core_proxy_local_addr=$CORE_PROXY_LOCAL_ADDR"
log "validation_profile=$VALIDATION_PROFILE perf_profile=$PERF_PROFILE"
log "ws_duration_seconds=$WS_DURATION_SECONDS ws_connections=$WS_CONNECTIONS ws_heartbeat_seconds=$WS_HEARTBEAT_SECONDS ws_min_connected_ratio=$WS_MIN_CONNECTED_RATIO ws_max_connect_failure_rate=$WS_MAX_CONNECT_FAILURE_RATE ws_max_message_loss=$WS_MAX_MESSAGE_LOSS"
log "lb_sample_requests=$LB_SAMPLE_REQUESTS lb_min_backends=$LB_MIN_BACKENDS lb_max_skew_ratio=$LB_MAX_SKEW_RATIO lb_proof_scope=$LB_PROOF_SCOPE sticky_requests_per_client=$STICKY_REQUESTS_PER_CLIENT"
log "perf_duration_seconds=$PERF_DURATION_SECONDS perf_concurrency=$PERF_CONCURRENCY perf_warmup_seconds=$PERF_WARMUP_SECONDS perf_min_rps=$PERF_MIN_RPS perf_max_p95_ms=$PERF_MAX_P95_MS perf_max_p99_ms=$PERF_MAX_P99_MS perf_max_error_rate=$PERF_MAX_ERROR_RATE"
log "http2_enforce_downstream_h2=$HTTP2_ENFORCE_DOWNSTREAM_H2"
log "results_json=$RESULT_JSON"

etcd_watchdog_preflight
core_proxy_transport_preflight

total_rows=0
failed_rows=0

for mode in "${MODES[@]}"; do
  for archetype in "${ARCHETYPES[@]}"; do
    total_rows=$((total_rows + 1))
    manifest="$(archetype_manifest "$archetype")"
    app_name="$(archetype_app_name "$archetype")"
    backend_port="$(archetype_backend_port "$archetype")"
    host="$(host_for_row "$mode" "$archetype")"
    [[ -f "$manifest" ]] || die "missing archetype manifest: $manifest"

    row_tmp_dir="$(mktemp -d)"
    row_log="$row_tmp_dir/row.log"
    row_started="$(date +%s)"

    if run_row "$mode" "$archetype" "$app_name" "$manifest" "$backend_port" "$host" "$row_tmp_dir" "$row_log"; then
      row_status="pass"
      row_note=""
    else
      row_status="fail"
      row_note="see failure artifacts"
      failed_rows=$((failed_rows + 1))
      row_failure_dir="$FAILURES_DIR/${mode}/${archetype}"
      collect_failure_diagnostics "$mode" "$archetype" "$host" "$row_failure_dir" "$row_log" "$row_tmp_dir"
      if (( FAIL_FAST == 1 )); then
        row_finished="$(date +%s)"
        row_duration=$((row_finished - row_started))
        append_result_row "$mode" "$archetype" "$app_name" "$manifest" "$host" "$backend_port" "$row_status" "$row_duration" "$row_note" "$POSTCHECK_EVIDENCE_JSON" "$POSTCHECK_PERF_JSON"
        rm -rf "$row_tmp_dir"
        break 2
      fi
    fi

    row_finished="$(date +%s)"
    row_duration=$((row_finished - row_started))
    append_result_row "$mode" "$archetype" "$app_name" "$manifest" "$host" "$backend_port" "$row_status" "$row_duration" "$row_note" "$POSTCHECK_EVIDENCE_JSON" "$POSTCHECK_PERF_JSON"
    rm -rf "$row_tmp_dir"
  done
done

write_json_result "$RESULT_JSON" "$RESULT_TSV"
rm -f "$RESULT_TSV"

log "summary total_rows=$total_rows failed_rows=$failed_rows result_json=$RESULT_JSON"
if (( failed_rows > 0 )); then
  log "failure artifacts: $FAILURES_DIR"
  exit 1
fi

log "PASS ingress matrix"
