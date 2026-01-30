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
        kind="App",
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


@pytest.mark.integration
def test_cri_hostpid_namespace():
    if os.getenv("AE_CRI_IT_HOSTNS", "0") != "1":
        pytest.skip("set AE_CRI_IT_HOSTNS=1 to enable CRI host namespace test")
    endpoint = _cri_endpoint()
    if not _cri_ready(endpoint):
        pytest.skip("CRI endpoint not ready or accessible")
    image = os.getenv("AE_CRI_HOSTNS_IMAGE", "busybox:1.36")
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="cri-hostpid"),
        spec=AppSpec(
            image=image,
            replicas=1,
            host_pid=True,
            command=["sleep"],
            args=["3600"],
        ),
    )
    runtime = CRIRuntime(endpoint=endpoint)
    app_name = app_key_for_manifest(manifest)
    revision = 1
    old_allow = os.getenv("AE_CRI_ALLOW_HOST_NS")
    os.environ["AE_CRI_ALLOW_HOST_NS"] = "1"
    try:
        running = False
        for _ in range(10):
            result = runtime.ensure_app(manifest, revision, keep_old=True)
            if any(st.status == "running" for st in (result.pod_states or [])):
                running = True
                break
            time.sleep(1)
        assert running, "CRI hostPID pod did not reach running state"

        replica_id = f"{app_name}-rev{revision}-0"
        runtime._ensure_clients()
        container_id = runtime._container_id_for_replica(replica_id)
        assert container_id, "CRI container not found for exec"
        pb2 = runtime._pb2()
        req = pb2.ExecSyncRequest(
            container_id=str(container_id), cmd=["cat", "/proc/1/comm"], timeout=5
        )
        resp = runtime._runtime_call("ExecSync", req)
        out = getattr(resp, "stdout", b"").decode("utf-8", "ignore").strip()
        assert out and out != "sleep", f"expected host PID 1, got {out!r}"
    finally:
        runtime.remove_app(app_name)
        if old_allow is None:
            os.environ.pop("AE_CRI_ALLOW_HOST_NS", None)
        else:
            os.environ["AE_CRI_ALLOW_HOST_NS"] = old_allow
