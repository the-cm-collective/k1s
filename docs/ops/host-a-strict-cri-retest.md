# Host A Strict-CRI Retest

Purpose
- Keep the current working Host A strict-CRI retest sequence in one copy/paste operator page.
- Rebuild the Host A GPU guest, start the host `k1s-core-cri` controller, and bring `core-a--hub` back to `Ready`.
- Use the current validated probes for this lane instead of the older `/readyz` and `ae nodes` checks.

Use this page when you want the exact retest flow. Use [Host A Linux GPU Guest](host-a-linux-gpu-guest.html) for the hardware profile, libvirt topology, and passthrough background.

Operational notes
- Run from the repo root with the repo venv on `PATH`.
- Step `A` is the destructive rebuild path. It purges the Host A guest artifacts and rebuilds the GPU qcow2 image.
- Start `B` only after `A` returns successfully.
- Leave `B` running while you execute `C`.
- The guest primary LAN IP is the published k1s identity. The libvirt `default` NAT IP is rescue-only.
- This lane installs repo Python deps from the synced guest checkout before starting `core-a--hub`. Those deps are not baked into the golden image.

## A) Purge, rebuild, boot, sync, and validate the guest

```zsh
(
  emulate -L zsh
  set -e
  setopt pipefail

  cd /home/m4xx3d0ut/git/k1s-wt/k1s
  sudo -v

  : "${HOST_A_GPU_ENV_FILE:=state/host-a-gpu.env}"
  [[ -f "$HOST_A_GPU_ENV_FILE" ]] || { print -u2 -- "missing $HOST_A_GPU_ENV_FILE"; exit 1; }

  set -a
  source "$HOST_A_GPU_ENV_FILE"
  set +a

  : "${HOST_A_GPU_DOMAIN_NAME:=k1s-core-a-gpu}"
  : "${HOST_A_GPU_STATE_ROOT:=state/libvirt-host-a}"
  : "${HOST_A_GPU_OVERLAY_DIR:=$HOME/VMs}"
  : "${HOST_A_GPU_BASE_IMAGE:=artifacts/images/ubuntu-22.04-k1s-gpu.qcow2}"
  : "${HOST_A_GPU_GUEST_REPO:=/home/ae/k1s}"

  STATE_DIR="${HOST_A_GPU_STATE_ROOT}/${HOST_A_GPU_DOMAIN_NAME}"
  OVERLAY_PATH="${HOST_A_GPU_OVERLAY_DIR}/${HOST_A_GPU_DOMAIN_NAME}.qcow2"
  SEED_PATH="${HOST_A_GPU_OVERLAY_DIR}/${HOST_A_GPU_DOMAIN_NAME}-seed.iso"
  GPU_IMAGE="${HOST_A_GPU_BASE_IMAGE}"
  GPU_IMAGE_SHA="${GPU_IMAGE}.sha256"
  GPU_IMAGE_META="${GPU_IMAGE}.meta.json"
  IPS_JSON="${STATE_DIR}/ips.startup.json"
  RUN_ID="host-a-$(date -u +%Y%m%dT%H%M%SZ)"

  print -r -- "[1/10] clearing stale Host A guest artifacts"
  scripts/lab/vm/labctl.sh host-a-gpu stop --force >/dev/null 2>&1 || true
  scripts/lab/vm/labctl.sh host-a-gpu undefine >/dev/null 2>&1 || true
  rm -rf "$STATE_DIR"
  rm -f "$OVERLAY_PATH" "$SEED_PATH"
  rm -f "$GPU_IMAGE" "$GPU_IMAGE_SHA" "$GPU_IMAGE_META"
  rm -rf artifacts/images/build-gpu

  print -r -- "[2/10] rebuilding GPU image"
  scripts/lab/vm/labctl.sh image build --variant gpu

  print -r -- "[3/10] verifying GPU image"
  scripts/lab/vm/labctl.sh image verify --variant gpu

  print -r -- "[4/10] host preflight"
  scripts/lab/vm/labctl.sh host-a-gpu preflight

  print -r -- "[5/10] rendering guest and creating overlay/seed"
  scripts/lab/vm/labctl.sh host-a-gpu render
  scripts/lab/vm/labctl.sh host-a-gpu create-overlay
  scripts/lab/vm/labctl.sh host-a-gpu create-seed

  print -r -- "[6/10] defining and starting guest"
  scripts/lab/vm/labctl.sh host-a-gpu define
  scripts/lab/vm/labctl.sh host-a-gpu start

  print -r -- "[7/10] waiting for guest IPs"
  typeset GUEST_IP=""
  for attempt in $(seq 1 30); do
    STATE="$(virsh -c qemu:///system domstate "$HOST_A_GPU_DOMAIN_NAME" 2>/dev/null | tr -d '\r' | head -n1 | xargs || true)"
    if [[ "$STATE" != "running" ]]; then
      REASON="$(virsh -c qemu:///system domstate "$HOST_A_GPU_DOMAIN_NAME" --reason 2>/dev/null | tr -d '\r' | tail -n1 | xargs || true)"
      print -u2 -- "guest left running state: state=${STATE:-unknown} reason=${REASON:-unknown}"
      sudo tail -n 120 "/var/log/libvirt/qemu/${HOST_A_GPU_DOMAIN_NAME}.log" || true
      exit 1
    fi

    scripts/lab/vm/labctl.sh host-a-gpu ips --json > "$IPS_JSON" 2>/dev/null || true
    GUEST_IP="$(
      python - "$IPS_JSON" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
value = ""
try:
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = str(payload.get("primary_ip") or "")
except Exception:
    value = ""
print(value)
PY
    )"
    if [[ -n "$GUEST_IP" ]]; then
      break
    fi
    sleep 5
  done

  [[ -n "$GUEST_IP" ]] || { print -u2 -- "guest never reported a primary LAN IP"; exit 1; }
  print -r -- "guest_ip=${GUEST_IP}"

  print -r -- "[8/10] syncing repo into guest"
  typeset -a ssh_opts
  ssh_opts=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -i /home/m4xx3d0ut/.ssh/id_rsa
  )

  ssh "${ssh_opts[@]}" "ae@${GUEST_IP}" "mkdir -p '${HOST_A_GPU_GUEST_REPO}'"

  rsync -az --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'state/' \
    --exclude 'runs/' \
    --exclude 'artifacts/' \
    --exclude 'docs/site/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude '.mypy_cache/' \
    -e "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i /home/m4xx3d0ut/.ssh/id_rsa" \
    ./ \
    "ae@${GUEST_IP}:${HOST_A_GPU_GUEST_REPO}/"

  print -r -- "[9/10] validating passthrough inside guest"
  python scripts/dev/gpu_guest_passthrough_validate.py validate \
    --run-id "$RUN_ID" \
    --vm-name "$HOST_A_GPU_DOMAIN_NAME" \
    --inventory "${STATE_DIR}/inventory.json" \
    --guest-repo "$HOST_A_GPU_GUEST_REPO" \
    --expected-gpu "TITAN RTX" \
    --min-vram-gib 24

  print -r -- "[10/10] guest boot and passthrough validation passed"
  print -r -- "run_id=${RUN_ID}"
  print -r -- "guest_ip=${GUEST_IP}"
)
```

Success signals
- the passthrough validator reports `status: "passed"`
- the final output includes `guest boot and passthrough validation passed`
- the printed `guest_ip` is the guest primary LAN IP, not the libvirt NAT IP

## B) Start the Host A controller and leave it running

Run this in a separate terminal after `A` succeeds:

```zsh
cd /home/m4xx3d0ut/git/k1s-wt/k1s

CONTROLLER_HOST_IP="$(
  ip route get 1.1.1.1 | awk '{
    for (i = 1; i <= NF; i++) {
      if ($i == "src") {
        print $(i+1)
        exit
      }
    }
  }'
)"

print -r -- "controller_host_ip=${CONTROLLER_HOST_IP}"

sudo -E \
  AE_DEV_LOCAL=1 \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_APISHIM_MODE=cri \
  AE_AGENT_API_PORT=9110 \
  AE_AGENT_API_TOKEN=devtoken \
  POSTGRES_PORT=55432 \
  POSTGRES_BIND_IP="${CONTROLLER_HOST_IP}" \
  AE_APISHIM_DSN=postgresql://shim:shim@127.0.0.1:55432/shim \
  AE_APISHIM_ETCD_ENDPOINTS=http://127.0.0.1:2379 \
  make k1s-core-cri
```

Quick check from another shell:

```zsh
curl -fsS http://127.0.0.1:9110/healthz
ss -ltnp | rg ':9110\\b|:7422\\b|:55432\\b'
```

Expected
- `http://127.0.0.1:9110/healthz` returns `{"ok": true}`
- the host is listening on `:9110`
- Postgres is bound on `:55432` so the guest can reach the apishim store

## C) Start `core-a--hub` in the guest after controller health is green

```zsh
(
  emulate -L zsh
  set -e
  setopt pipefail

  cd /home/m4xx3d0ut/git/k1s-wt/k1s

  CONTROLLER_HOST_IP="$(
    ip route get 1.1.1.1 | awk '{
      for (i = 1; i <= NF; i++) {
        if ($i == "src") {
          print $(i+1)
          exit
        }
      }
    }'
  )"
  CONTROLLER_URL="http://${CONTROLLER_HOST_IP}:9110"
  APISHIM_DSN="postgresql://shim:shim@${CONTROLLER_HOST_IP}:55432/shim"
  GUEST_REPO="/home/ae/k1s"
  IPS_JSON="state/libvirt-host-a/k1s-core-a-gpu/ips.start-node.json"

  print -r -- "[node] refreshing guest IPs"
  scripts/lab/vm/labctl.sh host-a-gpu ips --json > "$IPS_JSON"

  GUEST_IP="$(
    python - "$IPS_JSON" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload.get("primary_ip") or "")
PY
  )"

  [[ -n "$GUEST_IP" ]] || { print -u2 -- "missing guest primary_ip"; exit 1; }

  typeset -a ssh_opts
  ssh_opts=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -i /home/m4xx3d0ut/.ssh/id_rsa
  )

  print -r -- "[node] verifying controller from host and guest"
  curl -fsS "${CONTROLLER_URL}/healthz" >/dev/null
  ssh "${ssh_opts[@]}" "ae@${GUEST_IP}" "curl -fsS '${CONTROLLER_URL}/healthz' >/dev/null"
  ssh "${ssh_opts[@]}" "ae@${GUEST_IP}" "bash -lc 'cat </dev/null >/dev/tcp/${CONTROLLER_HOST_IP}/55432'"

  print -r -- "[node] installing guest deps and starting core-a--hub"
  ssh "${ssh_opts[@]}" "ae@${GUEST_IP}" bash -s -- "$GUEST_REPO" "$CONTROLLER_URL" "$GUEST_IP" "$APISHIM_DSN" <<'SH'
set -euo pipefail
guest_repo="$1"
controller_url="$2"
guest_ip="$3"
apishim_dsn="$4"

cd "$guest_repo"

if sudo python3 -m pip install --help 2>/dev/null | grep -q -- --break-system-packages; then
  sudo python3 -m pip install -r requirements.in --break-system-packages
else
  sudo python3 -m pip install -r requirements.in
fi

if pgrep -f 'k1s-core-node|python -m ae\.node' >/dev/null 2>&1; then
  sudo pkill -f -- 'k1s-core-node|python -m ae\.node' >/dev/null 2>&1 || true
  sleep 2
fi

nohup sudo env \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_RUNTIME_HANDLER=nvidia \
  AE_ENABLE_NETFS=1 \
  AE_APISHIM_DSN="$apishim_dsn" \
  AE_NODE_ID=core-a--hub \
  AE_NODE_LABELS="role=hub,site=core-a,gpu.sku=titan-rtx" \
  AE_POD_CIDR=10.42.0.0/24 \
  AE_CNI_SUBNET=10.42.0.0/24 \
  AE_ROSENPASS_ENABLED=0 \
  AE_CONTROLLER_URL="$controller_url" \
  AE_AGENT_ENDPOINT="http://${guest_ip}:9111" \
  AE_AGENT_TOKEN=devtoken \
  AE_NODE_PORT=9111 \
  make k1s-core-node > /home/ae/k1s-core-node.log 2>&1 </dev/null &
SH

  print -r -- "[node] waiting for guest node agent API"
  for attempt in $(seq 1 45); do
    if curl -fsS "http://${GUEST_IP}:9111/v1/containers" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  if ! curl -fsS "http://${GUEST_IP}:9111/v1/containers" >/dev/null 2>&1; then
    ssh "${ssh_opts[@]}" "ae@${GUEST_IP}" "tail -n 200 /home/ae/k1s-core-node.log || true"
    exit 1
  fi

  print -r -- "[node] waiting for controller registration"
  for attempt in $(seq 1 20); do
    if curl -fsS \
      -H 'X-Agent-Token: devtoken' \
      "${CONTROLLER_URL}/v1/nodes/core-a--hub/overlay" >/dev/null 2>&1; then
      break
    fi
    sleep 3
  done

  if ! curl -fsS \
    -H 'X-Agent-Token: devtoken' \
    "${CONTROLLER_URL}/v1/nodes/core-a--hub/overlay" >/dev/null 2>&1; then
    ssh "${ssh_opts[@]}" "ae@${GUEST_IP}" "tail -n 200 /home/ae/k1s-core-node.log || true"
    curl -sS -H 'X-Agent-Token: devtoken' "${CONTROLLER_URL}/v1/nodes" | python -m json.tool || true
    exit 1
  fi

  curl -sS -H 'X-Agent-Token: devtoken' "${CONTROLLER_URL}/v1/nodes" | python -m json.tool
  curl -sS -H 'X-Agent-Token: devtoken' "${CONTROLLER_URL}/v1/nodes/core-a--hub/overlay" | python -m json.tool

  print -r -- "controller_url=${CONTROLLER_URL}"
  print -r -- "apishim_dsn=${APISHIM_DSN}"
  print -r -- "guest_ip=${GUEST_IP}"
  print -r -- "agent_endpoint=http://${GUEST_IP}:9111"
)
```

Why this differs from the older Host A snippets
- the guest does not expose `/readyz` or `/healthz` on `:9111`; use `/v1/containers`
- controller registration is confirmed through the controller agent API overlay endpoint, not `ae nodes`
- `AE_ROSENPASS_ENABLED=0` is intentional for this lane
- the guest installs `requirements.in` from the synced checkout before starting `make k1s-core-node`

Success signals
- `GET /v1/nodes` shows `core-a--hub` with `status: "Ready"`
- `GET /v1/nodes/core-a--hub/overlay` returns `errors: []`
- the node advertises the expected TITAN RTX capability and `runtime_handlers: ["nvidia"]`

## Troubleshooting

Refresh guest IPs before reusing SSH commands:

```zsh
scripts/lab/vm/labctl.sh host-a-gpu ips --json
```

Tail the guest node log:

```zsh
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i /home/m4xx3d0ut/.ssh/id_rsa ae@<guest-primary-ip> 'tail -n 200 /home/ae/k1s-core-node.log'
```

Common causes
- `B` is healthy locally, but the host firewall is still blocking guest access to `9110` or `55432`
- the guest primary LAN IP changed since the last run
- the guest checkout no longer matches the installed Python deps because `requirements.in` was skipped
- the node agent is healthy, but the check is still using the old `/readyz` probe
