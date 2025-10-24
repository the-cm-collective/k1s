"""Simple logging setup helpers."""

from __future__ import annotations

import logging
import os
from typing import Literal


Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def configure_logging(level: Level | None = None) -> None:
    """Configure root logging with a concise timestamped formatter.

    Honors AE_LOG_LEVEL if no level is provided.
    """

    env_level = (os.getenv("AE_LOG_LEVEL") or "").upper()
    use_level = level or (env_level if env_level else "INFO")
    numeric = getattr(logging, use_level, logging.INFO)

    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

