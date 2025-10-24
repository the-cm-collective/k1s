#!/usr/bin/env python3
"""Very small Markdown → HTML builder for docs/*.md into docs/site/*.html.

Supported:
- #, ##, ... ###### headings
- paragraphs
- unordered lists (- )
- fenced code blocks ``` [lang] (mermaid → <pre class="mermaid">)
- inline code `code`
- links [text](url)

This is not a full Markdown implementation — just enough for our docs.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
SRC = ROOT
OUT = ROOT / "site"

TEMPLATE = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>{title}</title>
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; line-height: 1.55; }}
      code, pre {{ background: #f6f8fa; }}
      pre {{ padding: 12px; overflow-x: auto; }}
      h1, h2, h3 {{ margin-top: 1.5em; }}
      nav a {{ margin-right: 1rem; }}
      .container {{ max-width: 920px; }}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <script>mermaid.initialize({{ startOnLoad: true }});</script>
  </head>
  <body>
    <nav>
      <a href="index.html">Home</a>
      <a href="overview.html">Overview</a>
      <a href="architecture.html">Architecture</a>
      <a href="http-api.html">HTTP API</a>
      <a href="concepts.html">Concepts</a>
    </nav>
    <div class="container">
    {body}
    </div>
  </body>
</html>
"""


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    in_list = False

    def flush_paragraph(buf: list[str]):
        if not buf:
            return
        text = " ".join(buf)
        text = html.escape(text)
        # inline code
        text = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", text)
        # links
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<a href=\"\2\">\1</a>", text)
        out.append(f"<p>{text}</p>")
        buf.clear()

    para_buf: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if in_code:
            if line.strip().startswith("```"):
                # close
                if code_lang == "mermaid":
                    out.append("</pre>")
                else:
                    out.append("</code></pre>")
                in_code = False
                code_lang = ""
            else:
                if code_lang == "mermaid":
                    out.append(html.escape(line))
                else:
                    out.append(html.escape(line))
            continue

        if line.strip().startswith("```"):
            flush_paragraph(para_buf)
            lang = line.strip()[3:].strip().lower()
            code_lang = lang
            if lang == "mermaid":
                out.append('<pre class="mermaid">')
            else:
                out.append('<pre><code>')
            in_code = True
            continue

        if not line.strip():
            flush_paragraph(para_buf)
            if in_list:
                out.append("</ul>")
                in_list = False
            continue

        # headings
        if line.startswith("###### "):
            flush_paragraph(para_buf)
            out.append(f"<h6>{html.escape(line[7:])}</h6>")
            continue
        if line.startswith("##### "):
            flush_paragraph(para_buf)
            out.append(f"<h5>{html.escape(line[6:])}</h5>")
            continue
        if line.startswith("#### "):
            flush_paragraph(para_buf)
            out.append(f"<h4>{html.escape(line[5:])}</h4>")
            continue
        if line.startswith("### "):
            flush_paragraph(para_buf)
            out.append(f"<h3>{html.escape(line[4:])}</h3>")
            continue
        if line.startswith("## "):
            flush_paragraph(para_buf)
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
            continue
        if line.startswith("# "):
            flush_paragraph(para_buf)
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
            continue

        # lists
        if line.lstrip().startswith("- "):
            flush_paragraph(para_buf)
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = line.strip()[2:]
            item = html.escape(item)
            # inline code/links in list items
            item = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", item)
            item = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<a href=\"\2\">\1</a>", item)
            out.append(f"<li>{item}</li>")
            continue

        # paragraph accumulation
        para_buf.append(line)

    flush_paragraph(para_buf)
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def build_one(md_path: Path, out_path: Path) -> None:
    html_body = md_to_html(md_path.read_text(encoding="utf-8"))
    title = md_path.stem.replace("-", " ").title()
    out_path.write_text(TEMPLATE.format(title=title, body=html_body), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mapping = {
        "overview.md": "overview.html",
        "architecture.md": "architecture.html",
        "http-api.md": "http-api.html",
        "concepts.md": "concepts.html",
    }
    # index
    index = """
<h1>k1s Documentation</h1>
<ul>
  <li><a href="overview.html">Overview</a></li>
  <li><a href="architecture.html">Architecture</a></li>
  <li><a href="http-api.html">HTTP API</a></li>
  <li><a href="concepts.html">Concepts</a></li>
</ul>
"""
    (OUT / "index.html").write_text(TEMPLATE.format(title="k1s Docs", body=index), encoding="utf-8")

    for src_name, out_name in mapping.items():
        build_one(SRC / src_name, OUT / out_name)


if __name__ == "__main__":
    main()

