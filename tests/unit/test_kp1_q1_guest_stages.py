from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from scripts.qualification import kp1_q1_conductor as conductor
from scripts.qualification import kp1_q1_guest_stages as guest


def test_guest_stage_order_matches_the_closed_conductor_contract() -> None:
    assert guest.STAGES == conductor.ACCEPTANCE_STAGES
    assert tuple(guest.STAGE_HANDLERS) == guest.STAGES


@pytest.mark.parametrize(
    "path",
    (
        "",
        "/absolute",
        "../escape",
        "payload/../escape",
        "payload/.env",
        "payload/.env.production",
        "payload/.envrc",
    ),
)
def test_guest_archive_path_rejects_escape_and_protected_env(path: str) -> None:
    with pytest.raises(guest.GuestStageError):
        guest._safe_archive_path(path)


def test_guest_archive_path_accepts_exact_payload_member() -> None:
    assert guest._safe_archive_path("payload/offline/wheels/example.whl") == PurePosixPath(
        "payload/offline/wheels/example.whl"
    )


def test_guest_result_contains_only_metadata_safe_digests() -> None:
    result = guest._result("offline-install", [("installed-artifact", b"private-output")])

    assert result["status"] == "passed"
    assert result["stage"] == "offline-install"
    assert result["checks"] == [
        {
            "name": "installed-artifact",
            "sha256": "sha256:1a9192857a95d66ea839511cfbd3cd03cdeee9e1b94dba43dec63c1aa34acfc2",
            "size": 14,
            "status": "passed",
        }
    ]
