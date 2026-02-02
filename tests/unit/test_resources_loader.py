from ae.resources import loader as resource_loader


def test_load_text_reads_docs_template():
    html = resource_loader.load_text("observability", "docs.html")
    assert "<title>k1s API Docs</title>" in html


def test_render_text_replaces_placeholders():
    html = resource_loader.render_text(
        "observability",
        "dashboard.html",
        LABS_TOKEN="lab",  # noqa: S106
        APISHIM_BASE="base",
    )
    assert "__LABS_TOKEN__" not in html
    assert "__APISHIM_BASE__" not in html
    assert "lab" in html
    assert "base" in html


def test_load_sql_template():
    sql = resource_loader.load_text("sql", "controller", "create_app_status.sql")
    assert "CREATE TABLE IF NOT EXISTS app_status" in sql
