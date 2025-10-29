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
      footer.site-footer {{ margin-top: 3rem; border-top: 1px solid var(--border); }}
      footer.site-footer .inner {{ display: flex; align-items: center; gap: .75rem; padding: 14px 0; opacity: .85; }}
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
      <a href="ingress.html">Ingress</a>
      <a href="api-auth.html">API Auth</a>
      <a href="concepts.html">Concepts</a>
      <a href="benchmarks.html">Benchmarks</a>
      <a href="{api_base}/swagger" target="_blank" rel="noopener">Swagger</a>
      <a href="{api_base}/redoc" target="_blank" rel="noopener">ReDoc</a>
      <a href="{api_base}/dashboard" target="_blank" rel="noopener">Dashboard</a>
    </nav>
    <div class="container">
    {body}
    </div>
    <footer class="site-footer">
      <div class="container inner">
        <span>k1s Documentation</span>
        <span class="spacer"></span>
        <span>{footer_text}</span>
      </div>
    </footer>
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
                out.append("<pre><code>")
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
                    return f"{v/1024/1024:.1f}"
                def fmt_kib(val: str) -> str:
                    try:
                        v = int(val or 0)
                    except Exception:
                        v = 0
                    return f"{v/1024:.1f}"
                parts: list[str] = [
                    "<hr/>",
                    "<h2>Latest Benchmarks (Auto)</h2>",
                    "<p>Summarized from <code>combined/combined.csv</code> at build time.</p>",
                    "<table border=1 cellpadding=6 cellspacing=0>",
                    "<tr><th>Label</th><th>Mode</th><th>Timestamp</th><th>Ctrl‑Plane PSS (MiB)</th><th>System Cgroups (MiB)</th></tr>",
                ]
                for r in tail:
                    cp_mib = fmt_kib(r.get("control_plane_pss_kb", "0"))
                    sys_mib = fmt_mib(r.get("system_mem_bytes", "0"))
                    parts.append(
                        f"<tr><td>{html.escape(r.get('label',''))}</td><td>{html.escape(r.get('mode',''))}</td>"
                        f"<td>{html.escape(r.get('timestamp',''))}</td><td style='text-align:right'>{cp_mib}</td>"
                        f"<td style='text-align:right'>{sys_mib}</td></tr>"
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
                        ("per_pod_overhead.png", "Per‑Pod Overhead (MiB)")
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
    title = md_path.stem.replace("-", " ").title()
    out_path.write_text(
        TEMPLATE.format(title=title, body=html_body, api_base=API_BASE, footer_text=f"Built {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        , encoding="utf-8"
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mapping = {
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
    }
    # index
    index = f"""
<h1>k1s Documentation</h1>
<ul>
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
  <li><a href="{API_BASE}/dashboard" target="_blank" rel="noopener">Live Demo Dashboard</a></li>
</ul>
"""
    (OUT / "index.html").write_text(
        TEMPLATE.format(title="k1s Docs", body=index, api_base=API_BASE, footer_text=f"Built {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        , encoding="utf-8"
    )

    for src_name, out_name in mapping.items():
        build_one(SRC / src_name, OUT / out_name)


if __name__ == "__main__":
    main()
