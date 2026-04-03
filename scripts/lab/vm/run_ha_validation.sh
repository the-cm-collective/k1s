#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

# common.sh enables `set -e`; this runner intentionally continues across stages.
set +e
set -uo pipefail

DEFAULT_STAGES_CSV="stage1,retained,drain,stage2,stage2-live,drills"
STAGES_CSV="${STAGES_CSV:-$DEFAULT_STAGES_CSV}"
LIST_STAGES=0

PYTHON_BIN="$(lab_python)"
ROOT_PYTHONPATH="$ROOT_DIR/src"

declare -a REQUESTED_STAGES=()
declare -a STAGE_SUMMARY=()

usage() {
  cat <<EOF
Usage:
  $0 [--stages <csv>] [--list-stages]

Runs the checked-in HA validation flow from a file-backed script so child
processes cannot consume the remainder of the sequence from stdin.

Defaults:
  --stages $DEFAULT_STAGES_CSV

Stages:
  stage1       Stage 1 one-shot acceptance on ha-control-plane-attached-node
  retained     Retained stage-1 ingress smoke plus node cordon/uncordon checks
  drain        Supplemental two-worker non-HA drain/reschedule validation
  stage2       Stage 2 one-shot acceptance on ha-control-plane-core
  stage2-live  Stage 2 live helper on a live ha-control-plane-core run
  drills       Optional disruptive drills on ha-control-plane-core-drills

Examples:
  $0
  $0 --stages stage2,stage2-live,drills
  $0 --stages retained
EOF
}

list_stages() {
  cat <<'EOF'
stage1
retained
drain
stage2
stage2-live
drills
EOF
}

normalize_stage() {
  local stage="${1//[[:space:]]/}"
  case "$stage" in
    1|stage1) printf '%s\n' "stage1" ;;
    2|retained|stage1-retained) printf '%s\n' "retained" ;;
    3|drain|supplemental-drain) printf '%s\n' "drain" ;;
    4|stage2) printf '%s\n' "stage2" ;;
    5|stage2-live|live) printf '%s\n' "stage2-live" ;;
    6|drills) printf '%s\n' "drills" ;;
    *)
      err "unknown stage: $1"
      usage
      exit 2
      ;;
  esac
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --stages)
        STAGES_CSV="${2:-}"
        shift 2
        ;;
      --list-stages)
        LIST_STAGES=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        err "unknown arg: $1"
        usage
        exit 2
        ;;
    esac
  done
}

load_requested_stages() {
  local raw="${STAGES_CSV:-all}"
  local item=""
  local normalized=""
  local -A seen=()

  if [[ -z "$raw" || "$raw" == "all" ]]; then
    raw="$DEFAULT_STAGES_CSV"
  fi

  IFS=',' read -r -a items <<<"$raw"
  for item in "${items[@]}"; do
    [[ -n "${item//[[:space:]]/}" ]] || continue
    normalized="$(normalize_stage "$item")"
    if [[ -z "${seen[$normalized]:-}" ]]; then
      seen["$normalized"]=1
      REQUESTED_STAGES+=("$normalized")
    fi
  done
}

stage_enabled() {
  local target="$1"
  local stage=""
  for stage in "${REQUESTED_STAGES[@]}"; do
    [[ "$stage" == "$target" ]] && return 0
  done
  return 1
}

timestamp() {
  date -u +%Y%m%dT%H%M%SZ
}

stage_run_id() {
  local suffix="$1"
  printf '%s_%s' "$(timestamp)" "$suffix"
}

stage_header() {
  local label="$1"
  printf '\n%s\n\n' "$label"
}

print_summary_file() {
  local run_id="$1"
  local summary_file="$ROOT_DIR/runs/$run_id/summary.json"
  local ha_summary_file="$ROOT_DIR/runs/$run_id/ha_summary.json"
  [[ -f "$summary_file" ]] && jq . "$summary_file"
  [[ -f "$ha_summary_file" ]] && jq . "$ha_summary_file"
}

record_stage_result() {
  local name="$1"
  local rc="$2"
  STAGE_SUMMARY+=("${name}:${rc}")
}

print_final_summary() {
  local item=""
  local name=""
  local rc=""
  local status=""
  printf '\nFinal Summary\n\n'
  for item in "${STAGE_SUMMARY[@]}"; do
    name="${item%%:*}"
    rc="${item##*:}"
    status="passed"
    [[ "$rc" -ne 0 ]] && status="failed"
    printf '  - %s: %s (rc=%s)\n' "$name" "$status" "$rc"
  done
}

run_smoke() {
  local variant="$1"
  local run_id="$2"
  local extra_args="${3:-}"

  if [[ -n "$extra_args" ]]; then
    AE_CRI_CACHE_SEED_ENGINE="${AE_CRI_CACHE_SEED_ENGINE:-docker}" \
    AE_CRI_CACHE_SEED_MODE="${AE_CRI_CACHE_SEED_MODE:-required}" \
    AE_CRI_IMAGE_MIRROR_ALWAYS_PULL="${AE_CRI_IMAGE_MIRROR_ALWAYS_PULL:-0}" \
    LAB_VM_SMOKE_ARGS="$extra_args" \
    make lab-vm-smoke VARIANT="$variant" RUN_ID="$run_id"
    return $?
  fi

  AE_CRI_CACHE_SEED_ENGINE="${AE_CRI_CACHE_SEED_ENGINE:-docker}" \
  AE_CRI_CACHE_SEED_MODE="${AE_CRI_CACHE_SEED_MODE:-required}" \
  AE_CRI_IMAGE_MIRROR_ALWAYS_PULL="${AE_CRI_IMAGE_MIRROR_ALWAYS_PULL:-0}" \
  make lab-vm-smoke VARIANT="$variant" RUN_ID="$run_id"
}

load_ha_local_env() {
  local apishim_env="$ROOT_DIR/state/profiles/k1s-ha-core/apishim.env"
  local controller_env="$ROOT_DIR/state/profiles/k1s-ha-core/controller.env"
  local exports_text=""
  local rc=0

  if [[ ! -f "$apishim_env" || ! -f "$controller_env" ]]; then
    err "missing HA local env files under state/profiles/k1s-ha-core"
    return 1
  fi

  set -a
  # shellcheck source=/dev/null
  source "$controller_env" || rc=$?
  set +a

  exports_text="$(
    APISHIM_ENV_FILE="$apishim_env" \
    CONTROLLER_ENV_FILE="$controller_env" \
      bash "$ROOT_DIR/scripts/ae-env.sh" local
  )" || rc=$?

  if [[ -n "$exports_text" ]]; then
    eval "$exports_text" || rc=$?
  fi

  return "$rc"
}

run_stage1() {
  local run_id="${RUN_ID_STAGE1:-$(stage_run_id ha_attached_node_stage1)}"
  local rc=0

  stage_header "1. Stage 1 one-shot acceptance."
  run_smoke "$ROOT_DIR/lab/variants/ha-control-plane-attached-node.yaml" "$run_id"
  rc=$?
  printf '\n[stage1 one-shot rc=%d] RUN_ID=%s\n' "$rc" "$run_id"
  print_summary_file "$run_id"
  return "$rc"
}

run_retained_stage1() {
  local variant="$ROOT_DIR/lab/variants/ha-control-plane-attached-node.yaml"
  local run_id="${RUN_ID_RETAINED:-ha-attached-node-local}"
  local rc=0
  local purge_args="--destroy-network"

  stage_header "2. Retained stage-1 ingress smoke plus retained node mutation."

  RUN_ID="$run_id" VARIANT="$variant" \
    LAB_VM_HA_ATTACHED_NODE_ARGS="$purge_args" \
    make lab-vm-ha-attached-node-purge || rc=$?
  printf '\n[stage1 retained purge pre rc=%d]\n' "$rc"

  RUN_ID="$run_id" VARIANT="$variant" make lab-vm-ha-attached-node-up || rc=$?
  printf '\n[stage1 retained up rc=%d]\n' "$rc"

  RUN_ID="$run_id" VARIANT="$variant" make lab-vm-ha-attached-node-status || rc=$?
  printf '\n[stage1 retained status rc=%d]\n' "$rc"

  RUN_ID="$run_id" VARIANT="$variant" make lab-vm-ha-attached-node-workload-smoke || rc=$?
  printf '\n[stage1 retained workload smoke rc=%d]\n' "$rc"

  load_ha_local_env || rc=$?

  printf '\n[stage1 retained node inventory]\n'
  PYTHONPATH="$ROOT_PYTHONPATH" "$PYTHON_BIN" -m ae.cli nodes || rc=$?

  printf '\n[stage1 retained cordon attached-node-1]\n'
  PYTHONPATH="$ROOT_PYTHONPATH" "$PYTHON_BIN" -m ae.cli nodes attached-node-1 --cordon || rc=$?
  PYTHONPATH="$ROOT_PYTHONPATH" "$PYTHON_BIN" -m ae.cli nodes attached-node-1 || rc=$?

  printf '\n[stage1 retained uncordon attached-node-1]\n'
  PYTHONPATH="$ROOT_PYTHONPATH" "$PYTHON_BIN" -m ae.cli nodes attached-node-1 --uncordon || rc=$?
  PYTHONPATH="$ROOT_PYTHONPATH" "$PYTHON_BIN" -m ae.cli nodes attached-node-1 || rc=$?

  RUN_ID="$run_id" VARIANT="$variant" \
    LAB_VM_HA_ATTACHED_NODE_ARGS="$purge_args" \
    make lab-vm-ha-attached-node-purge || rc=$?
  printf '\n[stage1 retained purge post rc=%d]\n' "$rc"

  return "$rc"
}

show_drain_pods() {
  local ip="$1"
  local output=""
  output="$(run_remote "$ip" "sudo crictl pods | grep echo-worker-drain || true")"
  printf '%s\n' "$output"
}

count_drain_pods() {
  local output="$1"
  printf '%s\n' "$output" | grep -c 'echo-worker-drain' || true
}

run_drain_stage() {
  local variant="$ROOT_DIR/lab/variants/test3-abc-no-gpu.yaml"
  local run_id="${RUN_ID_DRAIN:-$(stage_run_id multi_non_gpu_drain)}"
  local rc=0
  local variant_json=""
  local a_core_ip=""
  local worker_a_ip=""
  local worker_b_ip=""
  local worker_a_output=""
  local worker_b_output=""
  local worker_b_count=0
  local baseline_rc=0
  local execute_rc=0
  local post_drain_rc=0
  local cleanup_rc=0
  local down_rc=0
  local state_dir="$ROOT_DIR/state/lab-vm/$run_id"
  local run_dir_path="$ROOT_DIR/runs/$run_id"

  stage_header "3. Supplemental real drain/reschedule on the two-worker VM harness."

  variant_json="$(variant_to_json "$variant")" || return $?
  a_core_ip="$(echo "$variant_json" | jq -r '.hosts[] | select(.name=="a-core") | .ip' | head -n1)"
  worker_a_ip="$(echo "$variant_json" | jq -r '.hosts[] | select(.name=="b-edge-node-1") | .ip' | head -n1)"
  worker_b_ip="$(echo "$variant_json" | jq -r '.hosts[] | select(.name=="c-edge-node-1") | .ip' | head -n1)"

  if [[ -z "$a_core_ip" || -z "$worker_a_ip" || -z "$worker_b_ip" ]]; then
    err "could not resolve a-core or worker IPs from $variant"
    return 1
  fi

  run_smoke "$variant" "$run_id" "--teardown never --lanes multi_non_gpu"
  rc=$?
  printf '\n[supplemental drain up rc=%d] RUN_ID=%s\n' "$rc" "$run_id"
  print_summary_file "$run_id"

  if [[ "$rc" -eq 0 ]]; then
    printf '\n[supplemental drain baseline apply]\n'
    run_remote "$a_core_ip" "bash -s" <<'EOF'
set -uo pipefail
rc=0
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
cd /mnt/host || rc=$?
auth_exports="$(
  sudo PYTHONPATH=src python3 -m ae.cli auth local --strict \
    --apishim-env state/profiles/k1s-core/apishim.cli.env
)" || rc=$?
if [[ -n "${auth_exports:-}" ]]; then
  eval "$auth_exports" || rc=$?
fi
export AE_RUNTIME_BACKEND=cri
export AE_INFRA_BACKEND=cri
export AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock
sudo -E PYTHONPATH=src python3 -m ae.cli delete --purge echo-worker-drain || true
cat >/tmp/echo-worker-drain.yaml <<'MANIFEST'
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: echo-worker-drain
spec:
  image: docker.io/library/demo-shell:latest
  replicas: 2
  nodeSelector:
    role: worker
  ports:
    - name: http
      containerPort: 8080
  health:
    readiness:
      httpGet:
        path: /healthz
        port: 8080
      initialDelaySeconds: 1
      timeoutSeconds: 2
      periodSeconds: 2
      successThreshold: 1
      failureThreshold: 5
    liveness:
      httpGet:
        path: /healthz
        port: 8080
      initialDelaySeconds: 5
      timeoutSeconds: 1
      periodSeconds: 10
      successThreshold: 1
      failureThreshold: 3
MANIFEST
sudo -E PYTHONPATH=src python3 -m ae.cli apply -f /tmp/echo-worker-drain.yaml || rc=$?
sudo -E PYTHONPATH=src python3 -m ae.cli status echo-worker-drain --watch 2 --timeout 180 --events || rc=$?
exit "$rc"
EOF
    baseline_rc=$?
    [[ "$baseline_rc" -ne 0 ]] && rc=1
    printf '\n[supplemental drain baseline apply rc=%d]\n' "$baseline_rc"

    printf '\n[supplemental drain baseline placement]\n'
    echo "===== $worker_a_ip ====="
    worker_a_output="$(show_drain_pods "$worker_a_ip")"
    printf '%s\n' "$worker_a_output"
    echo "===== $worker_b_ip ====="
    worker_b_output="$(show_drain_pods "$worker_b_ip")"
    printf '%s\n' "$worker_b_output"
    [[ -n "$worker_a_output" ]] || rc=1
    [[ -n "$worker_b_output" ]] || rc=1

    printf '\n[supplemental drain execute]\n'
    run_remote "$a_core_ip" "bash -s" <<'EOF'
set -uo pipefail
rc=0
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
cd /mnt/host || rc=$?
auth_exports="$(
  sudo PYTHONPATH=src python3 -m ae.cli auth local --strict \
    --apishim-env state/profiles/k1s-core/apishim.cli.env
)" || rc=$?
if [[ -n "${auth_exports:-}" ]]; then
  eval "$auth_exports" || rc=$?
fi
export AE_RUNTIME_BACKEND=cri
export AE_INFRA_BACKEND=cri
export AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock
sudo -E PYTHONPATH=src python3 -m ae.cli nodes edge-b--node-1 --drain || rc=$?
sudo -E PYTHONPATH=src python3 -m ae.cli nodes edge-b--node-1 || rc=$?
sudo -E PYTHONPATH=src python3 -m ae.cli status echo-worker-drain --watch 2 --timeout 180 --events || rc=$?
exit "$rc"
EOF
    execute_rc=$?
    [[ "$execute_rc" -ne 0 ]] && rc=1
    printf '\n[supplemental drain execute rc=%d]\n' "$execute_rc"

    printf '\n[supplemental drain post-drain placement]\n'
    echo "===== $worker_a_ip ====="
    worker_a_output="$(show_drain_pods "$worker_a_ip")"
    printf '%s\n' "$worker_a_output"
    echo "===== $worker_b_ip ====="
    worker_b_output="$(show_drain_pods "$worker_b_ip")"
    printf '%s\n' "$worker_b_output"

    worker_b_count="$(count_drain_pods "$worker_b_output")"
    if [[ -n "$worker_a_output" || "$worker_b_count" -lt 2 ]]; then
      local attempt=""
      printf '\n[supplemental drain placement pending; retrying for up to 30s]\n'
      for attempt in $(seq 1 15); do
        sleep 2
        worker_a_output="$(show_drain_pods "$worker_a_ip")"
        worker_b_output="$(show_drain_pods "$worker_b_ip")"
        worker_b_count="$(count_drain_pods "$worker_b_output")"
        if [[ -z "$worker_a_output" && "$worker_b_count" -ge 2 ]]; then
          break
        fi
      done
      printf '===== %s (final) =====\n' "$worker_a_ip"
      printf '%s\n' "$worker_a_output"
      printf '===== %s (final) =====\n' "$worker_b_ip"
      printf '%s\n' "$worker_b_output"
    fi

    if [[ -n "$worker_a_output" || "$worker_b_count" -lt 2 ]]; then
      post_drain_rc=1
      rc=1
    fi
    printf '\n[supplemental drain post-drain rc=%d]\n' "$post_drain_rc"

    printf '\n[supplemental drain cleanup]\n'
    run_remote "$a_core_ip" "bash -s" <<'EOF'
set -uo pipefail
rc=0
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
cd /mnt/host || rc=$?
auth_exports="$(
  sudo PYTHONPATH=src python3 -m ae.cli auth local --strict \
    --apishim-env state/profiles/k1s-core/apishim.cli.env
)" || rc=$?
if [[ -n "${auth_exports:-}" ]]; then
  eval "$auth_exports" || rc=$?
fi
export AE_RUNTIME_BACKEND=cri
export AE_INFRA_BACKEND=cri
export AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock
sudo -E PYTHONPATH=src python3 -m ae.cli nodes edge-b--node-1 --uncordon || true
sudo -E PYTHONPATH=src python3 -m ae.cli delete --purge echo-worker-drain || true
exit "$rc"
EOF
    cleanup_rc=$?
    [[ "$cleanup_rc" -ne 0 ]] && rc=1
    printf '\n[supplemental drain cleanup rc=%d]\n' "$cleanup_rc"
  fi

  if [[ -d "$state_dir" || -d "$run_dir_path" ]]; then
    scripts/lab/vm/labctl.sh variant down \
      --variant "$variant" \
      --run-id "$run_id" \
      --purge --destroy-network
    down_rc=$?
    [[ "$down_rc" -ne 0 ]] && rc=1
    printf '\n[supplemental drain down rc=%d]\n' "$down_rc"
  else
    printf '\n[supplemental drain down skipped: no state/run dir created for RUN_ID=%s]\n' "$run_id"
  fi

  return "$rc"
}

run_stage2() {
  local run_id="${RUN_ID_STAGE2:-$(stage_run_id ha_core_stage2)}"
  local rc=0

  stage_header "4. Stage 2 one-shot acceptance."
  run_smoke "$ROOT_DIR/lab/variants/ha-control-plane-core.yaml" "$run_id"
  rc=$?
  printf '\n[stage2 one-shot rc=%d] RUN_ID=%s\n' "$rc" "$run_id"
  print_summary_file "$run_id"
  return "$rc"
}

run_stage2_live() {
  local variant="$ROOT_DIR/lab/variants/ha-control-plane-core.yaml"
  local run_id="${RUN_ID_STAGE2_LIVE:-$(stage_run_id ha_core_live)}"
  local rc=0
  local up_rc=0
  local smoke_rc=0
  local down_rc=0

  stage_header "5. Stage 2 live helper on a live stage-2 run."
  run_smoke "$variant" "$run_id" "--teardown never"
  up_rc=$?
  printf '\n[stage2 live up rc=%d] RUN_ID=%s\n' "$up_rc" "$run_id"
  print_summary_file "$run_id"

  if [[ "$up_rc" -eq 0 ]]; then
    RUN_ID="$run_id" make lab-vm-ha-core-workload-smoke
    smoke_rc=$?
    printf '\n[stage2 live helper rc=%d] RUN_ID=%s\n' "$smoke_rc" "$run_id"
  else
    smoke_rc=1
    printf '\n[stage2 live helper skipped after failed bring-up] RUN_ID=%s\n' "$run_id"
  fi

  scripts/lab/vm/labctl.sh variant down \
    --variant "$variant" \
    --run-id "$run_id" \
    --purge --destroy-network
  down_rc=$?
  printf '\n[stage2 live down rc=%d]\n' "$down_rc"

  [[ "$up_rc" -ne 0 || "$smoke_rc" -ne 0 || "$down_rc" -ne 0 ]] && rc=1
  return "$rc"
}

run_drills() {
  local run_id="${RUN_ID_DRILLS:-$(stage_run_id ha_core_drills)}"
  local rc=0

  stage_header "6. Optional HA drills on the dedicated drills variant."
  run_smoke "$ROOT_DIR/lab/variants/ha-control-plane-core-drills.yaml" "$run_id"
  rc=$?
  printf '\n[ha drills rc=%d] RUN_ID=%s\n' "$rc" "$run_id"
  print_summary_file "$run_id"
  return "$rc"
}

main() {
  local overall_rc=0
  local stage_rc=0

  parse_args "$@"
  if [[ "$LIST_STAGES" -eq 1 ]]; then
    list_stages
    exit 0
  fi

  load_requested_stages

  cd "$ROOT_DIR" || exit 1
  require_cmd jq
  require_cmd make
  require_cmd sudo
  ensure_ssh_key

  sudo -v || exit $?

  if stage_enabled "stage1"; then
    run_stage1
    stage_rc=$?
    record_stage_result "stage1" "$stage_rc"
    [[ "$stage_rc" -ne 0 ]] && overall_rc=1
  fi

  if stage_enabled "retained"; then
    run_retained_stage1
    stage_rc=$?
    record_stage_result "retained" "$stage_rc"
    [[ "$stage_rc" -ne 0 ]] && overall_rc=1
  fi

  if stage_enabled "drain"; then
    run_drain_stage
    stage_rc=$?
    record_stage_result "drain" "$stage_rc"
    [[ "$stage_rc" -ne 0 ]] && overall_rc=1
  fi

  if stage_enabled "stage2"; then
    run_stage2
    stage_rc=$?
    record_stage_result "stage2" "$stage_rc"
    [[ "$stage_rc" -ne 0 ]] && overall_rc=1
  fi

  if stage_enabled "stage2-live"; then
    run_stage2_live
    stage_rc=$?
    record_stage_result "stage2-live" "$stage_rc"
    [[ "$stage_rc" -ne 0 ]] && overall_rc=1
  fi

  if stage_enabled "drills"; then
    run_drills
    stage_rc=$?
    record_stage_result "drills" "$stage_rc"
    [[ "$stage_rc" -ne 0 ]] && overall_rc=1
  fi

  print_final_summary
  exit "$overall_rc"
}

main "$@"
