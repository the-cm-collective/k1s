#!/usr/bin/env bash
set -euo pipefail

if [[ "${ACT:-}" == "true" ]]; then
  echo "ACT detected; skipping CRI setup"
  exit 0
fi

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
cni_version="${AE_CNI_VERSION:-1.0.0}"
cni_force="${AE_CNI_FORCE:-1}"

run_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    sudo -E "$@"
  else
    "$@"
  fi
}

echo "Installing containerd, CNI plugins, and crictl (via cri-tools)..."
run_root apt-get update
run_root apt-get install -y containerd containernetworking-plugins cri-tools iptables

echo "Ensuring containerd config exists..."
run_root mkdir -p /etc/containerd
if [[ ! -f /etc/containerd/config.toml ]]; then
  run_root containerd config default | run_root tee /etc/containerd/config.toml >/dev/null
fi

echo "Configuring crictl endpoint..."
run_root tee /etc/crictl.yaml >/dev/null <<EOF
runtime-endpoint: ${endpoint}
image-endpoint: ${endpoint}
timeout: 10
debug: false
EOF

echo "Starting containerd..."
run_root systemctl enable --now containerd || run_root service containerd start

echo "Ensuring CNI binaries are in /opt/cni/bin..."
if [[ -d /usr/lib/cni ]]; then
  run_root mkdir -p /opt/cni/bin
  run_root cp -a /usr/lib/cni/. /opt/cni/bin/
fi

echo "Initializing CNI configs (bridge + loopback)..."
run_root env AE_CNI_VERSION="${cni_version}" AE_CNI_FORCE="${cni_force}" ./scripts/cni_init.sh

echo "Restarting containerd after CNI init..."
run_root systemctl restart containerd || run_root service containerd restart
sleep 2

echo "Waiting for CRI readiness (RuntimeReady + NetworkReady)..."
ready=0
preflight_log="$(mktemp)"
trap 'rm -f "$preflight_log"' EXIT
for i in {1..30}; do
  if run_root env \
    AE_RUNTIME_BACKEND=cri \
    AE_CRI_ENDPOINT="${endpoint}" \
    AE_CRI_REQUIRE_NETWORK_READY=1 \
    ./scripts/cri_preflight.sh >"$preflight_log" 2>&1; then
    cat "$preflight_log"
    ready=1
    break
  fi
  if [[ $i -eq 30 ]]; then
    cat "$preflight_log" >&2
  fi
  sleep 1
done
if [[ $ready -ne 1 ]]; then
  echo "CRI did not become ready within timeout" >&2
  exit 1
fi

echo "CRI smoke..."
run_root env AE_CRI_ENDPOINT="${endpoint}" ./scripts/cri_smoke.sh

echo "CRI CI setup complete"
