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
K1ND_COMPOSE = ROOT / "ops" / "bench" / "k1nd-compose.yaml"
K1ND_ENTRYPOINT = ROOT / "ops" / "bench" / "k1nd-entrypoint.sh"


def test_run_all_baselines_keeps_rootless_and_rootful_podman_collection_split() -> None:
    text = RUN_ALL_BASELINES.read_text(encoding="utf-8")

    rootless_anchor = '# -------- Suite: k1s rootless --------'
    rootful_anchor = '# -------- Suite: k1s rootful (sudo) --------'

    assert rootless_anchor in text
    assert rootful_anchor in text

    rootless_block = text.split(rootless_anchor, maxsplit=1)[1].split(rootful_anchor, maxsplit=1)[0]
    rootful_block = text.split(rootful_anchor, maxsplit=1)[1]

    assert 'run_isolated_k1s_suite rootless 9210 bench-mem-e2e-k1s "$LBL_K1S_ROOTLESS" 0' in rootless_block
    assert 'run_isolated_k1s_suite rootful 9211 bench-mem-e2e-k1s-sudo "$LBL_K1S_ROOTFUL" 1' in rootful_block
    assert "bench_env_prep.sh" in text
    assert "bench_env_teardown.sh" in text
    assert 'APP="$BENCH_PRIMARY_MANIFEST"' in text
    assert 'APP_NAME="$BENCH_PRIMARY_APP"' in text
    assert 'BENCH_WAIT_RUNTIME="$BENCH_WAIT_RUNTIME"' in text


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

    assert 'ae apply -f "$startman" || true' not in run_rollout_text
    assert 'ae scale "$app_name" --replicas "$replicas" || true' not in run_rollout_text
    assert 'wait_ready "$app_name" "$replicas" || true' not in run_rollout_text
    assert 'ae apply -f "$tmpman" || true' not in run_rollout_text
    assert "settle_current_revision()" in run_matrix_text
    assert 'settle_current_revision "$app_name" "$n"' in run_matrix_text
    assert "settle_current_revision()" in run_rollout_text
    assert run_rollout_text.count('settle_current_revision "$app_name" "$replicas"') == 2
    assert 'host_manifest="$manifest"' in run_rollout_text
    assert 'host_apply_dir="${K1ND_APPLY_DIR:-state/bench-k1nd-apply}"' in run_rollout_text
    assert 'startman="${container_apply_dir}/rollout-start-${app_name}-${replicas}.yaml"' in run_rollout_text
    assert 'tmpman="${container_apply_dir}/rollout-${app_name}-${replicas}.yaml"' in run_rollout_text

    assert 'wait_ready "$app_name" "$n" || true' not in run_matrix_k3s_text
    assert 'wait_ready "$deploy" "$replicas" || true' not in run_rollout_k3s_text


def test_bench_env_prep_prefers_direct_podman_endpoints_for_sudo_controller() -> None:
    text = BENCH_ENV_PREP.read_text(encoding="utf-8")

    assert 'bench_podman_endpoint_prefer_direct_default="0"' in text
    assert 'bench_podman_endpoint_prefer_direct_default="1"' in text
    assert (
        'bench_podman_endpoint_prefer_direct="${BENCH_PODMAN_ENDPOINT_PREFER_DIRECT:-${AE_PODMAN_ENDPOINT_PREFER_DIRECT:-$bench_podman_endpoint_prefer_direct_default}}"'
        in text
    )
    assert 'AE_PODMAN_ENDPOINT_PREFER_DIRECT="$bench_podman_endpoint_prefer_direct" \\' in text
    assert 'export AE_PODMAN_ENDPOINT_PREFER_DIRECT="${bench_podman_endpoint_prefer_direct}"' in text
