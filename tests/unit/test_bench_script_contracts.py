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
PIN_RUNTIME_CLASS = ROOT / "scripts" / "bench" / "pin_runtime_class.py"
MEM_SNAPSHOT = ROOT / "scripts" / "bench" / "mem_snapshot.sh"
K1ND_COMPOSE = ROOT / "ops" / "bench" / "k1nd-compose.yaml"
K1ND_ENTRYPOINT = ROOT / "ops" / "bench" / "k1nd-entrypoint.sh"


def test_run_all_baselines_keeps_rootless_and_rootful_podman_collection_split() -> None:
    text = RUN_ALL_BASELINES.read_text(encoding="utf-8")

    rootless_anchor = '# -------- Suite: k1s rootless --------'
    rootful_anchor = '# -------- Suite: k1s rootful (sudo) --------'

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
    assert 'DISABLE_DEV_MIN=${DISABLE_DEV_MIN:-0}' in text
    assert "ctrl_key = 'k3s_control_plane_pss_kb' if sc == 'k3d' else 'controller_pss_kb'" in text
    assert 'Ctrl/CP = AE controller PSS for k1s/k1nd, k3s control-plane PSS for k3d' in text


def test_k1nd_single_auto_shifts_busy_host_ports() -> None:
    text = K1ND_SINGLE.read_text(encoding="utf-8")

    assert 'choose_port api "${K1ND_API_PORT:-9108}"' in text
    assert 'choose_port apishim "${K1ND_APISHIM_PORT:-8445}"' in text
    assert 'choose_port caddy-http "${K1ND_CADDY_HTTP_PORT:-8888}"' in text
    assert 'choose_port caddy-https "${K1ND_CADDY_HTTPS_PORT:-8443}"' in text
    assert 'export K1ND_CADDY_HTTP_PORT="$caddy_http_port"' in text
    assert 'export K1ND_CADDY_HTTPS_PORT="$caddy_https_port"' in text
    assert 'busy on host; using' in text
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
    assert 'stable_polls=${BENCH_READY_STABLE_POLLS:-2}' in run_matrix_text
    assert 'live=$(echo "$js"' in run_matrix_text
    assert 'revision_status=$(echo "$js"' in run_matrix_text
    stable_ready_check = (
        'if [[ "$ready" == "$want" && "$desired" == "$want" && "$live" == "$want" && '
        '"$revision_status" == "ready" ]]; then'
    )
    assert (
        stable_ready_check in run_matrix_text
    )

    assert 'ae apply -f "$startman" || true' not in run_rollout_text
    assert 'ae scale "$app_name" --replicas "$replicas" || true' not in run_rollout_text
    assert 'wait_ready "$app_name" "$replicas" || true' not in run_rollout_text
    assert 'ae apply -f "$tmpman" || true' not in run_rollout_text
    assert "settle_current_revision()" in run_matrix_text
    assert 'settle_current_revision "$app_name" "$n"' in run_matrix_text
    assert 'local settle_delay=${BENCH_SETTLE_DELAY:-2}' in run_matrix_text
    assert 'sleep "$settle_delay"' in run_matrix_text
    assert "settle_current_revision()" in run_rollout_text
    assert run_rollout_text.count('settle_current_revision "$app_name" "$replicas"') == 2
    assert 'stable_polls=${BENCH_READY_STABLE_POLLS:-2}' in run_rollout_text
    assert 'live=$(echo "$js"' in run_rollout_text
    assert 'revision_status=$(echo "$js"' in run_rollout_text
    assert (
        stable_ready_check in run_rollout_text
    )
    assert 'local settle_delay=${BENCH_SETTLE_DELAY:-2}' in run_rollout_text
    assert 'sleep "$settle_delay"' in run_rollout_text
    assert 'host_manifest="$manifest"' in run_rollout_text
    assert 'host_apply_dir="${K1ND_APPLY_DIR:-state/bench-k1nd-apply}"' in run_rollout_text
    assert (
        'startman="${container_apply_dir}/rollout-start-${app_name}-${replicas}.yaml"'
        in run_rollout_text
    )
    assert (
        'tmpman="${container_apply_dir}/rollout-${app_name}-${replicas}.yaml"'
        in run_rollout_text
    )
    assert (
        'during_capture_timing="${BENCH_ROLLOUT_DURING_CAPTURE_TIMING:-immediate}"'
        in run_rollout_text
    )
    assert 'post_capture_timing="${BENCH_ROLLOUT_POST_CAPTURE_TIMING:-warm}"' in run_rollout_text
    assert '--capture-timing "$during_capture_timing"' in run_rollout_text
    assert '--capture-timing "$post_capture_timing"' in run_rollout_text

    assert 'wait_ready "$app_name" "$n" || true' not in run_matrix_k3s_text
    assert 'wait_ready "$deploy" "$replicas" || true' not in run_rollout_k3s_text
    assert "current_pod_uids()" in run_matrix_k3s_text
    assert 'AE_K3S_POD_UIDS="$(current_pod_uids)"' in run_matrix_k3s_text
    assert 'LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}' in run_matrix_text
    assert 'NIX_LD_LIBRARY_PATH=${NIX_LD_LIBRARY_PATH:-}' in run_matrix_text
    assert 'NIX_LD=${NIX_LD:-}' in run_matrix_text
    assert 'LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}' in run_rollout_text
    assert 'NIX_LD_LIBRARY_PATH=${NIX_LD_LIBRARY_PATH:-}' in run_rollout_text
    assert 'NIX_LD=${NIX_LD:-}' in run_rollout_text
    assert "current_pod_uids()" in run_rollout_k3s_text
    assert 'AE_K3S_POD_UIDS="$(current_pod_uids)"' in run_rollout_k3s_text


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
        'export AE_PODMAN_ENDPOINT_PREFER_DIRECT="${bench_podman_endpoint_prefer_direct}"'
        in text
    )


def test_bench_env_teardown_cleans_cri_owned_app_pods_and_orphans() -> None:
    text = BENCH_ENV_TEARDOWN.read_text(encoding="utf-8")

    assert 'if [[ "${AE_RUNTIME_BACKEND:-}" == "cri" ]]; then' in text
    assert 'cri_cleanup_app "${BENCH_PRIMARY_APP:-}"' in text
    assert "collect_controller_pids_for_specs()" in text
    assert 'pgrep -af "python .*ae\\\\.controller"' in text
    assert 'kill_controller_pids "${bench_spec_rel:-$bench_spec_dir}" "${fallback_controller_pids[@]}"' in text
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
    assert '[bench-env] remaining orphan CRI containers for app=${app}' in text


def test_run_cri_refresh_waits_for_sandbox_cleanup_between_rollouts() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert "bench_cleanup()" in text
    assert "cleanup_env_file=" in text
    assert "trap 'rc=$?; set +e; bench_cleanup; exit \"$rc\"' EXIT" in text
    assert 'cleanup_env_file="$ENV_FILE"' in text
    assert "bench_cleanup" in text
    assert "cri_wait_pod_ids_gone()" in text
    assert 'cri_wait_pod_ids_gone "${pod_ids_arr[@]}"' in text
    assert 'cri_cmd pods -o json' in text
    assert 'cri_cmd pods -a -o json' not in text
    assert "cri_wait_container_ids_gone()" in text
    assert 'cri_wait_container_ids_gone "${orphan_ids_arr[@]}"' in text
    assert "cri_wait_runtime_ready()" in text
    assert 'cri_wait_runtime_ready "cleanup for app=${bench_app_name}"' in text
    assert 'cri_wait_runtime_ready "cleanup before rollout replicas=${rep}"' in text
    assert 'CRI_POD_CLEANUP_TIMEOUT' in text
    assert 'CRI_POD_CLEANUP_SETTLE' in text
    assert 'CRI_RUNTIME_READY_TIMEOUT' in text
    assert 'CRI_RUNTIME_READY_DELAY' in text
    assert 'CRI_RUNTIME_READY_SETTLE' in text


def test_run_cri_refresh_falls_back_to_sudo_when_socket_acl_is_lost() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert "cri_switch_to_sudo()" in text
    assert "run_cri_preflight_inner()" in text
    assert (
        'non-sudo CRI access unavailable after ${reason}; falling back to sudo for remainder of run'
        in text
    )
    assert 'if ! cri_can_nosudo && cri_switch_to_sudo "crictl $1"; then' in text
    assert 'if ! cri_can_nosudo && cri_switch_to_sudo "ctr $1"; then' in text
    assert 'if [[ "$cri_use_sudo" == "0" ]] && ! cri_can_nosudo && cri_switch_to_sudo "CRI preflight"; then' in text


def test_run_cri_refresh_cleans_orphan_cri_containers_by_app_labels() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert "cri_collect_app_state_json()" in text
    assert 'cri_cmd pods -o json' in text
    assert 'cri_cmd ps -a -o json' in text
    assert 'state_json="$(cri_collect_app_state_json "$app")"' in text
    assert 'done < <(cri_state_field_lines "orphan_container_ids" "$state_json")' in text
    assert 'labels.get("ae.app")' in text
    assert 'labels.get("ae.pod_name") or labels.get("ae.replica_id")' in text
    assert 'replica_id.startswith(f"{app}-rev")' in text
    assert '[cri-refresh] removing orphan CRI containers for app=${app}' in text
    assert '[cri-refresh] remaining stale CRI pods for app=${app}' in text
    assert '[cri-refresh] remaining orphan CRI containers for app=${app}' in text
    assert text.count('cri_cleanup_app_pods "$bench_app_name"') == 2
    assert 'log "cleanup CRI pods before rollout replicas=${rep}"' in text


def test_run_cri_refresh_pins_workloads_to_runc_and_rejects_kata_snapshots() -> None:
    text = RUN_CRI_REFRESH.read_text(encoding="utf-8")

    assert 'bench_runtime_handler="runc"' in text
    assert 'AE_CRI_RUNTIME_HANDLER="${bench_runtime_handler}"' in text
    assert "scripts/bench/pin_runtime_class.py" in text
    assert 'verify_snapshot_runtime_handler "${LABEL_CRI}-pods-1"' in text
    assert """find "snapshots/${label}" -type f -path '*/raw/containers_mem.csv'""" in text
    assert '"/k8s.io/kata"' in text


def test_run_cri_verify_persists_logs_and_checks_complete_run_rows() -> None:
    text = RUN_CRI_VERIFY.read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in text
    assert "count_combined_rows()" in text
    assert 'log_file="${CRI_VERIFY_LOG_FILE:-state/bench-cri-rerun-' in text
    assert 'exec > >(tee -a "$log_file") 2>&1' in text
    assert (
        'trap \'rc=$?; log "error at line $LINENO: $BASH_COMMAND (exit=$rc)"; exit $rc\' ERR'
        in text
    )
    assert 'PURGE_EXISTING_RUNS="${PURGE_EXISTING_RUNS:-0}"' in text
    assert "make bench-mem-cri" in text
    assert "import csv" in text
    assert 'base, sep, engine = label.rpartition("+")' in text
    assert 'if current.startswith(label + "-"):' in text
    assert 'if sep and oci and current.startswith(f"{base}+{oci}+{engine}-"):' in text
    assert 'rows="$(count_combined_rows "$label")"' in text
    assert 'log "rows ${label}: $(count_combined_rows "$label")"' in text


def test_clean_state_stops_bench_controllers_before_removing_bench_state() -> None:
    text = BENCH_CLEAN_STATE.read_text(encoding="utf-8")

    assert "collect_bench_controller_pids()" in text
    assert 'pgrep -af "python .*ae\\\\.controller"' in text
    assert 'sudo pgrep -af "python .*ae\\\\.controller"' in text
    assert "stop_bench_controllers" in text
    assert '[clean-state] stopping ${#pids[@]} benchmark controller(s): ${pids[*]}' in text
    assert 'if [[ "$cmd" == *"--specs ${abs_specs_prefix}"* || "$cmd" == *"--specs ${rel_specs_prefix}"* ]]; then' in text


def test_bench_mem_finalize_sudo_rebuilds_charts_and_docs_as_invoking_user() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")

    assert "bench-mem-finalize-sudo:" in text
    assert '@sudo env PATH="$${PATH}" PYTHONPATH="$${PYTHONPATH:-}" python scripts/bench/mem_combine.py $${GLOB:-snapshots/*/*}' in text
    assert text.count("@$(MAKE) bench-fix-perms") >= 2
    assert 'RUN_AS="$${SUDO_USER:+sudo -u $$SUDO_USER}"' in text
    assert 'USER_PATH="$$(pwd)/.venv/bin:$${PATH}"' in text
    assert 'LD_LIBRARY_PATH="$${LD_LIBRARY_PATH:-}"' in text
    assert 'NIX_LD_LIBRARY_PATH="$${NIX_LD_LIBRARY_PATH:-}"' in text
    assert 'NIX_LD="$${NIX_LD:-}"' in text
    assert '$$RUN_AS env PATH="$$USER_PATH" PYTHONPATH="$${PYTHONPATH:-}" LD_LIBRARY_PATH="$${LD_LIBRARY_PATH:-}" NIX_LD_LIBRARY_PATH="$${NIX_LD_LIBRARY_PATH:-}" NIX_LD="$${NIX_LD:-}" PLOT_LATEST=$${PLOT_LATEST:-500} python scripts/bench/plot_overhead.py' in text
    assert '$$RUN_AS env PATH="$$USER_PATH" PYTHONPATH="$${PYTHONPATH:-}" LD_LIBRARY_PATH="$${LD_LIBRARY_PATH:-}" NIX_LD_LIBRARY_PATH="$${NIX_LD_LIBRARY_PATH:-}" NIX_LD="$${NIX_LD:-}" DOCS_CHART_STALENESS_HOURS=$${DOCS_CHART_STALENESS_HOURS:-168} python docs/build_docs.py' in text


def test_pin_runtime_class_helper_exists_for_bench_local_manifest_overrides() -> None:
    text = PIN_RUNTIME_CLASS.read_text(encoding="utf-8")

    assert 'default="runc"' in text
    assert 'spec["runtimeClassName"] = args.runtime_class' in text
    assert 'kind not in {"app", "deployment"}' in text


def test_mem_snapshot_supports_immediate_capture_timing() -> None:
    text = MEM_SNAPSHOT.read_text(encoding="utf-8")

    assert 'capture_timing="warm"' in text
    assert "--capture-timing" in text
    assert 'if [[ "$capture_timing" != "warm" && "$capture_timing" != "immediate" ]]; then' in text
    assert 'capture_process_and_container_state()' in text
    assert 'if [[ "$capture_timing" == "immediate" ]]; then' in text
    assert 'if [[ "$capture_timing" == "warm" ]]; then' in text
