from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

IDENTITY_VERSION = 1
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")


class NodeIdentityError(ValueError):
    """Raised when a project-scoped node identity is not canonical."""


class NodeIdentityConflictError(RuntimeError):
    """Raised when a live node incarnation owns the requested identity."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class NodeIdentityView:
    """Public, non-secret identity fields exposed by node inventory APIs."""

    node_id: str
    identity_version: int
    identity_mode: str
    generation: int
    site_id: str | None
    local_node_id: str
    heartbeat_seen_at: datetime | None
    lease_expires_at: datetime | None


def validate_node_id(value: str, *, field: str = "node_id") -> str:
    """Return an exact canonical ID or fail without normalizing the input."""

    if not isinstance(value, str):
        raise NodeIdentityError(f"{field} must be a string")
    if not value:
        raise NodeIdentityError(f"{field} is required")
    if value != value.strip():
        raise NodeIdentityError(f"{field} must not contain surrounding whitespace")
    if not value.isascii() or not _NODE_ID_RE.fullmatch(value):
        raise NodeIdentityError(
            f"{field} must be 1..253 ASCII characters with alphanumeric endpoints "
            "and only '.', '_', or '-' internally"
        )
    return value


def split_scoped_node_id(node_id: str) -> tuple[str | None, str]:
    """Split a canonical project-scoped ID into optional site and local parts."""

    value = validate_node_id(node_id)
    if "--" not in value:
        return None, value
    site, local = value.split("--", 1)
    if "--" in local:
        raise NodeIdentityError("node_id must contain at most one site scope separator")
    return validate_node_id(site, field="site_id"), validate_node_id(local, field="local_node_id")


def scoped_node_id(site_id: str | None, node_id: str) -> str:
    site = validate_node_id(site_id, field="site_id") if site_id else ""
    node = validate_node_id(node_id)
    if not site:
        return node
    prefix = f"{site}--"
    if node.startswith(prefix):
        split_scoped_node_id(node)
        return node
    existing_site, _local = split_scoped_node_id(node)
    if existing_site is not None:
        raise NodeIdentityError(
            f"node_id is already scoped to {existing_site!r}, not requested site {site!r}"
        )
    return f"{prefix}{node}"


def identity_view(
    node_id: str,
    *,
    generation: int = 0,
    heartbeat_seen_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> NodeIdentityView:
    """Build the additive inventory contract without exposing claimant secrets."""

    explicit = int(generation) > 0 and lease_expires_at is not None
    if explicit:
        site_id, local_node_id = split_scoped_node_id(node_id)
    else:
        try:
            site_id, local_node_id = split_scoped_node_id(node_id)
        except NodeIdentityError:
            site_id, local_node_id = None, node_id
    return NodeIdentityView(
        node_id=node_id,
        identity_version=IDENTITY_VERSION if explicit else 0,
        identity_mode="explicit" if explicit else "legacy",
        generation=max(0, int(generation)),
        site_id=site_id,
        local_node_id=local_node_id,
        heartbeat_seen_at=heartbeat_seen_at,
        lease_expires_at=lease_expires_at,
    )


def node_lease_timing(value: object = "40") -> tuple[int, int]:
    """Validate the server-selected heartbeat TTL and derive its renew point."""

    raw_value = "40" if value is None or value == "" else value
    if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int)):
        raise NodeIdentityError("node heartbeat TTL must be a positive integer")
    try:
        ttl_seconds = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise NodeIdentityError("node heartbeat TTL must be a positive integer") from exc
    if ttl_seconds <= 1 or ttl_seconds > 86400:
        raise NodeIdentityError("node heartbeat TTL must be in the range 2..86400")
    ttl_ms = ttl_seconds * 1000
    return ttl_ms, min(max(1000, ttl_ms // 3), ttl_ms - 1)
