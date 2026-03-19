#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
RUN_ID="$(resolve_run_id)"
EXECUTE=0

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>] [--execute]

Default behavior writes per-host shared-infra bootstrap scripts under
runs/<RUN_ID>/ha-shared-infra. Use --execute to run them over SSH.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { err "--variant required"; exit 2; }
variant_json="$(variant_to_json "$VARIANT")"
ensure_run_dir "$RUN_ID"

python_bin="$(lab_python)"
state_dir="$ROOT_DIR/state/lab-vm/$RUN_ID/ha-infra"
run_dir_path="$(run_dir "$RUN_ID")/ha-shared-infra"
mkdir -p "$state_dir" "$run_dir_path"

mapfile -t ha_rows < <(echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-ha-core") | @base64')
if [[ "${#ha_rows[@]}" -ne 3 ]]; then
  err "ha shared infra requires exactly 3 hosts with role=k1s-ha-core"
  exit 2
fi

mapfile -t edge_site_ids < <(
  echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-edge-core" and (.site_id // "") != "") | .site_id' | sort -u
)

declare -a core_names=()
declare -a core_ips=()
declare -a core_node_ids=()
for row in "${ha_rows[@]}"; do
  host_json="$(printf '%s' "$row" | base64 -d)"
  core_names+=("$(echo "$host_json" | jq -r '.name')")
  core_ips+=("$(echo "$host_json" | jq -r '.ip')")
  core_node_ids+=("$(echo "$host_json" | jq -r '.node_id // .name')")
done

join_csv() {
  local IFS=","
  printf '%s' "$*"
}

expected_etcd_csv="$(
  {
    for ip in "${core_ips[@]}"; do
      printf 'http://%s:2379\n' "$ip"
    done
  } | sort | paste -sd, -
)"
configured_etcd_csv="$(
  echo "$variant_json" | jq -r '.ha.etcd_endpoints[]?' | sort | paste -sd, -
)"
if [[ "$configured_etcd_csv" != "$expected_etcd_csv" ]]; then
  err "variant ha.etcd_endpoints must point at the 3 k1s-ha-core VM IPs for shared infra"
  exit 2
fi

expected_hub_csv="$(
  for idx in "${!core_names[@]}"; do
    printf '%s=http://%s:8222\n' "${core_names[$idx]}" "${core_ips[$idx]}"
  done | sort | paste -sd, -
)"
configured_hub_csv="$(
  echo "$variant_json" | jq -r '.ha.hub_nodes[]? | "\(.name)=\(.monitor_url)"' | sort | paste -sd, -
)"
if [[ "$configured_hub_csv" != "$expected_hub_csv" ]]; then
  err "variant ha.hub_nodes must point at the 3 k1s-ha-core VM monitor URLs for shared infra"
  exit 2
fi

configured_nats_url="$(echo "$variant_json" | jq -r '.ha.nats_url // empty')"
nats_host="$("$python_bin" - <<'PY' "$configured_nats_url"
from urllib.parse import urlparse
import sys

raw = (sys.argv[1] or "").strip()
if not raw:
    print("")
    raise SystemExit(0)
parsed = urlparse(raw if "://" in raw else f"nats://{raw}")
print(parsed.hostname or "")
PY
)"
case ",$(join_csv "${core_ips[@]}")," in
  *,"$nats_host",*) ;;
  *)
    err "variant ha.nats_url must point at one of the k1s-ha-core VM IPs for shared infra"
    exit 2
    ;;
esac

initial_cluster="$(
  for idx in "${!core_node_ids[@]}"; do
    printf '%s=http://%s:2380\n' "${core_node_ids[$idx]}" "${core_ips[$idx]}"
  done | paste -sd, -
)"
routes_json="$(
  for idx in "${!core_names[@]}"; do
    printf '{"name":"%s","ip":"%s"}\n' "${core_names[$idx]}" "${core_ips[$idx]}"
  done | jq -s .
)"
site_ids_json="$(printf '%s\n' "${edge_site_ids[@]}" | jq -R . | jq -s .)"

for idx in "${!core_names[@]}"; do
  name="${core_names[$idx]}"
  ip="${core_ips[$idx]}"
  node_dir="$state_dir/$name"
  mkdir -p "$node_dir"
  conf_path="$node_dir/nats-hub.conf"
  "$python_bin" - <<'PY' "$ROOT_DIR/ops/dev/nats-hub.conf" "$conf_path" "$name" "$routes_json" "$site_ids_json"
from pathlib import Path
import json
import re
import sys


def user_block(site_id: str) -> str:
    return f'''
      {{
        user: "site-{site_id}-uplink"
        password: "dev"
        permissions: {{
          publish: [
            "k1s.v1.site.{site_id}.>",
            "$JS.API.>",
            "$JS.K1S.API.>",
            "$JS.API.CONSUMER.MSG.NEXT.K1S_WORK.WORK_SITE_{site_id}",
            "$JS.ACK.K1S_WORK.>"
          ]
          subscribe: ["_INBOX.>", "k1s.v1.site.{site_id}.routes.bundle"]
        }}
      }}
'''


template_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
server_name = sys.argv[3]
routes = json.loads(sys.argv[4])
site_ids = json.loads(sys.argv[5])
text = template_path.read_text(encoding="utf-8")
marker = "# --- site uplink users (managed by scripts/dev/add_edge_site.sh)"
if marker not in text:
    raise SystemExit("hub config missing marker")
text = re.sub(r'(?m)^server_name:\s*".*"$', f'server_name: "{server_name}"', text, count=1)
route_lines = ",\n".join(
    f'    nats://{item["ip"]}:6222'
    for item in routes
    if str(item.get("name") or "") != server_name
)
cluster_block = (
    'cluster {\n'
    '  name: "k1s-ha-hub"\n'
    '  port: 6222\n'
    '  routes = [\n'
    f'{route_lines}\n'
    '  ]\n'
    '}\n'
)
text = re.sub(r'(?m)^http:\s*\d+\s*$', lambda m: f"{m.group(0)}\n\n{cluster_block.rstrip()}", text, count=1)
rendered = text
for site_id in sorted({str(item).strip() for item in site_ids if str(item).strip()}):
    needle = f'user: "site-{site_id}-uplink"'
    if needle in rendered:
        continue
    rendered = rendered.replace(marker, marker + user_block(site_id), 1)
out_path.write_text(rendered, encoding="utf-8")
PY

  script_path="$run_dir_path/${name}.sh"
  cat >"$script_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export AE_CRI_ENDPOINT=\${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh
ensure_vm_bootstrap_prereqs
sudo env \
  PYTHONPATH=/mnt/host/src \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_DATA_ROOT=/var/lib/ae/cri \
  AE_CRI_RUNTIME_HANDLER=\${AE_CRI_RUNTIME_HANDLER:-runc} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  python3 /mnt/host/scripts/dev/cri_stack.py up-etcd \
    --profile k1s-ha-core \
    --name ${core_node_ids[$idx]} \
    --component k1s-ha-core-etcd \
    --listen-client-urls http://0.0.0.0:2379 \
    --advertise-client-urls http://${ip}:2379 \
    --listen-peer-urls http://0.0.0.0:2380 \
    --initial-advertise-peer-urls http://${ip}:2380 \
    --initial-cluster '${initial_cluster}' \
    --initial-cluster-state new \
    --data-dir-name ha-etcd \
    --recreate
sudo env \
  PYTHONPATH=/mnt/host/src \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_DATA_ROOT=/var/lib/ae/cri \
  AE_CRI_RUNTIME_HANDLER=\${AE_CRI_RUNTIME_HANDLER:-runc} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  python3 /mnt/host/scripts/dev/cri_stack.py up-nats-hub \
    --profile k1s-ha-core \
    --config /mnt/host/state/lab-vm/${RUN_ID}/ha-infra/${name}/nats-hub.conf \
    --recreate
echo ha-shared-infra-complete
EOF
  chmod +x "$script_path"
done

plan_file="$run_dir_path/plan.txt"
: >"$plan_file"
for idx in "${!core_names[@]}"; do
  printf '[%s %s]\nssh -i ${SSH_KEY_PATH:-$HOME/.ssh/id_rsa} ae@%s '\''bash -s'\'' < %s\n\n' \
    "${core_names[$idx]}" "${core_ips[$idx]}" "${core_ips[$idx]}" "$run_dir_path/${core_names[$idx]}.sh" >>"$plan_file"
done

if [[ "$EXECUTE" -eq 1 ]]; then
  for idx in "${!core_names[@]}"; do
    name="${core_names[$idx]}"
    ip="${core_ips[$idx]}"
    log "executing HA shared infra bootstrap on ${name} (${ip})"
    if ! wait_for_ssh "$ip" 80; then
      err "ssh not ready for ${name} (${ip})"
      exit 1
    fi
    with_repo_host_mount "$ip" >/dev/null 2>&1 || true
    run_remote "$ip" "bash -s" <"$run_dir_path/${name}.sh"
  done

  etcd_endpoints_json="$(
    for ip in "${core_ips[@]}"; do
      printf 'http://%s:2379\n' "$ip"
    done | jq -R . | jq -s .
  )"
  monitor_urls_json="$(
    for ip in "${core_ips[@]}"; do
      printf 'http://%s:8222\n' "$ip"
    done | jq -R . | jq -s .
  )"
  PYTHONPATH="$ROOT_DIR/src" "$python_bin" - <<'PY' "$etcd_endpoints_json" "$monitor_urls_json"
from __future__ import annotations

import json
import time
import urllib.request
import sys

from ae.ha.ops import NatsHubNodeTarget, fetch_nats_hub_monitor_record

etcd_endpoints = json.loads(sys.argv[1])
monitor_urls = json.loads(sys.argv[2])
deadline = time.time() + 180


def etcd_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    return payload.get("health") in {True, "true"}


while time.time() < deadline:
    if all(etcd_ok(url) for url in etcd_endpoints):
        break
    time.sleep(2)
else:
    raise SystemExit("HA shared infra etcd health did not converge")

while time.time() < deadline:
    records = []
    try:
        for idx, url in enumerate(monitor_urls):
            target = NatsHubNodeTarget(name=f"hub-{idx}", monitor_url=url)
            records.append(fetch_nats_hub_monitor_record(target, timeout_s=3.0))
    except Exception:
        time.sleep(2)
        continue
    if all(record.route_count >= 2 for record in records) and len({record.meta_leader for record in records if record.meta_leader}) == 1:
        break
    time.sleep(2)
else:
    raise SystemExit("HA shared infra NATS cluster did not converge")
PY
fi

log "ha shared infra scripts written under $run_dir_path"
if [[ "$EXECUTE" -eq 0 ]]; then
  log "run with --execute to apply automatically"
fi
