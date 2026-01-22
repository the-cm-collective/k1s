#!/usr/bin/env python3
# ruff: noqa
"""Export a curated set of docs/ markdown into a Codeberg-compatible wiki layout."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import unicodedata

from doc_map import DOCS_MAPPING, INTERACTIVE_SOURCES

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "wiki"
OUT = Path(os.getenv("DOCS_WIKI_OUT_DIR", str(DEFAULT_OUT)))

CATEGORY_TITLES = {
    "getting-started": "Getting Started",
    "guides": "Guides",
    "reference": "Reference",
    "benchmarks": "Benchmarks",
    "concepts-in-practice": "Concepts in Practice",
}

CATEGORY_ORDER = [
    "getting-started",
    "guides",
    "reference",
    "benchmarks",
    "concepts-in-practice",
]

TITLE_OVERRIDES = {
    "start-here": "Start Here",
    "playground": "Interactive Lab Playground",
}

HERO_IMAGE = "static/k1s-logo-horizontal.svg"


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _include_interactive() -> bool:
    if _truthy_env("DOCS_WIKI_INCLUDE_INTERACTIVE"):
        return True
    if _truthy_env("DOCS_WIKI_NON_INTERACTIVE"):
        return False
    if _truthy_env("DOCS_NON_INTERACTIVE") or _truthy_env("DOCS_EXPORT_NON_INTERACTIVE"):
        return False
    return True


def _docs_mapping(include_interactive: bool) -> list[tuple[str, str]]:
    mapping = list(DOCS_MAPPING.items())
    if not include_interactive:
        mapping = [(src, out) for src, out in mapping if src not in INTERACTIVE_SOURCES]
    return mapping


def _extract_title(md_text: str, fallback_slug: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", md_text, re.I | re.S)
    if m:
        raw = re.sub(r"<[^>]+>", "", m.group(1))
        title = " ".join(raw.split())
        if title:
            return title
    in_fence = False
    fence = ""
    for raw_line in md_text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            token = line.split()[0]
            if not in_fence:
                in_fence = True
                fence = token
            elif token == fence:
                in_fence = False
                fence = ""
            continue
        if in_fence or not line:
            continue
        m = re.match(r"^(#+)\s+(.*)$", line)
        if m:
            return m.group(2).strip()
    for raw_line in md_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        if line.startswith("<"):
            continue
        if line.startswith(("-", "*", "+")):
            continue
        return line
    return fallback_slug.replace("-", " ").title()


def _sanitize_wiki_text(text: str) -> str:
    replacements = {
        "\u00a0": " ",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "--",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u2192": "->",
        "\u2212": "-",
        "\u2248": "~=",
        "\u0394": "Delta",
        "\u26a0": "WARNING",
        "\u2705": "[x]",
        "\ufe0f": "",
        "\U0001f6a7": "[WIP]",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    cleaned: list[str] = []
    for ch in text:
        if ord(ch) < 128:
            cleaned.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        ascii_only = "".join(c for c in decomposed if ord(c) < 128)
        if ascii_only:
            cleaned.append(ascii_only)
    return "".join(cleaned)


def _strip_start_here_links(md_text: str) -> str:
    return re.sub(
        r'<div class="hero-links hero-links--local">.*?</div>',
        "",
        md_text,
        flags=re.S,
    )


def _strip_hero_block(md_text: str) -> str:
    match = re.search(r'<div\s+class="hero"[^>]*>', md_text, re.I)
    if not match:
        return md_text
    start = match.start()
    depth = 1
    tag_re = re.compile(r"</div>|<div\b", re.I)
    for tag_match in tag_re.finditer(md_text, match.end()):
        token = tag_match.group(0).lower()
        if token.startswith("</div"):
            depth -= 1
            if depth == 0:
                end = tag_match.end()
                trimmed = md_text[:start] + md_text[end:]
                return trimmed.lstrip("\n")
        else:
            depth += 1
    return md_text


def _hero_block() -> str:
    return "\n".join(
        [
            '<div align="center">',
            f'  <img src="{HERO_IMAGE}" alt="k1s logo" width="240" />',
            "</div>",
            "",
            "",
        ]
    )


def _prepend_hero_block(md_text: str) -> str:
    hero = _hero_block()
    return hero + md_text.lstrip("\n")


def _render_concepts_index(slug_to_title: dict[str, str]) -> str:
    sections = [
        (
            "Quick Links",
            [
                ("Start Here", "start-here"),
                ("Concepts Overview", "concepts"),
                ("Multi-Node Lab", "multinode-lab"),
            ],
        ),
        (
            "Core Control Loop",
            [
                (
                    slug_to_title.get("concepts-in-practice-01-desired-state-reconciliation"),
                    "concepts-in-practice-01-desired-state-reconciliation",
                ),
                (
                    slug_to_title.get("concepts-in-practice-02-declarative-apply"),
                    "concepts-in-practice-02-declarative-apply",
                ),
                (
                    slug_to_title.get("concepts-in-practice-03-scheduling-placement"),
                    "concepts-in-practice-03-scheduling-placement",
                ),
            ],
        ),
        (
            "Runtime & Exposure",
            [
                (
                    slug_to_title.get("concepts-in-practice-04-runtime-adapters"),
                    "concepts-in-practice-04-runtime-adapters",
                ),
                (
                    slug_to_title.get("concepts-in-practice-05-ingress-service-exposure"),
                    "concepts-in-practice-05-ingress-service-exposure",
                ),
            ],
        ),
        (
            "Reliability & Rollouts",
            [
                (
                    slug_to_title.get("concepts-in-practice-06-observability"),
                    "concepts-in-practice-06-observability",
                ),
                (
                    slug_to_title.get("concepts-in-practice-07-health-probes"),
                    "concepts-in-practice-07-health-probes",
                ),
                (
                    slug_to_title.get("concepts-in-practice-08-rollouts-updates"),
                    "concepts-in-practice-08-rollouts-updates",
                ),
            ],
        ),
        (
            "Policy & Operations",
            [
                (
                    slug_to_title.get("concepts-in-practice-09-configuration-secrets"),
                    "concepts-in-practice-09-configuration-secrets",
                ),
                (
                    slug_to_title.get("concepts-in-practice-10-access-policy"),
                    "concepts-in-practice-10-access-policy",
                ),
            ],
        ),
    ]
    lines = [
        "# Concepts in Practice",
        "",
        "Hands-on, chapterized walkthroughs for k1s orchestration concepts, mapped to Kubernetes equivalents.",
        "",
    ]
    for section_title, items in sections:
        lines.append(f"## {section_title}")
        for label, slug in items:
            if not label:
                continue
            lines.append(f"- [{label}]({slug})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _rewrite_dest(dest: str, html_to_slug: dict[str, str]) -> str:
    for html_name, slug in html_to_slug.items():
        if dest == html_name:
            return slug
        if dest.startswith(html_name + "#"):
            return slug + dest[len(html_name) :]
    return dest


def _rewrite_markdown_links(md_text: str, html_to_slug: dict[str, str]) -> str:
    md_link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    def repl(match: re.Match[str]) -> str:
        label = match.group(1)
        dest = match.group(2).strip()
        return f"[{label}]({_rewrite_dest(dest, html_to_slug)})"

    return md_link_re.sub(repl, md_text)


def _rewrite_reference_links(md_text: str, html_to_slug: dict[str, str]) -> str:
    ref_link_re = re.compile(r"^(\[[^\]]+\]:\s*)(\S+)$", re.M)

    def repl(match: re.Match[str]) -> str:
        prefix = match.group(1)
        dest = match.group(2)
        return prefix + _rewrite_dest(dest, html_to_slug)

    return ref_link_re.sub(repl, md_text)


def _rewrite_html_hrefs(md_text: str, html_to_slug: dict[str, str]) -> str:
    href_re = re.compile(r"href=([\"\'])([^\"\']+)\1", re.I)

    def repl(match: re.Match[str]) -> str:
        quote = match.group(1)
        dest = match.group(2)
        return f"href={quote}{_rewrite_dest(dest, html_to_slug)}{quote}"

    return href_re.sub(repl, md_text)


def _rewrite_links(md_text: str, html_to_slug: dict[str, str]) -> str:
    md_text = _rewrite_markdown_links(md_text, html_to_slug)
    md_text = _rewrite_reference_links(md_text, html_to_slug)
    md_text = _rewrite_html_hrefs(md_text, html_to_slug)
    return md_text


def _copy_static_assets(out_dir: Path) -> None:
    static_src = ROOT / "static"
    if not static_src.exists():
        return
    static_out = out_dir / "static"
    if static_out.exists():
        for existing in static_out.iterdir():
            if existing.is_dir():
                shutil.rmtree(existing)
            else:
                existing.unlink()
    static_out.mkdir(parents=True, exist_ok=True)
    for p in static_src.iterdir():
        if p.is_dir():
            shutil.copytree(p, static_out / p.name)
        elif p.is_file():
            shutil.copy2(p, static_out / p.name)


def main() -> None:
    include_interactive = _include_interactive()
    mapping = _docs_mapping(include_interactive)
    html_to_slug = {out: Path(out).stem for _src, out in mapping}
    excluded_slugs = {
        Path(DOCS_MAPPING[src]).stem for src in INTERACTIVE_SOURCES if src in DOCS_MAPPING
    }

    OUT.mkdir(parents=True, exist_ok=True)
    _copy_static_assets(OUT)
    if not include_interactive:
        for slug in excluded_slugs:
            stale = OUT / f"{slug}.md"
            if stale.exists():
                stale.unlink()

    pages: list[dict[str, str]] = []

    for src_rel, out_html in mapping:
        src_path = ROOT / src_rel
        slug = Path(out_html).stem
        md_text = src_path.read_text(encoding="utf-8")
        title = _extract_title(md_text, slug)
        title = TITLE_OVERRIDES.get(slug, title)
        pages.append(
            {
                "slug": slug,
                "title": title,
                "category": src_rel.split("/", 1)[0],
                "md_text": md_text,
            }
        )

    slug_to_title = {page["slug"]: page["title"] for page in pages}

    for page in pages:
        slug = page["slug"]
        md_text = page["md_text"]
        md_text = _strip_hero_block(md_text)
        if slug == "start-here":
            md_text = _strip_start_here_links(md_text)
        if slug == "concepts-in-practice":
            md_text = _render_concepts_index(slug_to_title)
        md_text = _prepend_hero_block(md_text)
        md_text = _rewrite_links(md_text, html_to_slug)
        md_text = _sanitize_wiki_text(md_text)
        title = _sanitize_wiki_text(page["title"])
        out_path = OUT / f"{slug}.md"
        out_path.write_text(md_text, encoding="utf-8")
        page["title"] = title

    pages_by_category: dict[str, list[dict[str, str]]] = {k: [] for k in CATEGORY_ORDER}
    for page in pages:
        cat = page["category"]
        if cat not in pages_by_category:
            pages_by_category.setdefault(cat, []).append(page)
        else:
            pages_by_category[cat].append(page)

    # Home.md
    home_lines: list[str] = [
        "# k1s Documentation",
        "",
        "Guides, labs, and reference for building, operating, and observing k1s clusters.",
        "",
    ]
    for cat in CATEGORY_ORDER:
        items = pages_by_category.get(cat, [])
        if not items:
            continue
        home_lines.append(f"## {CATEGORY_TITLES.get(cat, cat.title())}")
        for page in items:
            home_lines.append(f"- [{page['title']}]({page['slug']})")
        home_lines.append("")
    home_text = _hero_block() + "\n".join(home_lines).rstrip() + "\n"
    (OUT / "Home.md").write_text(home_text, encoding="utf-8")

    # _Sidebar.md
    sidebar_lines: list[str] = ["[[Home]]", ""]
    for cat in CATEGORY_ORDER:
        items = pages_by_category.get(cat, [])
        if not items:
            continue
        sidebar_lines.append(f"## {CATEGORY_TITLES.get(cat, cat.title())}")
        for page in items:
            sidebar_lines.append(f"- [{page['title']}]({page['slug']})")
        sidebar_lines.append("")
    (OUT / "_Sidebar.md").write_text("\n".join(sidebar_lines).rstrip() + "\n", encoding="utf-8")

    # _Footer.md
    footer = f"Generated from docs/ on {datetime.now().strftime('%Y-%m-%d')}."
    (OUT / "_Footer.md").write_text(footer + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
