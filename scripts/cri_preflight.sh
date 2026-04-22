#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${ROOT_DIR}/scripts/lib/nixos_bridge.sh" ]]; then
  source "${ROOT_DIR}/scripts/lib/nixos_bridge.sh"
fi

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
crictl_bin="${CRICTL_BIN:-crictl}"
require_network_ready="${AE_CRI_REQUIRE_NETWORK_READY:-0}"
if [[ -n "${AE_CRI_REQUIRE_RUNTIME_READY:-}" ]]; then
  require_runtime_ready="${AE_CRI_REQUIRE_RUNTIME_READY}"
else
  # Default to strict runtime readiness only when running as root.
  require_runtime_ready=$([[ "${EUID}" -eq 0 ]] && echo "1" || echo "0")
fi
required_runtime_handler="${AE_CRI_RUNTIME_HANDLER:-runc}"
python_bin="${PYTHON_BIN:-}"

if [[ -z "$python_bin" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    python_bin="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    python_bin="$(command -v python)"
  else
    echo "python3 or python not found" >&2
    exit 1
  fi
fi

if [[ "$endpoint" == unix://* ]]; then
  sock="${endpoint#unix://}"
  if [[ ! -S "$sock" ]]; then
    echo "CRI socket not found: $sock" >&2
    exit 1
  fi
else
  echo "Non-unix CRI endpoint configured: $endpoint"
fi

if ! command -v "$crictl_bin" >/dev/null 2>&1; then
  echo "crictl not found; install for debugging" >&2
else
  echo "crictl: $(command -v "$crictl_bin")"
  info_tmp="$(mktemp)"
  err_tmp="$(mktemp)"
  trap 'rm -f "$info_tmp" "$err_tmp"' EXIT
  if ! "$crictl_bin" --runtime-endpoint "$endpoint" info >"$info_tmp" 2>"$err_tmp"; then
    echo "crictl info failed for endpoint: $endpoint" >&2
    if [[ -s "$err_tmp" ]]; then
      echo "$(tr '\n' ' ' <"$err_tmp")" >&2
    fi
    if grep -qi "permission denied" "$err_tmp"; then
      echo "hint: run strict CRI profiles with sudo -E when containerd is root-only" >&2
      echo "hint: or grant temporary access via ./scripts/containerd_socket_access.sh --grant" >&2
    fi
    if [[ "$require_runtime_ready" == "1" || "$require_network_ready" == "1" ]]; then
      exit 1
    fi
  else
    "$python_bin" - "$info_tmp" "$require_runtime_ready" "$require_network_ready" "$required_runtime_handler" <<'PY'
import json
import sys

path = sys.argv[1]
require_runtime = sys.argv[2] == "1"
require_network = sys.argv[3] == "1"
required_handler = str(sys.argv[4] or "").strip()

with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

conds = {}
for cond in (data.get("status", {}) or {}).get("conditions", []) or []:
    ctype = str(cond.get("type") or "")
    cstatus = bool(cond.get("status"))
    conds[ctype] = (cstatus, str(cond.get("message") or ""))

runtime_ready, runtime_msg = conds.get("RuntimeReady", (False, "missing condition"))
network_ready, network_msg = conds.get("NetworkReady", (False, "missing condition"))

print(f"CRI condition RuntimeReady={runtime_ready}")
if runtime_msg:
    print(f"RuntimeReady message: {runtime_msg}")
print(f"CRI condition NetworkReady={network_ready}")
if network_msg:
    print(f"NetworkReady message: {network_msg}")

# Best effort runtime handler check.
runtime_cfg = ((data.get("config") or {}).get("containerd") or {})
default_handler = str(
    runtime_cfg.get("defaultRuntimeName")
    or runtime_cfg.get("default_runtime_name")
    or ""
).strip()
runtimes = runtime_cfg.get("runtimes") or {}
runtime_names = set()
if isinstance(runtimes, dict):
    runtime_names.update(str(k) for k in runtimes.keys())
if default_handler:
    runtime_names.add(default_handler)
if required_handler:
    print(f"CRI required runtime handler={required_handler}")
    if runtime_names:
        print("CRI available runtime handlers=" + ",".join(sorted(runtime_names)))
    if runtime_names and required_handler not in runtime_names:
        sys.exit(4)

if require_runtime and not runtime_ready:
    sys.exit(2)
if require_network and not network_ready:
    sys.exit(3)
PY
    rc=$?
    if [[ $rc -eq 2 ]]; then
      echo "RuntimeReady is false" >&2
      exit 1
    elif [[ $rc -eq 3 ]]; then
      echo "NetworkReady is false" >&2
      exit 1
    elif [[ $rc -eq 4 ]]; then
      echo "required CRI runtime handler '${required_runtime_handler}' is unavailable" >&2
      exit 1
    fi
  fi
fi

if [[ "${AE_ENABLE_SERVICE_PROXY:-0}" == "1" ]]; then
  provider="${AE_SERVICE_PROVIDER:-iptables}"
  if [[ "$provider" == "iptables" || "$provider" == "kubeproxy" || "$provider" == "cri" ]]; then
    ipt="${AE_IPTABLES_BIN:-iptables}"
    if ! command -v "$ipt" >/dev/null 2>&1; then
      echo "iptables not found; Service VIP proxy requires $ipt on PATH" >&2
    fi
    if [[ "${EUID}" -ne 0 ]]; then
      echo "Service VIP proxy requires root (iptables NAT rules)" >&2
    fi
  fi
fi

nixos_missing_cri_module=0
if declare -F k1s_is_nixos >/dev/null 2>&1 && k1s_is_nixos /etc/os-release; then
  if declare -F k1s_containerd_cni_env >/dev/null 2>&1; then
    resolved_cni_env="$(k1s_containerd_cni_env || true)"
    if [[ -n "$resolved_cni_env" ]]; then
      eval "$resolved_cni_env"
    fi
  fi
  if declare -F k1s_nixos_cri_module_imported >/dev/null 2>&1; then
    nixos_root="${AE_NIXOS_SEARCH_ROOT:-$(k1s_nixos_search_root)}"
    cri_module_dest="$(k1s_nixos_cri_module_dest)"
    if ! k1s_nixos_cri_module_imported "$nixos_root" "$cri_module_dest"; then
      nixos_missing_cri_module=1
    fi
  fi
fi

cni_bin="${CNI_BIN_DIR:-/opt/cni/bin}"
cni_conf="${CNI_CONF_DIR:-/etc/cni/net.d}"
required_plugins_raw="${AE_CNI_REQUIRED_PLUGINS:-bridge,portmap,firewall,tuning,loopback}"
if [[ "$nixos_missing_cri_module" -eq 1 ]]; then
  echo "NixOS strict CRI requires the k1s CRI host module" >&2
  if declare -F k1s_nixos_cri_bootstrap_instructions >/dev/null 2>&1; then
    k1s_nixos_cri_bootstrap_instructions "$ROOT_DIR" "$cri_module_dest" >&2
  fi
  exit 1
fi
if [[ ! -d "$cni_bin" ]]; then
  echo "CNI bin dir missing: $cni_bin" >&2
  path_plugins=()
  for plugin in $(printf '%s' "$required_plugins_raw" | tr ',' ' '); do
    candidate="$(command -v "$plugin" 2>/dev/null || true)"
    [[ -n "$candidate" ]] || continue
    resolved="$candidate"
    if command -v readlink >/dev/null 2>&1; then
      resolved="$(readlink -f "$candidate" 2>/dev/null || printf '%s' "$candidate")"
    fi
    if [[ "$candidate" == /run/current-system/sw/bin/* || "$candidate" == *"/cni/"* || "$candidate" == *"cni-plugins"* || "$resolved" == *"/cni/"* || "$resolved" == *"cni-plugins"* ]]; then
      path_plugins+=("$plugin")
    fi
  done
  if [[ "${#path_plugins[@]}" -gt 0 ]]; then
    echo "CNI plugins detected on PATH but not at ${cni_bin}: ${path_plugins[*]}" >&2
    echo "hint: run scripts/cni_bin_bootstrap.sh or retry strict profile startup to materialize ${cni_bin}" >&2
  fi
  exit 1
fi
missing_plugins=()
for plugin in $(printf '%s' "$required_plugins_raw" | tr ',' ' '); do
  [[ -x "${cni_bin%/}/${plugin}" ]] || missing_plugins+=("$plugin")
done
if [[ "${#missing_plugins[@]}" -gt 0 ]]; then
  echo "CNI plugins missing from ${cni_bin}: ${missing_plugins[*]}" >&2
  echo "hint: run scripts/cni_bin_bootstrap.sh to sync required plugins into ${cni_bin}" >&2
  exit 1
fi
if [[ ! -d "$cni_conf" ]]; then
  echo "CNI config dir missing: $cni_conf" >&2
  exit 1
fi

echo "CRI preflight OK"
