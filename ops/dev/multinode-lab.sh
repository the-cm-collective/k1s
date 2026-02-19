#!/usr/bin/env bash
# Minimal scaffold to spin up a two-node lab with overlay + Service VIPs.
# This is intentionally lightweight; edit to match your environment.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: AE_POD_CIDR_POOL=10.42.0.0/16 AE_SERVICE_IP_POOL=10.241.0.0/16 ./ops/dev/multinode-lab.sh

What it does (best-effort):
  - Starts the controller with Service proxy enabled (overlay provider) and agent API
  - Prints example commands to start agents on worker nodes with WireGuard config

Prereqs:
  - docker/podman installed
  - wireguard tools installed if you want tunnels (`wg`, `wg-quick`)
  - set AE_CONTROLLER_URL reachable from workers

Env knobs (defaults shown):
  AE_POD_CIDR_POOL=10.42.0.0/16
  AE_POD_CIDR_MASK=24
  AE_SERVICE_IP_POOL=10.241.0.0/16
  AE_OVERLAY_NET=ae-overlay
  AE_AGENT_API_PORT=9110
  AE_AGENT_API_TOKEN=<token>
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

echo "Phase 3 lab scaffold (controller + agents)"
echo "------------------------------------------"
echo "1) Start controller:"
cat <<EOF
AE_ENABLE_SERVICE_PROXY=1 \\
AE_SERVICE_PROVIDER=overlay \\
AE_OVERLAY_NET=\${AE_OVERLAY_NET:-ae-overlay} \\
AE_SERVICE_IP_POOL=\${AE_SERVICE_IP_POOL:-10.241.0.0/16} \\
AE_POD_CIDR_POOL=\${AE_POD_CIDR_POOL:-10.42.0.0/16} \\
AE_POD_CIDR_MASK=\${AE_POD_CIDR_MASK:-24} \\
AE_AGENT_API_PORT=\${AE_AGENT_API_PORT:-9110} \\
AE_AGENT_API_TOKEN=\${AE_AGENT_API_TOKEN:-changeme} \\
AE_AGENT_API_TLS_CERT=\${AE_AGENT_API_TLS_CERT:-} \\
AE_AGENT_API_TLS_KEY=\${AE_AGENT_API_TLS_KEY:-} \\
AE_AGENT_API_CLIENT_CA=\${AE_AGENT_API_CLIENT_CA:-} \\
AE_AGENT_API_REQUIRE_CLIENT_CERT=\${AE_AGENT_API_REQUIRE_CLIENT_CERT:-0} \\
python -m ae.controller --loop --specs specs/ --metrics-port 9108
EOF

echo
echo "2) On each worker node start the agent (with overlay bring-up if privileged):"
cat <<'EOF'
AE_CONTROLLER_URL=http://<controller-host>:9110 \
AE_AGENT_TOKEN=$AE_AGENT_API_TOKEN \
AE_NODE_ID=<node-name> \
AE_NODE_LABELS=role=worker \
AE_AGENT_ENDPOINT=http://<node-host>:9109 \
AE_AGENT_HEARTBEAT_SECONDS=10 \
AE_CONTROLLER_TLS_CA=<path-to-ca> \
AE_CONTROLLER_TLS_CERT=<node-cert> \
AE_CONTROLLER_TLS_KEY=<node-key> \
AE_POD_CIDR=<cidr-assigned-or-empty> \
AE_WG_CONFIG="$(cat /etc/wireguard/wg0.conf)" \
python -m ae.node --runtime-backend podman --port 9109 --ensure-pod-net
EOF

echo
echo "3) Apply a sample manifest:"
echo "python -m ae.cli apply -f specs/examples/echo.yaml"

echo
echo "Notes:"
echo "- Pod CIDRs are auto-assigned on first heartbeat if AE_POD_CIDR is empty."
echo "- WireGuard config is not generated here; supply one per node via AE_WG_CONFIG."
echo "- Prefer a dedicated specs directory (for example .local/spec/) to avoid reconciling all examples."
echo "- For multi-replica ingress via container DNS, set AE_PODMAN_NETWORK or AE_DOCKER_NETWORK."
echo "- This script is a helper; adapt for real labs/CI."

echo
echo "Remote Host Runbook (Site B behind NAT/CGNAT):"
cat <<'EOF'
# Full workflow lives in docs/ops/core-edge-wg-psk.md. Core steps:

# Hub (core) host
AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy \
AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=devtoken \
make k1s-core

AE_WG_ENDPOINT=<PUBLIC_IP>:51820 \
AE_NODE_LABELS="role=hub,site=hub,wg_role=hub,wg_psk=rp" \
AE_ROSENPASS_INTERFACE=wg-hub \
AE_WG_LISTEN_PORT=51820 \
AE_WG_ADDRESS=10.255.0.1/32 \
AE_LOG_LEVEL=debug \
AE_ROSENPASS_LOG_LEVEL=verbose \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://127.0.0.1:9110 \
make k1s-core-node

make edge-site SITE_ID=sea-edge-02 EDGE_PORT=4224 EDGE_HTTP_PORT=8224

# Remote edge host
AE_SITE_ID=sea-edge-02 \
AE_NODE_ID=edge-1 \
AE_NATS_URL=nats://gateway:dev@REMOTE_EDGE_NATS:4223 \
AE_LOG_LEVEL=debug \
make k1s-edge-core

sudo -E AE_NODE_ID=edge-1 \
AE_NODE_LABELS="site=sea-edge-02,wg_role=spk,wg_psk=rp" \
AE_ROSENPASS_INTERFACE=wg-edge \
AE_WG_LISTEN_PORT=51821 \
AE_WG_ADDRESS=10.255.0.2/32 \
AE_WG_TABLE=off \
AE_LOG_LEVEL=debug \
AE_ROSENPASS_LOG_LEVEL=verbose \
AE_AGENT_TOKEN=devtoken \
AE_CONTROLLER_URL=http://<HUB_IP>:9110 \
AE_ROSENPASS_DIR=/var/lib/ae/rosenpass \
make k1s-edge-node
EOF
