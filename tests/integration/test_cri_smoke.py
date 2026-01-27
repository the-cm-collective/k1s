import os
import shutil
import stat
import subprocess

import pytest


def _cri_endpoint() -> str:
    return os.getenv("AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock")


def _unix_socket_path(endpoint: str) -> str | None:
    if endpoint.startswith("unix://"):
        path = endpoint[len("unix://") :]
        if not path.startswith("/"):
            path = f"/{path}"
        return path
    return None


@pytest.mark.integration
def test_cri_smoke_info():
    endpoint = _cri_endpoint()
    crictl = shutil.which(os.getenv("CRICTL_BIN", "crictl"))
    if not crictl:
        pytest.skip("crictl not installed")
    sock = _unix_socket_path(endpoint)
    if sock:
        if not os.path.exists(sock):
            pytest.skip(f"CRI socket not found: {sock}")
        try:
            if not stat.S_ISSOCK(os.stat(sock).st_mode):
                pytest.skip(f"CRI endpoint is not a unix socket: {sock}")
        except Exception:
            pytest.skip(f"CRI endpoint not accessible: {sock}")
    else:
        if os.getenv("AE_CRI_SMOKE_ALLOW_TCP", "0") != "1":
            pytest.skip("Non-unix CRI endpoint; set AE_CRI_SMOKE_ALLOW_TCP=1 to enable")

    proc = subprocess.run(
        [crictl, "--runtime-endpoint", endpoint, "info"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").lower()
        if "permission denied" in err:
            pytest.skip(f"CRI socket not accessible: {proc.stderr or proc.stdout}")
    assert proc.returncode == 0, proc.stderr or proc.stdout


@pytest.mark.integration
def test_cri_smoke_pull():
    if os.getenv("AE_CRI_SMOKE_PULL", "0") != "1":
        pytest.skip("set AE_CRI_SMOKE_PULL=1 to enable image pull")
    endpoint = _cri_endpoint()
    crictl = shutil.which(os.getenv("CRICTL_BIN", "crictl"))
    if not crictl:
        pytest.skip("crictl not installed")
    sock = _unix_socket_path(endpoint)
    if sock:
        if not os.path.exists(sock):
            pytest.skip(f"CRI socket not found: {sock}")
        try:
            if not stat.S_ISSOCK(os.stat(sock).st_mode):
                pytest.skip(f"CRI endpoint is not a unix socket: {sock}")
        except Exception:
            pytest.skip(f"CRI endpoint not accessible: {sock}")
    else:
        if os.getenv("AE_CRI_SMOKE_ALLOW_TCP", "0") != "1":
            pytest.skip("Non-unix CRI endpoint; set AE_CRI_SMOKE_ALLOW_TCP=1 to enable")

    image = os.getenv("AE_CRI_SANDBOX_IMAGE", "registry.k8s.io/pause:3.9")
    proc = subprocess.run(
        [crictl, "--runtime-endpoint", endpoint, "pull", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").lower()
        if "permission denied" in err:
            pytest.skip(f"CRI socket not accessible: {proc.stderr or proc.stdout}")
    assert proc.returncode == 0, proc.stderr or proc.stdout
