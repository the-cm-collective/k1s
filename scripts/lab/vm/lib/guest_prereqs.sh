#!/usr/bin/env bash

vm_bootstrap_autofix_enabled() {
  case "${AE_VM_BOOTSTRAP_AUTOFIX:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

vm_bootstrap_missing_prereqs() {
  local missing=()

  command -v python3 >/dev/null 2>&1 || missing+=("python3")
  command -v python >/dev/null 2>&1 || missing+=("python")
  command -v crictl >/dev/null 2>&1 || missing+=("crictl")

  [[ -f /etc/crictl.yaml ]] || missing+=("/etc/crictl.yaml")
  [[ -S /run/containerd/containerd.sock ]] || missing+=("/run/containerd/containerd.sock")
  [[ -f /etc/containerd/config.toml ]] || missing+=("/etc/containerd/config.toml")
  [[ -d /opt/cni/bin ]] || missing+=("/opt/cni/bin")

  if [[ -d /opt/cni/bin ]] && ! find /opt/cni/bin -mindepth 1 -maxdepth 1 | read -r _; then
    missing+=("/opt/cni/bin/*")
  fi

  if ! sudo find /etc/cni/net.d -maxdepth 1 -type f \
    \( -name '*.conf' -o -name '*.conflist' \) -print -quit 2>/dev/null \
    | grep -q .; then
    missing+=("/etc/cni/net.d")
  fi

  if [[ -f /etc/containerd/config.toml ]] \
    && ! sudo containerd --config /etc/containerd/config.toml config dump >/dev/null 2>&1; then
    missing+=("containerd-config-valid")
  fi

  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi

  printf '%s\n' "${missing[@]}"
}

vm_bootstrap_collect_missing_prereqs() {
  local -n out_ref="$1"
  local item=""

  out_ref=()
  while IFS= read -r item; do
    [[ -n "$item" ]] || continue
    out_ref+=("$item")
  done < <(vm_bootstrap_missing_prereqs)
}

ensure_vm_bootstrap_prereqs() {
  local missing=()
  vm_bootstrap_collect_missing_prereqs missing
  if [[ "${#missing[@]}" -eq 0 ]]; then
    echo "[vm-prereqs] ready"
    return 0
  fi

  if vm_bootstrap_autofix_enabled; then
    echo "[vm-prereqs] autofix enabled; repairing: ${missing[*]}" >&2
    if ! command -v python >/dev/null 2>&1; then
      sudo env DEBIAN_FRONTEND=noninteractive apt-get update
      sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python-is-python3
    fi
    (
      cd /mnt/host \
        && sudo -E \
          DEBIAN_FRONTEND=noninteractive \
          AE_CRI_ENDPOINT="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}" \
          ./scripts/cri_ci_setup.sh
    )
    vm_bootstrap_collect_missing_prereqs missing
    if [[ "${#missing[@]}" -eq 0 ]]; then
      echo "[vm-prereqs] ready after autofix"
      return 0
    fi
    echo "[vm-prereqs] autofix incomplete: ${missing[*]}" >&2
  fi

  echo "[vm-prereqs] stale VM image; missing prerequisites: ${missing[*]}" >&2
  echo "[vm-prereqs] rebuild and verify images:" >&2
  echo "[vm-prereqs]   scripts/lab/vm/labctl.sh image build --variant all" >&2
  echo "[vm-prereqs]   scripts/lab/vm/labctl.sh image verify --variant all" >&2
  echo "[vm-prereqs] set AE_VM_BOOTSTRAP_AUTOFIX=1 only for manual debug recovery" >&2
  return 1
}
