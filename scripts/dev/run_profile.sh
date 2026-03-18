#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROFILE="${1:-}"

if [[ -z "$PROFILE" ]]; then
  echo "usage: $0 <dev-min|dev-etcd|k1s-core|k1s-ha-core|k1s-edge>" >&2
  exit 1
fi

RUNTIME_BACKEND_EXPLICIT=1
if [[ -z "${AE_RUNTIME_BACKEND:-}" ]]; then
  RUNTIME_BACKEND_EXPLICIT=0
  export AE_RUNTIME_BACKEND=podman
fi

if [[ "$PROFILE" == "k1s-ha-core" ]]; then
  if [[ "$RUNTIME_BACKEND_EXPLICIT" -eq 0 ]]; then
    export AE_RUNTIME_BACKEND=cri
  fi
  export AE_INFRA_BACKEND="${AE_INFRA_BACKEND:-cri}"
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

detect_running_core_cri() {
  local endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
  local crictl_bin="${CRICTL_BIN:-crictl}"
  local pods_json
  local py_bin="${PYTHON_BIN:-}"
  if [[ -z "$py_bin" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      py_bin="$(command -v python3)"
    else
      py_bin="python"
    fi
  fi
  if ! command -v "$crictl_bin" >/dev/null 2>&1; then
    return 1
  fi
  if ! pods_json="$("$crictl_bin" --runtime-endpoint "$endpoint" pods -o json 2>/dev/null)"; then
    return 1
  fi
  PODS_JSON="$pods_json" "$py_bin" - <<'PY'
import json
import os

raw = os.environ.get("PODS_JSON", "")
try:
    payload = json.loads(raw or "{}")
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
    if labels.get("ae.stack.profile") not in {"k1s-core", "k1s-ha-core"}:
        continue
    if labels.get("ae.stack.backend") == "cri":
        raise SystemExit(0)
raise SystemExit(1)
PY
}

resolve_infra_backend() {
  if [[ -n "${AE_INFRA_BACKEND:-}" ]]; then
    printf '%s' "${AE_INFRA_BACKEND}"
    return 0
  fi
  case "${AE_RUNTIME_BACKEND:-}" in
    cri|containerd) printf 'cri'; return 0 ;;
  esac
  # Only auto-follow a running CRI core stack for k1s-* profiles.
  # dev-min/dev-etcd stay on compose by default unless explicitly overridden.
  case "${PROFILE:-}" in
    k1s-*)
      if detect_running_core_cri; then
        printf 'cri'
        return 0
      fi
      ;;
  esac
  printf 'compose'
  return 0
}

is_strict_cri() {
  [[ "${INFRA_BACKEND:-compose}" == "cri" ]]
}

acquire_profile_lock() {
  local profile="$1"
  local lock_dir="$ROOT_DIR/state/profiles/$profile"
  local lock_file="$lock_dir/.profile.lock"

  # Shared /mnt/host across VMs: allow concurrent edge profiles by site.
  if [[ "$profile" == "k1s-edge" ]]; then
    local lock_scope="${AE_SITE_ID:-${AE_NODE_ID:-$(hostname -s 2>/dev/null || hostname)}}"
    lock_scope="${lock_scope//\//_}"
    lock_file="$lock_dir/.profile.${lock_scope}.lock"
  fi

  mkdir -p "$lock_dir"
  if ! command -v flock >/dev/null 2>&1; then
    return 0
  fi
  exec 9>"$lock_file"
  if ! flock -n 9; then
    echo "error: profile '$profile' is already running (lock: $lock_file)" >&2
    exit 1
  fi
}

guard_port_free() {
  local port="$1"
  local label="$2"
  if ss -ltn "( sport = :$port )" 2>/dev/null | grep -q LISTEN; then
    echo "error: required port ${port} is already in use (${label})" >&2
    exit 1
  fi
}

run_cri_stack() {
  PYTHONPATH=src "$PYTHON_BIN" "$ROOT_DIR/scripts/dev/cri_stack.py" "$@"
}

ensure_managed_registry_tls_material() {
  if ! is_strict_cri; then
    return 0
  fi
  if [[ "${AE_CRI_REGISTRY_MODE:-managed}" != "managed" ]]; then
    return 0
  fi
  if ! is_truthy "${AE_CRI_MANAGED_REGISTRY_TLS:-1}"; then
    return 0
  fi
  if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
    return 0
  fi
  if ! command -v openssl >/dev/null 2>&1; then
    echo "error: openssl is required when AE_CRI_MANAGED_REGISTRY_TLS=1" >&2
    exit 1
  fi

  local tls_dir ca_key ca_crt cert_key cert_crt cert_csr cert_ext
  local ca_days cert_days ca_subj cert_subj san
  tls_dir="${AE_CRI_MANAGED_REGISTRY_TLS_DIR:-$ROOT_DIR/state/profiles/${PROFILE}/registry/tls}"
  ca_key="$tls_dir/ca.key"
  ca_crt="$tls_dir/ca.crt"
  cert_key="$tls_dir/registry.key"
  cert_crt="$tls_dir/registry.crt"
  cert_csr="$tls_dir/registry.csr"
  cert_ext="$tls_dir/registry.ext"

  ca_days="${AE_CRI_MANAGED_REGISTRY_CA_DAYS:-3650}"
  cert_days="${AE_CRI_MANAGED_REGISTRY_CERT_DAYS:-365}"
  ca_subj="${AE_CRI_MANAGED_REGISTRY_CA_SUBJECT:-/CN=k1s-managed-registry-ca}"
  cert_subj="${AE_CRI_MANAGED_REGISTRY_SUBJECT:-/CN=localhost}"
  san="${AE_CRI_MANAGED_REGISTRY_TLS_SANS:-DNS:localhost,IP:127.0.0.1,IP:::1}"

  mkdir -p "$tls_dir"

  local regen_ca=0 regen_cert=0 cert_text=""
  if [[ ! -s "$ca_crt" || ! -s "$ca_key" ]]; then
    regen_ca=1
  elif ! openssl x509 -in "$ca_crt" -noout >/dev/null 2>&1; then
    regen_ca=1
  elif ! openssl pkey -in "$ca_key" -noout >/dev/null 2>&1; then
    regen_ca=1
  else
    cert_text="$(openssl x509 -in "$ca_crt" -noout -text 2>/dev/null || true)"
    if ! grep -q "CA:TRUE" <<<"$cert_text"; then
      regen_ca=1
    fi
  fi

  if [[ "$regen_ca" -eq 1 ]]; then
    rm -f "$ca_key" "$ca_crt" "$tls_dir/ca.srl"
    if ! openssl req -x509 -newkey rsa:4096 -sha256 -nodes \
      -keyout "$ca_key" -out "$ca_crt" -days "$ca_days" -subj "$ca_subj" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:1" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1; then
      openssl req -x509 -newkey rsa:4096 -sha256 -nodes \
        -keyout "$ca_key" -out "$ca_crt" -days "$ca_days" -subj "$ca_subj" >/dev/null 2>&1 || {
        echo "error: failed to generate managed registry CA cert" >&2
        exit 1
      }
    fi
    regen_cert=1
  fi

  if [[ "$regen_cert" -eq 0 ]]; then
    if [[ ! -s "$cert_crt" || ! -s "$cert_key" ]]; then
      regen_cert=1
    elif ! openssl x509 -in "$cert_crt" -noout >/dev/null 2>&1; then
      regen_cert=1
    elif ! openssl pkey -in "$cert_key" -noout >/dev/null 2>&1; then
      regen_cert=1
    elif ! openssl x509 -in "$cert_crt" -checkend 0 -noout >/dev/null 2>&1; then
      regen_cert=1
    else
      cert_text="$(openssl x509 -in "$cert_crt" -noout -text 2>/dev/null || true)"
      if ! grep -q "DNS:localhost" <<<"$cert_text"; then
        regen_cert=1
      elif ! grep -q "IP Address:127.0.0.1" <<<"$cert_text"; then
        regen_cert=1
      fi
    fi
  fi

  if [[ "$regen_cert" -eq 1 ]]; then
    rm -f "$cert_key" "$cert_crt" "$cert_csr"
    cat > "$cert_ext" <<EXT
[v3_req]
subjectAltName=${san}
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EXT
    openssl req -new -newkey rsa:2048 -sha256 -nodes \
      -keyout "$cert_key" -out "$cert_csr" -subj "$cert_subj" >/dev/null 2>&1 || {
      rm -f "$cert_ext"
      echo "error: failed to generate managed registry server CSR" >&2
      exit 1
    }
    openssl x509 -req -in "$cert_csr" -CA "$ca_crt" -CAkey "$ca_key" -CAcreateserial \
      -out "$cert_crt" -days "$cert_days" -sha256 -extfile "$cert_ext" -extensions v3_req >/dev/null 2>&1 || {
      rm -f "$cert_ext"
      echo "error: failed to sign managed registry server cert" >&2
      exit 1
    }
    rm -f "$cert_csr" "$cert_ext"
  fi

  chmod 600 "$ca_key" "$cert_key"
  chmod 644 "$ca_crt" "$cert_crt"

  export AE_CRI_MANAGED_REGISTRY_TLS_DIR="$tls_dir"
  export AE_CRI_MANAGED_REGISTRY_TLS_CA="$ca_crt"
  export AE_CRI_MANAGED_REGISTRY_TLS_CERT="$cert_crt"
  export AE_CRI_MANAGED_REGISTRY_TLS_KEY="$cert_key"
}

ensure_cri_registry_defaults() {
  if ! is_strict_cri; then
    return 0
  fi

  local preset mode
  local insecure_explicit=0
  local trust_explicit=0
  local trust_system_explicit=0

  [[ -n "${AE_CRI_REGISTRY_INSECURE+x}" ]] && insecure_explicit=1
  [[ -n "${AE_CRI_REGISTRY_TRUST+x}" ]] && trust_explicit=1
  [[ -n "${AE_CRI_REGISTRY_TRUST_SYSTEM+x}" ]] && trust_system_explicit=1

  preset="$(printf '%s' "${AE_CRI_REGISTRY_PRESET:-}" | tr '[:upper:]' '[:lower:]')"
  case "$preset" in
    "" ) ;;
    microk8s)
      export AE_CRI_REGISTRY_MODE="${AE_CRI_REGISTRY_MODE:-external}"
      export AE_CRI_REGISTRY="${AE_CRI_REGISTRY:-localhost:32000}"
      export AE_CRI_REGISTRY_INSECURE="${AE_CRI_REGISTRY_INSECURE:-1}"
      ;;
    local)
      export AE_CRI_REGISTRY_MODE="${AE_CRI_REGISTRY_MODE:-managed}"
      ;;
    *)
      echo "warning: unsupported AE_CRI_REGISTRY_PRESET='${AE_CRI_REGISTRY_PRESET}' (supported: microk8s, local)" >&2
      ;;
  esac

  mode="$(printf '%s' "${AE_CRI_REGISTRY_MODE:-}" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "$mode" ]]; then
    if [[ -n "${AE_CRI_REGISTRY:-}" || -n "${AE_REGISTRY_HOST:-}" ]]; then
      mode="external"
    else
      mode="managed"
    fi
  fi
  case "$mode" in
    managed|external|off) ;;
    *)
      echo "warning: invalid AE_CRI_REGISTRY_MODE='${AE_CRI_REGISTRY_MODE}'; defaulting by context." >&2
      if [[ -n "${AE_CRI_REGISTRY:-}" || -n "${AE_REGISTRY_HOST:-}" ]]; then
        mode="external"
      else
        mode="managed"
      fi
      ;;
  esac
  export AE_CRI_REGISTRY_MODE="$mode"

  if [[ "$mode" == "off" ]]; then
    echo "[cri] registry mode=off (no strict-CRI registry rewrite/readiness checks)" >&2
    return 0
  fi

  if [[ "$mode" == "external" ]]; then
    if [[ -z "${AE_CRI_REGISTRY:-}" && -n "${AE_REGISTRY_HOST:-}" ]]; then
      export AE_CRI_REGISTRY="${AE_REGISTRY_HOST}"
    fi
    if [[ -z "${AE_CRI_REGISTRY:-}" ]]; then
      echo "error: AE_CRI_REGISTRY_MODE=external requires AE_CRI_REGISTRY (or AE_REGISTRY_HOST)." >&2
      exit 1
    fi
    echo "[cri] registry mode=external endpoint=${AE_CRI_REGISTRY}" >&2
    return 0
  fi

  # Managed mode defaults: secure TLS endpoint by default with explicit insecure opt-out.
  if [[ -z "${AE_CRI_MANAGED_REGISTRY_TLS+x}" ]]; then
    export AE_CRI_MANAGED_REGISTRY_TLS=1
  fi

  if is_truthy "${AE_CRI_MANAGED_REGISTRY_TLS:-1}"; then
    if [[ "$insecure_explicit" -eq 0 ]]; then
      export AE_CRI_REGISTRY_INSECURE=0
    fi
    if [[ -z "${AE_CRI_REGISTRY:-}" ]]; then
      # Registry image rewrite expects OCI host[:port], not URL scheme.
      export AE_CRI_REGISTRY="localhost:${AE_CRI_MANAGED_REGISTRY_PORT:-5001}"
    fi
  else
    if [[ "$insecure_explicit" -eq 0 ]]; then
      export AE_CRI_REGISTRY_INSECURE=1
    fi
    if [[ -z "${AE_CRI_REGISTRY:-}" ]]; then
      export AE_CRI_REGISTRY="localhost:${AE_CRI_MANAGED_REGISTRY_PORT:-5001}"
    fi
  fi

  if is_truthy "${AE_CRI_MANAGED_REGISTRY_TLS:-0}" && ! is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
    ensure_managed_registry_tls_material
    if [[ "$trust_explicit" -eq 0 ]]; then
      export AE_CRI_REGISTRY_TRUST=1
    fi
    if [[ "$trust_system_explicit" -eq 0 ]]; then
      export AE_CRI_REGISTRY_TRUST_SYSTEM=1
    fi
    if [[ -z "${AE_CRI_REGISTRY_TRUST_CA:-}" && -n "${AE_CRI_MANAGED_REGISTRY_TLS_CA:-}" ]]; then
      export AE_CRI_REGISTRY_TRUST_CA="${AE_CRI_MANAGED_REGISTRY_TLS_CA}"
    fi
  fi

  echo "[cri] registry mode=managed endpoint=${AE_CRI_REGISTRY}" >&2
}

_cri_registry_host_port() {
  local raw="${1:-}"
  local scheme="${2:-}"
  raw="${raw#http://}"
  raw="${raw#https://}"
  raw="${raw%%/*}"
  local host="${raw%:*}"
  local port="${raw##*:}"
  if [[ "$host" == "$port" ]]; then
    host="$raw"
    if [[ "$scheme" == "http" ]]; then
      port="80"
    else
      port="443"
    fi
  fi
  printf '%s|%s\n' "$host" "$port"
}

_cri_registry_scheme() {
  local registry="${AE_CRI_REGISTRY:-}"
  if [[ "$registry" == http://* ]]; then
    printf 'http'
    return 0
  fi
  if [[ "$registry" == https://* ]]; then
    printf 'https'
    return 0
  fi
  if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
    printf 'http'
    return 0
  fi
  printf 'https'
}


_cri_registry_normalize_host() {
  local raw="${1:-}"
  raw="${raw#http://}"
  raw="${raw#https://}"
  raw="${raw%%/*}"
  printf '%s\n' "$raw"
}

_cri_registry_hosts_toml_scheme() {
  local registry_host
  registry_host="$(_cri_registry_normalize_host "${1:-}")"
  if [[ -z "$registry_host" ]]; then
    return 0
  fi
  local hosts_file="/etc/containerd/certs.d/${registry_host}/hosts.toml"
  if [[ ! -r "$hosts_file" ]]; then
    return 0
  fi
  sed -n 's/^[[:space:]]*server[[:space:]]*=[[:space:]]*"\(https\?\):\/\/.*/\1/p' "$hosts_file" | head -n1
}

ensure_cri_registry_ready() {
  if ! is_strict_cri; then
    return 0
  fi
  local mode="${AE_CRI_REGISTRY_MODE:-managed}"
  if [[ "$mode" == "off" ]]; then
    return 0
  fi
  if [[ -z "${AE_CRI_REGISTRY:-}" ]]; then
    echo "error: strict CRI registry mode '${mode}' requires AE_CRI_REGISTRY." >&2
    exit 1
  fi
  local scheme host_port host port
  scheme="$(_cri_registry_scheme)"
  host_port="$(_cri_registry_host_port "${AE_CRI_REGISTRY}" "$scheme")"
  IFS='|' read -r host port <<<"$host_port"
  if [[ -z "$host" || -z "$port" ]]; then
    echo "error: unable to parse AE_CRI_REGISTRY='${AE_CRI_REGISTRY}'." >&2
    exit 1
  fi

  local probe_host="$host"
  if [[ "$probe_host" == "0.0.0.0" ]]; then
    probe_host="127.0.0.1"
  fi

  local registry_args=()
  if [[ "$mode" == "managed" ]]; then
    if ! [[ "$host" == "localhost" || "$host" == "127.0.0.1" || "$host" == "0.0.0.0" ]]; then
      echo "error: AE_CRI_REGISTRY_MODE=managed expects a local registry host, got '${host}'." >&2
      echo "error: use AE_CRI_REGISTRY_MODE=external for remote or microk8s registries." >&2
      exit 1
    fi

    registry_args=(up-registry --profile "$PROFILE" --host "$probe_host" --port "$port")
    if is_truthy "${AE_CRI_MANAGED_REGISTRY_TLS:-0}" && ! is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
      if [[ -z "${AE_CRI_MANAGED_REGISTRY_TLS_CERT:-}" || -z "${AE_CRI_MANAGED_REGISTRY_TLS_KEY:-}" ]]; then
        echo "error: managed registry TLS requested but cert/key paths are missing." >&2
        exit 1
      fi
      registry_args+=(--tls-cert "${AE_CRI_MANAGED_REGISTRY_TLS_CERT}" --tls-key "${AE_CRI_MANAGED_REGISTRY_TLS_KEY}")
    fi

    if ! port_open "$probe_host" "$port"; then
      if ! run_cri_stack "${registry_args[@]}"; then
        echo "error: failed to start managed CRI registry at ${probe_host}:${port}" >&2
        exit 1
      fi
    fi
  fi

  local timeout_s="${AE_CRI_REGISTRY_HEALTH_TIMEOUT:-10}"
  if ! [[ "$timeout_s" =~ ^[0-9]+$ ]]; then
    timeout_s=10
  fi
  local start_ts now_ts
  start_ts="$(date +%s)"
  while ! port_open "$probe_host" "$port"; do
    now_ts="$(date +%s)"
    if (( now_ts - start_ts >= timeout_s )); then
      if [[ "$mode" == "external" ]]; then
        echo "error: external strict-CRI registry is unreachable at ${probe_host}:${port}" >&2
        if [[ "$probe_host" == "localhost" || "$probe_host" == "127.0.0.1" ]] && [[ "$port" == "32000" ]]; then
          echo "hint: microk8s common path: ensure registry is enabled and reachable on localhost:32000." >&2
          echo "hint: example: microk8s enable registry" >&2
        fi
      else
        echo "error: managed strict-CRI registry did not become reachable at ${probe_host}:${port}" >&2
      fi
      exit 1
    fi
    sleep 0.2
  done

  if command -v curl >/dev/null 2>&1; then
    local probe_url="${scheme}://${probe_host}:${port}/v2/"
    local curl_args=(-sS -o /dev/null -w "%{http_code}" --connect-timeout 2 --max-time 3)
    local recreated_for_probe=0
    if [[ "$scheme" == "https" ]]; then
      curl_args+=(-k)
    fi
    start_ts="$(date +%s)"
    while true; do
      local code
      code="$(curl "${curl_args[@]}" "$probe_url" 2>/dev/null || true)"
      if [[ "$code" == "200" || "$code" == "401" ]]; then
        break
      fi
      now_ts="$(date +%s)"
      if (( now_ts - start_ts >= timeout_s )); then
        if [[ "$mode" == "managed" && "$recreated_for_probe" -eq 0 && "${#registry_args[@]}" -gt 0 ]]; then
          echo "[cri] registry probe mismatch on ${probe_url}; recreating managed registry with current TLS settings" >&2
          if run_cri_stack "${registry_args[@]}" --recreate; then
            recreated_for_probe=1
            start_ts="$(date +%s)"
            continue
          fi
        fi
        echo "error: registry endpoint responded unexpectedly at ${probe_url} (last_http=${code:-000})" >&2
        exit 1
      fi
      sleep 0.2
    done
  fi
}

ensure_cri_registry_trust() {
  if ! is_strict_cri; then
    return 0
  fi
  if [[ "${AE_CRI_REGISTRY_MODE:-}" == "off" ]]; then
    return 0
  fi
  if [[ -z "${AE_CRI_REGISTRY:-}" ]]; then
    return 0
  fi
  if ! is_truthy "${AE_CRI_REGISTRY_TRUST:-0}" && ! is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
    return 0
  fi

  local trust_script="$ROOT_DIR/scripts/containerd_registry_trust.sh"
  if [[ ! -x "$trust_script" ]]; then
    echo "error: missing registry trust helper: $trust_script" >&2
    exit 1
  fi

  local registry_host
  registry_host="$(_cri_registry_normalize_host "${AE_CRI_REGISTRY}")"
  local scheme="${AE_CRI_REGISTRY_TRUST_SCHEME:-}"
  if [[ -z "$scheme" ]]; then
    if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
      scheme="http"
    else
      scheme="https"
    fi
  fi

  local previous_scheme
  previous_scheme="$(_cri_registry_hosts_toml_scheme "$registry_host")"
  local scheme_changed=0
  if [[ -n "$previous_scheme" && "$previous_scheme" != "$scheme" ]]; then
    scheme_changed=1
  fi

  local auto_restart="${AE_CRI_REGISTRY_AUTO_RESTART:-1}"
  local restart_selected=0
  local trust_args=(--host "$registry_host" --scheme "$scheme")

  if [[ "$scheme_changed" -eq 1 ]]; then
    if is_truthy "$auto_restart"; then
      if ! command -v systemctl >/dev/null 2>&1; then
        echo "error: registry scheme transition detected (${previous_scheme} -> ${scheme}) for ${registry_host}, but systemctl is unavailable for automatic restart." >&2
        echo "error: restart containerd manually, or set AE_CRI_REGISTRY_TRUST_RESTART=1 when a restart mechanism is available." >&2
        exit 1
      fi
      trust_args+=(--restart)
      restart_selected=1
      echo "[cri] registry trust scheme transition detected (${previous_scheme} -> ${scheme}) for ${registry_host}; restarting containerd to apply resolver state" >&2
    elif ! is_truthy "${AE_CRI_REGISTRY_TRUST_RESTART:-0}"; then
      echo "error: registry scheme transition detected (${previous_scheme} -> ${scheme}) for ${registry_host}." >&2
      echo "error: containerd must be restarted to apply resolver state changes." >&2
      echo "error: set AE_CRI_REGISTRY_AUTO_RESTART=1 (default), or AE_CRI_REGISTRY_TRUST_RESTART=1, or run 'sudo systemctl restart containerd' and retry." >&2
      exit 1
    fi
  fi

  if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}" || is_truthy "${AE_CRI_REGISTRY_TRUST_INSECURE:-0}"; then
    trust_args+=(--insecure)
  elif [[ -n "${AE_CRI_REGISTRY_TRUST_CA:-}" ]]; then
    trust_args+=(--ca "${AE_CRI_REGISTRY_TRUST_CA}")
  fi
  if is_truthy "${AE_CRI_REGISTRY_TRUST_SYSTEM:-0}"; then
    trust_args+=(--system-trust)
  fi
  if is_truthy "${AE_CRI_REGISTRY_TRUST_RESTART:-0}" && [[ "$restart_selected" -eq 0 ]]; then
    trust_args+=(--restart)
  fi

  "$trust_script" "${trust_args[@]}"
}

ensure_cri_preflight() {
  if ! is_strict_cri; then
    return 0
  fi
  if ! "$ROOT_DIR/scripts/cri_preflight.sh"; then
    echo "error: strict CRI infra selected but preflight failed" >&2
    exit 1
  fi
  if ! run_cri_stack preflight --profile "$PROFILE"; then
    echo "error: strict CRI infra selected but CRI stack preflight failed" >&2
    exit 1
  fi
}

cri_ref_has_registry_prefix() {
  local ref="${1:-}"
  [[ "$ref" == */* ]] || return 1
  local first="${ref%%/*}"
  [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]]
}

resolve_cri_registry_target_ref() {
  local source_ref="$1"
  local registry_host="$2"
  local namespace="${3:-}"

  local ref="$source_ref"
  local digest=""
  local tag=""
  local last_segment
  if [[ "$ref" == *@* ]]; then
    digest="@${ref##*@}"
    ref="${ref%@*}"
  fi

  last_segment="${ref##*/}"
  if [[ "$last_segment" == *:* ]]; then
    tag=":${last_segment##*:}"
    ref="${ref%:*}"
  fi

  if cri_ref_has_registry_prefix "$ref"; then
    ref="${ref#*/}"
  fi

  ref="${ref#/}"
  namespace="${namespace#/}"
  namespace="${namespace%/}"
  if [[ -n "$namespace" ]]; then
    ref="${namespace}/${ref}"
  fi
  printf '%s/%s%s%s' "${registry_host%/}" "$ref" "$tag" "$digest"
}

ensure_cri_registry_preload_images() {
  if ! is_strict_cri; then
    return 0
  fi
  if [[ "${PROFILE:-}" != "k1s-core" && "${PROFILE:-}" != "k1s-ha-core" && "${PROFILE:-}" != "k1s-edge" ]]; then
    return 0
  fi
  if [[ "${AE_CRI_REGISTRY_MODE:-managed}" == "off" ]]; then
    return 0
  fi

  local preload_default=0
  if [[ "${AE_CRI_REGISTRY_MODE:-managed}" == "managed" ]]; then
    preload_default=1
  fi
  if ! is_truthy "${AE_CRI_REGISTRY_PRELOAD:-$preload_default}"; then
    return 0
  fi

  local registry_host="${AE_CRI_REGISTRY:-${AE_REGISTRY_HOST:-}}"
  if [[ -z "$registry_host" ]]; then
    return 0
  fi
  registry_host="${registry_host#http://}"
  registry_host="${registry_host#https://}"
  registry_host="${registry_host%%/*}"

  local mirror_script="$ROOT_DIR/scripts/dev/cri_image_mirror.sh"
  if [[ ! -x "$mirror_script" ]]; then
    echo "error: missing CRI image mirror helper: $mirror_script" >&2
    exit 1
  fi

  local -a source_images=()
  if [[ -n "${AE_CRI_REGISTRY_PRELOAD_IMAGES:-}" ]]; then
    local raw_item
    while IFS= read -r raw_item; do
      raw_item="${raw_item#"${raw_item%%[![:space:]]*}"}"
      raw_item="${raw_item%"${raw_item##*[![:space:]]}"}"
      [[ -n "$raw_item" ]] && source_images+=("$raw_item")
    done < <(printf '%s' "${AE_CRI_REGISTRY_PRELOAD_IMAGES}" | tr ',' '\n')
  else
    if [[ "${PROFILE:-}" == "k1s-edge" ]]; then
      source_images=(
        "docker.io/library/nats:2.10"
        "${AE_RATHOLE_IMAGE:-docker.io/rapiz1/rathole:v0.5.0}"
      )
    elif [[ "${PROFILE:-}" == "k1s-ha-core" ]]; then
      source_images=(
        "${AE_ENVOY_IMAGE:-docker.io/envoyproxy/envoy:v1.29-latest}"
        "${AE_RATHOLE_IMAGE:-docker.io/rapiz1/rathole:v0.5.0}"
        "docker.io/library/caddy:2.8"
      )
    else
      source_images=(
        "quay.io/coreos/etcd:v3.5.13"
        "docker.io/library/nats:2.10"
        "docker.io/library/postgres:16"
        "${AE_ENVOY_IMAGE:-docker.io/envoyproxy/envoy:v1.29-latest}"
        "${AE_RATHOLE_IMAGE:-docker.io/rapiz1/rathole:v0.5.0}"
      )
    fi
  fi

  if [[ "${#source_images[@]}" -eq 0 ]]; then
    return 0
  fi

  local namespace="${AE_CRI_REGISTRY_NAMESPACE:-}"
  local source target
  for source in "${source_images[@]}"; do
    target="$(resolve_cri_registry_target_ref "$source" "$registry_host" "$namespace")"
    echo "[cri] preloading strict-CRI image: source=${source} target=${target}" >&2
    if ! bash "$mirror_script" \
      --source "$source" \
      --target "$target" \
      --cri-endpoint "${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}" \
      --pull-cri; then
      echo "error: failed to preload strict-CRI image '${source}' into '${target}'" >&2
      exit 1
    fi
  done
}

ensure_backend_not_mixed_with_core_cri() {
  if is_strict_cri; then
    return 0
  fi
  case "$PROFILE" in
    k1s-core|k1s-ha-core|k1s-edge) ;;
    *) return 0 ;;
  esac
  if detect_running_core_cri; then
    echo "error: core CRI stack detected while compose infra is selected." >&2
    echo "error: use 'make k1s-core-cri', 'make k1s-ha-core', 'make k1s-edge-cri', or 'make k1s-edge-core-cri'." >&2
    echo "error: alternatively set AE_RUNTIME_BACKEND=cri AE_INFRA_BACKEND=cri." >&2
    exit 1
  fi
}

render_strict_caddy_sites() {
  local https_port="${1:-8443}"
  local target_dir="$ROOT_DIR/state/caddy-cri"
  local src_dir="$ROOT_DIR/ops/dev/caddy/sites"
  mkdir -p "$target_dir"
  cp -f "$src_dir"/*.caddy "$target_dir"/
  for file in "$target_dir"/*.caddy; do
    [[ -f "$file" ]] || continue
    sed -i "1 s/:443/:${https_port}/g" "$file" >/dev/null 2>&1 || true
  done
}

write_dash_caddy_site() {
  local host_alias="$1"
  local out_file="$2"
  local https_port="$3"
  cat > "$out_file" <<EOF
https://dash.home.arpa:${https_port} {
    log {
        output stdout
        format console
    }
    header -Strict-Transport-Security
    tls internal
    @no_sse {
        not path /dashboard/sse/* /logs/*/stream
    }
    encode @no_sse gzip zstd

    @sse {
        path /dashboard/sse/* /logs/*/stream
    }
    handle @sse {
        reverse_proxy ${host_alias}:${METRICS_PORT:-9108} {
            flush_interval -1
            stream_close_delay 5m
            stream_timeout 24h
            header_down X-Accel-Buffering no
            header_down Cache-Control no-cache
        }
    }

    handle {
        reverse_proxy ${host_alias}:${METRICS_PORT:-9108}
    }
}
EOF
}

prepare_strict_edge_nats_config() {
  local site_id="$1"
  local edge_port="${2:-4224}"
  local edge_http_port="${3:-8224}"
  local hub_leaf_host="${4:-127.0.0.1}"
  local hub_leaf_port="${5:-7422}"
  local src="$ROOT_DIR/ops/dev/nats-edge.conf"
  local out="$ROOT_DIR/ops/dev/nats-edge-${site_id}.conf"
  if [[ ! -f "$src" ]]; then
    echo "error: missing nats edge template: $src" >&2
    exit 1
  fi
  "$PYTHON_BIN" - <<PY "$src" "$out" "$site_id" "$edge_port" "$edge_http_port" "$hub_leaf_host" "$hub_leaf_port"
from pathlib import Path
import re
import sys

src, out, site, port, http_port, hub_host, hub_port = sys.argv[1:8]
text = Path(src).read_text(encoding="utf-8")
text = text.replace("sfo-edge-01", site)
text = text.replace("edge-sfo-01", f"edge-{site}")
text = re.sub(r'@nats-hub:\d+"', f'@{hub_host}:{hub_port}"', text)
text = re.sub(r"(?m)^port:\\s*\\d+\\s*$", f"port: {port}", text)
text = re.sub(r"(?m)^http:\\s*\\d+\\s*$", f"http: {http_port}", text)
Path(out).write_text(text, encoding="utf-8")
print(out)
PY
}

resolve_strict_edge_hub_leaf_host() {
  if [[ -n "${AE_NATS_HUB_LEAF_HOST:-}" ]]; then
    printf '%s' "${AE_NATS_HUB_LEAF_HOST}"
    return 0
  fi
  if [[ -n "${AE_CONTROLLER_URL:-}" ]]; then
    local controller_host
    controller_host="$(
      "$PYTHON_BIN" - <<'PY' "${AE_CONTROLLER_URL}"
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

resolve_strict_edge_hub_leaf_port() {
  if [[ -n "${AE_NATS_HUB_LEAF_PORT:-}" ]]; then
    printf '%s' "${AE_NATS_HUB_LEAF_PORT}"
    return 0
  fi
  printf '7422'
}

resolve_strict_edge_port() {
  if [[ -n "${EDGE_PORT:-}" ]]; then
    printf '%s' "${EDGE_PORT}"
    return 0
  fi
  if [[ -n "${AE_NATS_URL:-}" ]]; then
    local parsed_out
    parsed_out="$(
      PYTHONPATH=src "$PYTHON_BIN" - <<'PY' "${AE_NATS_URL}"
from ae.config.transport import parse_nats_explicit_port
import sys

raw = sys.argv[1]
try:
    port = parse_nats_explicit_port(raw)
except Exception:
    print("INVALID")
    raise SystemExit(0)

if port is None:
    print("NONE")
else:
    print(f"PORT={port}")
PY
    )"
    case "$parsed_out" in
      PORT=*)
        printf '%s' "${parsed_out#PORT=}"
        return 0
        ;;
      INVALID)
        echo "warning: unable to parse AE_NATS_URL='${AE_NATS_URL}'; defaulting EDGE_PORT=4223." >&2
        ;;
    esac
  fi
  printf '4223'
}

resolve_strict_edge_http_port() {
  if [[ -n "${EDGE_HTTP_PORT:-}" ]]; then
    printf '%s' "${EDGE_HTTP_PORT}"
    return 0
  fi
  printf '8223'
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
  local default_api_base="http://127.0.0.1:${METRICS_PORT:-9108}"
  local default_dash_url="${default_api_base}/dashboard"
  if [[ "${CORE_CADDY:-0}" == "1" ]]; then
    local https_port="${CADDY_HTTPS_PORT:-8443}"
    default_api_base="https://api.home.arpa:${https_port}"
    default_dash_url="https://dash.home.arpa:${https_port}/dashboard"
  fi
  local api_base="${DOCS_API_BASE:-$default_api_base}"
  local dash_url="${DOCS_DASHBOARD_URL:-$default_dash_url}"

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
  local apishim_port="${APISHIM_PORT:-8445}"
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
  if [[ "$apishim_port" == "5432" ]]; then
    apishim_port="8445"
  elif [[ -n "${POSTGRES_PORT:-}" && "$apishim_port" == "${POSTGRES_PORT}" ]]; then
    apishim_port="8445"
  fi
  if [[ "${AE_APISHIM_MODE:-}" == "container" && "$ENGINE_BIN" == "podman" ]]; then
    if "$ENGINE_BIN" network inspect dev_default >/dev/null 2>&1; then
      apishim_upstream="apishim:${apishim_port}"
      caddy_network="dev_default"
    fi
  fi
  if [[ -z "$apishim_upstream" ]]; then
    apishim_upstream="${host_alias}:${apishim_port}"
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
    @no_sse {
        not path /dashboard/sse/* /logs/*/stream
    }
    encode @no_sse gzip zstd

    @sse {
        path /dashboard/sse/* /logs/*/stream
    }
    handle @sse {
        reverse_proxy ${host_alias}:${METRICS_PORT:-9108} {
            flush_interval -1
            # Keep SSE streams alive across config reloads to reduce reconnect churn.
            stream_close_delay 5m
            stream_timeout 24h
            header_down X-Accel-Buffering no
            header_down Cache-Control no-cache
        }
    }

    handle {
        reverse_proxy ${host_alias}:${METRICS_PORT:-9108}
    }
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

apishim_health_code() {
  local port="$1"
  local token="${2:-}"
  local path="${AE_APISHIM_HEALTH_PATH:-/healthz}"
  if ! command -v curl >/dev/null 2>&1; then
    echo "000|curl-not-found"
    return 0
  fi
  local url="https://127.0.0.1:${port}${path}"
  local out
  if [[ -n "$token" ]]; then
    out="$(curl -sk -H "Authorization: Bearer ${token}" "${url}" -w "\n%{http_code}" 2>/dev/null || true)"
  else
    out="$(curl -sk "${url}" -w "\n%{http_code}" 2>/dev/null || true)"
  fi
  local code="${out##*$'\n'}"
  local body="${out%$'\n'*}"
  if [[ -z "$code" ]]; then
    code="000"
  fi
  echo "${code}|${body}"
}

apishim_is_healthy_once() {
  local port="$1"
  local token="${2:-}"
  local probe code
  probe="$(apishim_health_code "$port" "$token")"
  code="${probe%%|*}"
  [[ "$code" == "200" ]]
}

apishim_wait_healthy() {
  local port="$1"
  local token="${2:-}"
  local timeout_s="${3:-12}"
  local start now probe code body
  if ! [[ "$timeout_s" =~ ^[0-9]+$ ]]; then
    timeout_s=12
  fi
  start="$(date +%s)"
  while true; do
    if ! command -v curl >/dev/null 2>&1; then
      if port_open "127.0.0.1" "$port"; then
        return 0
      fi
      code="000"
      body="curl-not-found"
    else
      probe="$(apishim_health_code "$port" "$token")"
      code="${probe%%|*}"
      body="${probe#*|}"
      if [[ "$code" == "200" ]]; then
        return 0
      fi
      if [[ ("$code" == "401" || "$code" == "403") && -n "$token" ]] && \
        grep -qi "missing/invalid bearer token" <<<"$body"; then
        echo "error: apishim responded ${code}; AE_APISHIM_TOKEN does not match running server." >&2
        return 1
      fi
    fi
    now="$(date +%s)"
    if (( now - start >= timeout_s )); then
      echo "error: apishim did not become healthy on 127.0.0.1:${port} within ${timeout_s}s (last=${code})." >&2
      if [[ -n "${body:-}" ]]; then
        echo "error: apishim health response: ${body}" >&2
      fi
      return 1
    fi
    sleep 0.3
  done
}

apishim_port_debug() {
  local port="$1"
  if ! command -v ss >/dev/null 2>&1; then
    return 0
  fi
  echo "debug: listeners on :${port}" >&2
  ss -ltnp "( sport = :${port} )" 2>/dev/null >&2 || true
}

apishim_pid_alive() {
  local pid_file="$1"
  local pid
  if [[ ! -f "$pid_file" ]]; then
    return 1
  fi
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  kill -0 "$pid" 2>/dev/null
}

read_env_var_file() {
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

normalize_apishim_port() {
  local candidate="${1:-}"
  local postgres_port="${2:-5432}"
  if ! [[ "$candidate" =~ ^[0-9]+$ ]]; then
    candidate="8445"
  fi
  if [[ "$candidate" == "5432" || ( -n "$postgres_port" && "$candidate" == "$postgres_port" ) ]]; then
    echo "8445"
    return 0
  fi
  echo "$candidate"
}

warn_apishim_dsn() {
  local dsn="${AE_APISHIM_DSN:-}"
  local scheme=""
  local host=""
  local dsn_port=""
  if [[ -z "$dsn" ]]; then
    return 0
  fi
  local parsed=""
  parsed="$("$PYTHON_BIN" - <<'PY' "$dsn" 2>/dev/null || true
import sys
from urllib.parse import urlparse

dsn = sys.argv[1]
try:
    parsed = urlparse(dsn)
except Exception:
    sys.exit(0)

scheme = parsed.scheme or ""
host = parsed.hostname or ""
port = parsed.port or ""
if not scheme:
    sys.exit(0)
print(f"{scheme}|{host}|{port}")
PY
  )"
  if [[ -z "$parsed" ]]; then
    return 0
  fi
  IFS='|' read -r scheme host dsn_port <<<"$parsed"
  if [[ "$scheme" != "postgres" && "$scheme" != "postgresql" ]]; then
    return 0
  fi
  if [[ -z "$host" ]]; then
    return 0
  fi
  if [[ -z "${dsn_port:-}" ]]; then
    dsn_port=5432
  fi
  if [[ "$host" == "postgres" ]]; then
    if ! "$ENGINE_BIN" ps --format '{{.Names}}' 2>/dev/null | grep -q 'postgres'; then
      echo "warning: AE_APISHIM_DSN points to postgres service but no postgres container is running; apishim may exit." >&2
    fi
    return 0
  fi
  if ! port_open "$host" "$dsn_port"; then
    echo "warning: AE_APISHIM_DSN points to ${host}:${dsn_port}, but it is not reachable from the host; apishim may exit." >&2
  fi
}

start_apishim() {
  local profile_dir="$1"
  local host="${APISHIM_HOST:-127.0.0.1}"
  local requested_port="${APISHIM_PORT:-8445}"
  local port="$requested_port"
  local postgres_port="${POSTGRES_PORT:-5432}"
  local pid_file="${APISHIM_PID_FILE:-$ROOT_DIR/state/apishim.pid}"
  local env_file="${APISHIM_ENV_FILE:-$profile_dir/apishim.env}"
  local cli_env_file="${APISHIM_CLI_ENV_FILE:-$profile_dir/apishim.cli.env}"
  local cert_file="${APISHIM_CERT_FILE:-$profile_dir/apishim.crt}"
  local key_file="${APISHIM_KEY_FILE:-$profile_dir/apishim.key}"
  local mode="${AE_APISHIM_MODE:-container}"
  local startup_timeout="${AE_APISHIM_STARTUP_TIMEOUT:-12}"
  local health_token=""
  local already_running=0
  export AE_APISHIM_ENV_FILE="${AE_APISHIM_ENV_FILE:-$env_file}"

  if ! is_truthy "${AE_APISHIM_AUTOSTART:-1}"; then
    return 0
  fi
  if [[ -f "$pid_file" ]] && ! apishim_pid_alive "$pid_file"; then
    rm -f "$pid_file" >/dev/null 2>&1 || true
  fi

  mkdir -p "$profile_dir"
  APISHIM_ENV_FILE="$env_file" APISHIM_CERT_FILE="$cert_file" APISHIM_KEY_FILE="$key_file" \
    "$ROOT_DIR/scripts/ensure_apishim_env.sh" >/dev/null 2>&1 || true
  if [[ -f "$env_file" ]]; then
    local env_apishim_token=""
    local env_apishim_read_token=""
    local env_apishim_session_secret=""
    local env_apishim_mint_token=""
    local env_api_admin_token=""
    local env_labs_token=""
    env_apishim_token="$(read_env_var_file "AE_APISHIM_TOKEN" "$env_file" || true)"
    env_apishim_read_token="$(read_env_var_file "AE_APISHIM_READ_TOKEN" "$env_file" || true)"
    env_apishim_session_secret="$(read_env_var_file "AE_APISHIM_SESSION_SECRET" "$env_file" || true)"
    env_apishim_mint_token="$(read_env_var_file "AE_APISHIM_MINT_TOKEN" "$env_file" || true)"
    env_api_admin_token="$(read_env_var_file "AE_API_ADMIN_TOKEN" "$env_file" || true)"
    env_labs_token="$(read_env_var_file "AE_LABS_TOKEN" "$env_file" || true)"
    [[ -n "$env_apishim_token" ]] && export AE_APISHIM_TOKEN="$env_apishim_token"
    [[ -n "$env_apishim_read_token" ]] && export AE_APISHIM_READ_TOKEN="$env_apishim_read_token"
    [[ -n "$env_apishim_session_secret" ]] && export AE_APISHIM_SESSION_SECRET="$env_apishim_session_secret"
    [[ -n "$env_apishim_mint_token" ]] && export AE_APISHIM_MINT_TOKEN="$env_apishim_mint_token"
    [[ -n "$env_api_admin_token" ]] && export AE_API_ADMIN_TOKEN="$env_api_admin_token"
    [[ -n "$env_labs_token" ]] && export AE_LABS_TOKEN="$env_labs_token"
  fi
  port="$(normalize_apishim_port "$requested_port" "$postgres_port")"
  if [[ "$port" != "$requested_port" ]]; then
    echo "warning: APISHIM_PORT=${requested_port} conflicts with POSTGRES_PORT=${postgres_port}; forcing APISHIM_PORT=${port}." >&2
  fi
  export APISHIM_PORT="$port"

  export AE_APISHIM_RUNTIME="${AE_APISHIM_RUNTIME:-${AE_RUNTIME_BACKEND:-docker}}"
  export AE_APISHIM_ENABLE=1
  export AE_APISHIM_ALLOW_ANON="${AE_APISHIM_ALLOW_ANON:-0}"
  export AE_APISHIM_RBAC="${AE_APISHIM_RBAC:-1}"
  export AE_APISHIM_RBAC_EVAL="${AE_APISHIM_RBAC_EVAL:-0}"
  export AE_APISHIM_DB="${AE_APISHIM_DB:-$profile_dir/apishim.db}"
  export AE_APISHIM_TLS_CERT="${AE_APISHIM_TLS_CERT:-$cert_file}"
  export AE_APISHIM_TLS_KEY="${AE_APISHIM_TLS_KEY:-$key_file}"
  export AE_APISHIM_SERVER="https://127.0.0.1:${port}"
  APISHIM_ENV_FILE="$env_file" APISHIM_CLI_ENV_FILE="$cli_env_file" APISHIM_CERT_FILE="$cert_file" \
    AE_APISHIM_SERVER="${AE_APISHIM_SERVER}" AE_CLI_SHARED_GROUP="${AE_CLI_SHARED_GROUP:-aecli}" \
    "$ROOT_DIR/scripts/ensure_apishim_cli_env.sh" >/dev/null 2>&1 || \
    echo "warning: failed to sync shared apishim CLI env" >&2
  # In compose-container mode, loopback etcd endpoints resolve inside the apishim
  # container, not on the host. Default to the compose service endpoint.
  if [[ "$mode" == "container" && "${AE_STATE_BACKEND:-}" == "etcd" && -z "${AE_APISHIM_ETCD_ENDPOINTS:-}" ]]; then
    if [[ "${AE_ETCD_ENDPOINTS:-}" == *"127.0.0.1"* || "${AE_ETCD_ENDPOINTS:-}" == *"localhost"* ]]; then
      export AE_APISHIM_ETCD_ENDPOINTS="http://etcd:2379"
    fi
  fi
  warn_apishim_dsn
  local renorm_port
  renorm_port="$(normalize_apishim_port "$port" "$postgres_port")"
  if [[ "$renorm_port" != "$port" ]]; then
    echo "warning: apishim port drift detected (${port}); forcing APISHIM_PORT=${renorm_port}." >&2
    port="$renorm_port"
    export APISHIM_PORT="$port"
    export AE_APISHIM_SERVER="https://127.0.0.1:${port}"
  fi
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
  echo "[apishim] mode=${mode} port=${port} postgres_port=${postgres_port} env_file=${env_file}" >&2
  health_token="${AE_APISHIM_TOKEN:-}"

  if port_open "127.0.0.1" "$port"; then
    if apishim_is_healthy_once "$port" "$health_token"; then
      already_running=1
    elif [[ "$mode" == "container" || "$mode" == "cri" ]]; then
      # Existing containerized apishim should be recreated to refresh env/tokens.
      already_running=1
    else
      # Host mode: try to recover only if the tracked PID is ours; otherwise fail fast.
      if apishim_pid_alive "$pid_file"; then
        local existing_pid existing_cmd
        existing_pid="$(cat "$pid_file" 2>/dev/null || true)"
        existing_cmd="$(ps -p "$existing_pid" -o cmd= 2>/dev/null || true)"
        if [[ "$existing_cmd" == *"ae.apishim"* ]]; then
          kill "$existing_pid" >/dev/null 2>&1 || true
          sleep 0.2
        fi
        if [[ -n "$existing_pid" ]] && ! kill -0 "$existing_pid" 2>/dev/null; then
          rm -f "$pid_file" >/dev/null 2>&1 || true
        fi
      fi
      if port_open "127.0.0.1" "$port"; then
        echo "error: apishim port ${port} is in use but health checks failed; refusing to continue." >&2
        apishim_port_debug "$port"
        return 1
      fi
    fi
  fi

  if [[ "$already_running" -eq 1 ]]; then
    if [[ "$mode" == "cri" ]]; then
      if is_truthy "${AE_APISHIM_RECREATE_ON_START:-1}"; then
        run_cri_stack up-apishim --profile "$PROFILE" --host "$host" --port "$port" \
          --env-file "$env_file" --cert-file "$cert_file" --key-file "$key_file" --recreate || \
          echo "warning: failed to recreate apishim CRI component" >&2
      fi
    elif [[ "$mode" == "container" ]]; then
      local profile_rel="$profile_dir"
      if [[ "$profile_dir" == "$ROOT_DIR/"* ]]; then
        profile_rel="${profile_dir#"$ROOT_DIR/"}"
      fi
      export APISHIM_ENV_FILE="$env_file"
      export APISHIM_PROFILE_DIR="${APISHIM_PROFILE_DIR:-$profile_rel}"
      export APISHIM_PORT="$port"
      export APISHIM_CONTAINER=1
      APISHIM_PORT="$port" APISHIM_HOST_PORT="${APISHIM_HOST_PORT:-$port}" \
        AE_CONTAINER_CLI="$ENGINE_BIN" APISHIM_CONTAINER=1 \
        "$ROOT_DIR/scripts/ensure_dev_env.sh" >/dev/null 2>&1 || true
      # Keep apishim env in sync with the active profile. This is required for
      # storage seeding and state backend changes (for example AE_STORAGE_NFS_*
      # and AE_STATE_BACKEND=etcd) to take effect between profile restarts.
      if is_truthy "${AE_APISHIM_RECREATE_ON_START:-1}"; then
        if ! APISHIM_PORT="$port" APISHIM_HOST_PORT="${APISHIM_HOST_PORT:-$port}" \
          "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.yaml" \
          up -d --force-recreate apishim; then
          echo "warning: failed to recreate apishim container" >&2
        fi
      fi
    fi
    if ! apishim_wait_healthy "$port" "$health_token" "$startup_timeout"; then
      echo "error: apishim appears unhealthy at https://127.0.0.1:${port}." >&2
      if [[ -f "$profile_dir/apishim.log" ]]; then
        tail -n 80 "$profile_dir/apishim.log" >&2 || true
      fi
      return 1
    fi
    return 0
  fi

  if [[ "$mode" == "host" ]]; then
    local apishim_pythonpath="$ROOT_DIR/src"
    if [[ -n "${PYTHONPATH:-}" ]]; then
      apishim_pythonpath="${apishim_pythonpath}:${PYTHONPATH}"
    fi
    nohup env PYTHONPATH="$apishim_pythonpath" "$PYTHON_BIN" -m ae.apishim serve --host "$host" --port "$port" --tls \
      >"$profile_dir/apishim.log" 2>&1 &
    echo $! > "$pid_file"
    if ! apishim_wait_healthy "$port" "$health_token" "$startup_timeout"; then
      echo "error: apishim failed to start in host mode on https://127.0.0.1:${port}." >&2
      if [[ -f "$profile_dir/apishim.log" ]]; then
        tail -n 80 "$profile_dir/apishim.log" >&2 || true
      fi
      return 1
    fi
    return 0
  fi

  if [[ "$mode" == "cri" ]]; then
    local cri_recreate=()
    if is_truthy "${AE_APISHIM_RECREATE_ON_START:-1}"; then
      cri_recreate=(--recreate)
    fi
    run_cri_stack up-apishim --profile "$PROFILE" --host "$host" --port "$port" \
      --env-file "$env_file" --cert-file "$cert_file" --key-file "$key_file" \
      "${cri_recreate[@]}" || {
      echo "error: failed to start apishim in strict CRI mode." >&2
      return 1
    }
    if ! apishim_wait_healthy "$port" "$health_token" "$startup_timeout"; then
      echo "error: apishim failed to become healthy in CRI mode on https://127.0.0.1:${port}." >&2
      return 1
    fi
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
  APISHIM_PORT="$port" APISHIM_HOST_PORT="${APISHIM_HOST_PORT:-$port}" \
    AE_CONTAINER_CLI="$ENGINE_BIN" APISHIM_CONTAINER=1 \
    "$ROOT_DIR/scripts/ensure_dev_env.sh" >/dev/null 2>&1 || true
  APISHIM_PORT="$port" APISHIM_HOST_PORT="${APISHIM_HOST_PORT:-$port}" \
    "$ENGINE_BIN" compose -f "$ROOT_DIR/ops/dev/docker-compose.yaml" \
    up -d apishim || echo "warning: failed to start apishim container" >&2
  connect_apishim_network
  if ! apishim_wait_healthy "$port" "$health_token" "$startup_timeout"; then
    echo "error: apishim failed to become healthy in container mode on https://127.0.0.1:${port}." >&2
    local apishim_container
    apishim_container="$("$ENGINE_BIN" ps --format '{{.Names}}' 2>/dev/null | awk '/apishim/ {print $1; exit}' || true)"
    if [[ -n "$apishim_container" ]]; then
      "$ENGINE_BIN" logs "$apishim_container" --tail 80 >&2 || true
    fi
    return 1
  fi
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
run_etcd_maintenance_cmd() {
  local action="$1"
  local script="$ROOT_DIR/scripts/dev/etcd_maintenance.sh"
  if [[ ! -x "$script" ]]; then
    echo "warning: etcd maintenance helper not found: $script" >&2
    return 0
  fi
  "$script" "$action"
}

ensure_etcd_maintenance() {
  if ! is_truthy "${AE_ETCD_MAINTENANCE_ENABLE:-1}"; then
    return 0
  fi
  run_etcd_maintenance_cmd status >/dev/null || echo "warning: etcd maintenance status check failed" >&2
  run_etcd_maintenance_cmd watchdog >/dev/null || echo "warning: etcd maintenance watchdog failed" >&2
}

run_ha_core_preflight() {
  PYTHONPATH=src "$PYTHON_BIN" "$ROOT_DIR/scripts/dev/ha_core_preflight.py"
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
  DOCS_LABS_TOKEN="$token" "$PYTHON_BIN" docs/build_docs.py || true
  if [[ -f "docs/site/playground.html" ]]; then
    "$PYTHON_BIN" - <<'PY' "$token" || true
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

require_envoy_core_proxy_config() {
  local cfg="$1"
  [[ -f "$cfg" ]] || {
    echo "error: missing Envoy config: $cfg" >&2
    return 1
  }
  rg -q 'edge_listener_http' "$cfg" || {
    echo "error: Envoy bootstrap missing HTTP listener (edge_listener_http): $cfg" >&2
    return 1
  }
  rg -q 'port_value:[[:space:]]*10080' "$cfg" || {
    echo "error: Envoy bootstrap missing HTTP port 10080: $cfg" >&2
    return 1
  }
}

wait_for_listener_port() {
  local port="$1"
  local timeout_s="${2:-30}"
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    if ss -ltn 2>/dev/null | rg -q ":${port}\\b"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

ensure_core_proxy_transport_ready() {
  local envoy_cfg="$1"
  local timeout_s="${2:-30}"

  require_envoy_core_proxy_config "$envoy_cfg" || return 1

  local missing=()
  local p
  for p in 10080 10443 2333 18080; do
    if ! wait_for_listener_port "$p" "$timeout_s"; then
      missing+=("$p")
    fi
  done

  if (( ${#missing[@]} > 0 )); then
    echo "error: core-proxy transport not ready; missing listener(s): ${missing[*]}" >&2
    return 1
  fi
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
INFRA_BACKEND="$(resolve_infra_backend)"
export AE_INFRA_BACKEND="$INFRA_BACKEND"
if is_strict_cri; then
  if [[ "$RUNTIME_BACKEND_EXPLICIT" -eq 0 ]]; then
    export AE_RUNTIME_BACKEND="cri"
  fi
  export AE_CRI_RUNTIME_HANDLER="${AE_CRI_RUNTIME_HANDLER:-runc}"
  export AE_CRI_SET_HOSTNAME="${AE_CRI_SET_HOSTNAME:-0}"
fi
ENGINE_BIN="$(detect_engine)"

acquire_profile_lock "$PROFILE"
ensure_backend_not_mixed_with_core_cri
ensure_cri_registry_defaults
ensure_cri_registry_trust
ensure_cri_preflight
ensure_cri_registry_ready

if [[ "${BENCH_MODE:-0}" == "1" ]]; then
  export AE_APISHIM_AUTOSTART="${AE_APISHIM_AUTOSTART:-0}"
  export AE_LABS="${AE_LABS:-0}"
  export CORE_CADDY="${CORE_CADDY:-0}"
  export CORE_DOCS="${CORE_DOCS:-0}"
  export AE_DEV_LOCAL="${AE_DEV_LOCAL:-0}"
fi

if [[ -z "${AE_APISHIM_MODE:-}" ]]; then
  if is_strict_cri && [[ "$PROFILE" == "k1s-core" || "$PROFILE" == "k1s-ha-core" ]]; then
    AE_APISHIM_MODE="cri"
  elif is_strict_cri; then
    AE_APISHIM_MODE="host"
  elif [[ "$ENGINE_BIN" == "podman" ]]; then
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

if is_strict_cri; then
  if [[ "${AE_APISHIM_MODE:-host}" == "container" ]]; then
    echo "error: strict CRI infra does not support AE_APISHIM_MODE=container; use AE_APISHIM_MODE=cri or host" >&2
    exit 1
  fi
  if [[ "${AE_APISHIM_MODE:-host}" == "cri" && "$PROFILE" != "k1s-core" && "$PROFILE" != "k1s-ha-core" ]]; then
    echo "error: AE_APISHIM_MODE=cri is currently supported only for k1s-core and k1s-ha-core profiles." >&2
    exit 1
  fi
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
    export AE_ETCD_MAINTENANCE_ENABLE="${AE_ETCD_MAINTENANCE_ENABLE:-1}"
    export AE_ETCD_MAINTENANCE_THRESHOLD_PCT="${AE_ETCD_MAINTENANCE_THRESHOLD_PCT:-80}"
    ensure_etcd_maintenance
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
    PROFILE_DIR="$(abs_path "${PROFILE_DIR:-state/profiles/k1s-core}")"
    POSTGRES_BIND_IP="${POSTGRES_BIND_IP:-127.0.0.1}"
    POSTGRES_PORT="${POSTGRES_PORT:-5432}"
    export POSTGRES_BIND_IP POSTGRES_PORT
    if [[ "${APISHIM_PORT:-}" == "${POSTGRES_PORT}" ]]; then
      echo "warning: APISHIM_PORT (${APISHIM_PORT}) conflicts with POSTGRES_PORT; resetting APISHIM_PORT to 8445." >&2
      unset APISHIM_PORT
    fi
    if [[ -z "${AE_APISHIM_DSN:-}" ]]; then
      if [[ "${AE_APISHIM_MODE:-container}" == "host" || "${AE_APISHIM_MODE:-container}" == "cri" ]]; then
        AE_APISHIM_DSN="postgresql://shim:shim@${POSTGRES_BIND_IP}:${POSTGRES_PORT}/shim"
      else
        AE_APISHIM_DSN="postgresql://shim:shim@postgres:5432/shim"
      fi
    fi
    export AE_APISHIM_DSN
    if is_strict_cri; then
      generated_hub_conf="${PROFILE_DIR}/generated/nats-hub.generated.conf"
      if [[ -z "${AE_NATS_HUB_CONFIG:-}" && -f "$generated_hub_conf" ]]; then
        export AE_NATS_HUB_CONFIG="$generated_hub_conf"
      fi
      ensure_cri_registry_preload_images
      run_cri_stack up-core-base --profile k1s-core
    else
      compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d etcd nats-hub postgres
    fi
    SPECS_DIR="$(abs_path "${SPECS_DIR:-$PROFILE_DIR/specs}")"
    ensure_specs_dir "$SPECS_DIR"
    export DEV_PROFILE_DIR="$PROFILE_DIR"
    export AE_SPECS_DIR="$SPECS_DIR"
    export AE_PROJECTION_ROOT="${AE_PROJECTION_ROOT:-$PROFILE_DIR/projections}"
    export AE_STATE_BACKEND="${AE_STATE_BACKEND:-etcd}"
    export AE_ETCD_ENDPOINTS="${AE_ETCD_ENDPOINTS:-http://127.0.0.1:2379}"
    export AE_ETCD_PREFIX="${AE_ETCD_PREFIX:-k1s/profiles/k1s-core}"
    export AE_ETCD_MAINTENANCE_ENABLE="${AE_ETCD_MAINTENANCE_ENABLE:-1}"
    export AE_ETCD_MAINTENANCE_THRESHOLD_PCT="${AE_ETCD_MAINTENANCE_THRESHOLD_PCT:-80}"
    ensure_etcd_maintenance
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-nats-js}"
    export AE_NATS_URL="${AE_NATS_URL:-nats://hub-controller:dev@127.0.0.1:4222}"
    export AE_JS_DOMAIN="${AE_JS_DOMAIN:-K1S}"
    export AE_NODE_PROFILE="${AE_NODE_PROFILE:-k1s-core}"
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
      export AE_EDGE_INGRESS_RATHOLE_RELOAD="${AE_EDGE_INGRESS_RATHOLE_RELOAD:-1}"
      export AE_RATHOLE_INOTIFY_AUTOTUNE="${AE_RATHOLE_INOTIFY_AUTOTUNE:-1}"
      export AE_RATHOLE_INOTIFY_WARN_PCT="${AE_RATHOLE_INOTIFY_WARN_PCT:-95}"
      export AE_RATHOLE_INOTIFY_MAX_WATCHES="${AE_RATHOLE_INOTIFY_MAX_WATCHES:-4194304}"
    else
      export AE_EDGE_INGRESS_CORE_PROXY=0
      unset AE_EDGE_INGRESS_RATHOLE_RELOAD
    fi
    if [[ "$EDGE_INGRESS_START" == "1" ]]; then
      write_envoy_bootstrap "$EDGE_ENVOY_CONFIG"
      write_rathole_server_bootstrap "$EDGE_RATHOLE_SERVER" "$AE_RATHOLE_BIND_ADDR" "$AE_RATHOLE_DEFAULT_TOKEN"
      ENVOY_CONTAINER="${ENVOY_CONTAINER:-k1s-core-envoy}"
      RATHOLE_SERVER_CONTAINER="${RATHOLE_SERVER_CONTAINER:-k1s-core-rathole}"
      if [[ "$INGRESS_MODE" == "core-proxy" ]]; then
        if is_strict_cri; then
          export AE_EDGE_INGRESS_RELOAD_CMD="$PYTHON_BIN $ROOT_DIR/scripts/dev/cri_stack.py up-envoy --profile k1s-core --config $EDGE_ENVOY_CONFIG"
          export AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD="$PYTHON_BIN $ROOT_DIR/scripts/dev/cri_stack.py up-rathole-server --profile k1s-core --config $EDGE_RATHOLE_SERVER"
          run_cri_stack up-envoy --profile k1s-core --config "$EDGE_ENVOY_CONFIG"
          run_cri_stack up-rathole-server --profile k1s-core --config "$EDGE_RATHOLE_SERVER"
        else
          export AE_EDGE_INGRESS_RELOAD_CMD="$ENGINE_BIN restart $ENVOY_CONTAINER"
          export AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD="$ENGINE_BIN restart $RATHOLE_SERVER_CONTAINER"
          start_envoy_container "$ENVOY_CONTAINER" "$EDGE_ENVOY_CONFIG"
          start_rathole_server_container "$RATHOLE_SERVER_CONTAINER" "$EDGE_RATHOLE_SERVER"
        fi
      elif [[ "$INGRESS_MODE" == "core-to-edge-public" ]]; then
        if is_strict_cri; then
          export AE_EDGE_INGRESS_RELOAD_CMD="$PYTHON_BIN $ROOT_DIR/scripts/dev/cri_stack.py up-envoy --profile k1s-core --config $EDGE_ENVOY_CONFIG"
          unset AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD
          run_cri_stack up-envoy --profile k1s-core --config "$EDGE_ENVOY_CONFIG"
        else
          export AE_EDGE_INGRESS_RELOAD_CMD="$ENGINE_BIN restart $ENVOY_CONTAINER"
          unset AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD
          start_envoy_container "$ENVOY_CONTAINER" "$EDGE_ENVOY_CONFIG"
        fi
      fi
    fi
    METRICS_PORT="${METRICS_PORT:-9108}"
    export APISHIM_PORT="${APISHIM_PORT:-8445}"
    if [[ "${APISHIM_PORT}" == "${POSTGRES_PORT}" ]]; then
      echo "warning: APISHIM_PORT (${APISHIM_PORT}) conflicts with POSTGRES_PORT; forcing APISHIM_PORT=8445." >&2
      APISHIM_PORT="8445"
      export APISHIM_PORT
    fi
    export APISHIM_HOST_PORT="${APISHIM_HOST_PORT:-$APISHIM_PORT}"
    if [[ "${APISHIM_HOST_PORT}" == "${POSTGRES_PORT}" || "${APISHIM_HOST_PORT}" == "5432" ]]; then
      APISHIM_HOST_PORT="8445"
      export APISHIM_HOST_PORT
    fi
    start_apishim "$PROFILE_DIR"
    if [[ "${AE_LABS:-0}" == "1" ]]; then
      build_docs_with_labs_token
    fi
    if [[ "${CORE_CADDY:-0}" == "1" ]]; then
      if is_strict_cri; then
        caddy_https_port="${CADDY_HTTPS_PORT:-8443}"
        caddy_sites="$ROOT_DIR/state/caddy"
        mkdir -p "$caddy_sites"
        render_strict_caddy_sites "$caddy_https_port"
        write_dash_caddy_site "127.0.0.1" "${caddy_sites}/dash.caddy" "$caddy_https_port"
        run_cri_stack up-caddy --profile k1s-core --metrics-port "$METRICS_PORT" --apishim-port "${APISHIM_PORT:-8445}" --recreate
      else
        export AE_CADDY_CONTAINER="${AE_CADDY_CONTAINER:-dev-caddy-1}"
        export AE_CONTAINER_CLI="${AE_CONTAINER_CLI:-$ENGINE_BIN}"
        export AE_CADDY_FILE="${AE_CADDY_FILE:-/etc/caddy/Caddyfile}"
        export AE_CADDY_SITES="${AE_CADDY_SITES:-$ROOT_DIR/state/caddy}"
        start_caddy
      fi
    fi
    if [[ "${CORE_DOCS:-0}" == "1" ]]; then
      start_docs_server
    fi
    ensure_dev_local "$PROFILE_DIR"
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.controller --loop --metrics-port "$METRICS_PORT" --watch
    ;;
  k1s-ha-core)
    PROFILE_DIR="$(abs_path "${PROFILE_DIR:-state/profiles/k1s-ha-core}")"
    mkdir -p "$PROFILE_DIR"
    if ! is_strict_cri; then
      echo "error: k1s-ha-core requires strict CRI infra (AE_RUNTIME_BACKEND=cri AE_INFRA_BACKEND=cri)." >&2
      exit 1
    fi
    export DEV_PROFILE_DIR="$PROFILE_DIR"
    export AE_HA_MODE=1
    export AE_STATE_BACKEND="${AE_STATE_BACKEND:-etcd}"
    export AE_TRANSPORT_BACKEND="${AE_TRANSPORT_BACKEND:-nats-js}"
    export AE_JS_DOMAIN="${AE_JS_DOMAIN:-K1S}"
    export AE_NODE_PROFILE="${AE_NODE_PROFILE:-k1s-ha-core}"
    export AE_APISHIM_MODE="${AE_APISHIM_MODE:-cri}"
    export AE_ETCD_MAINTENANCE_ENABLE="${AE_ETCD_MAINTENANCE_ENABLE:-0}"
    export AE_ETCD_MAINTENANCE_THRESHOLD_PCT="${AE_ETCD_MAINTENANCE_THRESHOLD_PCT:-80}"
    export AE_APISHIM_ETCD_ENDPOINTS="${AE_APISHIM_ETCD_ENDPOINTS:-${AE_ETCD_ENDPOINTS:-}}"
    export AE_PROJECTION_ROOT="${AE_PROJECTION_ROOT:-$PROFILE_DIR/projections}"
    if [[ "${AE_DEV_LOCAL:-0}" == "1" ]]; then
      export AE_REGISTER_LOCAL_NODE="${AE_REGISTER_LOCAL_NODE:-1}"
      export AE_LABS="${AE_LABS:-1}"
      export CORE_CADDY="${CORE_CADDY:-1}"
      export CORE_DOCS="${CORE_DOCS:-1}"
    else
      export AE_LABS="${AE_LABS:-0}"
      export CORE_CADDY="${CORE_CADDY:-0}"
      export CORE_DOCS="${CORE_DOCS:-0}"
    fi
    ensure_cri_registry_preload_images
    run_ha_core_preflight
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
      export AE_EDGE_INGRESS_RATHOLE_RELOAD="${AE_EDGE_INGRESS_RATHOLE_RELOAD:-1}"
      export AE_RATHOLE_INOTIFY_AUTOTUNE="${AE_RATHOLE_INOTIFY_AUTOTUNE:-1}"
      export AE_RATHOLE_INOTIFY_WARN_PCT="${AE_RATHOLE_INOTIFY_WARN_PCT:-95}"
      export AE_RATHOLE_INOTIFY_MAX_WATCHES="${AE_RATHOLE_INOTIFY_MAX_WATCHES:-4194304}"
    else
      export AE_EDGE_INGRESS_CORE_PROXY=0
      unset AE_EDGE_INGRESS_RATHOLE_RELOAD
    fi
    if [[ "$EDGE_INGRESS_START" == "1" ]]; then
      write_envoy_bootstrap "$EDGE_ENVOY_CONFIG"
      write_rathole_server_bootstrap "$EDGE_RATHOLE_SERVER" "$AE_RATHOLE_BIND_ADDR" "$AE_RATHOLE_DEFAULT_TOKEN"
      ENVOY_CONTAINER="${ENVOY_CONTAINER:-k1s-ha-core-envoy}"
      RATHOLE_SERVER_CONTAINER="${RATHOLE_SERVER_CONTAINER:-k1s-ha-core-rathole}"
      if [[ "$INGRESS_MODE" == "core-proxy" ]]; then
        export AE_EDGE_INGRESS_RELOAD_CMD="$PYTHON_BIN $ROOT_DIR/scripts/dev/cri_stack.py up-envoy --profile k1s-ha-core --config $EDGE_ENVOY_CONFIG"
        export AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD="$PYTHON_BIN $ROOT_DIR/scripts/dev/cri_stack.py up-rathole-server --profile k1s-ha-core --config $EDGE_RATHOLE_SERVER"
        run_cri_stack up-envoy --profile k1s-ha-core --config "$EDGE_ENVOY_CONFIG"
        run_cri_stack up-rathole-server --profile k1s-ha-core --config "$EDGE_RATHOLE_SERVER"
      elif [[ "$INGRESS_MODE" == "core-to-edge-public" ]]; then
        export AE_EDGE_INGRESS_RELOAD_CMD="$PYTHON_BIN $ROOT_DIR/scripts/dev/cri_stack.py up-envoy --profile k1s-ha-core --config $EDGE_ENVOY_CONFIG"
        unset AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD
        run_cri_stack up-envoy --profile k1s-ha-core --config "$EDGE_ENVOY_CONFIG"
      fi
    fi
    METRICS_PORT="${METRICS_PORT:-9108}"
    export APISHIM_PORT="${APISHIM_PORT:-8445}"
    export APISHIM_HOST_PORT="${APISHIM_HOST_PORT:-$APISHIM_PORT}"
    start_apishim "$PROFILE_DIR"
    if [[ "${AE_LABS:-0}" == "1" ]]; then
      build_docs_with_labs_token
    fi
    if [[ "${CORE_CADDY:-0}" == "1" ]]; then
      caddy_https_port="${CADDY_HTTPS_PORT:-8443}"
      caddy_sites="$ROOT_DIR/state/caddy"
      mkdir -p "$caddy_sites"
      render_strict_caddy_sites "$caddy_https_port"
      write_dash_caddy_site "127.0.0.1" "${caddy_sites}/dash.caddy" "$caddy_https_port"
      run_cri_stack up-caddy --profile k1s-ha-core --metrics-port "$METRICS_PORT" --apishim-port "${APISHIM_PORT:-8445}" --recreate
    fi
    if [[ "${CORE_DOCS:-0}" == "1" ]]; then
      start_docs_server
    fi
    ensure_dev_local "$PROFILE_DIR"
    PYTHONPATH=src exec "$PYTHON_BIN" -m ae.controller --loop --metrics-port "$METRICS_PORT"
    ;;
  k1s-edge)
    COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/ops/dev/docker-compose.nats-etcd.yaml}"
    export AE_SITE_ID="${AE_SITE_ID:-sfo-edge-01}"
    export AE_NODE_ID="${AE_NODE_ID:-edge-node-1}"
    EDGE_START_NATS="${EDGE_START_NATS:-1}"
    if [[ "$EDGE_START_NATS" == "1" ]]; then
      if is_strict_cri; then
        ensure_cri_registry_preload_images
        strict_edge_port="$(resolve_strict_edge_port)"
        strict_edge_http_port="$(resolve_strict_edge_http_port)"
        strict_hub_host="$(resolve_strict_edge_hub_leaf_host)"
        strict_hub_port="$(resolve_strict_edge_hub_leaf_port)"
        strict_edge_conf="$(prepare_strict_edge_nats_config "$AE_SITE_ID" "$strict_edge_port" "$strict_edge_http_port" "$strict_hub_host" "$strict_hub_port")"
        run_cri_stack up-edge-nats --profile "${EDGE_PROFILE:-k1s-edge}" --site-id "$AE_SITE_ID" --config "$strict_edge_conf" --recreate
      else
        compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d nats-edge
      fi
    fi
    EDGE_START_POSTGRES="${EDGE_START_POSTGRES:-0}"
    if [[ "$EDGE_START_POSTGRES" == "1" ]]; then
      POSTGRES_BIND_IP="${POSTGRES_BIND_IP:-127.0.0.1}"
      POSTGRES_PORT="${POSTGRES_PORT:-5432}"
      export POSTGRES_BIND_IP POSTGRES_PORT
      if is_strict_cri; then
        echo "error: EDGE_START_POSTGRES=1 is not supported in strict CRI profile mode" >&2
        exit 1
      else
        compose "$ENGINE_BIN" -f "$COMPOSE_FILE" up -d postgres
      fi
    fi
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
    if [[ -z "${AE_NODE_LABELS:-}" ]]; then
      export AE_NODE_LABELS="role=gateway,profile=${EDGE_PROFILE}"
    fi
    EDGE_RATHOLE_CLIENT="${EDGE_RATHOLE_CLIENT:-$EDGE_INGRESS_DIR/rathole-client-${AE_SITE_ID}.toml}"
    edge_nats_port_default="4223"
    if is_strict_cri; then
      edge_nats_port_default="$(resolve_strict_edge_port)"
    fi
    export AE_NATS_URL="${AE_NATS_URL:-nats://gateway:dev@127.0.0.1:${edge_nats_port_default}}"
    export AE_JS_DOMAIN="${AE_JS_DOMAIN:-K1S}"
    export AE_GATEWAY_SPOOL_PATH="${AE_GATEWAY_SPOOL_PATH:-$PROFILE_DIR/gateway-${AE_SITE_ID}-${AE_NODE_ID}.db}"
    export AE_EDGE_INGRESS_LOCAL_ADDR="${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081}"
    export AE_RATHOLE_DEFAULT_TOKEN="${AE_RATHOLE_DEFAULT_TOKEN:-dev}"
    export AE_RATHOLE_SERVER_ADDR="${AE_RATHOLE_SERVER_ADDR:-127.0.0.1:2333}"
    if [[ "$INGRESS_MODE" == "edge-local" ]]; then
      edge_local_config_dir="${AE_EDGE_LOCAL_INGRESS_CONFIG_DIR:-$PROFILE_DIR/edge-local}"
      edge_local_config_dir="$(abs_path "$edge_local_config_dir")"
      export AE_EDGE_LOCAL_INGRESS_CONFIG_DIR="$edge_local_config_dir"
      mkdir -p "$AE_EDGE_LOCAL_INGRESS_CONFIG_DIR"
      if [[ ! -w "$AE_EDGE_LOCAL_INGRESS_CONFIG_DIR" ]]; then
        echo "error: AE_EDGE_LOCAL_INGRESS_CONFIG_DIR is not writable: $AE_EDGE_LOCAL_INGRESS_CONFIG_DIR" >&2
        echo "hint: set AE_EDGE_LOCAL_INGRESS_CONFIG_DIR to a writable path (for example state/profiles/k1s-core/edge-local)." >&2
        exit 1
      fi
      echo "edge-local ingress config dir: $AE_EDGE_LOCAL_INGRESS_CONFIG_DIR"
    fi
    if [[ "$INGRESS_MODE" == "core-proxy" && "$EDGE_INGRESS_START" == "1" ]]; then
      write_rathole_client_config "$EDGE_RATHOLE_CLIENT" "$AE_RATHOLE_SERVER_ADDR" "$AE_RATHOLE_DEFAULT_TOKEN" "$AE_SITE_ID" "$AE_EDGE_INGRESS_LOCAL_ADDR"
      RATHOLE_CLIENT_CONTAINER="${RATHOLE_CLIENT_CONTAINER:-k1s-edge-${AE_SITE_ID}-${AE_NODE_ID}-rathole}"
      if is_strict_cri; then
        run_cri_stack up-rathole-client --profile "$EDGE_PROFILE" --site-id "$AE_SITE_ID" --node-id "$AE_NODE_ID" --config "$EDGE_RATHOLE_CLIENT"
      else
        start_rathole_client_container "$RATHOLE_CLIENT_CONTAINER" "$EDGE_RATHOLE_CLIENT"
      fi
    fi
    EDGE_START_WORKER="${EDGE_START_WORKER:-1}"
    if [[ "$EDGE_START_WORKER" == "1" ]]; then
      WORKER_NODE_ID="${EDGE_WORKER_NODE_ID:-$AE_NODE_ID}"
      WORKER_NATS_URL="${EDGE_WORKER_NATS_URL:-nats://worker:dev@127.0.0.1:4223}"
      WORKER_DELAY_MS="${EDGE_WORKER_DELAY_MS:-50}"
      WORKER_PROGRESS="${EDGE_WORKER_PROGRESS:-5}"
      WORKER_LOG_LEVEL="${EDGE_WORKER_LOG_LEVEL:-}"
      WORKER_LOG_FLAGS=()
      if [[ -n "$WORKER_LOG_LEVEL" ]]; then
        WORKER_LOG_FLAGS=(--log-level "$WORKER_LOG_LEVEL")
      fi
      PYTHONPATH=src "$PYTHON_BIN" -m ae.worker_stub \
        --node-id "$WORKER_NODE_ID" \
        --nats-url "$WORKER_NATS_URL" \
        --delay-ms "$WORKER_DELAY_MS" \
        --progress-interval "$WORKER_PROGRESS" \
        "${WORKER_LOG_FLAGS[@]}" &
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
