from __future__ import annotations

# ruff: noqa: S603, S607
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.integration._profile_smoke import (
    apply_manifest,
    cleanup_dev_state,
    find_free_port,
    isolated_test_env,
    port_in_use,
    start_make_target,
    stop_target,
    tail_log,
    wait_http_ok,
    wait_profile_api_token,
    wait_status_ready,
    write_http_smoke_manifest,
)

ROOT = Path(__file__).resolve().parents[2]

_STRICT_CRI_ENV_PRESERVE = ("AE_APISHIM_IMAGE", "AE_CRI_ENDPOINT", "CRICTL_BIN")


def _strict_cri_enabled() -> bool:
    return os.getenv("AE_STRICT_CRI_PROFILE_SMOKE", "0") == "1"


def _cri_endpoint() -> str:
    return os.getenv("AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock")


def _sudo_ready() -> bool:
    return subprocess.run(
        ["sudo", "-n", "true"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0


@pytest.mark.integration
def test_k1s_core_cri_profile_smoke(tmp_path: Path) -> None:
    if not _strict_cri_enabled():
        pytest.skip("set AE_STRICT_CRI_PROFILE_SMOKE=1 to enable strict CRI profile smoke")

    crictl = shutil.which(os.getenv("CRICTL_BIN", "crictl"))
    if not crictl:
        pytest.skip("crictl not installed")

    cleanup_dev_state()
    busy_ports = [port for port in (2379, 4222, 8222) if port_in_use(port)]
    if busy_ports:
        pytest.skip(f"required fixed strict-CRI ports busy: {busy_ports}")

    if os.geteuid() != 0 and not _sudo_ready():
        pytest.skip(
            "strict CRI smoke requires root or cached sudo on this host; "
            "rerun with 'sudo -E make strict-cri-smoke' or grant temporary "
            "containerd access via ./scripts/containerd_socket_access.sh --grant"
        )

    preflight = subprocess.run(
        ["./scripts/cri_preflight.sh"],
        cwd=ROOT,
        env={
            **isolated_test_env(preserve=_STRICT_CRI_ENV_PRESERVE),
            "AE_CRI_REQUIRE_RUNTIME_READY": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if preflight.returncode != 0:
        pytest.skip(
            "strict CRI smoke requires a prepared host with runtime-ready "
            f"containerd access: {preflight.stderr or preflight.stdout}"
        )

    profile_dir = tmp_path / "k1s-core-cri"
    specs_dir = profile_dir / "specs"
    metrics_port = find_free_port()
    apishim_port = find_free_port()
    docs_port = find_free_port()
    postgres_port = find_free_port()
    app_port = find_free_port()
    smoke_app_name = "core-cri-smoke"
    smoke_manifest = write_http_smoke_manifest(
        tmp_path / "core-cri-smoke.yaml",
        app_name=smoke_app_name,
        port=app_port,
        strict_cri=True,
    )

    env = isolated_test_env(preserve=_STRICT_CRI_ENV_PRESERVE)
    env.update(
        {
            "AE_DEV_LOCAL": "0",
            "AE_API_MUTATIONS": "1",
            "CORE_DOCS": "1",
            "CORE_CADDY": "0",
            "PROFILE_DIR": str(profile_dir),
            "SPECS_DIR": str(specs_dir),
            "METRICS_PORT": str(metrics_port),
            "APISHIM_PORT": str(apishim_port),
            "AE_DOCS_PORT": str(docs_port),
            "POSTGRES_PORT": str(postgres_port),
            "AE_ETCD_PREFIX": f"k1s/tests/k1s-core-cri-{uuid.uuid4().hex[:8]}",
            "PYTHONUNBUFFERED": "1",
            "AE_CRI_REQUIRE_RUNTIME_READY": "1",
        }
    )

    running = start_make_target("k1s-core-cri", env, tmp_path / "k1s-core-cri.log")
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
            timeout_s=300.0,
        )
        wait_http_ok(
            f"http://127.0.0.1:{docs_port}/",
            running=running,
            text_fragment="k1s docs",
            timeout_s=240.0,
        )

        info = subprocess.run(
            [crictl, "--runtime-endpoint", _cri_endpoint(), "info"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert info.returncode == 0, info.stderr or info.stdout

        try:
            apply_manifest(
                smoke_manifest,
                server_base=f"http://127.0.0.1:{metrics_port}",
                bearer_token=admin_token,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "strict CRI smoke apply failed\n"
                f"{exc.stdout or exc.stderr or exc}\n"
                f"{tail_log(tmp_path / 'k1s-core-cri.log')}"
            ) from exc
        wait_status_ready(
            metrics_port,
            smoke_app_name,
            running=running,
            bearer_token=api_token,
            timeout_s=300.0,
        )
    finally:
        stop_target(running)
        cleanup_dev_state()
