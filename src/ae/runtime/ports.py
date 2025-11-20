"""Helpers for allocating host ports for local runtimes."""

from __future__ import annotations

from contextlib import closing
import socket
from typing import Iterable, Optional, Set, Tuple

_PORT_MIN = 1
_PORT_MAX = 65535
_DEFAULT_SEARCH_SPAN = 200


def _port_candidates(preferred: int, span: int) -> Iterable[int]:
    """Yield preferred port followed by +/- offsets up to `span`."""
    yield preferred
    for delta in range(1, span + 1):
        for candidate in (preferred + delta, preferred - delta):
            if _PORT_MIN <= candidate <= _PORT_MAX:
                yield candidate


def _port_is_free(port: int) -> bool:
    """Return True if the port can be bound on the host."""
    families = []
    try:
        families.append((socket.AF_INET, ("0.0.0.0", port)))
    except OSError:
        pass
    try:
        families.append((socket.AF_INET6, ("::", port)))
    except OSError:
        pass
    # Fall back to AF_INET only when IPv6 is unavailable
    if not families:
        families.append((socket.AF_INET, ("0.0.0.0", port)))
    for family, addr in families:
        try:
            with closing(socket.socket(family, socket.SOCK_STREAM)) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(addr)
        except OSError:
            return False
    return True


def choose_host_port(
    preferred: Optional[int],
    *,
    reserved: Optional[Set[int]] = None,
    blocked: Optional[Set[int]] = None,
    search_span: int = _DEFAULT_SEARCH_SPAN,
) -> Tuple[Optional[int], bool]:
    """Pick an available host port, preferring `preferred` when possible.

    Returns (port, used_preferred). If no port could be reserved, returns (None, False).
    `reserved` tracks ports picked during in-process planning so we don't assign
    duplicates before containers actually bind to them.
    """

    if preferred is None:
        return None, False

    reserved_ports = reserved if reserved is not None else set()
    blocked_ports = blocked if blocked is not None else set()

    for candidate in _port_candidates(int(preferred), search_span):
        if candidate in reserved_ports or candidate in blocked_ports:
            continue
        if _port_is_free(candidate):
            reserved_ports.add(candidate)
            return candidate, candidate == preferred

    return None, False


def is_port_free(port: int) -> bool:
    """Expose the low-level check for unit tests."""
    return _port_is_free(int(port))
