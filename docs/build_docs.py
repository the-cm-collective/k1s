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
from datetime import datetime

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


def render_template(
    *, title: str, body: str, api_base: str, extra_head: str, footer_text: str
) -> str:
    """Render TEMPLATE safely without str.format interfering with braces.

    Uses simple token replacement on unique markers unlikely to collide.
    """
    t = (
        TEMPLATE.replace("{title}", "{__TITLE__}")
        .replace("{body}", "{__BODY__}")
        .replace("{api_base}", "{__API_BASE__}")
        .replace("{extra_head}", "{__EXTRA__}")
        .replace("{footer_text}", "{__FOOT__}")
    )
    t = t.replace("{__TITLE__}", title)
    t = t.replace("{__BODY__}", body)
    t = t.replace("{__API_BASE__}", api_base)
    t = t.replace("{__EXTRA__}", extra_head)
    t = t.replace("{__FOOT__}", footer_text)
    return t


TEMPLATE = """<!doctype html>
<html data-theme="dark">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>{title}</title>
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs/%3E%3Crect width='64' height='64' rx='12' fill='%23096ad9'/%3E%3Cpath d='M16 45h32M16 19h32M16 32h32' stroke='white' stroke-width='6' stroke-linecap='round'/%3E%3C/svg%3E"/>
    <style>
      /* Base layout */
      html, body { height: 100%; }
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; line-height: 1.55; min-height: 100vh; display: flex; flex-direction: column; }
      code, pre { background: #f6f8fa; }
      pre { padding: 12px; overflow-x: auto; }
      h1, h2, h3 { margin-top: 1.5em; }
      nav a { margin-right: 1rem; }
      .container { max-width: 920px; }
      /* Make main content take remaining height so footer sits at bottom */
      .container { flex: 1 0 auto; }
    </style>
    <style>
      :root {
        --bg: #0b0f15;
        --fg: #e6edf3;
        --muted: #161b22;
        --link: #79c0ff;
        --code-bg: #0f1623;
        --border: #263040;
      }
      html[data-theme="light"] {
        --bg: #ffffff;
        --fg: #0b0f15;
        --muted: #f6f8fa;
        --link: #0969da;
        --code-bg: #f6f8fa;
        --border: #e5e7eb;
      }
      body { background: var(--bg); color: var(--fg); }
      a { color: var(--link); }
      code, pre { background: var(--code-bg); border: 1px solid var(--border); }
      nav { display: flex; align-items: center; gap: .75rem; margin-bottom: 1.25rem; }
      nav a { margin-right: 1rem; }
      .spacer { flex: 1 1 auto; }
      button#theme-toggle { background: var(--muted); color: var(--fg); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; cursor: pointer; }
      /* Improve Mermaid readability in dark mode by inverting SVG colors */
      html[data-theme="dark"] .mermaid svg { filter: invert(1) hue-rotate(180deg) contrast(1.05) saturate(1.1); }
      html[data-theme="dark"] .mermaid { background: var(--bg); }
      footer.site-footer { margin-top: 3rem; border-top: 1px solid var(--border); flex: 0 0 auto; }
      footer.site-footer .inner { display: flex; align-items: center; gap: .75rem; padding: 14px 0; opacity: .85; }
    </style>
    <script>
      (function() {
        const key = 'k1s-theme';
        const saved = localStorage.getItem(key);
        const initial = saved || 'dark';
        document.documentElement.setAttribute('data-theme', initial);
        function setLabel(btn) {
          var cur = document.documentElement.getAttribute('data-theme') || 'dark';
          btn.textContent = (cur === 'dark') ? 'Light Mode' : 'Dark Mode';
        }
        function ensureButton() {
          var nav = document.querySelector('nav');
          if (!nav) return;
          var btn = document.getElementById('theme-toggle');
          if (!btn) {
            var spacer = document.createElement('span');
            spacer.className = 'spacer';
            btn = document.createElement('button');
            btn.id = 'theme-toggle';
            btn.addEventListener('click', function() {
              var cur = document.documentElement.getAttribute('data-theme') || 'dark';
              var next = (cur === 'dark') ? 'light' : 'dark';
              document.documentElement.setAttribute('data-theme', next);
              localStorage.setItem(key, next);
              setLabel(btn);
            });
            nav.appendChild(spacer);
            nav.appendChild(btn);
          }
          setLabel(btn);
        }
        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', ensureButton);
        } else {
          ensureButton();
        }
      })();
    </script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <script>mermaid.initialize({ startOnLoad: true });</script>
    <script>window.DOCS_API_BASE='{api_base}';</script>
    {extra_head}
  </head>
  <body>
    <nav>
      <a href="index.html">Home</a>
      <a href="start-here.html">Start Here</a>
      <a href="overview.html">Overview</a>
      <a href="architecture.html">Architecture</a>
      <a href="http-api.html">HTTP API</a>
      <a href="ingress.html">Ingress</a>
      <a href="api-auth.html">API Auth</a>
      <a href="concepts.html">Concepts</a>
      <a href="benchmarks.html">Benchmarks</a>
      <a href="/swagger" target="_blank" rel="noopener">Swagger</a>
      <a href="/redoc" target="_blank" rel="noopener">ReDoc</a>
      <a href="/dashboard" target="_blank" rel="noopener">Dashboard</a>
      <a href="playground.html">Playground</a>
    </nav>
    <div class="container">
    {body}
    </div>
    <footer class="site-footer">
      <div class="container inner">
        <span>k1s Documentation</span>
        <span class="spacer"></span>
        <button id="api-mode-toggle">API Mode</button>
        <span id="api-mode-label" style="opacity:.8;margin-left:.5rem"></span>
        <span>{footer_text}</span>
      </div>
    </footer>
    <script>
      (function() {
        var btn = document.getElementById('api-mode-toggle');
        if (!btn) return;
        function label() {
          var mode = localStorage.getItem('docsApiMode') || 'proxy';
          btn.textContent = (mode === 'direct') ? 'API Mode: Direct' : 'API Mode: Proxy';
          var lab = document.getElementById('api-mode-label');
          if (lab) {
            if (mode === 'direct') {
              var base = (window.DOCS_API_BASE||'').trim() || '(unset)';
              lab.textContent = ' [' + base + ']';
            } else {
              lab.textContent = ' (proxy)';
            }
          }
        }
        btn.addEventListener('click', function() {
          var cur = localStorage.getItem('docsApiMode') || 'proxy';
          var next = (cur === 'direct') ? 'proxy' : 'direct';
          localStorage.setItem('docsApiMode', next);
          label();
          if (location.pathname.endsWith('playground.html')) location.reload();
        });
        label();
      })();
    </script>
  </body>
</html>
"""


def format_inline(text: str, *, allow_raw_html: bool = False) -> str:
    """Render inline markdown constructs.

    - When ``allow_raw_html`` is False (default), escape all HTML first.
    - When True, keep raw HTML tags intact but still escape contents of
      code spans and link URLs.
    """
    if not allow_raw_html:
        text = html.escape(text)

        def repl_code(m: re.Match[str]) -> str:
            return f"<code>{m.group(1)}</code>"
    else:
        # Preserve tags; only escape code span contents.
        def repl_code(m: re.Match[str]) -> str:
            return f"<code>{html.escape(m.group(1))}</code>"

    text = re.sub(r"`([^`]+)`", repl_code, text)
    # links [text](url) — escape URL attribute; keep link text as-is
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    return text


def md_to_html(md: str, *, allow_raw_html: bool = False) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    in_list = False
    li_buf: list[str] | None = None

    def flush_paragraph(buf: list[str]):
        if not buf:
            return
        text = " ".join(buf)
        # If raw HTML allowed and paragraph looks like a block tag, emit as-is
        if allow_raw_html and text.lstrip().startswith("<"):
            out.append(text)
        else:
            rendered = format_inline(text, allow_raw_html=allow_raw_html)
            out.append(f"<p>{rendered}</p>")
        buf.clear()

    def flush_li():
        nonlocal li_buf
        if li_buf is None:
            return
        content = "\n".join(li_buf)
        rendered = format_inline(content, allow_raw_html=allow_raw_html)
        out.append(f"<li>{rendered}</li>")
        li_buf = None

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
                out.append("<pre><code>")
            in_code = True
            continue

        # horizontal rule: lines with only ---
        if re.fullmatch(r"\s*-{3,}\s*", line):
            flush_paragraph(para_buf)
            if li_buf is not None:
                flush_li()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr/>")
            continue

        if not line.strip():
            flush_paragraph(para_buf)
            if li_buf is not None:
                flush_li()
            if in_list:
                out.append("</ul>")
                in_list = False
            continue

        # headings
        if line.startswith("###### "):
            flush_paragraph(para_buf)
            if li_buf is not None:
                flush_li()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h6>{format_inline(line[7:])}</h6>")
            continue
        if line.startswith("##### "):
            flush_paragraph(para_buf)
            if li_buf is not None:
                flush_li()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h5>{format_inline(line[6:])}</h5>")
            continue
        if line.startswith("#### "):
            flush_paragraph(para_buf)
            if li_buf is not None:
                flush_li()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h4>{format_inline(line[5:])}</h4>")
            continue
        if line.startswith("### "):
            flush_paragraph(para_buf)
            if li_buf is not None:
                flush_li()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{format_inline(line[4:])}</h3>")
            continue
        if line.startswith("## "):
            flush_paragraph(para_buf)
            if li_buf is not None:
                flush_li()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{format_inline(line[3:])}</h2>")
            continue
        if line.startswith("# "):
            flush_paragraph(para_buf)
            if li_buf is not None:
                flush_li()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1>{format_inline(line[2:])}</h1>")
            continue

        # lists (supports continued lines indented under a list item)
        if line.lstrip().startswith("- "):
            flush_paragraph(para_buf)
            if not in_list:
                out.append("<ul>")
                in_list = True
            # flush previous list item if open
            if li_buf is not None:
                flush_li()
            li_buf = [line.strip()[2:]]
            continue

        # list item continuation: treat indented lines as part of current <li>
        if in_list and li_buf is not None and (line.startswith("  ") or line.startswith("\t")):
            li_buf.append(line.lstrip())
            continue

        # paragraph accumulation
        # normal paragraph text
        para_buf.append(line)

    flush_paragraph(para_buf)
    if li_buf is not None:
        flush_li()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def build_one(md_path: Path, out_path: Path) -> None:
    allow_raw = md_path.name == "playground.md"
    html_body = md_to_html(md_path.read_text(encoding="utf-8"), allow_raw_html=allow_raw)
    # Inject K8s compliance status if building parity/compliance pages and a report exists
    try:
        if md_path.name in {"K8S_PARITY.md", "k8s-compliance.md"}:
            status_path = OUT / "k8s_status.json"
            if status_path.exists():
                import json

                data = json.loads(status_path.read_text(encoding="utf-8"))
                score = data.get("overall_score", 0)
                grade = data.get("grade", "n/a")
                samples = int(data.get("samples_count", 0))
                html_body += "\n" + "\n".join(
                    [
                        "<hr/>",
                        "<h2>K8s Compliance Status</h2>",
                        f"<p><strong>Score:</strong> {score}/100 &nbsp; <strong>Grade:</strong> {grade} &nbsp; <strong>Samples:</strong> {samples}</p>",
                        "<details><summary>Per-sample details</summary>",
                        "<ul>",
                    ]
                )
                for r in data.get("results", []):
                    name = Path(r.get("sample", "")).name
                    v_ok = "ok" if r.get("validate", {}).get("ok") else "fail"
                    kc = r.get("kubeconform", {})
                    kc_str = "skip" if not kc.get("ran") else ("ok" if kc.get("ok") else "fail")
                    dr = r.get("server_dry_run", {})
                    dr_str = "skip" if not dr.get("ran") else ("ok" if dr.get("ok") else "fail")
                    pe = int(r.get("policy_strict", {}).get("errors", 0))
                    html_body += f"<li>{name}: score={r.get('score')} validate={v_ok} kubeconform={kc_str} dry-run={dr_str} policyErrors={pe}</li>"
                html_body += "</ul></details>"
    except Exception:
        # Non-fatal: keep page renderable if injection fails
        pass

    # Inject latest memory benchmark summary into the k1s memory testing page
    try:
        if md_path.name == "testing-memory-k1s.md":
            # Look for outputs at repo root: ./combined and ./charts
            repo_root = ROOT.parent
            combined_csv = repo_root / "combined" / "combined.csv"
            charts_dir = repo_root / "charts"
            if combined_csv.exists():
                import csv

                rows: list[dict[str, str]] = []
                with combined_csv.open("r", encoding="utf-8", errors="ignore") as fh:
                    for r in csv.DictReader(fh):
                        rows.append(r)
                # Sort by timestamp (YYYYMMDD-HHMMSS) and keep the last N entries
                try:
                    rows.sort(key=lambda r: str(r.get("timestamp", "")))
                except Exception:
                    pass
                tail = rows[-8:] if len(rows) > 8 else rows

                def fmt_mib(val: str) -> str:
                    try:
                        v = int(val or 0)
                    except Exception:
                        v = 0
                    # control_plane_pss_kb comes in KiB; others in bytes
                    return f"{v / 1024 / 1024:.1f}"

                def fmt_kib(val: str) -> str:
                    try:
                        v = int(val or 0)
                    except Exception:
                        v = 0
                    return f"{v / 1024:.1f}"

                parts: list[str] = [
                    "<hr/>",
                    "<h2>Latest Benchmarks (Auto)</h2>",
                    "<p>Summarized from <code>combined/combined.csv</code> at build time.</p>",
                    "<table border=1 cellpadding=6 cellspacing=0>",
                    (
                        "<tr>"
                        "<th>Label</th><th>Mode</th><th>Timestamp</th>"
                        "<th>Ctrl‑Plane PSS (MiB)</th>"
                        "<th>App Cgroups (MiB)</th>"
                        "<th>Infra Cgroups (MiB)</th>"
                        "<th>Host System (MiB)</th>"
                        "<th>MemAvail Δ (MiB)</th>"
                        "</tr>"
                    ),
                ]
                for r in tail:
                    cp_mib = fmt_kib(r.get("control_plane_pss_kb", "0"))
                    app_mib = fmt_mib(r.get("app_mem_bytes", "0"))
                    infra_mib = fmt_mib(r.get("system_mem_bytes", "0"))
                    # Prefer host services only; fall back to container system bytes for older rows
                    sys_host = r.get("host_system_cgroups_bytes")
                    host_mib = fmt_mib(
                        sys_host if sys_host is not None else r.get("system_mem_bytes", "0")
                    )
                    mdelta_mib = fmt_mib(r.get("mem_available_delta_bytes", "0"))
                    parts.append(
                        f"<tr><td>{html.escape(r.get('label', ''))}</td><td>{html.escape(r.get('mode', ''))}</td>"
                        f"<td>{html.escape(r.get('timestamp', ''))}</td><td style='text-align:right'>{cp_mib}</td>"
                        f"<td style='text-align:right'>{app_mib}</td>"
                        f"<td style='text-align:right'>{infra_mib}</td>"
                        f"<td style='text-align:right'>{host_mib}</td>"
                        f"<td style='text-align:right'>{mdelta_mib}</td></tr>"
                    )
                parts.append("</table>")
                # Inline charts below the table
                if charts_dir.exists():
                    import shutil

                    charts_out = OUT / "charts"
                    charts_out.mkdir(parents=True, exist_ok=True)
                    chart_map = [
                        ("control_plane_pss.png", "Control Plane PSS (MiB)"),
                        ("system_cgroups.png", "System Cgroups (MiB)"),
                        ("per_pod_overhead.png", "Per‑Pod Overhead (MiB)"),
                    ]
                    inline_blocks: list[str] = []
                    for fname, title_txt in chart_map:
                        src = charts_dir / fname
                        if src.exists():
                            shutil.copy2(src, charts_out / fname)
                            inline_blocks.append(
                                f"<h3>{html.escape(title_txt)}</h3>"
                                f"<img src='charts/{fname}' alt='{html.escape(title_txt)}' "
                                f"style='max-width:100%;height:auto;border:1px solid var(--border);margin:8px 0'/>"
                            )
                    if inline_blocks:
                        parts.append("<h2>Charts</h2>" + "".join(inline_blocks))
                html_body += "\n" + "\n".join(parts)
    except Exception:
        # Non-fatal: keep page renderable if injection fails
        pass
    extra_head = ""
    if md_path.name == "playground.md":
        ver = str(int(datetime.now().timestamp()))
        extra_head = "\n".join(
            [
                f'<link rel="stylesheet" href="static/labs.css?v={ver}"/>',
                f"<script>window.DOCS_API_BASE='{html.escape(API_BASE)}';</script>",
                # Optional: inject a demo Labs token so the playground can prefill
                (
                    f"<script>window.DOCS_LABS_TOKEN='{html.escape(os.getenv('DOCS_LABS_TOKEN', ''))}';</script>"
                ),
                '<script src="https://unpkg.com/htmx.org@1.9.12" crossorigin="anonymous"></script>',
                '<script src="https://unpkg.com/htmx.org@1.9.12/dist/ext/sse.js"></script>',
                f'<script defer src="static/labs.js?v={ver}"></script>',
            ]
        )
        # Removed: no extra Echo YAML dump at the end of the page
    title = md_path.stem.replace("-", " ").title()
    out_path.write_text(
        render_template(
            title=title,
            body=html_body,
            api_base=API_BASE,
            extra_head=extra_head,
            footer_text=f"Built {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ),
        encoding="utf-8",
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Copy static assets if present
    try:
        static_src = SRC / "static"
        if static_src.exists():
            import shutil

            static_out = OUT / "static"
            static_out.mkdir(parents=True, exist_ok=True)
            for p in static_src.iterdir():
                if p.is_file():
                    shutil.copy2(p, static_out / p.name)
    except Exception:
        pass
    mapping = {
        "start-here.md": "start-here.html",
        "overview.md": "overview.html",
        "architecture.md": "architecture.html",
        "http-api.md": "http-api.html",
        "ingress.md": "ingress.html",
        "api-auth.md": "api-auth.html",
        "concepts.md": "concepts.html",
        "benchmarks.md": "benchmarks.html",
        "testing-memory-k1s.md": "testing-memory-k1s.html",
        "benchmark-k3s.md": "benchmark-k3s.html",
        "configs-secrets.md": "configs-secrets.html",
        "demo-modes.md": "demo-modes.html",
        "rollouts.md": "rollouts.html",
        "storage.md": "storage.html",
        "observability.md": "observability.html",
        "examples.md": "examples.html",
        "scheduling.md": "scheduling.html",
        "e2e.md": "e2e.html",
        "K8S_PARITY.md": "k8s-parity.html",
        "k8s-compliance.md": "k8s-compliance.html",
        "playground.md": "playground.html",
    }
    # index
    index = f"""
<h1>k1s Documentation</h1>
<ul>
  <li><a href="start-here.html">Start Here (Onboarding)</a></li>
  <li><a href="overview.html">Overview</a></li>
  <li><a href="architecture.html">Architecture</a></li>
  <li><a href="http-api.html">HTTP API</a></li>
  <li><a href="ingress.html">Ingress</a></li>
  <li><a href="api-auth.html">API Auth</a></li>
  <li><a href="concepts.html">Concepts</a></li>
  <li><a href="configs-secrets.html">Configs &amp; Secrets</a></li>
  <li><a href="demo-modes.html">Demo Modes</a></li>
  <li><a href="rollouts.html">Rollouts</a></li>
  <li><a href="storage.html">Storage</a></li>
  <li><a href="observability.html">Observability</a></li>
  <li><a href="benchmarks.html">Benchmarks</a></li>
  <li><a href="examples.html">Examples</a></li>
  <li><a href="scheduling.html">Scheduling</a></li>
  <li><a href="e2e.html">End-to-End Guide</a></li>
  <li><a href="k8s-parity.html">K8s Parity</a></li>
  <li><a href="k8s-compliance.html">K8s Compliance Status</a></li>
  <li><a href="playground.html">Interactive Lab Playground</a></li>
  <li><a href="/dashboard" target="_blank" rel="noopener">Live Demo Dashboard</a></li>
</ul>
"""
    (OUT / "index.html").write_text(
        render_template(
            title="k1s Docs",
            body=index,
            api_base=API_BASE,
            extra_head="",
            footer_text=f"Built {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ),
        encoding="utf-8",
    )

    for src_name, out_name in mapping.items():
        build_one(SRC / src_name, OUT / out_name)

    # Copy curated example YAMLs to /examples for playground preview
    try:
        examples_src = ROOT.parent / "specs" / "examples"
        examples_out = OUT / "examples"
        examples_out.mkdir(parents=True, exist_ok=True)
        if examples_src.exists():
            import shutil

            for p in examples_src.glob("*.y*ml"):
                try:
                    shutil.copy2(p, examples_out / p.name)
                except Exception:
                    pass
    except Exception:
        pass


if __name__ == "__main__":
    main()
