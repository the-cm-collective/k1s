"""Test configuration ensuring the project src directory is importable and tidy."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _container_bins() -> list[str]:
    """Return available container CLIs, preferring Docker then Podman."""
    bins: list[str] = []
    for cand in ("docker", "podman"):
        if shutil.which(cand):
            bins.append(cand)
    return bins


def _cleanup_service_proxies() -> None:
    """Best-effort removal of exited ae-svc-* proxy containers after tests."""
    for bin_name in _container_bins():
        try:
            ps = subprocess.run(
                [bin_name, "ps", "-aq", "--filter", "name=ae-svc-"],
                check=False,
                capture_output=True,
                text=True,
            )
            ids = ps.stdout.split()
            if not ids:
                continue
            subprocess.run(
                [bin_name, "rm", "-f", *ids],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            # Never fail tests due to cleanup
            continue


@pytest.fixture(scope="session", autouse=True)
def cleanup_service_proxies():
    """Ensure ae-svc-* proxy containers do not linger across test runs."""
    _cleanup_service_proxies()
    yield
    _cleanup_service_proxies()
