"""UTC compatibility constant for Python versions before 3.11."""

# ruff: noqa: UP017

from __future__ import annotations

from datetime import timezone

try:  # Python 3.11+
    from datetime import UTC as UTC
except ImportError:  # pragma: no cover - only hit on Python < 3.11
    UTC = timezone.utc
