"""Runtime factory backend selection tests."""

from ae.cli.__main__ import runtime_factory
from ae.runtime import CRIRuntime, ContainerdRuntime


def test_runtime_factory_cri_backend(monkeypatch):
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "cri")
    runtime = runtime_factory()
    assert isinstance(runtime, CRIRuntime)


def test_runtime_factory_containerd_backend(monkeypatch):
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "containerd")
    monkeypatch.setenv("AE_NERDCTL_BIN", "/usr/bin/nerdctl")
    runtime = runtime_factory()
    assert isinstance(runtime, ContainerdRuntime)
