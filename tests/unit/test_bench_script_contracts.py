from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_ALL_BASELINES = ROOT / "scripts" / "bench" / "run_all_baselines.sh"
K1ND_SINGLE = ROOT / "scripts" / "bench" / "k1nd_single.sh"
MAKEFILE = ROOT / "Makefile"
RUN_MATRIX = ROOT / "scripts" / "bench" / "run_matrix.sh"
RUN_ROLLOUT_K1S = ROOT / "scripts" / "bench" / "run_rollout_k1s.sh"
RUN_MATRIX_K3S = ROOT / "scripts" / "bench" / "run_matrix_k3s.sh"
RUN_ROLLOUT_K3S = ROOT / "scripts" / "bench" / "run_rollout_k3s.sh"
BENCH_ENV_PREP = ROOT / "scripts" / "bench" / "bench_env_prep.sh"
BENCH_ENV_TEARDOWN = ROOT / "scripts" / "bench" / "bench_env_teardown.sh"
BENCH_CLEAN_STATE = ROOT / "scripts" / "bench" / "clean_state.sh"
RUN_CRI_REFRESH = ROOT / "scripts" / "bench" / "run_cri_refresh.sh"
RUN_CRI_VERIFY = ROOT / "scripts" / "bench" / "run_cri_verify.sh"
RUN_CRI_ROLLOUT_PROBE = ROOT / "scripts" / "bench" / "run_cri_rollout_probe.sh"
RUN_CRI_ROLLOUT_CANDIDATE = ROOT / "scripts" / "bench" / "run_cri_rollout_candidate.sh"
AUDIT_CP_METRICS = ROOT / "scripts" / "bench" / "audit_cp_metrics.py"
SUMMARIZE_ROLLOUT_CANDIDATE = ROOT / "scripts" / "bench" / "summarize_rollout_candidate.py"
PIN_RUNTIME_CLASS = ROOT / "scripts" / "bench" / "pin_runtime_class.py"
PIN_ROLLOUT_POLICY = ROOT / "scripts" / "bench" / "pin_rollout_policy.py"
WAIT_ROLLOUT_STEADY = ROOT / "scripts" / "bench" / "wait_rollout_steady.py"
MEM_COMBINE = ROOT / "scripts" / "bench" / "mem_combine.py"
RUN_ROLLOUT_TUNING_EXPERIMENT = ROOT / "scripts" / "bench" / "run_rollout_tuning_experiment.sh"
MEM_SNAPSHOT = ROOT / "scripts" / "bench" / "mem_snapshot.sh"
K1ND_COMPOSE = ROOT / "ops" / "bench" / "k1nd-compose.yaml"
K1ND_ENTRYPOINT = ROOT / "ops" / "bench" / "k1nd-entrypoint.sh"


def test_run_all_baselines_keeps_rootless_and_rootful_podman_collection_split() -> None:
    text = RUN_ALL_BASELINES.read_text(encoding="utf-8")

    rootless_anchor = "# -------- Suite: k1s rootless --------"
    rootful_anchor = "# -------- Suite: k1s rootful (sudo) --------"

    assert rootless_anchor in text
    assert rootful_anchor in text

    rootless_block = text.split(rootless_anchor, maxsplit=1)[1].split(
        rootful_anchor,
        maxsplit=1,
    )[0]
    rootful_block = text.split(rootful_anchor, maxsplit=1)[1]

    assert (
        'run_isolated_k1s_suite rootless 9210 bench-mem-e2e-k1s "$LBL_K1S_ROOTLESS" 0'
        in rootless_block
    )
    assert (
        'run_isolated_k1s_suite rootful 9211 bench-mem-e2e-k1s-sudo "$LBL_K1S_ROOTFUL" 1'
        in rootful_block
    )
    assert "bench_env_prep.sh" in text
    assert "bench_env_teardown.sh" in text
    assert 'APP="$BENCH_PRIMARY_MANIFEST"' in text
    assert 'APP_NAME="$BENCH_PRIMARY_APP"' in text
    assert 'BENCH_WAIT_RUNTIME="$BENCH_WAIT_RUNTIME"' in text
    assert "DISABLE_DEV_MIN=${DISABLE_DEV_MIN:-0}" in text
    assert "ctrl_key = 'k3s_control_plane_pss_kb' if sc == 'k3d' else 'controller_pss_kb'" in text
    assert "Ctrl/CP = AE controller PSS for k1s/k1nd, k3s control-plane PSS for k3d" in text


def test_k1nd_single_auto_shifts_busy_host_ports() -> None:
    text = K1ND_SINGLE.read_text(encoding="utf-8")

    assert 'choose_port api "${K1ND_API_PORT:-9108}"' in text
    assert 'choose_port apishim "${K1ND_APISHIM_PORT:-8445}"' in text
    assert 'choose_port caddy-http "${K1ND_CADDY_HTTP_PORT:-8888}"' in text
    assert 'choose_port caddy-https "${K1ND_CADDY_HTTPS_PORT:-8443}"' in text
    assert 'export K1ND_CADDY_HTTP_PORT="$caddy_http_port"' in text
    assert 'export K1ND_CADDY_HTTPS_PORT="$caddy_https_port"' in text
    assert "busy on host; using" in text
    assert 'ports_state_file="$state_dir/ports.env"' in text
    assert "save_port_state()" in text
    assert "load_port_state()" in text
    assert 'source "$ports_state_file"' in text
    assert 'rm -f "$ports_state_file"' in text
    assert "reset_k1nd_dirs()" in text
    assert 'rm -rf "$state_dir" "$specs_dir" "$apply_dir"' in text


def test_k1nd_compose_sets_probe_loopback_fallback_for_containerized_controller() -> None:
    compose_text = K1ND_COMPOSE.read_text(encoding="utf-8")
    entrypoint_text = K1ND_ENTRYPOINT.read_text(encoding="utf-8")

    assert "AE_DOCKER_NETWORK: bench_default" in compose_text
    assert 'AE_DOCKER_ENDPOINT_PREFER_NETWORK: "1"' in compose_text
    assert "AE_PROBE_LOOPBACK_FALLBACK: host.docker.internal" in compose_text
    assert '"host.docker.internal:host-gateway"' in compose_text
    assert '"host.containers.internal:host-gateway"' in compose_text
    assert 'export AE_DOCKER_NETWORK="${AE_DOCKER_NETWORK:-bench_default}"' in entrypoint_text
    assert (
        'export AE_DOCKER_ENDPOINT_PREFER_NETWORK="${AE_DOCKER_ENDPOINT_PREFER_NETWORK:-1}"'
        in entrypoint_text
    )
    assert (
        'export AE_PROBE_LOOPBACK_FALLBACK="${AE_PROBE_LOOPBACK_FALLBACK:-host.docker.internal}"'
        in entrypoint_text
    )


def test_k1nd_make_targets_fail_fast_on_startup_errors() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")

    for target in (
        "bench-mem-e2e-k1nd:",
        "bench-mem-e2e-k1nd-sudo:",
        "bench-mem-e2e-k1nd-quick:",
        "bench-mem-e2e-k1nd-resume-rollout:",
    ):
        block = text.split(target, maxsplit=1)[1]
        assert "\t@set -e; \\" in block
        assert "BENCH_WAIT_RUNTIME=$${BENCH_WAIT_RUNTIME:-1}" in block


def test_benchmark_runners_fail_on_apply_scale_or_wait_errors() -> None:
    run_matrix_text = RUN_MATRIX.read_text(encoding="utf-8")
    run_rollout_text = RUN_ROLLOUT_K1S.read_text(encoding="utf-8")
    run_matrix_k3s_text = RUN_MATRIX_K3S.read_text(encoding="utf-8")
    run_rollout_k3s_text = RUN_ROLLOUT_K3S.read_text(encoding="utf-8")

    assert 'ae apply -f "$manifest" || true' not in run_matrix_text
    assert 'ae scale "$app_name" --replicas "$n" || true' not in run_matrix_text
    assert 'wait_ready "$app_name" "$n" || true' not in run_matrix_text
    assert "stable_polls=${BENCH_READY_STABLE_POLLS:-2}" in run_matrix_text
    assert 'live=$(echo "$js"' in run_matrix_text
    assert 'revision_status=$(echo "$js"' in run_matrix_text
    stable_ready_check = (
        'if [[ "$ready" == "$want" && "$desired" == "$want" && "$live" == "$want" && '
        '"$revision_status" == "ready" ]]; then'
    )
    assert stable_ready_check in run_matrix_text

    assert 'ae apply -f "$startman" || true' not in run_rollout_text
    assert 'ae scale "$app_name" --replicas "$replicas" || true' not in run_rollout_text
    assert 'wait_ready "$app_name" "$replicas" || true' not in run_rollout_text
    assert 'ae apply -f "$tmpman" || true' not in run_rollout_text
    assert "settle_current_revision()" in run_matrix_text
    assert 'settle_current_revision "$app_name" "$n"' in run_matrix_text
    assert "local settle_delay=${BENCH_SETTLE_DELAY:-2}" in run_matrix_text
    assert 'sleep "$settle_delay"' in run_matrix_text
    assert "settle_current_revision()" in run_rollout_text
    assert run_rollout_text.count('settle_current_revision "$app_name" "$replicas"') == 2
    assert "stable_polls=${BENCH_READY_STABLE_POLLS:-2}" in run_rollout_text
    assert 'live=$(echo "$js"' in run_rollout_text
    assert 'revision_status=$(echo "$js"' in run_rollout_text
    assert stable_ready_check in run_rollout_text
    assert "local settle_delay=${BENCH_SETTLE_DELAY:-2}" in run_rollout_text
    assert 'sleep "$settle_delay"' in run_rollout_text
    assert 'host_manifest="$manifest"' in run_rollout_text
    assert 'host_apply_dir="${K1ND_APPLY_DIR:-state/bench-k1nd-apply}"' in run_rollout_text
    assert (
        'startman="${container_apply_dir}/rollout-start-${app_name}-${replicas}.yaml"'
        in run_rollout_text
    )
    assert (
        'tmpman="${container_apply_dir}/rollout-${app_name}-${replicas}.yaml"' in run_rollout_text
    )
    assert (
        'during_capture_timing="${BENCH_ROLLOUT_DURING_CAPTURE_TIMING:-immediate}"'
        in run_rollout_text
    )
    assert (
        'during_warm_capture_timing="${BENCH_ROLLOUT_DURING_WARM_CAPTURE_TIMING:-warm}"'
        in run_rollout_text
    )
    assert 'post_capture_timing="${BENCH_ROLLOUT_POST_CAPTURE_TIMING:-warm}"' in run_rollout_text
    assert "run_rollout_hook()" in run_rollout_text
    assert "BENCH_PRE_POST_SNAPSHOT_CMD" in run_rollout_text
    assert 'BENCH_SNAPSHOT_STAGE="$stage"' in run_rollout_text
    assert 'BENCH_BACKEND="${AE_RUNTIME_BACKEND:-podman}"' in run_rollout_text
    assert 'BENCH_APP_NAME="$app_name"' in run_rollout_text
    assert '--capture-timing "$capture_timing"' in run_rollout_text
    assert 'run_rollout_snapshot "$label" "$capture_timing" >/dev/null' in run_rollout_text
    assert 'printf -v "$pid_var" \'%s\' "$!"' in run_rollout_text
    assert 'during_pid="$(start_rollout_snapshot' not in run_rollout_text
    assert 'during_warm_pid="$(start_rollout_snapshot' not in run_rollout_text
    assert (
        'start_rollout_snapshot during_pid "$during_label" "$during_capture_timing" "DURING"'
        in run_rollout_text
    )
    assert (
        'start_rollout_snapshot during_warm_pid "$during_warm_label" "$during_warm_capture_timing" "DURING-WARM"'
        in run_rollout_text
    )
    assert (
        'local during_warm_label="${label_suite}-rollout-${replicas}-during-warm"'
        in run_rollout_text
    )

    assert 'wait_ready "$app_name" "$n" || true' not in run_matrix_k3s_text
    assert 'wait_ready "$deploy" "$replicas" || true' not in run_rollout_k3s_text
    assert "current_pod_uids()" in run_matrix_k3s_text
    assert 'AE_K3S_POD_UIDS="$(current_pod_uids)"' in run_matrix_k3s_text
    assert "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}" in run_matrix_text
    assert "NIX_LD_LIBRARY_PATH=${NIX_LD_LIBRARY_PATH:-}" in run_matrix_text
    assert "NIX_LD=${NIX_LD:-}" in run_matrix_text
    assert "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}" in run_rollout_text
    assert "NIX_LD_LIBRARY_PATH=${NIX_LD_LIBRARY_PATH:-}" in run_rollout_text
    assert "NIX_LD=${NIX_LD:-}" in run_rollout_text
    assert "current_pod_uids()" in run_rollout_k3s_text
    assert 'AE_K3S_POD_UIDS="$(current_pod_uids)"' in run_rollout_k3s_text
    assert (
        'during_capture_timing="${BENCH_ROLLOUT_DURING_CAPTURE_TIMING:-immediate}"'
        in run_rollout_k3s_text
    )
    assert (
        'during_warm_capture_timing="${BENCH_ROLLOUT_DURING_WARM_CAPTURE_TIMING:-warm}"'
        in run_rollout_k3s_text
    )
    assert (
        'post_capture_timing="${BENCH_ROLLOUT_POST_CAPTURE_TIMING:-warm}"' in run_rollout_k3s_text
    )
    assert '--capture-timing "$capture_timing"' in run_rollout_k3s_text
    assert 'run_rollout_snapshot "$label" "$capture_timing" >/dev/null' in run_rollout_k3s_text
    assert 'printf -v "$pid_var" \'%s\' "$!"' in run_rollout_k3s_text
    assert 'during_pid="$(start_rollout_snapshot' not in run_rollout_k3s_text
    assert 'during_warm_pid="$(start_rollout_snapshot' not in run_rollout_k3s_text
    assert (
        'start_rollout_snapshot during_pid "$during_label" "$during_capture_timing" "DURING"'
        in run_rollout_k3s_text
    )
    assert (
        'start_rollout_snapshot during_warm_pid "$during_warm_label" "$during_warm_capture_timing" "DURING-WARM"'
        in run_rollout_k3s_text
    )
    assert (
        'local during_warm_label="${label_suite}-rollout-${replicas}-during-warm"'
        in run_rollout_k3s_text
    )


def test_bench_env_prep_prefers_direct_podman_endpoints_for_sudo_controller() -> None:
    text = BENCH_ENV_PREP.read_text(encoding="utf-8")

    assert 'bench_podman_endpoint_prefer_direct_default="0"' in text
    assert 'bench_podman_endpoint_prefer_direct_default="1"' in text
    assert (
        'bench_podman_endpoint_prefer_direct="${BENCH_PODMAN_ENDPOINT_PREFER_DIRECT:-${AE_PODMAN_ENDPOINT_PREFER_DIRECT:-$bench_podman_endpoint_prefer_direct_default}}"'
        in text
    )
    assert 'LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" \\' in text
    assert 'NIX_LD_LIBRARY_PATH="${NIX_LD_LIBRARY_PATH:-}" \\' in text
    assert 'NIX_LD="${NIX_LD:-}" \\' in text
    assert 'AE_PODMAN_ENDPOINT_PREFER_DIRECT="$bench_podman_endpoint_prefer_direct" \\' in text
    assert (
        'export AE_PODMAN_ENDPOINT_PREFER_DIRECT="${bench_podman_endpoint_prefer_direct}"' in text
    )


def test_bench_env_teardown_cleans_cri_owned_app_pods_and_orphans() -> None:
    text = BENCH_ENV_TEARDOWN.read_text(encoding="utf-8")

    assert 'if [[ "${AE_RUNTIME_BACKEND:-}" == "cri" ]]; then' in text
    assert 'cri_cleanup_app "${BENCH_PRIMARY_APP:-}"' in text
    assert "collect_controller_pids_for_specs()" in text
    assert 'pgrep -af "python .*ae\\\\.controller"' in text
    assert (
        'kill_controller_pids "${bench_spec_rel:-$bench_spec_dir}" "${fallback_controller_pids[@]}"'
        in text
    )
    assert "cri_can_nosudo()" in text
    assert "cri_switch_to_sudo()" in text
    assert 'crictl --runtime-endpoint "$AE_CRI_ENDPOINT" info' in text
    assert 'if ! cri_can_nosudo && cri_switch_to_sudo "crictl $1"; then' in text
    assert 'state_json="$(cri_collect_app_state_json "$app")"' in text
    assert 'done < <(cri_state_field_lines "orphan_container_ids" "$state_json")' in text
    assert 'labels.get("ae.app") == app' in text
    assert 'replica_id = labels.get("ae.pod_name") or labels.get("ae.replica_id") or ""' in text
    assert 'echo "[bench-env] removing CRI pods for app=${app}: ${#pod_ids_arr[@]}" >&2' in text
    assert (
        'echo "[bench-env] removing orphan CRI containers for app=${app}: ${#container_ids_arr[@]}" >&2'
        in text
    )
    assert "[bench-env] remaining orphan CRI containers for app=${app}" in text


def test_run_cri_refresh_waits_for_sandbox_cleanup_between_rollouts() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert "bench_cleanup()" in text
    assert "cleanup_env_file=" in text
    assert "trap 'rc=$?; set +e; bench_cleanup; exit \"$rc\"' EXIT" in text
    assert 'cleanup_env_file="$ENV_FILE"' in text
    assert "bench_cleanup" in text
    assert "cri_wait_pod_ids_gone()" in text
    assert 'cri_wait_pod_ids_gone "${pod_ids_arr[@]}"' in text
    assert "cri_cmd pods -o json" in text
    assert "cri_cmd pods -a -o json" not in text
    assert "cri_wait_container_ids_gone()" in text
    assert 'cri_wait_container_ids_gone "${orphan_ids_arr[@]}"' in text
    assert "cri_wait_runtime_ready()" in text
    assert 'cri_wait_runtime_ready "cleanup for app=${bench_app_name}"' in text
    assert 'cri_wait_runtime_ready "delete before rollout replicas=${rep}"' in text
    assert 'cri_wait_runtime_ready "cleanup before rollout replicas=${rep}"' in text
    assert "cri_wait_app_quiet()" in text
    assert 'cri_wait_app_quiet "$bench_app_name" "cleanup for app=${bench_app_name}"' in text
    assert 'cri_wait_app_quiet "$bench_app_name" "cleanup before rollout replicas=${rep}"' in text
    assert ': "${BENCH_READY_STABLE_POLLS:=3}"' in text
    assert ': "${BENCH_SETTLE_DELAY:=5}"' in text
    assert "CRI_POD_CLEANUP_TIMEOUT" in text
    assert "CRI_POD_CLEANUP_SETTLE" in text
    assert "CRI_RUNTIME_READY_TIMEOUT" in text
    assert "CRI_RUNTIME_READY_DELAY" in text
    assert "CRI_RUNTIME_READY_SETTLE" in text
    assert "CRI_IDLE_QUIET_TIMEOUT" in text
    assert "CRI_IDLE_QUIET_DELAY" in text
    assert "CRI_IDLE_QUIET_POLLS" in text
    assert ': "${BENCH_IDLE_VALIDATE_ZERO_APP:=1}"' in text
    assert "export BENCH_READY_STABLE_POLLS" in text
    assert "export BENCH_SETTLE_DELAY" in text
    assert "export CRI_IDLE_QUIET_POLLS" in text
    assert "export BENCH_IDLE_VALIDATE_ZERO_APP" in text
    assert 'BENCH_IDLE_VALIDATE_ZERO_APP="$BENCH_IDLE_VALIDATE_ZERO_APP" \\' in text


def test_run_cri_refresh_falls_back_to_sudo_when_socket_acl_is_lost() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert "cri_switch_to_sudo()" in text
    assert "run_cri_preflight_inner()" in text
    assert (
        "non-sudo CRI access unavailable after ${reason}; falling back to sudo for remainder of run"
        in text
    )
    assert 'if ! cri_can_nosudo && cri_switch_to_sudo "crictl $1"; then' in text
    assert 'if ! cri_can_nosudo && cri_switch_to_sudo "ctr $1"; then' in text
    assert (
        'if [[ "$cri_use_sudo" == "0" ]] && ! cri_can_nosudo && cri_switch_to_sudo "CRI preflight"; then'
        in text
    )


def test_run_cri_refresh_cleans_orphan_cri_containers_by_app_labels() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert "cri_collect_app_state_json()" in text
    assert "cri_cmd pods -o json" in text
    assert "cri_cmd ps -a -o json" in text
    assert 'state_json="$(cri_collect_app_state_json "$app")"' in text
    assert '"live_container_ids"' in text
    assert 'done < <(cri_state_field_lines "orphan_container_ids" "$state_json")' in text
    assert 'labels.get("ae.app")' in text
    assert 'labels.get("ae.pod_name") or labels.get("ae.replica_id")' in text
    assert 'replica_id.startswith(f"{app}-rev")' in text
    assert "container_pod_ids = set()" in text
    assert "pod_ids.update(container_pod_ids)" in text
    assert "[cri-refresh] removing orphan CRI containers for app=${app}" in text
    assert "[cri-refresh] remaining stale CRI pods for app=${app}" in text
    assert "[cri-refresh] remaining orphan CRI containers for app=${app}" in text
    assert text.count('cri_cleanup_app_pods "$bench_app_name"') == 2
    assert 'delete_bench_app "$bench_app_name" "rollout replicas=${rep}"' in text
    assert 'log "cleanup CRI pods before rollout replicas=${rep}"' in text
    assert 'printf \'%s\' "$state_json" | "$python_bin" -c ' in text


def test_run_cri_refresh_captures_debug_and_preidle_inspect_guard() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert ': "${CRI_DEBUG_STATE_DIR:=state/bench-cri-debug}"' in text
    assert ': "${CRI_DEBUG_CAPTURE_ON_QUIET:=1}"' in text
    assert "cri_dump_app_state_debug()" in text
    assert 'printf \'%s\\n\' "$state_json" > "$outdir/state.json"' in text
    assert 'printf \'%s\\n\' "$pods_json" > "$outdir/pods.json"' in text
    assert 'printf \'%s\\n\' "$ps_json" > "$outdir/containers.json"' in text
    assert "cri_collect_matching_containers_via_inspect()" in text
    assert 'cri_cmd inspect -o json "$cid"' in text
    assert 'cri_cmd inspectp -o json "$pod_id"' in text
    assert "cri_assert_app_absent_via_inspect()" in text
    assert 'cri_dump_app_state_debug "$app" "quiet-${reason}"' in text
    assert 'cri_dump_app_state_debug "$app" "quiet-timeout-${reason}"' in text
    assert 'cri_dump_app_state_debug "$app" "inspect-failure-${reason}"' in text
    assert (
        'cri_assert_app_absent_via_inspect "$bench_app_name" "cleanup for app=${bench_app_name}"'
        in text
    )
    assert (
        'cri_assert_app_absent_via_inspect "$bench_app_name" "cleanup before rollout replicas=${rep}"'
        in text
    )


def test_run_cri_refresh_uses_bench_local_ae_cli_for_rollout_reset() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert "ae_cli()" in text
    assert (
        'sudo env "${sudo_env_clean[@]}" "${sudo_env_cli[@]}" "$python_bin" -m ae.cli "$@"' in text
    )
    assert "delete_bench_app()" in text
    assert 'ae_cli delete "$app"' in text
    assert 'log "delete desired state for app=${app} before ${reason}"' in text


def test_run_matrix_can_validate_idle_snapshots() -> None:
    text = RUN_MATRIX.read_text(encoding="utf-8")

    assert "validate_idle_snapshot()" in text
    assert 'if [[ "${BENCH_IDLE_VALIDATE_ZERO_APP:-0}" != "1" ]]; then' in text
    assert 'scripts/bench/check_idle_snapshot.py "$snapshot_path" --app-name "$name"' in text
    assert (
        'idle_snapshot_path="$(scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-idle" --duration "$duration")"'
        in text
    )
    assert 'validate_idle_snapshot "$idle_snapshot_path" "$app_name"' in text
    assert "run_pre_snapshot_hook()" in text
    assert "BENCH_PRE_STEADY_SNAPSHOT_CMD" in text
    assert 'export BENCH_SNAPSHOT_STAGE="$stage"' in text
    assert 'BENCH_BACKEND="${AE_RUNTIME_BACKEND:-podman}"' in text
    assert 'BENCH_APP_NAME="$app_name"' in text


def test_bench_env_teardown_reads_state_fields_from_json_stdin() -> None:
    text = BENCH_ENV_TEARDOWN.read_text(encoding="utf-8")

    assert 'printf \'%s\' "$state_json" | "${PYTHON_BIN:-python}" -c ' in text


def test_run_cri_refresh_pins_workloads_to_runc_and_rejects_kata_snapshots() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert 'bench_runtime_handler="runc"' in text
    assert 'bench_rollout_strategy="${BENCH_CRI_ROLLOUT_STRATEGY:-ordered}"' in text
    assert 'bench_steady_quiet="${BENCH_CRI_STEADY_QUIET:-1}"' in text
    assert 'AE_CRI_RUNTIME_HANDLER="${bench_runtime_handler}"' in text
    assert "scripts/bench/pin_runtime_class.py" in text
    assert "scripts/bench/pin_rollout_policy.py" in text
    assert "build_steady_cmd()" in text
    assert 'BENCH_PRE_STEADY_SNAPSHOT_CMD="$steady_hook_cmd"' in text
    assert 'BENCH_PRE_POST_SNAPSHOT_CMD="$steady_hook_cmd"' in text
    assert 'published CRI profile: strategy=${bench_rollout_strategy} steady_quiet=${bench_steady_quiet}' in text
    assert "first_requested_replica()" in text
    assert 'first_steady_replica="$(first_requested_replica "$REPLICAS")"' in text
    assert 'verify_snapshot_runtime_handler "${LABEL_CRI}-pods-${first_steady_replica}"' in text
    assert """find "snapshots/${label}" -type f -path '*/raw/containers_mem.csv'""" in text
    assert '"/k8s.io/kata"' in text
    assert 'if [[ -n "${BENCH_EXPERIMENT_OUTPUT_ROOT:-}" ]]; then' in text
    assert "skipping shared combined/charts/docs rebuild" in text


def test_run_cri_verify_persists_logs_and_checks_complete_run_rows() -> None:
    text = RUN_CRI_VERIFY.read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in text
    assert "count_requested_replicas()" in text
    assert "first_requested_replica()" in text
    assert "count_combined_rows()" in text
    assert 'log_file="${CRI_VERIFY_LOG_FILE:-state/bench-cri-rerun-' in text
    assert 'exec > >(tee -a "$log_file") 2>&1' in text
    assert (
        "trap 'rc=$?; log \"error at line $LINENO: $BASH_COMMAND (exit=$rc)\"; exit $rc' ERR"
        in text
    )
    assert 'PURGE_EXISTING_RUNS="${PURGE_EXISTING_RUNS:-0}"' in text
    assert "make bench-mem-cri" in text
    assert "import csv" in text
    assert 'base, sep, engine = label.rpartition("+")' in text
    assert 'if current.startswith(label + "-"):' in text
    assert 'if sep and oci and current.startswith(f"{base}+{oci}+{engine}-"):' in text
    assert 'first_steady_replica="$(first_requested_replica "$REPLICAS")"' in text
    assert "expected_rows=$((1 + steady_count + (3 * rollout_count)))" in text
    assert 'find "snapshots/${label}-pods-${first_steady_replica}"' in text
    assert 'rows="$(count_combined_rows "$label")"' in text
    assert 'test "$rows" -eq "$expected_rows"' in text
    assert 'log "rows ${label}: $(count_combined_rows "$label")"' in text


def test_run_cri_rollout_probe_wraps_narrow_verify_suite() -> None:
    text = RUN_CRI_ROLLOUT_PROBE.read_text(encoding="utf-8")

    assert "Usage: scripts/bench/run_cri_rollout_probe.sh" in text
    assert "--during-capture-timing MODE" in text
    assert 'during_capture_timing="${BENCH_ROLLOUT_DURING_CAPTURE_TIMING:-immediate}"' in text
    assert 'during_warm_capture_timing="${BENCH_ROLLOUT_DURING_WARM_CAPTURE_TIMING:-warm}"' in text
    assert 'post_capture_timing="${BENCH_ROLLOUT_POST_CAPTURE_TIMING:-warm}"' in text
    assert 'REPLICAS="5" \\' in text
    assert 'ROLL_REPLICAS="5" \\' in text
    assert 'BENCH_ROLLOUT_DURING_CAPTURE_TIMING="$during_capture_timing" \\' in text
    assert 'BENCH_ROLLOUT_DURING_WARM_CAPTURE_TIMING="$during_warm_capture_timing" \\' in text
    assert 'BENCH_ROLLOUT_POST_CAPTURE_TIMING="$post_capture_timing" \\' in text
    assert "./scripts/bench/run_cri_verify.sh" in text


def test_audit_cp_metrics_reads_shadow_control_plane_fields() -> None:
    text = AUDIT_CP_METRICS.read_text(encoding="utf-8")

    assert "controller_pss_kb" in text
    assert "ingress_pss_kb" in text
    assert "k3s_control_plane_pss_kb" in text
    assert "control_plane_pss_kb" in text
    assert "host_system_cgroups_bytes" in text
    assert "mem_available_delta_bytes" in text
    assert "docs_cp_mib" in text


def test_clean_state_stops_bench_controllers_before_removing_bench_state() -> None:
    text = BENCH_CLEAN_STATE.read_text(encoding="utf-8")

    assert "collect_bench_controller_pids()" in text
    assert 'pgrep -af "python .*ae\\\\.controller"' in text
    assert 'sudo pgrep -af "python .*ae\\\\.controller"' in text
    assert "stop_bench_controllers" in text
    assert "[clean-state] stopping ${#pids[@]} benchmark controller(s): ${pids[*]}" in text
    assert (
        'if [[ "$cmd" == *"--specs ${abs_specs_prefix}"* || "$cmd" == *"--specs ${rel_specs_prefix}"* ]]; then'
        in text
    )


def test_bench_mem_finalize_sudo_rebuilds_charts_and_docs_as_invoking_user() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")

    assert "bench-mem-finalize-sudo:" in text
    assert (
        '@sudo env PATH="$${PATH}" PYTHONPATH="$${PYTHONPATH:-}" python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}'
        in text
    )
    assert text.count("@$(MAKE) bench-fix-perms") >= 2
    assert 'RUN_AS="$${SUDO_USER:+sudo -u $$SUDO_USER}"' in text
    assert 'USER_PATH="$$(pwd)/.venv/bin:$${PATH}"' in text
    assert 'LD_LIBRARY_PATH="$${LD_LIBRARY_PATH:-}"' in text
    assert 'NIX_LD_LIBRARY_PATH="$${NIX_LD_LIBRARY_PATH:-}"' in text
    assert 'NIX_LD="$${NIX_LD:-}"' in text
    assert (
        '$$RUN_AS env PATH="$$USER_PATH" PYTHONPATH="$${PYTHONPATH:-}" LD_LIBRARY_PATH="$${LD_LIBRARY_PATH:-}" NIX_LD_LIBRARY_PATH="$${NIX_LD_LIBRARY_PATH:-}" NIX_LD="$${NIX_LD:-}" PLOT_LATEST=$${PLOT_LATEST:-500} python scripts/bench/plot_overhead.py'
        in text
    )
    assert (
        '$$RUN_AS env PATH="$$USER_PATH" PYTHONPATH="$${PYTHONPATH:-}" LD_LIBRARY_PATH="$${LD_LIBRARY_PATH:-}" NIX_LD_LIBRARY_PATH="$${NIX_LD_LIBRARY_PATH:-}" NIX_LD="$${NIX_LD:-}" DOCS_CHART_STALENESS_HOURS=$${DOCS_CHART_STALENESS_HOURS:-168} python docs/build_docs.py'
        in text
    )


def test_pin_runtime_class_helper_exists_for_bench_local_manifest_overrides() -> None:
    text = PIN_RUNTIME_CLASS.read_text(encoding="utf-8")

    assert 'default="runc"' in text
    assert 'spec["runtimeClassName"] = args.runtime_class' in text
    assert 'kind not in {"app", "deployment"}' in text


def test_pin_rollout_policy_helper_exists_for_experiment_manifest_overrides() -> None:
    text = PIN_ROLLOUT_POLICY.read_text(encoding="utf-8")

    assert 'ALLOWED_STRATEGIES = {"ordered", "parallel"}' in text
    assert "choices=sorted(ALLOWED_STRATEGIES)" in text
    assert 'rollout["strategy"] = args.strategy' in text
    assert 'kind not in {"app", "deployment"}' in text


def test_wait_rollout_steady_helper_supports_backend_aware_sampling() -> None:
    text = WAIT_ROLLOUT_STEADY.read_text(encoding="utf-8")

    assert 'choices=("cri", "podman", "docker")' in text
    assert "def collect_cri_sample" in text
    assert "def collect_podman_sample" in text
    assert "def collect_docker_sample" in text
    assert 'docker", "inspect", *ids' in text
    assert 'podman_bin, "ps", "-a", "--format", "json"' in text


def test_mem_combine_supports_experiment_local_output_dirs() -> None:
    text = MEM_COMBINE.read_text(encoding="utf-8")
    make_text = MAKEFILE.read_text(encoding="utf-8")

    assert "scripts/bench/mem_combine.py [--outdir DIR]" in text
    assert 'outdir = Path("combined")' in text
    assert 'if arg == "--outdir":' in text
    assert "combined_dir/combined.csv with" not in text
    assert "$${COMBINE_OUTDIR:+--outdir $$COMBINE_OUTDIR}" in make_text


def test_rollout_tuning_experiment_runner_stays_off_the_publish_path() -> None:
    text = RUN_ROLLOUT_TUNING_EXPERIMENT.read_text(encoding="utf-8")
    make_text = MAKEFILE.read_text(encoding="utf-8")

    assert "state/bench-experiments/" in text
    assert '*"+exp+"*' in text
    assert "BENCH_EXPERIMENT_STEADY_QUIET=${BENCH_EXPERIMENT_STEADY_QUIET:-0}" in text
    assert "BENCH_EXPERIMENT_STEADY_QUIET    enable steady quiet hooks (default: 0)" in text
    assert 'BENCH_SPECS_SRC="$ROOT_DIR"' in text
    assert 'BENCH_EXPERIMENT_OUTPUT_ROOT="$experiment_root"' in text
    assert 'scripts/bench/mem_combine.py --outdir "$experiment_combined_dir"' in text
    assert (
        'scripts/bench/plot_overhead.py "$experiment_combined_dir/combined.csv" "$experiment_charts_dir"'
        in text
    )
    assert "make bench-mem-docs" not in text
    assert "combined/combined.csv" not in text
    assert "bench-rollout-tuning-experiment:" in make_text


def test_cri_rollout_candidate_wrapper_uses_grouped_experiment_outputs() -> None:
    text = RUN_CRI_ROLLOUT_CANDIDATE.read_text(encoding="utf-8")
    make_text = MAKEFILE.read_text(encoding="utf-8")

    assert 'CONTROL_STRATEGY="${CONTROL_STRATEGY:-parallel}"' in text
    assert 'GROUP_ROOT="${GROUP_ROOT:-state/bench-experiments/${GROUP_ID}}"' in text
    assert 'LABEL_BASE_PREFIX="$(ensure_label_base "${LABEL_BASE_PREFIX:-}")"' in text
    assert 'BENCH_EXPERIMENT_OUTPUT_ROOT="$exp_dir"' in text
    assert 'BENCH_EXPERIMENT_STEADY_QUIET="$CONTROL_STEADY_QUIET"' not in text
    assert 'BENCH_EXPERIMENT_STEADY_QUIET="$steady_quiet"' in text
    assert 'BENCH_EXPERIMENT_ROLLOUT_STRATEGY="$rollout_strategy"' in text
    assert 'python scripts/bench/summarize_rollout_candidate.py "$GROUP_ROOT"' in text
    assert "baseline-r1" not in text
    assert "bench-cri-rollout-candidate:" in make_text


def test_rollout_candidate_summary_helper_reports_promotion_gates() -> None:
    text = SUMMARIZE_ROLLOUT_CANDIDATE.read_text(encoding="utf-8")

    assert 'default=30.0' in text
    assert 'default=3.0' in text
    assert '"rollout-5-during app improvement"' in text
    assert '"pods-5 steady-state app drift"' in text
    assert '"rollout-5-post app drift"' in text
    assert 'candidate group must include both baseline and ordered experiment runs' in text


def test_mem_snapshot_supports_immediate_capture_timing() -> None:
    text = MEM_SNAPSHOT.read_text(encoding="utf-8")

    assert 'capture_timing="warm"' in text
    assert "--capture-timing" in text
    assert 'if [[ "$capture_timing" != "warm" && "$capture_timing" != "immediate" ]]; then' in text
    assert "capture_process_and_container_state()" in text
    assert 'if [[ "$capture_timing" == "immediate" ]]; then' in text
    assert "cri_ps.json" in text
    assert "cri_pods.json" in text
    assert "cri_info.json" in text
    assert 'if [[ "$capture_timing" == "warm" ]]; then' in text
