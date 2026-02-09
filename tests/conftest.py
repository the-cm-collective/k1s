"""Pytest defaults for local developer environments.

Clear state backend env vars unless explicitly preserved, so unit tests
don't accidentally point at a live etcd instance from shell env state.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_state_env(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("AE_TEST_PRESERVE_ENV") == "1":
        return
    for key in ("AE_STATE_BACKEND", "AE_ETCD_ENDPOINTS", "AE_ETCD_PREFIX", "AE_PROFILE"):
        monkeypatch.delenv(key, raising=False)
