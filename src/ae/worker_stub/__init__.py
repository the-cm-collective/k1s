"""Stub worker for local NATS work dispatch testing."""

from __future__ import annotations

from typing import Any


def main(*args: Any, **kwargs: Any) -> int:
    from ae.worker_stub.__main__ import main as _main

    return _main(*args, **kwargs)


__all__ = ["main"]
