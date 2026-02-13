#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

MODES_CSV="${MODES_CSV:-core-proxy,core-to-edge-public,edge-local}"
ARCHETYPES_CSV="${ARCHETYPES_CSV:-http-static,http-path-routing,http-multi-replica,http-multiport,http-redirect,http-large-payload,http2-unary}"
TIER="${TIER:-tier1}"
STRICT=0
KEEP_SPECS=0
FAIL_FAST=0

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

WAIT_TIMEOUT_S="${WAIT_TIMEOUT_S:-90}"
READY_TIMEOUT_S="${READY_TIMEOUT_S:-180}"
STABILITY_REQUESTS="${STABILITY_REQUESTS:-30}"
LARGE_PAYLOAD_PATH="${LARGE_PAYLOAD_PATH:-/}"
LARGE_PAYLOAD_MIN_BYTES="${LARGE_PAYLOAD_MIN_BYTES:-65536}"

RESULTS_DIR="${RESULTS_DIR:-$ROOT_DIR/state/test-results}"
RUN_STAMP="${RUN_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULT_JSON="${RESULT_JSON:-$RESULTS_DIR/ingress-matrix-${RUN_STAMP}.json}"
RESULT_TSV=""
FAILURES_DIR=""

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

  --wait-timeout <seconds>         Reconcile wait timeout per row (default: 90)
  --ready-timeout <seconds>        Workload ready timeout per row (default: 180)
  --stability-requests <n>         Requests for stability check (default: 30)
  --large-payload-path <path>      Path used for large-payload assertion (default: /)
  --large-payload-min-bytes <n>    Minimum bytes expected for large-payload assertion (default: 65536)

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
    http-static|http-path-routing|http-multi-replica|http-multiport|http-redirect|http-large-payload|http2-unary) ;;
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

render_route_file() {
  local mode="$1"
  local archetype="$2"
  local app_name="$3"
  local host="$4"
  local out="$5"
  local route_name
  local service_port
  route_name="$(route_name_for_row "$mode" "$archetype")"
  service_port="$(archetype_service_port "$archetype")"

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
  } > "$out"
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

postcheck_archetype() {
  local mode="$1"
  local archetype="$2"
  local host="$3"
  local profile
  profile="$(archetype_assertion_profile "$archetype")"

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
      stability_check "$base_url" "$host" "$STABILITY_REQUESTS" || return 1
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
      [[ -n "$meta" ]] || return 1
      code="$(awk '{print $1}' <<<"$meta")"
      version="$(awk '{print $2}' <<<"$meta")"
      [[ "$code" =~ ^[23][0-9][0-9]$ ]] || return 1
      [[ "$version" == "2" ]] || return 1
      ;;
    *)
      return 1
      ;;
  esac

  return 0
}

collect_failure_diagnostics() {
  local mode="$1"
  local archetype="$2"
  local host="$3"
  local row_dir="$4"
  local row_log="$5"

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
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$mode" "$archetype" "$app_name" "$manifest" "$host" "$backend_port" "$status" "$duration_s" "$note" \
    >> "$RESULT_TSV"
}

write_json_result() {
  local out_json="$1"
  local in_tsv="$2"
  python - "$in_tsv" "$out_json" <<'PY'
import json
import sys
from pathlib import Path

in_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])

rows = []
for raw in in_path.read_text(encoding="utf-8").splitlines():
    if not raw.strip():
        continue
    mode, archetype, app_name, manifest, host, backend_port, status, duration_s, note = raw.split("\t")
    rows.append(
        {
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
    )

summary = {
    "total_rows": len(rows),
    "passed_rows": sum(1 for row in rows if row["status"] == "pass"),
    "failed_rows": sum(1 for row in rows if row["status"] == "fail"),
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

  local route_src="$row_tmp_dir/route-${mode}-${archetype}.yaml"
  render_route_file "$mode" "$archetype" "$app_name" "$host" "$route_src"

  local backend_url="${EDGE_BACKEND_SCHEME}://${EDGE_BACKEND_HOST}:${backend_port}/"
  local -a cmd=(
    "$ROOT_DIR/scripts/dev/test_ingress_modes_single_host.sh"
    --mode "$mode"
    --tier "$TIER"
    --site-id "$SITE_ID"
    --app-name "$app_name"
    --app-manifest "$manifest"
    --core-specs-dir "$CORE_SPECS_DIR"
    --core-envoy-config "$CORE_ENVOY_CONFIG"
    --edge-local-caddy-file "$EDGE_LOCAL_CADDY_FILE"
    --core-ingress-url "$CORE_INGRESS_URL"
    --core-ingress-tls-url "$CORE_INGRESS_TLS_URL"
    --edge-backend-url "$backend_url"
    --wait-timeout "$WAIT_TIMEOUT_S"
    --ready-timeout "$READY_TIMEOUT_S"
  )

  case "$mode" in
    core-proxy)
      cmd+=(--core-proxy-route-src "$route_src")
      ;;
    core-to-edge-public)
      cmd+=(--core-ingress-url "$CORE_PUBLIC_INGRESS_URL")
      cmd+=(--core-to-edge-public-route-src "$route_src")
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

  log "row start mode=$mode archetype=$archetype host=$host backend_port=$backend_port"
  if "${cmd[@]}" 2>&1 | tee "$row_log"; then
    if postcheck_archetype "$mode" "$archetype" "$host"; then
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

[[ "$LARGE_PAYLOAD_MIN_BYTES" =~ ^[0-9]+$ ]] || die "--large-payload-min-bytes must be an integer"

need_cmd rg
need_cmd curl
need_cmd python

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
log "results_json=$RESULT_JSON"

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
      collect_failure_diagnostics "$mode" "$archetype" "$host" "$row_failure_dir" "$row_log"
      if (( FAIL_FAST == 1 )); then
        row_finished="$(date +%s)"
        row_duration=$((row_finished - row_started))
        append_result_row "$mode" "$archetype" "$app_name" "$manifest" "$host" "$backend_port" "$row_status" "$row_duration" "$row_note"
        rm -rf "$row_tmp_dir"
        break 2
      fi
    fi

    row_finished="$(date +%s)"
    row_duration=$((row_finished - row_started))
    append_result_row "$mode" "$archetype" "$app_name" "$manifest" "$host" "$backend_port" "$row_status" "$row_duration" "$row_note"
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
