#!/usr/bin/env bash
set -euo pipefail

if [[ "${ACT:-}" == "true" ]]; then
  echo "ACT detected; skipping CRI setup"
  exit 0
fi

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"

echo "Installing containerd, CNI plugins, and crictl (via cri-tools)..."
sudo apt-get update
sudo apt-get install -y containerd containernetworking-plugins cri-tools iptables

echo "Ensuring containerd config exists..."
sudo mkdir -p /etc/containerd
if [[ ! -f /etc/containerd/config.toml ]]; then
  sudo containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
fi

echo "Configuring crictl endpoint..."
sudo tee /etc/crictl.yaml >/dev/null <<EOF
runtime-endpoint: ${endpoint}
image-endpoint: ${endpoint}
timeout: 10
debug: false
EOF

echo "Starting containerd..."
sudo systemctl enable --now containerd || sudo service containerd start

echo "Ensuring CNI binaries are in /opt/cni/bin..."
if [[ -d /usr/lib/cni ]]; then
  sudo mkdir -p /opt/cni/bin
  sudo cp -a /usr/lib/cni/. /opt/cni/bin/
fi

echo "Initializing CNI configs (bridge + loopback)..."
sudo ./scripts/cni_init.sh

echo "Restarting containerd after CNI init..."
sudo systemctl restart containerd || sudo service containerd restart
sleep 2

echo "CRI preflight..."
AE_RUNTIME_BACKEND=cri ./scripts/cri_preflight.sh

echo "CRI smoke..."
./scripts/cri_smoke.sh

echo "CRI CI setup complete"

