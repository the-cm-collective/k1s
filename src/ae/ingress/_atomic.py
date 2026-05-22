"""Atomic file writes for generated ingress configuration."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path


def write_text_atomic(
    path: Path, content: str, *, encoding: str = "utf-8", mode: int | None = 0o644
) -> None:
    """Write text by replacing the target after the complete payload is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


__all__ = ["write_text_atomic"]
