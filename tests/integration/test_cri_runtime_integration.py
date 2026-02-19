from __future__ import annotations

import os
import stat
import time

import pytest

from ae.controller.spec import AppManifest, AppSpec, Metadata, app_key_for_manifest
from ae.runtime import CRIRuntime


def _cri_endpoint() -> str:
    return os.getenv("AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock")


def _unix_socket_path(endpoint: str) -> str | None:
    if endpoint.startswith("unix://"):
        path = endpoint[len("unix://") :]
        if not path.startswith("/"):
            path = f"/{path}"
        return path
    return None


def _cri_ready(endpoint: str) -> bool:
    sock = _unix_socket_path(endpoint)
    if not sock:
        return os.getenv("AE_CRI_SMOKE_ALLOW_TCP", "0") == "1"
    if not os.path.exists(sock):
        return False
    try:
        return stat.S_ISSOCK(os.stat(sock).st_mode)
    except Exception:
        return False


def _manifest(name: str, image: str) -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=name),
        spec=AppSpec(
            image=image,
            replicas=1,
        ),
    )


@pytest.mark.integration
def test_cri_runtime_lifecycle():
    if os.getenv("AE_CRI_IT", "0") != "1":
        pytest.skip("set AE_CRI_IT=1 to enable CRI runtime lifecycle test")
    endpoint = _cri_endpoint()
    if not _cri_ready(endpoint):
        pytest.skip("CRI endpoint not ready or accessible")
    image = os.getenv("AE_CRI_IT_IMAGE", "registry.k8s.io/pause:3.9")
    manifest = _manifest("cri-it", image)
    runtime = CRIRuntime(endpoint=endpoint)
    app_name = app_key_for_manifest(manifest)
    revision = 1
    try:
        running = False
        for _ in range(10):
            result = runtime.ensure_app(manifest, revision, keep_old=True)
            if any(st.status == "running" for st in (result.pod_states or [])):
                running = True
                break
            time.sleep(1)
        assert running, "CRI runtime did not reach running state"
    finally:
        runtime.remove_app(app_name)
