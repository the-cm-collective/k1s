from datetime import UTC, datetime

import pytest

from ae.controller.node_identity import (
    IDENTITY_VERSION,
    NodeIdentityError,
    identity_view,
    node_lease_timing,
    scoped_node_id,
    split_scoped_node_id,
    validate_node_id,
)


@pytest.mark.parametrize(
    "value",
    ["node-1", "core_a.1", "sea-edge-02--edge-1", "A", "a" * 253],
)
def test_validate_node_id_accepts_exact_path_safe_ids(value: str) -> None:
    assert validate_node_id(value) == value


@pytest.mark.parametrize(
    "value",
    ["", " node", "node ", "node/name", "node\\name", "-node", "node-", "nåde", "a" * 254],
)
def test_validate_node_id_rejects_noncanonical_ids(value: str) -> None:
    with pytest.raises(NodeIdentityError):
        validate_node_id(value)


def test_scoped_node_id_is_idempotent_and_rejects_foreign_scope() -> None:
    assert scoped_node_id("site-a", "node-1") == "site-a--node-1"
    assert scoped_node_id("site-a", "site-a--node-1") == "site-a--node-1"
    assert split_scoped_node_id("site-a--node-1") == ("site-a", "node-1")
    with pytest.raises(NodeIdentityError, match="already scoped"):
        scoped_node_id("site-b", "site-a--node-1")


def test_identity_view_never_exposes_claimant_secrets() -> None:
    seen = datetime(2026, 1, 1, tzinfo=UTC)
    expires = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)

    view = identity_view(
        "site-a--node-1",
        generation=3,
        heartbeat_seen_at=seen,
        lease_expires_at=expires,
    )

    assert view.identity_version == IDENTITY_VERSION
    assert view.identity_mode == "explicit"
    assert view.site_id == "site-a"
    assert view.local_node_id == "node-1"
    assert view.generation == 3
    assert not hasattr(view, "session_id")
    assert not hasattr(view, "lease_id")


def test_identity_view_keeps_noncanonical_legacy_records_readable() -> None:
    view = identity_view(
        "legacy/node",
        generation=0,
        heartbeat_seen_at=None,
        lease_expires_at=None,
    )

    assert view.identity_version == 0
    assert view.identity_mode == "legacy"
    assert view.site_id is None
    assert view.local_node_id == "legacy/node"


def test_node_lease_timing_is_server_selected_and_bounded() -> None:
    assert node_lease_timing("40") == (40000, 13333)
    with pytest.raises(NodeIdentityError):
        node_lease_timing("1")
    with pytest.raises(NodeIdentityError):
        node_lease_timing("invalid")
