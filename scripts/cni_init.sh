#!/usr/bin/env bash
set -euo pipefail

conf_dir="${CNI_CONF_DIR:-/etc/cni/net.d}"
bridge_file="${CNI_BRIDGE_FILE:-$conf_dir/10-k1s-bridge.conflist}"
loopback_file="${CNI_LOOPBACK_FILE:-$conf_dir/99-loopback.conf}"
bridge_name="${AE_CNI_BRIDGE_NAME:-cni0}"
subnet="${AE_CNI_SUBNET:-10.88.0.0/16}"
cni_version="${AE_CNI_VERSION:-0.4.0}"
force="${AE_CNI_FORCE:-0}"

need_sudo=0
if [[ ! -d "$conf_dir" ]]; then
  need_sudo=1
elif [[ ! -w "$conf_dir" ]]; then
  need_sudo=1
fi

if [[ $need_sudo -eq 1 && $EUID -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to write to $conf_dir" >&2
    exit 1
  fi
fi

run_cmd() {
  if [[ $need_sudo -eq 1 && $EUID -ne 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}

write_file() {
  local path="$1"
  local content="$2"
  if [[ $need_sudo -eq 1 && $EUID -ne 0 ]]; then
    printf "%s" "$content" | sudo tee "$path" >/dev/null
  else
    printf "%s" "$content" > "$path"
  fi
}

run_cmd mkdir -p "$conf_dir"

if [[ "$force" == "1" ]] && command -v ip >/dev/null 2>&1; then
  if run_cmd ip link show "$bridge_name" >/dev/null 2>&1; then
    run_cmd ip addr flush dev "$bridge_name" >/dev/null 2>&1 || true
    echo "Flushed existing bridge addresses on $bridge_name"
  fi
fi

# Detect existing non-loopback CNI config
has_non_loopback=0
if [[ "$force" != "1" ]] && find "$conf_dir" -maxdepth 1 -type f \
  \( -name '*.conf' -o -name '*.conflist' \) -print -quit | grep -q .; then
  for cfg in "$conf_dir"/*; do
    base="$(basename "$cfg")"
    case "$base" in
      *loopback* ) continue ;;
      *.conf|*.conflist ) has_non_loopback=1 ; break ;;
    esac
  done
fi

if [[ "$force" == "1" ]]; then
  echo "AE_CNI_FORCE=1 set; rewriting bridge and loopback configs"
fi

if [[ $has_non_loopback -eq 0 ]]; then
  bridge_cfg=$(cat <<EOF
{
  "cniVersion": "${cni_version}",
  "name": "${bridge_name}",
  "plugins": [
    {
      "type": "bridge",
      "bridge": "${bridge_name}",
      "isGateway": true,
      "ipMasq": true,
      "promiscMode": true,
      "ipam": {
        "type": "host-local",
        "ranges": [[ { "subnet": "${subnet}" } ]],
        "routes": [ { "dst": "0.0.0.0/0" } ]
      }
    },
    { "type": "portmap", "capabilities": { "portMappings": true } },
    { "type": "firewall" },
    { "type": "tuning" }
  ]
}
EOF
)
  write_file "$bridge_file" "$bridge_cfg"
  echo "Wrote bridge CNI config: $bridge_file"
else
  echo "Existing non-loopback CNI config found; skipping bridge config"
fi

if [[ "$force" == "1" || ! -f "$loopback_file" ]]; then
  loopback_cfg=$(cat <<EOF
{
  "cniVersion": "${cni_version}",
  "name": "lo",
  "type": "loopback"
}
EOF
)
  write_file "$loopback_file" "$loopback_cfg"
  echo "Wrote loopback CNI config: $loopback_file"
else
  echo "Loopback config already present: $loopback_file"
fi

echo "Done. If NetworkReady is still false, restart containerd and re-check with crictl info."
