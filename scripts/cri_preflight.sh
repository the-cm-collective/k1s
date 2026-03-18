#!/usr/bin/env bash
set -euo pipefail

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
  trap 'rm -f "$info_tmp"' EXIT
  if ! "$crictl_bin" --runtime-endpoint "$endpoint" info >"$info_tmp" 2>/dev/null; then
    echo "crictl info failed for endpoint: $endpoint" >&2
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

cni_bin="${CNI_BIN_DIR:-/opt/cni/bin}"
cni_conf="${CNI_CONF_DIR:-/etc/cni/net.d}"
if [[ ! -d "$cni_bin" ]]; then
  echo "CNI bin dir missing: $cni_bin" >&2
  exit 1
fi
if [[ ! -d "$cni_conf" ]]; then
  echo "CNI config dir missing: $cni_conf" >&2
  exit 1
fi

echo "CRI preflight OK"
