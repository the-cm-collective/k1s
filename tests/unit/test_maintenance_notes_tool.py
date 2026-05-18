from __future__ import annotations

from pathlib import Path

from scripts.dev import maintenance_notes


def test_maintenance_scanner_ignores_plain_shim(tmp_path: Path) -> None:
    source = tmp_path / "adapter.py"
    source.write_text(
        "\n".join(
            [
                '"""API shim adapter."""',
                "SHIM_NAME = 'k1s-apishim'",
                "# fallback_port is a local variable, not a maintenance marker",
            ]
        ),
        encoding="utf-8",
    )

    assert maintenance_notes.scan_source_file(source) == []


def test_maintenance_scanner_flags_real_markers(tmp_path: Path) -> None:
    source = tmp_path / "adapter.py"
    source.write_text(
        "\n".join(
            [
                "# TODO: remove when authority converges",
                "# legacy path for old storage records",
                "# fallback to marker-only behavior when registry is missing",
            ]
        ),
        encoding="utf-8",
    )

    markers = maintenance_notes.scan_source_file(source)

    assert [marker.term for marker in markers] == ["TODO", "legacy", "fallback"]


def test_maintenance_section_replacement_and_readme_collapse(tmp_path: Path) -> None:
    root = tmp_path / "src" / "ae"
    package = root / "apishim"
    docs = package / "docs"
    docs.mkdir(parents=True)
    source = package / "adapter.py"
    source.write_text("# TODO: tighten invalid input handling\n", encoding="utf-8")
    doc = docs / "adapter.md"
    doc.write_text("# adapter\n\n## Maintenance Notes\nold\n\n## Tests\nnone\n", encoding="utf-8")
    readme = package / "README.md"
    readme.write_text("# apishim\n\n## Maintenance Notes\nold\n", encoding="utf-8")

    updates = maintenance_notes.planned_updates(root)

    assert "- Line 1: `# TODO: tighten invalid input handling`" in updates[doc]
    assert "`adapter.py`: 1 marker(s)" in updates[readme]
    assert "# TODO: tighten invalid input handling" not in updates[readme]
    assert "## Tests\nnone" in updates[doc]
