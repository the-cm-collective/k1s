from __future__ import annotations

# ruff: noqa: S603
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HREF_RE = re.compile(r"""(?:href|src)=["']([^"'#]+(?:#[^"']*)?)["']""")
BASE_RE = re.compile(r"""<base\s+href=["']([^"']+)["']""", re.IGNORECASE)


def _normalize_ref(base_dir: Path, raw_ref: str) -> Path | None:
    ref = raw_ref.split("#", 1)[0].split("?", 1)[0].strip()
    if not ref:
        return None
    if ref.startswith(("http://", "https://", "mailto:", "javascript:", "data:", "/")):
        return None
    candidate = (base_dir / ref).resolve()
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate


@pytest.mark.integration
def test_docs_export_builds_expected_pages_and_resolves_relative_links(tmp_path: Path) -> None:
    out_dir = tmp_path / "site"
    wiki_dir = tmp_path / "wiki"
    env = {
        **os.environ,
        "DOCS_OUT_DIR": str(out_dir),
        "DOCS_WIKI_OUT_DIR": str(wiki_dir),
        "DOCS_API_BASE": "https://api.home.arpa:8443",
        "DOCS_DASHBOARD_URL": "https://dash.home.arpa:8443/dashboard",
        "DOCS_NON_INTERACTIVE": "1",
    }
    build = subprocess.run(
        [sys.executable, "docs/build_docs.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr or build.stdout

    for expected in (
        "index.html",
        "start-here.html",
        "overview.html",
        "examples.html",
        "runtime-profiles.html",
        "runbook.html",
        "roadmap-status.html",
    ):
        assert (out_dir / expected).exists(), expected

    html_files = sorted(out_dir.rglob("*.html"))
    assert html_files, "docs export produced no html files"

    failures: list[str] = []
    for html_path in html_files:
        text = html_path.read_text(encoding="utf-8", errors="ignore")
        base_match = BASE_RE.search(text)
        if base_match:
            base_dir = (html_path.parent / base_match.group(1)).resolve()
        else:
            base_dir = html_path.parent.resolve()
        text_for_refs = BASE_RE.sub("", text)
        for raw_ref in HREF_RE.findall(text_for_refs):
            target = _normalize_ref(base_dir, raw_ref)
            if target is None:
                continue
            try:
                target.relative_to(out_dir.resolve())
            except ValueError:
                failures.append(
                    f"{html_path.relative_to(out_dir)} -> escapes output dir: {raw_ref}"
                )
                continue
            if not target.exists():
                failures.append(f"{html_path.relative_to(out_dir)} -> missing: {raw_ref}")

    assert not failures, "\n".join(failures[:50])

    wiki = subprocess.run(
        [sys.executable, "docs/export_wiki.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert wiki.returncode == 0, wiki.stderr or wiki.stdout
    assert (wiki_dir / "Home.md").exists()
    assert (wiki_dir / "_Sidebar.md").exists()
    assert not (wiki_dir / "playground.md").exists()

    markdown = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(wiki_dir.glob("*.md"))
    )
    assert not re.search(r"\]\(/(?:swagger|redoc|openapi|dashboard|playground)", markdown)
