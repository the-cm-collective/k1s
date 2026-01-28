import os

import pytest


@pytest.fixture(autouse=True)
def _enable_local_node_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep local-dev semantics in tests by registering a local node."""
    monkeypatch.setenv("AE_REGISTER_LOCAL_NODE", "1")
