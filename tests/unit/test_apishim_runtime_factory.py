"""API shim runtime factory backend selection tests."""

from ae.apishim import adapter, server
from ae.runtime import ContainerdRuntime, CRIRuntime


def test_apishim_runtime_factory_cri_backend(monkeypatch):
    monkeypatch.delenv("AE_APISHIM_RUNTIME", raising=False)
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "cri")

    assert isinstance(adapter._runtime_from_env(), CRIRuntime)
    assert isinstance(server._runtime_from_env(), CRIRuntime)


def test_apishim_runtime_factory_containerd_backend(monkeypatch):
    monkeypatch.delenv("AE_APISHIM_RUNTIME", raising=False)
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "containerd")
    monkeypatch.setenv("AE_NERDCTL_BIN", "/usr/bin/nerdctl")

    assert isinstance(adapter._runtime_from_env(), ContainerdRuntime)
    assert isinstance(server._runtime_from_env(), ContainerdRuntime)
