from __future__ import annotations

# ruff: noqa: S603
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.integration._profile_smoke import (
    TlsProbe,
    apply_manifest,
    cleanup_dev_state,
    compose_available,
    find_free_port,
    isolated_test_env,
    port_in_use,
    wait_profile_api_token,
    select_runtime,
    start_make_target,
    stop_target,
    wait_file,
    wait_http_ok,
    wait_status_ready,
    wait_tls_probe,
    write_http_smoke_manifest,
)

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProfileCase:
    target: str
    fixed_ports: tuple[int, ...] = ()
    needs_compose: bool = False
    local_docs: bool = False
    apply_echo: bool = False
    expected_apps: tuple[str, ...] = ()
    expect_apishim_env: bool = False
    tls_probes: tuple[TlsProbe, ...] = ()
    extra_env: dict[str, str] = field(default_factory=dict)


CASES = (
    ProfileCase(
        target="demo",
        needs_compose=True,
    ),
    ProfileCase(
        target="labs-up",
        fixed_ports=(2379,),
        needs_compose=True,
        expect_apishim_env=True,
    ),
    ProfileCase(
        target="labs-aio-up",
        fixed_ports=(2379,),
        needs_compose=True,
        expect_apishim_env=True,
    ),
    ProfileCase(target="dev-min", apply_echo=True),
    ProfileCase(
        target="dev-etcd",
        fixed_ports=(2379,),
        needs_compose=True,
        apply_echo=True,
    ),
    ProfileCase(
        target="k1s-core",
        fixed_ports=(2379, 4222, 8222),
        needs_compose=True,
        local_docs=True,
        apply_echo=True,
        extra_env={"CORE_DOCS": "1", "CORE_CADDY": "0"},
    ),
)


def _env_enabled() -> bool:
    return os.getenv("AE_PROFILE_SMOKE", "0") == "1"


def _compose_state_dirs(base_dir: Path) -> dict[str, str]:
    compose_state_dir = base_dir / "compose-state"
    etcd_dir = compose_state_dir / "etcd"
    nats_hub_dir = compose_state_dir / "nats-hub"
    postgres_dir = compose_state_dir / "postgres"
    for directory in (compose_state_dir, etcd_dir, nats_hub_dir, postgres_dir):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    return {
        "AE_DEV_ETCD_DATA_DIR": str(etcd_dir),
        "AE_DEV_NATS_HUB_DATA_DIR": str(nats_hub_dir),
        "AE_DEV_POSTGRES_DATA_DIR": str(postgres_dir),
    }


@pytest.mark.integration
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.target)
def test_profile_entrypoints(case: ProfileCase, tmp_path: Path) -> None:
    if not _env_enabled():
        pytest.skip("set AE_PROFILE_SMOKE=1 to enable live profile smoke tests")

    runtime = select_runtime()
    if runtime is None:
        pytest.skip("no working podman/docker runtime found for live profile smoke tests")
    if case.needs_compose and not compose_available(runtime):
        pytest.skip(f"{runtime} compose provider not available for {case.target}")

    cleanup_dev_state()
    busy_ports = [port for port in case.fixed_ports if port_in_use(port)]
    if busy_ports:
        pytest.skip(f"required fixed ports busy for {case.target}: {busy_ports}")

    profile_dir = tmp_path / case.target
    specs_dir = profile_dir / "specs"
    metrics_port = find_free_port()
    apishim_port = find_free_port()
    docs_port = find_free_port()
    caddy_http_port = find_free_port()
    caddy_https_port = find_free_port()
    postgres_port = find_free_port()
    app_port = find_free_port()
    smoke_app_name = f"{case.target.replace('-', '-')}-smoke"
    smoke_manifest = write_http_smoke_manifest(
        tmp_path / f"{case.target}-smoke.yaml",
        app_name=smoke_app_name,
        port=app_port,
    )

    env = isolated_test_env()
    env.update(
        {
            "AE_DEV_LOCAL": "0",
            "AE_API_MUTATIONS": "1",
            "AE_RUNTIME_BACKEND": runtime,
            "AE_APISHIM_MODE": "host",
            "AE_APISHIM_STARTUP_TIMEOUT": "45",
            "PYTHONUNBUFFERED": "1",
            "PROFILE_DIR": str(profile_dir),
            "SPECS_DIR": str(specs_dir),
            "METRICS_PORT": str(metrics_port),
            "APISHIM_PORT": str(apishim_port),
            "AE_DOCS_PORT": str(docs_port),
            "CADDY_HTTP_PORT": str(caddy_http_port),
            "CADDY_HTTPS_PORT": str(caddy_https_port),
            "POSTGRES_PORT": str(postgres_port),
            "AE_ETCD_PREFIX": f"k1s/tests/{case.target}-{uuid.uuid4().hex[:8]}",
        }
    )
    if case.needs_compose:
        env.update(_compose_state_dirs(tmp_path / case.target))
    if 2379 in case.fixed_ports:
        env["AE_ETCD_MAINTENANCE_ENABLE"] = "0"
    env.update(case.extra_env)

    log_path = tmp_path / f"{case.target}.log"
    running = start_make_target(case.target, env, log_path)
    try:
        api_token = wait_profile_api_token(profile_dir, running=running, timeout_s=120.0)
        admin_token = wait_profile_api_token(
            profile_dir,
            running=running,
            timeout_s=120.0,
            prefer_admin=True,
        )
        wait_http_ok(
            f"http://127.0.0.1:{metrics_port}/status",
            running=running,
            bearer_token=api_token,
            timeout_s=240.0,
        )
        if case.expect_apishim_env:
            wait_file(profile_dir / "apishim.env", running=running, timeout_s=120.0)
        if case.local_docs:
            wait_http_ok(
                f"http://127.0.0.1:{docs_port}/",
                running=running,
                text_fragment="k1s docs",
                timeout_s=180.0,
            )
        for probe in case.tls_probes:
            wait_tls_probe(
                probe,
                https_port=caddy_https_port,
                running=running,
                timeout_s=240.0,
            )
        if case.apply_echo:
            apply_manifest(
                smoke_manifest,
                server_base=f"http://127.0.0.1:{metrics_port}",
                bearer_token=admin_token,
                env=env,
            )
            wait_status_ready(
                metrics_port,
                smoke_app_name,
                running=running,
                bearer_token=api_token,
                timeout_s=240.0,
            )
        for app_name in case.expected_apps:
            wait_status_ready(
                metrics_port,
                app_name,
                running=running,
                bearer_token=api_token,
                timeout_s=240.0,
            )
    finally:
        stop_target(running)
        cleanup_dev_state()
