from __future__ import annotations

import os
from pathlib import Path

import pytest

from ae.kctl.profile_paths import (
    ProfileLayout,
    ProfilePathError,
    ProfileRoots,
    validate_bootstrap_id,
    validate_identifier,
    validate_relative_path,
    validate_run_id,
)


def _roots(tmp_path: Path) -> ProfileRoots:
    return ProfileRoots.from_environment(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        owner_uid=os.geteuid(),
    )


def test_xdg_roots_are_versioned_absolute_and_owner_private(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    roots.ensure()

    assert roots.config == tmp_path / "config/k1s/v1"
    assert roots.state == tmp_path / "state/k1s/v1"
    assert roots.data == tmp_path / "data/k1s/v1"
    assert roots.cache == tmp_path / "cache/k1s/v1"
    for root in (roots.config, roots.state, roots.data, roots.cache):
        assert root.stat().st_mode & 0o777 == 0o700
        assert root.stat().st_uid == os.geteuid()
        assert root.parent.stat().st_mode & 0o777 == 0o700


def test_xdg_fallbacks_are_derived_from_explicit_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    roots = ProfileRoots.from_environment({"HOME": str(home)}, owner_uid=os.geteuid())

    assert roots.config == home / ".config/k1s/v1"
    assert roots.state == home / ".local/state/k1s/v1"
    assert roots.data == home / ".local/share/k1s/v1"
    assert roots.cache == home / ".cache/k1s/v1"


def test_all_explicit_xdg_bases_do_not_require_home(tmp_path: Path) -> None:
    roots = ProfileRoots.from_environment(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        owner_uid=os.geteuid(),
    )

    roots.ensure()
    assert roots.state == tmp_path / "state/k1s/v1"


@pytest.mark.parametrize(
    ("environment", "match"),
    [
        ({}, "HOME is required"),
        ({"HOME": "relative"}, "HOME must be an absolute"),
        ({"HOME": "/var/lib/../lib"}, "HOME must be canonical"),
        (
            {"HOME": "/var/lib", "XDG_STATE_HOME": "relative"},
            "XDG_STATE_HOME must be an absolute",
        ),
    ],
)
def test_xdg_roots_reject_missing_relative_or_noncanonical_inputs(
    environment: dict[str, str], match: str
) -> None:
    with pytest.raises(ProfilePathError, match=match):
        ProfileRoots.from_environment(environment)


def test_existing_symlink_or_broad_app_root_is_rejected(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    roots.state.parent.mkdir(mode=0o700, parents=True)
    roots.state.symlink_to(tmp_path)
    with pytest.raises(ProfilePathError, match="symlink|regular directory"):
        roots.ensure()

    roots.state.unlink()
    roots.state.mkdir(mode=0o755)
    with pytest.raises(ProfilePathError, match="mode 0700"):
        roots.ensure()


def test_profile_layout_resolves_alias_before_constructing_owned_paths(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    run_id = "r1-0123456789abcdef0123456789abcdef"
    layout = ProfileLayout.resolve(roots, "core-edge", run_id=run_id)
    layout.ensure_run()

    assert layout.requested_profile == "core-edge"
    assert layout.canonical_profile == "core"
    assert layout.lock == roots.state / "profiles/core/profile.lock"
    assert layout.current == roots.state / "profiles/core/current.json"
    assert layout.transaction == roots.state / "profiles/core/transaction.json"
    assert layout.run_state == roots.state / f"profiles/core/runs/{run_id}/state.json"
    assert layout.log_path("controller") == (
        roots.state / f"profiles/core/runs/{run_id}/logs/controller.log"
    )
    assert layout.secret_path("join-token") == (
        roots.data / f"profiles/core/runs/{run_id}/secrets/join-token"
    )
    assert layout.run_cache == roots.cache / f"profiles/core/runs/{run_id}"
    for path in (
        layout.run_state.parent,
        layout.run_logs,
        layout.run_data,
        layout.run_secrets,
        layout.run_cache,
    ):
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "value",
    [
        "",
        "UPPER",
        " leading",
        "trailing ",
        ".",
        "..",
        "a/b",
        "a\\b",
        "a\x00b",
        "under_score",
        "dotted.name",
    ],
)
def test_identifiers_fail_closed(value: str) -> None:
    with pytest.raises(ProfilePathError):
        validate_identifier(value, field="test ID")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute",
        "../escape",
        "root/../escape",
        "root/./file",
        "root//file",
        "root\\file",
        "root/UPPER",
        "root/\x00file",
    ],
)
def test_receipt_paths_fail_closed(value: str) -> None:
    with pytest.raises(ProfilePathError):
        validate_relative_path(value, field="receipt path")


def test_receipt_path_allows_canonical_filename_segments() -> None:
    assert validate_relative_path("logs/controller.log", field="log") == "logs/controller.log"


def test_run_id_is_versioned_random_hex_only() -> None:
    assert (
        validate_run_id("r1-0123456789abcdef0123456789abcdef")
        == "r1-0123456789abcdef0123456789abcdef"
    )
    for invalid in (
        "0123456789abcdef0123456789abcdef",
        "r1-ABCDEF0123456789abcdef0123456789",
        "r2-0123456789abcdef0123456789abcdef",
        "r1-short",
    ):
        with pytest.raises(ProfilePathError, match="run ID"):
            validate_run_id(invalid)


def test_bootstrap_id_and_standard_private_path_are_exact(tmp_path: Path) -> None:
    identifier = "b1-0123456789abcdef0123456789abcdef"
    layout = ProfileLayout.resolve(_roots(tmp_path), "node")

    assert validate_bootstrap_id(identifier) == identifier
    assert layout.bootstrap_path(identifier) == (
        tmp_path / f"config/k1s/v1/profiles/node/bootstraps/{identifier}.json"
    )
    layout.ensure_bootstrap_directory()
    assert layout.bootstraps.stat().st_mode & 0o777 == 0o700

    for invalid in ("b1-short", "B1-" + "a" * 32, "b2-" + "a" * 32):
        with pytest.raises(ProfilePathError, match="bootstrap ID"):
            validate_bootstrap_id(invalid)
