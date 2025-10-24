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

# Decide API base for Swagger/ReDoc links.
def detect_api_base() -> str:
    env = os.getenv("DOCS_API_BASE")
    if env:
        return env.rstrip("/")
    try:
        hosts = Path("/etc/hosts").read_text(encoding="utf-8", errors="ignore")
        if "api.home.arpa" in hosts:
            return "https://api.home.arpa:8443"
    except Exception:
        pass
    return "http://127.0.0.1:9108"

API_BASE = detect_api_base()

TEMPLATE = """<!doctype html>
<html data-theme="dark">
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
    <style>
      :root {{
        --bg: #0b0f15;
        --fg: #e6edf3;
        --muted: #161b22;
        --link: #79c0ff;
        --code-bg: #0f1623;
        --border: #263040;
      }}
      html[data-theme="light"] {{
        --bg: #ffffff;
        --fg: #0b0f15;
        --muted: #f6f8fa;
        --link: #0969da;
        --code-bg: #f6f8fa;
        --border: #e5e7eb;
      }}
      body {{ background: var(--bg); color: var(--fg); }}
      a {{ color: var(--link); }}
      code, pre {{ background: var(--code-bg); border: 1px solid var(--border); }}
      nav {{ display: flex; align-items: center; gap: .75rem; margin-bottom: 1.25rem; }}
      nav a {{ margin-right: 1rem; }}
      .spacer {{ flex: 1 1 auto; }}
      button#theme-toggle {{ background: var(--muted); color: var(--fg); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; cursor: pointer; }}
      /* Improve Mermaid readability in dark mode by inverting SVG colors */
      html[data-theme="dark"] .mermaid svg {{ filter: invert(1) hue-rotate(180deg) contrast(1.05) saturate(1.1); }}
      html[data-theme="dark"] .mermaid {{ background: var(--bg); }}
    </style>
    <script>
      (function() {{
        const key = 'k1s-theme';
        const saved = localStorage.getItem(key);
        const initial = saved || 'dark';
        document.documentElement.setAttribute('data-theme', initial);
        function setLabel(btn) {{
          var cur = document.documentElement.getAttribute('data-theme') || 'dark';
          btn.textContent = (cur === 'dark') ? 'Light Mode' : 'Dark Mode';
        }}
        function ensureButton() {{
          var nav = document.querySelector('nav');
          if (!nav) return;
          var btn = document.getElementById('theme-toggle');
          if (!btn) {{
            var spacer = document.createElement('span');
            spacer.className = 'spacer';
            btn = document.createElement('button');
            btn.id = 'theme-toggle';
            btn.addEventListener('click', function() {{
              var cur = document.documentElement.getAttribute('data-theme') || 'dark';
              var next = (cur === 'dark') ? 'light' : 'dark';
              document.documentElement.setAttribute('data-theme', next);
              localStorage.setItem(key, next);
              setLabel(btn);
            }});
            nav.appendChild(spacer);
            nav.appendChild(btn);
          }}
          setLabel(btn);
        }}
        if (document.readyState === 'loading') {{
          document.addEventListener('DOMContentLoaded', ensureButton);
        }} else {{
          ensureButton();
        }}
      }})();
    </script>
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
      <a href="{api_base}/swagger" target="_blank" rel="noopener">Swagger</a>
      <a href="{api_base}/redoc" target="_blank" rel="noopener">ReDoc</a>
    </nav>
    <div class="container">
    {body}
    </div>
  </body>
</html>
"""


def format_inline(text: str) -> str:
    """Escape HTML, then render inline code and links.
    Code spans are not escaped further to preserve characters like < and >.
    """
    text = html.escape(text)
    # inline code (text already escaped; keep raw contents inside <code>)
    text = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", text)
    # links [text](url) — escape URL attribute
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    return text


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    in_list = False

    def flush_paragraph(buf: list[str]):
        if not buf:
            return
        text = format_inline(" ".join(buf))
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
            out.append(f"<h6>{format_inline(line[7:])}</h6>")
            continue
        if line.startswith("##### "):
            flush_paragraph(para_buf)
            out.append(f"<h5>{format_inline(line[6:])}</h5>")
            continue
        if line.startswith("#### "):
            flush_paragraph(para_buf)
            out.append(f"<h4>{format_inline(line[5:])}</h4>")
            continue
        if line.startswith("### "):
            flush_paragraph(para_buf)
            out.append(f"<h3>{format_inline(line[4:])}</h3>")
            continue
        if line.startswith("## "):
            flush_paragraph(para_buf)
            out.append(f"<h2>{format_inline(line[3:])}</h2>")
            continue
        if line.startswith("# "):
            flush_paragraph(para_buf)
            out.append(f"<h1>{format_inline(line[2:])}</h1>")
            continue

        # lists
        if line.lstrip().startswith("- "):
            flush_paragraph(para_buf)
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = format_inline(line.strip()[2:])
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
    out_path.write_text(TEMPLATE.format(title=title, body=html_body, api_base=API_BASE), encoding="utf-8")


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
    (OUT / "index.html").write_text(TEMPLATE.format(title="k1s Docs", body=index, api_base=API_BASE), encoding="utf-8")

    for src_name, out_name in mapping.items():
        build_one(SRC / src_name, OUT / out_name)


if __name__ == "__main__":
    main()
