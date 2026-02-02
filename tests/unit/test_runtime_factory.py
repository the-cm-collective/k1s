"""Runtime factory backend selection tests."""

import pytest

from ae.cli.__main__ import runtime_factory
from ae.runtime import CRIRuntime


@pytest.mark.parametrize("backend", ["cri", "containerd"])
def test_runtime_factory_cri_backend(monkeypatch, backend):
    monkeypatch.setenv("AE_RUNTIME_BACKEND", backend)
    runtime = runtime_factory()
    assert isinstance(runtime, CRIRuntime)
