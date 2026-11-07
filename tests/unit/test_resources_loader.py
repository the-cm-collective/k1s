from __future__ import annotations

import pytest

from ae.resources import loader as resource_loader


def test_load_text_reads_docs_template():
    html = resource_loader.load_text("observability", "docs.html")
    assert "<title>k1s API Docs</title>" in html


def test_render_text_replaces_placeholders():
    html = resource_loader.render_text(
        "observability",
        "dashboard.html",
        LABS_TOKEN="lab",  # noqa: S106
        DASHBOARD_TOKEN="dash",  # noqa: S106
        APISHIM_BASE="base",
    )
    assert "__LABS_TOKEN__" not in html
    assert "__DASHBOARD_TOKEN__" not in html
    assert "__APISHIM_BASE__" not in html
    assert "lab" in html
    assert "dash" in html
    assert "base" in html


def test_load_sql_template():
    sql = resource_loader.load_text("sql", "controller", "create_app_status.sql")
    assert "CREATE TABLE IF NOT EXISTS app_status" in sql


def test_load_binary_reads_the_canonical_profile_resource() -> None:
    payload = resource_loader.load_binary("profiles", "profile-catalog-v1.json")

    assert payload.startswith(b"{\n")
    assert payload.endswith(b"\n")
    assert b'"kind": "ProfileCatalog"' in payload


@pytest.mark.parametrize(
    "parts",
    [
        (),
        ("",),
        (".",),
        ("..",),
        ("/absolute",),
        ("profiles/catalog.json",),
        ("profiles\\catalog.json",),
        ("profiles", "../catalog.json"),
        ("profiles", "catalog\x00.json"),
    ],
)
def test_resource_paths_fail_closed_before_traversal(parts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="resource path|unsafe resource"):
        resource_loader.load_binary(*parts)


def test_missing_resource_has_no_checkout_fallback() -> None:
    with pytest.raises(FileNotFoundError):
        resource_loader.load_binary("profiles", "missing-profile-catalog.json")
