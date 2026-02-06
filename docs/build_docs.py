#!/usr/bin/env python3
# ruff: noqa
"""Very small Markdown → HTML builder for docs/**/*.md into docs/site/*.html.

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

import hashlib
import html
import json
import os
import time
from pathlib import Path
import re
from datetime import datetime

from doc_map import DOCS_MAPPING, INTERACTIVE_SOURCES

ROOT = Path(__file__).resolve().parent
SRC = ROOT
DEFAULT_OUT = ROOT / "site"
OUT = Path(os.getenv("DOCS_OUT_DIR", str(DEFAULT_OUT)))


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


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


def detect_dashboard_url() -> str:
    env = os.getenv("DOCS_DASHBOARD_URL")
    if env:
        return env.strip()
    try:
        hosts = Path("/etc/hosts").read_text(encoding="utf-8", errors="ignore")
        if "dash.home.arpa" in hosts:
            return "https://dash.home.arpa:8443/dashboard"
    except Exception:
        pass
    return "/dashboard"


DASHBOARD_URL = detect_dashboard_url()
EXPORT_NON_INTERACTIVE = _truthy_env("DOCS_NON_INTERACTIVE") or _truthy_env(
    "DOCS_EXPORT_NON_INTERACTIVE"
)
SWAGGER_UI_VENDOR = _truthy_env("DOCS_SWAGGER_VENDOR")
SWAGGER_UI_VERSION = os.getenv("DOCS_SWAGGER_UI_VERSION", "5.11.0").strip()
SWAGGER_UI_CDN_BASE = os.getenv(
    "DOCS_SWAGGER_UI_CDN_BASE",
    f"https://unpkg.com/swagger-ui-dist@{SWAGGER_UI_VERSION}",
).strip()
SWAGGER_UI_DEFAULT_URL = os.getenv(
    "DOCS_SWAGGER_DEFAULT_URL",
    "http://localhost:8080/k1s/swagger/openapi-v3.json",
).strip()
SWAGGER_UI_CONTROLLER_DEFAULT_URL = os.getenv(
    "DOCS_SWAGGER_CONTROLLER_DEFAULT_URL",
    "http://localhost:8080/k1s/openapi.json",
).strip()

RSS_FEED_URL = os.getenv("DOCS_RSS_FEED_URL", "https://codeberg.org/th3_4rchit3ct/k1s.rss").strip()
RSS_FEED_TITLE = "k1s Repo Activity"
SOURCE_REPO_URL = os.getenv(
    "DOCS_SOURCE_REPO_URL", "https://codeberg.org/th3_4rchit3ct/k1s"
).strip()
SOURCE_REPO_LABEL = os.getenv(
    "DOCS_SOURCE_REPO_LABEL", "Upstream Repository (CODEBERG)"
).strip()
COLLAB_REPO_URL = os.getenv(
    "DOCS_COLLAB_REPO_URL", "https://github.com/the-cm-collective/k1s"
).strip()
COLLAB_REPO_LABEL = os.getenv(
    "DOCS_COLLAB_REPO_LABEL", "Issues & PRs / SPONSORS (GitHub)"
).strip()

INTERACTIVE_HREF_TOKENS = (
    "/swagger",
    "/redoc",
    "/dashboard",
    "/openapi/v3",
    "playground.html",
    "/playground",
)

NAV_LINKS = [
    ("Start Here", "start-here.html", False, False),
    ("Overview", "overview.html", False, False),
    ("Demos", "examples.html", False, False),
    ("Architecture", "architecture.html", False, False),
    ("CRI containerd", "cri-containerd.html", False, False),
    ("Multi-Node", "multinode-lab.html", False, False),
    ("HTTP API", "http-api.html", False, False),
    ("API Shim", "apishim-compatibility-matrix.html", False, False),
    ("OpenAPI v3", "/openapi/v3", True, True),
    ("Ingress", "ingress.html", False, False),
    ("API Auth", "api-auth.html", False, False),
    ("Concepts", "concepts.html", False, False),
    ("Benchmarks", "benchmarks.html", False, False),
    ("Swagger", "/swagger", True, True),
    ("ReDoc", "/redoc", True, True),
    ("Dashboard", DASHBOARD_URL, True, True),
    ("Playground", "playground.html", True, False),
    ("Concepts in Practice", "concepts-in-practice.html", False, False),
]

STATIC_SWAGGER_LABEL = "Swagger (Static)"
STATIC_SWAGGER_HREF = "swagger/"
STATIC_SWAGGER_APISHIM_HREF = "swagger/apishim/"
STATIC_SWAGGER_CONTROLLER_LABEL = "Controller API"
STATIC_SWAGGER_APISHIM_LABEL = "API Shim"
STATIC_SWAGGER_CONTROLLER_OUT_PATH = "swagger/index.html"
STATIC_SWAGGER_APISHIM_OUT_PATH = "swagger/apishim/index.html"
STATIC_SWAGGER_CONTROLLER_SPEC = "controller-openapi.json"
STATIC_SWAGGER_APISHIM_SPEC = "openapi-v3.json"


def is_interactive_href(href: str) -> bool:
    href_lower = href.strip().lower()
    return any(token in href_lower for token in INTERACTIVE_HREF_TOKENS)


def render_nav(
    *,
    include_interactive: bool,
    include_static_swagger: bool = False,
    href_prefix: str = "",
) -> str:
    def normalize_href(href: str) -> str:
        if not href_prefix:
            return href
        if href.startswith(("http://", "https://", "/", "#")):
            return href
        return f"{href_prefix}{href}"

    parts = []
    brand_href = normalize_href("index.html")
    brand_logo_src = normalize_href("static/k1s-logo-circle.svg")
    parts.append(
        f'      <a class="nav-brand" href="{brand_href}" aria-label="k1s docs home">'
        f'<img src="{brand_logo_src}" alt="k1s logo" />'
        "<span>k1s docs</span>"
        "</a>"
    )
    injected_static = False
    for label, href, interactive, external in NAV_LINKS:
        if interactive and not include_interactive:
            continue
        attrs = []
        if external:
            attrs.append('target="_blank"')
            attrs.append('rel="noopener"')
        attr_str = " " + " ".join(attrs) if attrs else ""
        href = normalize_href(href)
        parts.append(f'      <a href="{href}"{attr_str}>{label}</a>')
        if include_static_swagger and label == "HTTP API":
            parts.append(
                f'      <a href="{normalize_href(STATIC_SWAGGER_HREF)}">{STATIC_SWAGGER_LABEL}</a>'
            )
            injected_static = True
    if include_static_swagger and not injected_static:
        parts.append(
            f'      <a href="{normalize_href(STATIC_SWAGGER_HREF)}">{STATIC_SWAGGER_LABEL}</a>'
        )
    return "\n".join(parts)


def render_template(
    *,
    title: str,
    body: str,
    api_base: str,
    dashboard_url: str,
    extra_head: str,
    footer_text: str,
    nav_html: str,
    api_mode_widget: str,
    api_mode_script: str,
    base_tag: str = "",
) -> str:
    """Render TEMPLATE safely without str.format interfering with braces.

    Uses simple token replacement on unique markers unlikely to collide.
    """
    t = (
        TEMPLATE.replace("{title}", "{__TITLE__}")
        .replace("{body}", "{__BODY__}")
        .replace("{api_base}", "{__API_BASE__}")
        .replace("{dashboard_url}", "{__DASHBOARD_URL__}")
        .replace("{extra_head}", "{__EXTRA__}")
        .replace("{base_tag}", "{__BASE__}")
        .replace("{footer_text}", "{__FOOT__}")
        .replace("{nav}", "{__NAV__}")
        .replace("{api_mode_widget}", "{__API_MODE_WIDGET__}")
        .replace("{api_mode_script}", "{__API_MODE_SCRIPT__}")
    )
    t = t.replace("{__TITLE__}", title)
    t = t.replace("{__BODY__}", body)
    t = t.replace("{__API_BASE__}", api_base)
    t = t.replace("{__DASHBOARD_URL__}", dashboard_url)
    t = t.replace("{__EXTRA__}", extra_head)
    t = t.replace("{__BASE__}", base_tag)
    t = t.replace("{__FOOT__}", footer_text)
    t = t.replace("{__NAV__}", nav_html)
    t = t.replace("{__API_MODE_WIDGET__}", api_mode_widget)
    t = t.replace("{__API_MODE_SCRIPT__}", api_mode_script)
    return t


BUILD_STAMP_RE = re.compile(r"Built\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}")
BUILD_STAMP_PLACEHOLDER = "Built __DOCS_BUILD_TS__"


def _normalize_build_stamp(text: str) -> str:
    return BUILD_STAMP_RE.sub(BUILD_STAMP_PLACEHOLDER, text)


def _extract_build_stamp(text: str) -> str | None:
    match = BUILD_STAMP_RE.search(text)
    return match.group(0) if match else None


def _current_build_stamp() -> str:
    return f"Built {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"


def _apply_stable_build_stamp(out_path: Path, html_text: str) -> str:
    if out_path.exists():
        try:
            previous = out_path.read_text(encoding="utf-8")
        except Exception:
            previous = ""
        if previous:
            if _normalize_build_stamp(previous) == html_text:
                prev_stamp = _extract_build_stamp(previous)
                if prev_stamp:
                    return html_text.replace(BUILD_STAMP_PLACEHOLDER, prev_stamp)
    return html_text.replace(BUILD_STAMP_PLACEHOLDER, _current_build_stamp())


def _load_ae_version() -> str:
    version_path = ROOT.parent / "src" / "ae" / "__init__.py"
    try:
        text = version_path.read_text(encoding="utf-8")
    except Exception:
        return "0.0.0"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match:
        return match.group(1)
    return "0.0.0"


def build_controller_openapi_doc() -> dict[str, object]:
    # Keep in sync with src/ae/observability/http_api.py:_handle_openapi.
    tokens_configured = bool(
        os.getenv("AE_API_ADMIN_TOKEN")
        or os.getenv("AE_API_SCALER_TOKEN")
        or os.getenv("AE_API_READ_TOKEN")
    )
    version = _load_ae_version()
    doc: dict[str, object] = {
        "openapi": "3.0.0",
        "info": {"title": "k1s Controller API", "version": version},
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
            },
            "schemas": {
                "AppStatus": {
                    "type": "object",
                    "properties": {
                        "app_name": {"type": "string"},
                        "name": {"type": "string"},
                        "namespace": {"type": "string"},
                        "deployment": {"type": "string"},
                        "desired_replicas": {"type": "integer"},
                        "ready_replicas": {"type": "integer"},
                        "live_replicas": {"type": "integer"},
                        "revision": {"type": "integer"},
                        "revision_status": {"type": "string"},
                        "image": {"type": "string"},
                        "ingress_host": {"type": "string", "nullable": True},
                        "ingress_path": {"type": "string", "nullable": True},
                    },
                    "required": [
                        "app_name",
                        "desired_replicas",
                        "ready_replicas",
                        "live_replicas",
                        "revision",
                        "revision_status",
                        "image",
                    ],
                },
                "Event": {
                    "type": "object",
                    "properties": {
                        "app_name": {"type": "string"},
                        "revision": {"type": "integer"},
                        "event_type": {"type": "string"},
                        "message": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                    "required": ["app_name", "revision", "event_type", "message", "created_at"],
                },
            },
        },
        "paths": {
            "/metrics": {
                "get": {
                    "summary": "Prometheus metrics",
                    "responses": {"200": {"description": "Prometheus text"}},
                }
            },
            "/health": {
                "get": {"summary": "Controller health", "responses": {"200": {"description": "OK"}}}
            },
            "/status": {
                "get": {
                    "summary": "List app statuses (paginated)",
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
                        {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                        {"name": "app", "in": "query", "schema": {"type": "string"}},
                        {"name": "wildcard", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/status/{app}": {
                "get": {
                    "summary": "Get a single app status",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "OK"}, "404": {"description": "Not Found"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/manifest/{app}": {
                "get": {
                    "summary": "Get the latest stored manifest for an app",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "OK"}, "404": {"description": "Not Found"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/events/{app}": {
                "get": {
                    "summary": "List app events (paginated)",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}},
                        {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/scale/{app}": {
                "post": {
                    "summary": "Scale an app",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/delete/{app}": {
                "post": {
                    "summary": "Delete an app",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "purge", "in": "query", "schema": {"type": "boolean"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/logs/{app}": {
                "get": {
                    "summary": "Tail application logs",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "container", "in": "query", "schema": {"type": "string"}},
                        {"name": "tail", "in": "query", "schema": {"type": "integer"}},
                        {"name": "since", "in": "query", "schema": {"type": "integer"}},
                        {"name": "follow", "in": "query", "schema": {"type": "boolean"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/plan": {
                "post": {
                    "summary": "Validate a manifest and return plan diagnostics",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/tls/verify": {
                "get": {
                    "summary": "Verify tlsSecretName resolvability under AE_TLS_DIR",
                    "parameters": [
                        {"name": "name", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "root", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/rollout/pause/{app}": {
                "post": {
                    "summary": "Pause rollout for an app",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
            "/rollout/resume/{app}": {
                "post": {
                    "summary": "Resume rollout for an app",
                    "parameters": [
                        {"name": "app", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "OK"}},
                    "security": [{"bearerAuth": []}],
                }
            },
        },
    }
    if tokens_configured:
        doc["security"] = [{"bearerAuth": []}]
    return doc


def _hash_files(paths: list[Path]) -> str | None:
    h = hashlib.sha256()
    found = False
    for path in paths:
        if not path.exists():
            continue
        try:
            h.update(path.read_bytes())
            found = True
        except Exception:
            continue
    if not found:
        return None
    return h.hexdigest()[:10]


def _labs_asset_version() -> str:
    static_root = SRC / "static"
    ver = _hash_files([static_root / "labs.css", static_root / "labs.js"])
    if ver:
        return ver
    return str(int(time.time()))


TEMPLATE = """<!doctype html>
<html data-theme="dark">
  <head>
    <meta charset="utf-8"/>
    {base_tag}
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>{title}</title>
    <link rel="icon" href="static/favicon.ico" sizes="any"/>
    <link rel="icon" type="image/svg+xml" href="static/favicon-32x32.svg"/>
    <link rel="icon" type="image/png" sizes="32x32" href="static/favicon-32x32.png"/>
    <link rel="icon" type="image/svg+xml" href="static/favicon-16x16.svg"/>
    <link rel="icon" type="image/png" sizes="16x16" href="static/favicon-16x16.png"/>
    <link rel="icon" type="image/svg+xml" href="static/favicon-48x48.svg"/>
    <link rel="icon" type="image/png" sizes="48x48" href="static/favicon-48x48.png"/>
    <link rel="apple-touch-icon" sizes="180x180" href="static/icon-180x180.png"/>
    <style>
      :root {
        color-scheme: light dark;
        --k1s-bg: #121212;
        --k1s-surface: #181818;
        --k1s-panel: #2c2c2c;
        --k1s-border: #404040;
        --k1s-border-soft: #4a4a4a;
        --k1s-text: #e5e7eb;
        --k1s-text-muted: #9ca3af;
        --k1s-primary: #2563eb;
        --k1s-primary-soft: #3b82f6;
        --k1s-highlight: #60a5fa;
        --k1s-info: #0284c7;
        --k1s-info-bg: #e0f2fe;
        --k1s-success: #16a34a;
        --k1s-success-bg: #16a34a33;
        --k1s-warn: #f59e0b;
        --k1s-warn-bg: #f59e0b33;
        --k1s-danger: #ef4444;
        --k1s-danger-bg: #ef444433;
        --k1s-card-bg: #2c2c2c;
        --k1s-header-bg: #0a0a0a10;
        --k1s-radius: 8px;
        --k1s-radius-pill: 999px;
        --k1s-gap: 12px;
        --k1s-brand-gold: #fbc02d;
        --k1s-brand-graphite: #404040;
        --k1s-brand-mist: #f1f1f1;
        --k1s-page-bg-image: url('static/dash-assets/page-background-3840x2160.png');
        --k1s-page-overlay: linear-gradient(rgba(7,10,14,0.72), rgba(7,10,14,0.72));
        /* Legacy aliases used by docs/labs styles */
        --bg: var(--k1s-bg);
        --fg: var(--k1s-text);
        --muted: var(--k1s-panel);
        --link: #5a86c9;
        --link-hover: #7aa0e8;
        --code-bg: #1b1b1b;
        --border: var(--k1s-border);
      }
      html[data-theme="light"] {
        --k1s-bg: #f4f5f7;
        --k1s-surface: #f8fafb;
        --k1s-panel: #ffffff;
        --k1s-border: #d4d7dd;
        --k1s-border-soft: #e6e8ec;
        --k1s-text: #0f141c;
        --k1s-text-muted: #4a5565;
        --k1s-card-bg: #ffffff;
        --k1s-header-bg: rgba(255,255,255,0.82);
        --code-bg: #f5f6f8;
        --k1s-page-overlay: linear-gradient(rgba(244,245,247,0.9), rgba(244,245,247,0.9));
        --bg: var(--k1s-bg);
        --fg: var(--k1s-text);
        --muted: var(--k1s-panel);
        --link: #2f59b9;
        --link-hover: #3b63c5;
        --border: var(--k1s-border);
      }
    </style>
    <style>
      /* Base layout aligned to dashboard palette */
      html { height: 100%; }
      *, *::before, *::after { box-sizing: border-box; }
      body {
        font-family: system-ui, -apple-system, "Segoe UI", "Roboto", sans-serif;
        margin: 0;
        padding: 2rem;
        line-height: 1.55;
        box-sizing: border-box;
        min-height: 100vh;
        display: flex;
        flex-direction: column;
        background-color: var(--bg);
        background-image: var(--k1s-page-overlay), var(--k1s-page-bg-image);
        background-size: 100% 100%, auto 100%;
        background-position: center top, center top;
        background-repeat: repeat-y, repeat-y;
        color: var(--fg);
      }
      img { max-width: 100%; height: auto; }
      nav {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: .6rem;
        justify-content: center;
        margin: 0 auto 1.25rem auto;
        width: min(100%, 1320px);
        padding: 10px 16px;
        box-sizing: border-box;
        background: var(--k1s-panel);
        border: 1px solid var(--k1s-border);
        border-radius: 14px;
        box-shadow: 0 8px 30px rgba(0,0,0,0.12);
        position: sticky;
        top: 12px;
        z-index: 10;
        backdrop-filter: blur(6px);
        -webkit-backdrop-filter: blur(6px);
      }
      nav::after {
        content: "";
        position: absolute;
        left: 14px;
        right: 14px;
        bottom: 6px;
        height: 2px;
        border-radius: 999px;
        background: linear-gradient(90deg, transparent, var(--k1s-brand-gold), transparent);
        opacity: 0.5;
        pointer-events: none;
      }
      nav a {
        display: inline-flex;
        align-items: center;
        gap: .25rem;
        padding: 7px 12px;
        border: 1px solid var(--k1s-border);
        border-radius: 10px;
        background: var(--k1s-card-bg);
        color: var(--fg);
        text-decoration: none;
        box-shadow: 0 6px 16px rgba(0,0,0,0.16);
        transition: background .15s ease, border-color .15s ease, transform .12s ease, color .15s ease;
        font-weight: 600;
      }
      nav a:hover {
        background: var(--k1s-surface);
        border-color: var(--k1s-brand-gold);
        color: var(--link-hover);
        transform: translateY(-1px);
      }
      nav .nav-brand {
        gap: 10px;
        padding: 6px 10px;
        border: 1px solid transparent;
        background: transparent;
        box-shadow: none;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        font-size: 11px;
        color: var(--k1s-text-muted);
      }
      nav .nav-brand:hover {
        background: color-mix(in srgb, var(--k1s-card-bg) 60%, transparent);
        border-color: var(--k1s-brand-gold);
        color: var(--fg);
        transform: translateY(0);
      }
      nav .nav-brand img {
        width: 28px;
        height: 28px;
        border-radius: 999px;
        box-shadow: 0 6px 16px rgba(0,0,0,0.2);
      }
      nav a:active { transform: translateY(0); }
      .spacer { flex: 1 1 auto; }
      button#api-mode-toggle {
        background: var(--k1s-card-bg);
        color: var(--fg);
        border: 1px solid var(--k1s-border);
        border-radius: 8px;
        padding: 6px 10px;
        cursor: pointer;
        transition: background .15s ease, border-color .15s ease;
      }
      button#api-mode-toggle:hover {
        background: var(--k1s-surface);
        border-color: var(--k1s-border-soft);
      }
      .theme-fab {
        position: fixed;
        right: 18px;
        bottom: 140px;
        width: 52px;
        height: 52px;
        border-radius: 50%;
        border: 1px solid var(--k1s-border);
        background: var(--k1s-card-bg);
        color: var(--fg);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 12px 35px rgba(0,0,0,0.32);
        cursor: pointer;
        z-index: 20;
        transition: background .15s ease, border-color .15s ease, transform .15s ease, box-shadow .15s ease;
      }
      .theme-fab:hover {
        background: var(--k1s-surface);
        border-color: var(--k1s-border-soft);
        transform: translateY(-1px);
        box-shadow: 0 14px 40px rgba(0,0,0,0.38);
      }
      .theme-fab:active { transform: translateY(0); }
      .theme-fab svg { width: 26px; height: 26px; fill: currentColor; }
      .theme-fab .icon-sun { display: none; }
      html[data-theme="light"] .theme-fab .icon-sun { display: block; }
      html[data-theme="light"] .theme-fab .icon-moon { display: none; }
      html[data-theme="dark"] .theme-fab .icon-moon { display: block; }
      @media (max-height: 740px) {
        .theme-fab { bottom: 32px; }
      }
      code, pre {
        background: var(--code-bg);
        border: 1px solid var(--border);
        color: var(--fg);
        border-radius: 8px;
      }
      code { overflow-wrap: anywhere; }
      pre code { overflow-wrap: normal; }
      table {
        width: 100%;
        border-collapse: collapse;
        margin: 1rem 0;
        background: var(--k1s-panel);
        border: 1px solid var(--k1s-border);
        border-radius: 10px;
        overflow: hidden;
      }
      thead th {
        text-align: left;
        padding: 12px 14px;
        background: var(--k1s-card-bg);
        border-bottom: 1px solid var(--k1s-border);
        color: var(--fg);
        font-weight: 600;
      }
      tbody td {
        padding: 12px 14px;
        border-top: 1px solid var(--k1s-border-soft);
        color: var(--fg);
        vertical-align: top;
      }
      tbody tr:nth-child(even) td {
        background: color-mix(in srgb, var(--k1s-panel) 92%, #000000 8%);
      }
      html[data-theme="light"] tbody tr:nth-child(even) td {
        background: color-mix(in srgb, var(--k1s-panel) 92%, #ffffff 8%);
      }
      pre {
        padding: 12px;
        overflow-x: auto;
        scrollbar-width: none;
        -ms-overflow-style: none;
        position: relative;
      }
      pre::-webkit-scrollbar { width: 0; height: 0; }
      pre.has-copy { padding-top: 32px; }
      .copy-btn {
        position: absolute;
        top: 8px;
        right: 10px;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 9px;
        border-radius: 8px;
        border: 1px solid var(--k1s-border);
        background: var(--k1s-surface);
        color: var(--fg);
        font-size: 12px;
        line-height: 1;
        cursor: pointer;
        box-shadow: 0 8px 22px rgba(0,0,0,0.24);
        opacity: 0;
        transition: opacity .14s ease, transform .12s ease, background .12s ease, border-color .12s ease;
        z-index: 1;
      }
      pre:hover .copy-btn,
      pre:focus-within .copy-btn {
        opacity: .96;
      }
      .copy-btn:hover {
        background: var(--k1s-panel);
        border-color: var(--k1s-border-soft);
      }
      .copy-btn:active { transform: translateY(0); }
      .copy-btn svg { width: 16px; height: 16px; fill: currentColor; }
      .copy-btn.copied { color: var(--k1s-success); border-color: var(--k1s-success); }
      h1 { margin-top: 0; font-size: 28px; letter-spacing: 0.01em; }
      h2 { margin-top: 1.5em; font-size: 20px; }
      h3 { margin-top: 1.1em; font-size: 17px; }
      a { color: var(--link); }
      a:hover { color: var(--link-hover); }
      a:focus-visible { outline: 2px solid var(--k1s-highlight); outline-offset: 2px; border-radius: 4px; }
      .container {
        width: min(100%, 1320px);
        max-width: 1320px;
        margin: 0 auto;
        flex: 1 0 auto;
      }
      .card {
        border: 1px solid var(--k1s-border);
        background: var(--k1s-card-bg);
        border-radius: var(--k1s-radius);
        padding: 10px 12px;
      }
      .hero {
        position: relative;
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 16px;
        padding: 22px;
        border: 1px solid var(--k1s-border);
        border-radius: 18px;
        background:
          radial-gradient(circle at 92% 8%, color-mix(in srgb, var(--k1s-brand-gold) 22%, transparent) 0%, transparent 55%),
          linear-gradient(135deg, color-mix(in srgb, var(--k1s-panel) 86%, #000000 14%) 0%, var(--k1s-panel) 55%, color-mix(in srgb, var(--k1s-brand-gold) 14%, var(--k1s-panel)) 100%);
        overflow: hidden;
        box-shadow: 0 16px 40px rgba(0,0,0,0.18);
      }
      html[data-theme="light"] .hero {
        background:
          radial-gradient(circle at 92% 8%, color-mix(in srgb, var(--k1s-brand-gold) 26%, transparent) 0%, transparent 60%),
          linear-gradient(135deg, #ffffff 0%, #f8f7f2 60%, color-mix(in srgb, var(--k1s-brand-gold) 18%, #ffffff) 100%);
      }
      .hero::after {
        content: "";
        position: absolute;
        right: -24px;
        bottom: -36px;
        width: 220px;
        height: 220px;
        background: url('static/k1s-logo-circle.svg') no-repeat center / contain;
        opacity: 0.12;
        pointer-events: none;
      }
      .hero-brand {
        display: flex;
        flex-direction: column;
        gap: 10px;
        z-index: 1;
      }
      .hero-logo-row {
        display: flex;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
      }
      .hero-logo {
        width: min(320px, 90%);
        height: auto;
        opacity: 0.85;
      }
      .hero-pill {
        padding: 6px 12px;
        border-radius: 999px;
        font-weight: 700;
        font-size: 12px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        background: var(--k1s-brand-gold);
        color: #2b2b2b;
        box-shadow: 0 10px 22px rgba(251, 192, 45, 0.25);
      }
      .hero h1 {
        font-size: 32px;
        margin: 4px 0 0;
      }
      .hero-tagline {
        max-width: 52ch;
        color: var(--k1s-text-muted);
        font-size: 15px;
      }
      .hero-links {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 6px;
      }
      .hero-repo {
        margin-top: 14px;
        padding: 10px 12px;
        display: flex;
        flex-wrap: wrap;
        align-items: flex-start;
        gap: 6px 10px;
        border: 1px solid var(--k1s-border);
        border-radius: 12px;
        background: var(--k1s-panel);
        box-shadow: 0 8px 22px rgba(0,0,0,0.18);
      }
      .hero-repo-label {
        font-size: 0.7rem;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--k1s-text-muted);
      }
      .hero-repo-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
        min-width: 0;
      }
      .hero-repo-item {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .hero-repo-item-label {
        font-size: 0.65rem;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: var(--k1s-text-muted);
      }
      .hero-repo-link {
        color: var(--link);
        font-weight: 600;
        text-decoration: none;
        word-break: break-word;
      }
      .hero-repo-link:hover {
        color: var(--link-hover);
        text-decoration: underline;
      }
      .hero-link {
        text-decoration: none;
        padding: 6px 10px;
        border-radius: 10px;
        border: 1px solid var(--k1s-border-soft);
        background: color-mix(in srgb, var(--k1s-card-bg) 70%, transparent);
        color: var(--fg);
        font-weight: 600;
        font-size: 13px;
      }
      .hero-link--stack {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .hero-link-title {
        font-weight: 700;
        font-size: 13px;
      }
      .hero-link-sub {
        font-weight: 400;
        font-size: 11px;
        line-height: 1.3;
        color: var(--k1s-text-muted);
      }
      .hero-link:hover {
        border-color: var(--k1s-brand-gold);
        color: var(--fg);
      }
      .hero-actions {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 12px;
        align-items: start;
        z-index: 1;
      }
      .hero-card {
        border: 1px solid var(--k1s-border-soft);
        background: color-mix(in srgb, var(--k1s-card-bg) 85%, transparent);
        border-radius: 14px;
        padding: 12px;
        box-shadow: 0 12px 28px rgba(0,0,0,0.18);
      }
      .hero-card h2 {
        margin: 0 0 6px;
        font-size: 16px;
      }
      .hero-card p {
        margin: 0 0 10px;
        color: var(--k1s-text-muted);
        font-size: 13px;
      }
      .hero-card pre {
        margin: 8px 0 0;
        font-size: 12px;
      }
      .hero-index {
        grid-template-columns: minmax(280px, 1.1fr) minmax(320px, 2fr);
      }
      .hero-index .hero-actions {
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      }
      .hero-card--section {
        min-height: 180px;
      }
      .hero-links--dense {
        gap: 8px;
      }
      .hero-links--dense .hero-link {
        font-size: 12px;
        padding: 6px 10px;
      }
      .callout, blockquote {
        border-left: 4px solid var(--k1s-primary-soft);
        background: var(--k1s-card-bg);
        border: 1px solid var(--k1s-border);
        border-radius: 8px;
        padding: 10px 12px;
      }
      .muted, .note { color: var(--k1s-text-muted); }
      /* Shared helper to allow scrolling without visible bars */
      .scrollbar-hide { scrollbar-width: none; -ms-overflow-style: none; }
      .scrollbar-hide::-webkit-scrollbar { width: 0; height: 0; }
      /* Improve Mermaid readability in dark mode by inverting SVG colors */
      html[data-theme="dark"] .mermaid svg { filter: invert(1) hue-rotate(180deg) contrast(1.05) saturate(1.1); }
      html[data-theme="dark"] .mermaid { background: var(--bg); }
      footer.site-footer {
        margin-top: 3rem;
        border-top: 1px solid var(--k1s-border);
        flex: 0 0 auto;
      }
      footer.site-footer .inner {
        display: flex;
        align-items: center;
        gap: .75rem;
        padding: 14px 0;
        opacity: .85;
      }
      @media (hover: none) {
        .copy-btn { opacity: .92; }
        pre.has-copy { padding-top: 36px; }
      }
      @media (max-width: 980px) {
        body { padding: 1.5rem; }
        nav {
          width: 100%;
          padding: 8px 10px;
          gap: .5rem;
          justify-content: flex-start;
        }
        nav a {
          padding: 6px 10px;
          font-size: 12px;
        }
        nav .nav-brand {
          font-size: 10px;
          letter-spacing: 0.1em;
        }
        .hero {
          padding: 18px;
        }
        .hero h1 { font-size: 28px; }
        .hero-tagline { font-size: 14px; }
        .hero-index { grid-template-columns: 1fr; }
        .hero-actions { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
        .hero-card--section { min-height: auto; }
        .theme-fab { right: 12px; bottom: 88px; }
        footer.site-footer .inner { flex-wrap: wrap; }
      }
      @media (max-width: 720px) {
        body { padding: 1.1rem; }
        nav {
          top: 8px;
          flex-wrap: nowrap;
          overflow-x: auto;
          scrollbar-width: none;
        }
        nav::-webkit-scrollbar { width: 0; height: 0; }
        nav a { flex: 0 0 auto; }
        nav::after { left: 8px; right: 8px; }
        h1 { font-size: 24px; }
        h2 { font-size: 18px; }
        h3 { font-size: 15px; }
        .hero { padding: 16px; }
        .hero-logo { width: min(220px, 90%); }
        .hero-pill { font-size: 11px; }
        .hero-links { gap: 8px; }
        .hero-link { font-size: 12px; }
        .hero-actions { grid-template-columns: 1fr; }
        .theme-fab { bottom: 24px; width: 48px; height: 48px; }
        pre { font-size: 13px; }
        table {
          display: block;
          max-width: 100%;
          width: max-content;
          min-width: 100%;
          overflow-x: auto;
          -webkit-overflow-scrolling: touch;
        }
        thead th, tbody td { padding: 10px 12px; }
      }
    </style>
    <script>
      (function() {
        const key = 'k1s-theme';
        const saved = localStorage.getItem(key);
        const initial = saved || 'dark';
        document.documentElement.setAttribute('data-theme', initial);
        const labels = {
          dark: 'Switch to light mode',
          light: 'Switch to dark mode',
        };
        function update(btn) {
          var cur = document.documentElement.getAttribute('data-theme') || 'dark';
          var label = labels[cur] || 'Toggle theme';
          btn.setAttribute('aria-label', label);
          btn.setAttribute('title', label);
        }
        function wire() {
          var btn = document.getElementById('theme-toggle');
          if (!btn) return;
          update(btn);
          btn.addEventListener('click', function() {
            var cur = document.documentElement.getAttribute('data-theme') || 'dark';
            var next = (cur === 'dark') ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem(key, next);
            update(btn);
          });
        }
        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', wire);
        } else {
          wire();
        }
      })();
    </script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <script>mermaid.initialize({ startOnLoad: true });</script>
    <script>window.DOCS_API_BASE='{api_base}';</script>
    <script>window.DOCS_DASHBOARD_URL='{dashboard_url}';</script>
    <script>
      // Attach copy buttons to all code blocks client-side
      (function() {
        function addButtons() {
          var blocks = document.querySelectorAll('pre > code');
          blocks.forEach(function(code) {
            var pre = code.parentElement;
            if (pre.dataset.hasCopy) return;
            pre.dataset.hasCopy = '1';
            pre.classList.add('has-copy');

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'copy-btn';
            btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1Zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2Zm0 16H8V7h11v14Z"/></svg><span>Copy</span>';

            btn.addEventListener('click', function() {
              var text = code.textContent || '';
              var setState = function(ok) {
                btn.classList.toggle('copied', ok);
                btn.lastChild.textContent = ok ? 'Copied!' : 'Copy';
                setTimeout(function() {
                  btn.classList.remove('copied');
                  btn.lastChild.textContent = 'Copy';
                }, 1400);
              };
              if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function() { setState(true); }, function() { setState(false); });
              } else {
                try {
                  var ta = document.createElement('textarea');
                  ta.value = text;
                  ta.style.position = 'fixed';
                  ta.style.opacity = '0';
                  document.body.appendChild(ta);
                  ta.select();
                  document.execCommand('copy');
                  document.body.removeChild(ta);
                  setState(true);
                } catch (e) {
                  setState(false);
                }
              }
            });

            pre.style.position = 'relative';
            pre.insertBefore(btn, code);
          });
        }
        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', addButtons);
        } else {
          addButtons();
        }
      })();
    </script>
    {extra_head}
  </head>
  <body>
    <nav>
{nav}
    </nav>
    <button id="theme-toggle" class="theme-fab" aria-label="Toggle theme" title="Toggle theme">
      <svg class="icon-sun" viewBox="0 -960 960 960" aria-hidden="true" focusable="false">
        <path d="M480-360q50 0 85-35t35-85q0-50-35-85t-85-35q-50 0-85 35t-35 85q0 50 35 85t85 35Zm0 80q-83 0-141.5-58.5T280-480q0-83 58.5-141.5T480-680q83 0 141.5 58.5T680-480q0 83-58.5 141.5T480-280ZM200-440H40v-80h160v80Zm720 0H760v-80h160v80ZM440-760v-160h80v160h-80Zm0 720v-160h80v160h-80ZM256-650l-101-97 57-59 96 100-52 56Zm492 496-97-101 53-55 101 97-57 59Zm-98-550 97-101 59 57-100 96-56-52ZM154-212l101-97 55 53-97 101-59-57Zm326-268Z"/>
      </svg>
      <svg class="icon-moon" viewBox="0 -960 960 960" aria-hidden="true" focusable="false">
        <path d="M480-120q-150 0-255-105T120-480q0-150 105-255t255-105q14 0 27.5 1t26.5 3q-41 29-65.5 75.5T444-660q0 90 63 153t153 63q55 0 101-24.5t75-65.5q2 13 3 26.5t1 27.5q0 150-105 255T480-120Zm0-80q88 0 158-48.5T740-375q-20 5-40 8t-40 3q-123 0-209.5-86.5T364-660q0-20 3-40t8-40q-78 32-126.5 102T200-480q0 116 82 198t198 82Zm-10-270Z"/>
      </svg>
    </button>
    <div class="container">
    {body}
    </div>
    <footer class="site-footer">
      <div class="container inner">
        <span>k1s Documentation</span>
        <span class="spacer"></span>
{api_mode_widget}
        <span>{footer_text}</span>
      </div>
    </footer>
{api_mode_script}
  </body>
</html>
"""


def api_mode_fragments(include: bool) -> tuple[str, str]:
    if not include:
        return ("", "")
    widget = "\n".join(
        [
            '        <button id="api-mode-toggle">API Mode</button>',
            '        <span id="api-mode-label" style="opacity:.8;margin-left:.5rem"></span>',
        ]
    )
    script = "\n".join(
        [
            "    <script>",
            "      (function() {",
            "        var btn = document.getElementById('api-mode-toggle');",
            "        if (!btn) return;",
            "        function label() {",
            "          var mode = localStorage.getItem('docsApiMode') || 'proxy';",
            "          btn.textContent = (mode === 'direct') ? 'API Mode: Direct' : 'API Mode: Proxy';",
            "          var lab = document.getElementById('api-mode-label');",
            "          if (lab) {",
            "            if (mode === 'direct') {",
            "              var base = (window.DOCS_API_BASE||'').trim() || '(unset)';",
            "              lab.textContent = ' [' + base + ']';",
            "            } else {",
            "              lab.textContent = ' (proxy)';",
            "            }",
            "          }",
            "        }",
            "        btn.addEventListener('click', function() {",
            "          var cur = localStorage.getItem('docsApiMode') || 'proxy';",
            "          var next = (cur === 'direct') ? 'proxy' : 'direct';",
            "          localStorage.setItem('docsApiMode', next);",
            "          label();",
            "          if (location.pathname.endsWith('playground.html')) location.reload();",
            "        });",
            "        label();",
            "      })();",
            "    </script>",
            "    <script>",
            "      (function() {",
            "        function hideLinks(match) {",
            "          var links = document.querySelectorAll('a[href]');",
            "          links.forEach(function(a) {",
            "            var href = a.getAttribute('href') || '';",
            "            if (match(href)) { a.style.display = 'none'; }",
            "          });",
            "        }",
            "        function rewriteDashboardLinks(target) {",
            "          if (!target) return;",
            "          var links = document.querySelectorAll('a[href]');",
            "          links.forEach(function(a) {",
            "            var href = a.getAttribute('href') || '';",
            "            if (href.indexOf('/dashboard') === 0) {",
            "              a.setAttribute('href', target);",
            "              if (!a.getAttribute('target')) {",
            "                a.setAttribute('target', '_blank');",
            "                a.setAttribute('rel', 'noopener');",
            "              }",
            "            }",
            "          });",
            "        }",
            "        function disablePlaygroundPage() {",
            "          var path = (location && location.pathname) ? location.pathname : '';",
            "          if (!/playground/i.test(path)) return;",
            "          var container = document.querySelector('.container');",
            "          if (container) container.style.display = 'none';",
            "          var msg = document.createElement('div');",
            "          msg.style.maxWidth = '960px';",
            "          msg.style.margin = '32px auto';",
            "          msg.style.padding = '16px 20px';",
            "          msg.style.border = '1px solid var(--border)';",
            "          msg.style.borderRadius = '12px';",
            "          msg.style.background = 'var(--k1s-panel)';",
            "          msg.style.color = 'var(--k1s-text)';",
            "          msg.innerHTML = '<h2 style=\"margin:0 0 8px\">Playground disabled</h2>' +",
            "            '<p style=\"margin:0;color:var(--k1s-text-muted)\">This environment has disabled the interactive playground. Enable AE_PLAYGROUND=1 on the controller to re-enable it.</p>';",
            "          document.body.insertBefore(msg, document.body.firstChild);",
            "        }",
            "        fetch('/ui/features', { credentials: 'same-origin' })",
            "          .then(function(r) { return r.ok ? r.json() : null; })",
            "          .then(function(f) {",
            "            if (!f) return;",
            "            if (f.dashboard === false) {",
            "              hideLinks(function(h) { return h.indexOf('/dashboard') !== -1; });",
            "            } else {",
            "              var dashUrl = (window.DOCS_DASHBOARD_URL || '').trim();",
            "              rewriteDashboardLinks(dashUrl);",
            "            }",
            "            if (f.playground === false) {",
            "              hideLinks(function(h) { return h.indexOf('playground') !== -1; });",
            "              disablePlaygroundPage();",
            "            }",
            "          })",
            "          .catch(function() {});",
            "      })();",
            "    </script>",
        ]
    )
    return (widget, script)


def format_inline(
    text: str, *, allow_raw_html: bool = False, strip_interactive_links: bool = False
) -> str:
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
    def repl_link(m: re.Match[str]) -> str:
        href = m.group(2)
        if strip_interactive_links and is_interactive_href(href):
            return m.group(1)
        return f'<a href="{html.escape(href, quote=True)}">{m.group(1)}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl_link, text)
    return text


def md_to_html(
    md: str, *, allow_raw_html: bool = False, strip_interactive_links: bool = False
) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    code_buf: list[str] | None = None

    def fmt(text: str) -> str:
        return format_inline(
            text,
            allow_raw_html=allow_raw_html,
            strip_interactive_links=strip_interactive_links,
        )

    def flush_paragraph(buf: list[str]):
        if not buf:
            return
        text = " ".join(buf)
        # If raw HTML allowed and paragraph looks like a block tag, emit as-is
        if allow_raw_html and text.lstrip().startswith("<"):
            out.append(text)
        else:
            rendered = fmt(text)
            out.append(f"<p>{rendered}</p>")
        buf.clear()

    def render_list_block(list_lines: list[str]) -> str:
        nodes: list[dict[str, object]] = []
        stack: list[tuple[int, list[dict[str, object]]]] = [(-1, nodes)]
        last_node: dict[str, object] | None = None

        for line in list_lines:
            m = re.match(r"^(?P<indent>\s*)-\s+(?P<text>.+)$", line)
            if m:
                indent_str = m.group("indent").replace("\t", "  ")
                indent = len(indent_str)
                text = m.group("text").strip()
                while stack and indent <= stack[-1][0]:
                    stack.pop()
                parent = stack[-1][1]
                node = {"text": text, "children": []}
                parent.append(node)
                stack.append((indent, node["children"]))  # type: ignore[list-item]
                last_node = node
            else:
                cont = line.strip()
                if cont and last_node is not None:
                    last_node["text"] = f"{last_node['text']} {cont}"

        def render_nodes(items: list[dict[str, object]]) -> str:
            if not items:
                return ""
            parts = ["<ul>"]
            for item in items:
                content = fmt(str(item["text"]))
                children = item.get("children") or []
                if children:
                    parts.append(f"<li>{content}")
                    parts.append(render_nodes(children))  # type: ignore[arg-type]
                    parts.append("</li>")
                else:
                    parts.append(f"<li>{content}</li>")
            parts.append("</ul>")
            return "\n".join(parts)

        return render_nodes(nodes)

    def split_table_row(row: str) -> list[str]:
        stripped = row.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [cell.strip() for cell in stripped.split("|")]

    para_buf: list[str] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip("\n")
        if in_code:
            if line.strip().startswith("```"):
                # close
                # Join buffered code lines without inserting a leading newline
                content = "\n".join(code_buf or [])
                if code_lang == "mermaid":
                    out.append(f'<pre class="mermaid">{content}</pre>')
                else:
                    out.append(f"<pre><code>{content}</code></pre>")
                in_code = False
                code_lang = ""
                code_buf = None
            else:
                # Preserve original code lines; escape HTML either way
                if code_buf is None:
                    code_buf = []
                code_buf.append(html.escape(line))
            i += 1
            continue

        if line.strip().startswith("```"):
            flush_paragraph(para_buf)
            lang = line.strip()[3:].strip().lower()
            code_lang = lang
            code_buf = []
            in_code = True
            i += 1
            continue

        # horizontal rule: lines with only ---
        if re.fullmatch(r"\s*-{3,}\s*", line):
            flush_paragraph(para_buf)
            out.append("<hr/>")
            i += 1
            continue

        # tables (pipe-delimited, requires header + separator row)
        if "|" in line and i + 1 < len(lines):
            next_line = lines[i + 1].rstrip("\n")
            if re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*", next_line):
                flush_paragraph(para_buf)
                headers = split_table_row(line)
                i += 2
                rows: list[list[str]] = []
                while i < len(lines):
                    row_line = lines[i].rstrip("\n")
                    if not row_line.strip():
                        break
                    if "|" not in row_line:
                        break
                    if row_line.strip().startswith("```"):
                        break
                    if re.fullmatch(r"\s*-{3,}\s*", row_line):
                        break
                    if row_line.lstrip().startswith("#"):
                        break
                    rows.append(split_table_row(row_line))
                    i += 1
                table_parts = ["<table>", "<thead><tr>"]
                for cell in headers:
                    table_parts.append(f"<th>{fmt(cell)}</th>")
                table_parts.append("</tr></thead>")
                if rows:
                    table_parts.append("<tbody>")
                    for row in rows:
                        table_parts.append("<tr>")
                        for cell in row:
                            table_parts.append(f"<td>{fmt(cell)}</td>")
                        table_parts.append("</tr>")
                    table_parts.append("</tbody>")
                table_parts.append("</table>")
                out.append("".join(table_parts))
                continue

        if not line.strip():
            flush_paragraph(para_buf)
            i += 1
            continue

        # headings
        if line.startswith("###### "):
            flush_paragraph(para_buf)
            out.append(f"<h6>{fmt(line[7:])}</h6>")
            i += 1
            continue
        if line.startswith("##### "):
            flush_paragraph(para_buf)
            out.append(f"<h5>{fmt(line[6:])}</h5>")
            i += 1
            continue
        if line.startswith("#### "):
            flush_paragraph(para_buf)
            out.append(f"<h4>{fmt(line[5:])}</h4>")
            i += 1
            continue
        if line.startswith("### "):
            flush_paragraph(para_buf)
            out.append(f"<h3>{fmt(line[4:])}</h3>")
            i += 1
            continue
        if line.startswith("## "):
            flush_paragraph(para_buf)
            out.append(f"<h2>{fmt(line[3:])}</h2>")
            i += 1
            continue
        if line.startswith("# "):
            flush_paragraph(para_buf)
            out.append(f"<h1>{fmt(line[2:])}</h1>")
            i += 1
            continue

        # lists (supports nested lists via indentation and continued lines)
        if re.match(r"\s*-\s+", line):
            flush_paragraph(para_buf)
            list_lines: list[str] = []
            while i < len(lines):
                cur = lines[i].rstrip("\n")
                if not cur.strip():
                    break
                if cur.strip().startswith("```"):
                    break
                if re.fullmatch(r"\s*-{3,}\s*", cur):
                    break
                if cur.lstrip().startswith("#"):
                    break
                if re.match(r"\s*-\s+", cur) or re.match(r"\s+", cur):
                    list_lines.append(cur)
                    i += 1
                    continue
                break
            out.append(render_list_block(list_lines))
            continue

        # paragraph accumulation
        # normal paragraph text
        para_buf.append(line)
        i += 1

    flush_paragraph(para_buf)
    return "\n".join(out)


def build_one(
    md_path: Path,
    out_path: Path,
    *,
    nav_html: str,
    strip_interactive_links: bool,
    api_mode_widget: str,
    api_mode_script: str,
) -> None:
    allow_raw = md_path.name in {"playground.md", "start-here.md", "memory.md"} or (
        md_path.name == "index.md" and md_path.parent.name == "concepts-in-practice"
    )
    html_body = md_to_html(
        md_path.read_text(encoding="utf-8"),
        allow_raw_html=allow_raw,
        strip_interactive_links=strip_interactive_links,
    )
    if strip_interactive_links and md_path.name == "start-here.md":
        html_body = re.sub(
            r'<div class="hero-links hero-links--local">.*?</div>',
            "",
            html_body,
            flags=re.S,
        )
    # Inject K8s compliance status if building the compliance page and a report exists
    try:
        if md_path.name == "k8s-compliance.md":
            status_path = OUT / "k8s_status.json"
            if not status_path.exists() and OUT != DEFAULT_OUT:
                fallback = DEFAULT_OUT / "k8s_status.json"
                if fallback.exists():
                    status_path = fallback
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
    except Exception as e:
        # Non-fatal: keep page renderable if injection fails, but log why
        try:
            import traceback, sys

            # quiet failure: keep page renderable
            _ = (e, traceback)
        except Exception:
            pass

    # Inject latest memory benchmark summary into the benchmarks page
    try:
        if md_path.name == "memory.md":
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
                # Sort by timestamp (YYYYMMDD-HHMMSS)
                try:
                    rows.sort(key=lambda r: str(r.get("timestamp", "")))
                except Exception:
                    pass
                # Filter to most relevant mode for this page: default k1s only
                latest_filter = (os.getenv("DOCS_LATEST_FILTER") or "k1s").strip().lower()
                filtered = rows
                if latest_filter in ("k1s", "k1s-only", "k1s_only"):
                    filtered = [r for r in rows if str(r.get("mode", "")).lower() == "k1s"]
                # Keep the last N entries from the filtered set; fallback to all rows if empty
                base = filtered if filtered else rows
                tail = base[-8:] if len(base) > 8 else base

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
                    "<p>Summarized from <code>combined/combined.csv</code> at build time."
                    + (" (k1s only)" if latest_filter in ("k1s", "k1s-only", "k1s_only") else "")
                    + "</p>",
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

                # Derived CP PSS helper: k3s -> k3s_control_plane_pss_kb; k1s -> controller+ingress; fallback to legacy
                def cp_pss_mib_derived(row: dict[str, str]) -> str:
                    try:
                        mode = str(row.get("mode", "")).lower()
                        if mode == "k3s":
                            v = row.get("k3s_control_plane_pss_kb")
                            # Treat 0/empty as missing; fall back to legacy
                            if v not in (None, "", "0", 0):
                                x = float(v or 0)
                                return f"{x / 1024.0:.1f}"
                        c = row.get("controller_pss_kb")
                        i = row.get("ingress_pss_kb")
                        c_val = float(c or 0)
                        i_val = float(i or 0)
                        if (c_val > 0) or (i_val > 0):
                            total_kib = c_val + i_val
                            return f"{total_kib / 1024.0:.1f}"
                    except Exception:
                        pass
                    try:
                        x = float(row.get("control_plane_pss_kb", "0") or 0)
                    except Exception:
                        x = 0.0
                    return f"{x / 1024.0:.1f}"

                for r in tail:
                    cp_mib = cp_pss_mib_derived(r)
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
                # ---- Comparison Matrix (pivot by scenario) ----
                try:
                    # Build scenario buckets from all rows (not only tail) to catch freshest per scenario
                    def to_float_mib(val: str, kib: bool = False) -> float:
                        try:
                            v = float(val or 0)
                        except Exception:
                            v = 0.0
                        if kib:
                            return v / 1024.0
                        return v / (1024.0 * 1024.0)

                    # Scenario detection from mode/backend/label
                    def scenario_name(row: dict[str, str]) -> str:
                        mode = (row.get("mode") or "").lower().strip() or "?"
                        backend = (row.get("backend") or "").lower().strip() or "?"
                        label = row.get("label", "")
                        root_tag = (
                            "rootless"
                            if "+rootless+" in label
                            else ("priv" if "+priv+" in label else "?")
                        )
                        if mode == "k1s" and backend == "podman" and root_tag == "rootless":
                            return "k1s rootless"
                        if mode == "k1s" and backend == "podman" and root_tag == "priv":
                            return "k1s rootful"
                        if mode == "k1s" and backend == "cri":
                            return "k1s cri"
                        if mode == "k1s" and backend == "docker":
                            return "k1nd"
                        if mode == "k3s":
                            return "k3d"
                        return f"{mode} {backend}"

                    # Extract stage from label suffix
                    def stage_name(label: str) -> str:
                        l = label or ""
                        if l.endswith("-idle"):
                            return "idle"
                        m = re.search(r"-pods-(\d+)$", l)
                        if m:
                            return f"pods-{m.group(1)}"
                        m = re.search(r"-rollout-(\d+)-(during|post)$", l)
                        if m:
                            return f"rollout-{m.group(1)}-{m.group(2)}"
                        return "other"

                    # Keep the latest row per (scenario, stage)
                    latest: dict[tuple[str, str], dict[str, str]] = {}
                    for r in rows:
                        sc = scenario_name(r)
                        st = stage_name(r.get("label", ""))
                        if st == "other":
                            continue
                        key = (sc, st)
                        prev = latest.get(key)
                        if prev is None or str(r.get("timestamp", "")) > str(
                            prev.get("timestamp", "")
                        ):
                            latest[key] = r

                    # Desired column order
                    col_order = ["k1s rootless", "k1s rootful", "k1s cri", "k1nd", "k3d"]
                    # Gather all stages we have across these columns
                    stages = sorted({k[1] for k in latest.keys()})
                    coverage_min = float(os.getenv("DOCS_COVERAGE_MIN", "0.8") or 0.8)
                    coverage_map: dict[str, float] = {}
                    low_coverage: set[str] = set()

                    # Heatmap coloring helper (lower is better)
                    def color_for(values: list[float], val: float) -> str:
                        good = min(values)
                        bad = max(values)
                        if good == bad:
                            return "background:rgba(128,128,128,.2)"  # flat
                        t = (val - good) / (bad - good)  # 0..1
                        # green (0.3) → yellow (0.5) → red (0.8)
                        # map t into hue 120→0
                        hue = int((1.0 - t) * 120)
                        return f"background:hsl({hue},60%,25%); color:#fff"

                    def render_metric_table(title_txt: str, extractor) -> str:
                        html_parts: list[str] = []
                        html_parts.append(f"<h3>{html.escape(title_txt)}</h3>")
                        header_cells: list[str] = []
                        for c in col_order:
                            if c in low_coverage:
                                cov = coverage_map.get(c, 0.0)
                                header_cells.append(
                                    f"<th title='coverage {cov:.0%} (min {coverage_min:.0%})'>{c} ⚠</th>"
                                )
                            else:
                                header_cells.append(f"<th>{c}</th>")
                        html_parts.append(
                            "<table class='mini' style='border-collapse:collapse;width:100%'>"
                            + "<thead><tr><th>Stage</th>"
                            + "".join(header_cells)
                            + "</tr></thead><tbody>"
                        )
                        for st in stages:
                            row_vals: dict[str, float] = {}
                            for c in col_order:
                                r = latest.get((c, st))
                                if r:
                                    _v = extractor(r)
                                    if _v is not None:
                                        row_vals[c] = _v
                            # Compute colors per stage
                            vals = [v for v in row_vals.values() if v is not None]
                            html_parts.append(f"<tr><td>{st}</td>")
                            for c in col_order:
                                if c in row_vals:
                                    v = row_vals[c]
                                    style = color_for(vals, v) if len(vals) > 1 else ""
                                    html_parts.append(
                                        f"<td style='text-align:right;{style}'>" f"{v:.1f}</td>"
                                    )
                                else:
                                    html_parts.append("<td style='opacity:.5'>—</td>")
                            html_parts.append("</tr>")
                        html_parts.append("</tbody></table>")
                        return "".join(html_parts)

                    parts.append(
                        "<style>table.mini td, table.mini th { border:1px solid var(--border); padding:6px; }</style>"
                    )
                    parts.append("<h2>Latest Comparison Matrix</h2>")
                    # Overall winner band (normalized across stages x metrics; lower is better)
                    try:

                        def to_norm(vals: list[float], v: float) -> float:
                            if not vals:
                                return 0.0
                            good, bad = min(vals), max(vals)
                            rng = (bad - good) or 1.0
                            return (v - good) / rng

                        def cp_pss_float_derived(r: dict[str, str]) -> float | None:
                            try:
                                mode = str(r.get("mode", "")).lower()
                                if mode == "k3s":
                                    v = r.get("k3s_control_plane_pss_kb")
                                    if v not in (None, "", "0", 0):
                                        return float(v or 0) / 1024.0
                                c = r.get("controller_pss_kb")
                                i = r.get("ingress_pss_kb")
                                c_val = float(c or 0)
                                i_val = float(i or 0)
                                if (c_val > 0) or (i_val > 0):
                                    return (c_val + i_val) / 1024.0
                            except Exception:
                                pass
                            # Fallback
                            try:
                                return float(r.get("control_plane_pss_kb", "0") or 0) / 1024.0
                            except Exception:
                                return None

                        def _mad_backfill_local(r: dict[str, str]) -> float:
                            try:
                                v = float(r.get("mem_available_delta_bytes", 0) or 0)
                            except Exception:
                                v = 0.0
                            if v == 0.0:
                                try:
                                    before = float(r.get("mem_available_before_bytes", 0) or 0)
                                    after = float(r.get("mem_available_after_bytes", 0) or 0)
                                    v = after - before
                                except Exception:
                                    v = 0.0
                            return to_float_mib(v)

                        metric_extractors = [
                            ("Control Plane PSS", lambda r: cp_pss_float_derived(r)),
                            ("App Cgroups", lambda r: to_float_mib(r.get("app_mem_bytes", "0"))),
                            (
                                "Host System Cgroups",
                                lambda r: to_float_mib(
                                    r.get("host_system_cgroups_bytes")
                                    if r.get("host_system_cgroups_bytes") is not None
                                    else r.get("system_mem_bytes", "0")
                                ),
                            ),
                            ("MemAvail Δ", lambda r: _mad_backfill_local(r)),
                        ]
                        totals: dict[str, tuple[float, int]] = {c: (0.0, 0) for c in col_order}
                        for st in stages:
                            for _mt, ex in metric_extractors:
                                vals_per_col: dict[str, float] = {}
                                for c in col_order:
                                    rr = latest.get((c, st))
                                    if rr:
                                        _val = ex(rr)
                                        if _val is not None:
                                            vals_per_col[c] = _val
                                if len(vals_per_col) < 2:
                                    continue
                                arr = list(vals_per_col.values())
                                for c, v in vals_per_col.items():
                                    s, n = totals[c]
                                    totals[c] = (s + to_norm(arr, v), n + 1)
                        # Compute a coverage-aware ranking: blend missing coverage toward worst-case (1.0)
                        ranking = []
                        try:
                            max_n = max(n for (_s, n) in totals.values()) or 1
                        except ValueError:
                            max_n = 1
                        for c in col_order:
                            s, n = totals[c]
                            avg = (s / n) if n else 1.0
                            coverage = (n / max_n) if max_n else 0.0
                            # Adjusted score keeps scale [0,1], lower is better.
                            # When coverage < 1, blend missing portion toward worst-case (1.0).
                            adjusted = (avg * coverage) + (1.0 * (1.0 - coverage))
                            ranking.append((adjusted, avg, c, n, coverage))
                        ranking.sort(key=lambda x: x[0])
                        # Apply coverage threshold to filter scenarios from ranking (tables still show all).
                        allowed_cols = [
                            c
                            for (_adj, _avg, c, n, coverage) in ranking
                            if (max_n and (coverage >= coverage_min))
                        ]
                        coverage_map = {
                            c: coverage for (_adj, _avg, c, _n, coverage) in ranking
                        }
                        low_coverage = {
                            c for c in col_order if coverage_map.get(c, 0.0) < coverage_min
                        }
                        # Winner band (only allowed columns)
                        parts.append(
                            "<style> .pill { display:inline-block; padding:4px 10px; border:1px solid var(--border); border-radius:999px; margin-right:8px; }"
                            " .pill.win { background:#144d2a; color:#fff; border-color:#1f6f3e; }"
                            " .pill.place2 { background:#4d4a14; color:#fff; border-color:#6f6a1f; }"
                            " .pill.place3 { background:#4d2b14; color:#fff; border-color:#6f3f1f; }"
                            " .pill.dim { opacity:.7; }"
                            " .band { margin:6px 0 12px 0 }"
                            "</style>"
                        )
                        bl: list[str] = ["<div class='band'><strong>Overall Ranking:</strong> "]
                        shown = [
                            (adj, _avg, c, n, cov)
                            for (adj, _avg, c, n, cov) in ranking
                            if c in allowed_cols
                        ]
                        if not shown:
                            shown = [
                                (adj, _avg, c, n, cov)
                                for (adj, _avg, c, n, cov) in ranking
                                if cov > 0
                            ] or ranking
                        for idx, (adjusted, _avg, c, n, coverage) in enumerate(shown):
                            cls = (
                                "win"
                                if idx == 0
                                else ("place2" if idx == 1 else ("place3" if idx == 2 else "dim"))
                            )
                            score = int(round(adjusted * 100))
                            title = f"comparisons:{n} coverage:{coverage:.0%}"
                            bl.append(
                                f"<span class='pill {cls}' title='{title}'> {c} <span style='opacity:.85'>&nbsp;({score})</span></span>"
                            )
                        bl.append("</div>")
                        if low_coverage:
                            bl.append(
                                "<div class='band' style='opacity:.8'>Low coverage (shown but excluded from ranking until "
                                + f"≥{coverage_min:.0%}): "
                                + ", ".join(sorted(low_coverage))
                                + "</div>"
                            )
                        parts.append("".join(bl))
                    except Exception:
                        pass
                    parts.append(
                        render_metric_table(
                            "Control Plane PSS (MiB) — lower is better",
                            lambda r: cp_pss_float_derived(r),
                        )
                    )
                    parts.append(
                        render_metric_table(
                            "App Cgroups (MiB) — lower is better",
                            lambda r: to_float_mib(r.get("app_mem_bytes", "0")),
                        )
                    )
                    parts.append(
                        render_metric_table(
                            "Host System Cgroups (MiB) — lower is better",
                            lambda r: to_float_mib(
                                r.get("host_system_cgroups_bytes")
                                if r.get("host_system_cgroups_bytes") is not None
                                else r.get("system_mem_bytes", "0")
                            ),
                        )
                    )

                    def _memavail_delta_backfill(r):
                        try:
                            v = float(r.get("mem_available_delta_bytes", 0) or 0)
                        except Exception:
                            v = 0.0
                        if v == 0.0:
                            try:
                                before = float(r.get("mem_available_before_bytes", 0) or 0)
                                after = float(r.get("mem_available_after_bytes", 0) or 0)
                                v = after - before
                            except Exception:
                                v = 0.0
                        return to_float_mib(v)

                    parts.append(
                        render_metric_table(
                            "MemAvail Δ (MiB) — lower is better",
                            lambda r: _memavail_delta_backfill(r),
                        )
                    )
                except Exception:
                    # Best-effort: if anything fails, skip the matrix
                    pass
                # Inline charts below the table
                # Inline charts from one or more chart directories (charts, charts-user)
                import shutil

                charts_out = OUT / "charts"
                charts_out.mkdir(parents=True, exist_ok=True)
                charts_dirs = []
                for nm in ["charts", "charts-user"]:
                    p = repo_root / nm
                    if p.exists():
                        charts_dirs.append(p)
                if charts_dirs:
                    chart_map = [
                        ("control_plane_pss_k3d.png", "Control‑plane PSS — Timeline (k3s)"),
                        (
                            "control_plane_pss_k1s_rootless.png",
                            "Control‑plane PSS — Timeline (k1s rootless)",
                        ),
                        (
                            "control_plane_pss_k1s_rootful.png",
                            "Control‑plane PSS — Timeline (k1s rootful)",
                        ),
                        ("control_plane_pss_k1nd.png", "Control‑plane PSS — Timeline (k1nd)"),
                        ("system_cgroups_k3d.png", "System Cgroups — Timeline (k3s)"),
                        (
                            "system_cgroups_k1s_rootless.png",
                            "System Cgroups — Timeline (k1s rootless)",
                        ),
                        (
                            "system_cgroups_k1s_rootful.png",
                            "System Cgroups — Timeline (k1s rootful)",
                        ),
                        ("system_cgroups_k1nd.png", "System Cgroups — Timeline (k1nd)"),
                        ("per_pod_overhead.png", "Per‑Pod Overhead (MiB)"),
                        ("per_pod_scaling.png", "Per‑Pod Scaling (MiB)"),
                        ("rollout_pairs.png", "Rollout During vs Post (CP PSS)"),
                        ("matrix_heatmap.png", "Latest Comparison Heatmap"),
                    ]
                    inline_blocks: list[str] = []
                    copied: set[str] = set()
                    stale: list[str] = []
                    # Optional: allow configuring or disabling staleness warnings
                    try:
                        _stale_disable = os.getenv("DOCS_CHART_STALENESS_DISABLE", "0") == "1"
                        # Default to one week (7 days) if not set
                        _stale_hours = int(os.getenv("DOCS_CHART_STALENESS_HOURS", "168"))
                        if _stale_hours < 0:
                            _stale_hours = 0
                    except Exception:
                        _stale_disable = False
                        _stale_hours = 168

                    for cdir in charts_dirs:
                        for fname, title_txt in chart_map:
                            src = cdir / fname
                            dst = charts_out / fname
                            if src.exists() and fname not in copied:
                                try:
                                    if not _stale_disable:
                                        try:
                                            src_mtime = src.stat().st_mtime
                                            now = time.time()
                                            # Mark as stale if older than configured hours (default 6)
                                            if (now - src_mtime) > (_stale_hours * 3600):
                                                stale.append(fname)
                                        except Exception:
                                            pass
                                    shutil.copy2(src, dst)
                                    copied.add(fname)
                                    inline_blocks.append(
                                        f"<h3>{html.escape(title_txt)}</h3>"
                                        f"<img src='charts/{fname}' alt='{html.escape(title_txt)}' "
                                        f"style='max-width:100%;height:auto;border:1px solid var(--border);margin:8px 0'/>"
                                    )
                                except Exception:
                                    pass
                        # Dynamic comparison charts: comparison_<metric>_<stage>.png
                        for src in cdir.glob("comparison_*.png"):
                            try:
                                dst = charts_out / src.name
                                if src.name not in copied:
                                    shutil.copy2(src, dst)
                                    copied.add(src.name)
                                    title_txt = (
                                        src.stem.replace("comparison_", "")
                                        .replace("_", " ")
                                        .title()
                                    )
                                    inline_blocks.append(
                                        f"<h3>{html.escape(title_txt)}</h3>"
                                        f"<img src='charts/{src.name}' alt='{html.escape(title_txt)}' "
                                        f"style='max-width:100%;height:auto;border:1px solid var(--border);margin:8px 0'/>"
                                    )
                            except Exception:
                                pass
                    # Cleanup: remove mixed timeline charts if present to keep site tidy
                    try:
                        for nm in ["control_plane_pss.png", "system_cgroups.png"]:
                            p = charts_out / nm
                            if p.exists():
                                p.unlink()
                    except Exception:
                        pass
                    if inline_blocks:
                        warn = ""
                        if stale:
                            # Use singular/plural hours label and reflect configured threshold
                            hrs_label = f"{_stale_hours} hour" + ("s" if _stale_hours != 1 else "")
                            warn = (
                                "<div style='margin:8px 0; padding:8px; border:1px solid var(--border); color:#f59e0b'>"
                                + "Staleness warning: "
                                + ", ".join(stale)
                                + f" are older than {hrs_label}; regenerate charts if this is unexpected."
                                + "</div>"
                            )
                        parts.append("<h2>Charts</h2>" + warn + "".join(inline_blocks))
                html_body += "\n" + "\n".join(parts)
    except Exception:
        # Non-fatal: keep page renderable if injection fails
        pass
    extra_head = ""
    if md_path.name == "playground.md":
        ver = _labs_asset_version()
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
    html_out = render_template(
        title=title,
        body=html_body,
        api_base=API_BASE,
        dashboard_url=DASHBOARD_URL,
        extra_head=extra_head,
        footer_text=BUILD_STAMP_PLACEHOLDER,
        nav_html=nav_html,
        api_mode_widget=api_mode_widget,
        api_mode_script=api_mode_script,
    )
    html_out = _apply_stable_build_stamp(out_path, html_out)
    out_path.write_text(html_out, encoding="utf-8")


def copy_openapi_specs() -> None:
    try:
        src_root = SRC / "openapi"
        if not src_root.exists():
            return
        out_root = OUT / "openapi"
        out_root.mkdir(parents=True, exist_ok=True)
        swagger_root = OUT / "swagger"
        swagger_root.mkdir(parents=True, exist_ok=True)
        swagger_apishim_root = swagger_root / "apishim"
        swagger_apishim_root.mkdir(parents=True, exist_ok=True)
        import shutil

        v3_path = src_root / "openapi-v3.json"
        if v3_path.exists():
            try:
                shutil.copy2(v3_path, swagger_root / STATIC_SWAGGER_APISHIM_SPEC)
                shutil.copy2(v3_path, swagger_apishim_root / STATIC_SWAGGER_APISHIM_SPEC)
            except Exception:
                pass
        try:
            controller_doc = build_controller_openapi_doc()
            controller_payload = json.dumps(controller_doc).encode("utf-8")
            (swagger_root / STATIC_SWAGGER_CONTROLLER_SPEC).write_bytes(controller_payload)
            (OUT / "openapi.json").write_bytes(controller_payload)
        except Exception:
            pass
        for p in src_root.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in {".json", ".yml", ".yaml"}:
                continue
            try:
                shutil.copy2(p, out_root / p.name)
            except Exception:
                pass
    except Exception:
        # Non-fatal: keep docs renderable if copy fails
        pass


def build_static_swagger_page(
    *,
    nav_html: str,
    api_mode_widget: str,
    api_mode_script: str,
    use_vendor_assets: bool,
    title: str,
    spec_path: str,
    default_url: str,
    base_tag: str,
    out_path: str,
    active_tab: str,
) -> None:
    vendor_root = SRC / "static" / "swagger-ui"
    if use_vendor_assets and vendor_root.exists():
        css_href = "../static/swagger-ui/swagger-ui.css"
        bundle_src = "../static/swagger-ui/swagger-ui-bundle.js"
        preset_src = "../static/swagger-ui/swagger-ui-standalone-preset.js"
    else:
        css_href = f"{SWAGGER_UI_CDN_BASE}/swagger-ui.css"
        bundle_src = f"{SWAGGER_UI_CDN_BASE}/swagger-ui-bundle.js"
        preset_src = f"{SWAGGER_UI_CDN_BASE}/swagger-ui-standalone-preset.js"
    default_url_js = json.dumps(default_url)
    active_controller = " is-active" if active_tab == "controller" else ""
    active_apishim = " is-active" if active_tab == "apishim" else ""
    topbar = "\n".join(
        [
            '<div class="swagger-topbar">',
            f'  <a class="swagger-tab{active_controller}" href="{STATIC_SWAGGER_HREF}">{STATIC_SWAGGER_CONTROLLER_LABEL}</a>',
            f'  <a class="swagger-tab{active_apishim}" href="{STATIC_SWAGGER_APISHIM_HREF}">{STATIC_SWAGGER_APISHIM_LABEL}</a>',
            "</div>",
        ]
    )
    body = "\n".join(
        [
            topbar,
            '<div class="swagger-shell">',
            f"  <h1>{html.escape(title)}</h1>",
            "  <p>Static API docs generated by docs-export.</p>",
            '  <div id="swagger-ui"></div>',
            "  <noscript>Enable JavaScript to view the API documentation.</noscript>",
            "  <script>",
            "    window.addEventListener('load', function() {",
            "      if (!window.SwaggerUIBundle) return;",
            "      var specUrl = new URL(",
            f"        '{spec_path}',",
            "        window.location.href",
            "      ).toString();",
            "      window.ui = SwaggerUIBundle({",
            "        url: specUrl,",
            "        dom_id: '#swagger-ui',",
            "        deepLinking: true,",
            "        presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],",
            "        layout: 'StandaloneLayout'",
            "      });",
            f"      var defaultUrl = {default_url_js};",
            "      if (defaultUrl) {",
            "        var input = document.querySelector('.download-url-input');",
            "        if (input) {",
            "          input.value = defaultUrl;",
            "          input.setAttribute('value', defaultUrl);",
            "        }",
            "      }",
            "    });",
            "  </script>",
            "</div>",
        ]
    )
    extra_head = "\n".join(
        [
            f'<link rel="stylesheet" href="{css_href}"/>',
            "<style>",
            "  .swagger-topbar {",
            "    display: flex;",
            "    gap: 12px;",
            "    align-items: center;",
            "    padding: 12px 16px;",
            "    border-radius: 16px;",
            "    background: color-mix(in srgb, var(--k1s-panel) 80%, #000000 20%);",
            "    border: 1px solid var(--k1s-border);",
            "    margin-bottom: 16px;",
            "  }",
            "  .swagger-tab {",
            "    font-weight: 600;",
            "    color: var(--fg);",
            "    text-decoration: none;",
            "    padding: 6px 10px;",
            "    border-radius: 10px;",
            "    border: 1px solid transparent;",
            "  }",
            "  .swagger-tab:hover {",
            "    border-color: var(--k1s-border-soft);",
            "    color: var(--link-hover);",
            "  }",
            "  .swagger-tab.is-active {",
            "    background: color-mix(in srgb, var(--k1s-brand-gold) 20%, transparent);",
            "    border-color: color-mix(in srgb, var(--k1s-brand-gold) 40%, var(--k1s-border));",
            "  }",
            "  .swagger-shell {",
            "    background: #ffffff;",
            "    color: #111111;",
            "    padding: 16px;",
            "    border-radius: 16px;",
            "    box-shadow: 0 14px 34px rgba(0,0,0,0.24);",
            "    color-scheme: light;",
            "  }",
            "  .swagger-shell p { color: #444444; }",
            "  .swagger-shell .swagger-ui { color: #111111; }",
            "</style>",
            f'<script defer src="{bundle_src}"></script>',
            f'<script defer src="{preset_src}"></script>',
        ]
    )
    out_path = OUT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_out = render_template(
        title=title,
        body=body,
        api_base=API_BASE,
        dashboard_url=DASHBOARD_URL,
        extra_head=extra_head,
        base_tag=base_tag,
        footer_text=BUILD_STAMP_PLACEHOLDER,
        nav_html=nav_html,
        api_mode_widget=api_mode_widget,
        api_mode_script=api_mode_script,
    )
    html_out = _apply_stable_build_stamp(out_path, html_out)
    out_path.write_text(html_out, encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    include_interactive = not EXPORT_NON_INTERACTIVE
    include_static_swagger = EXPORT_NON_INTERACTIVE
    vendor_swagger_assets = SWAGGER_UI_VENDOR and (
        (SRC / "static" / "swagger-ui" / "swagger-ui.css").exists()
    )
    nav_html = render_nav(
        include_interactive=include_interactive,
        include_static_swagger=include_static_swagger,
    )
    nav_html_swagger = nav_html
    api_mode_widget, api_mode_script = api_mode_fragments(include_interactive)
    # Copy static assets if present
    try:
        static_src = SRC / "static"
        if static_src.exists():
            import shutil

            static_out = OUT / "static"
            if static_out.exists():
                for existing in static_out.iterdir():
                    if existing.is_dir():
                        shutil.rmtree(existing)
                    else:
                        existing.unlink()
            static_out.mkdir(parents=True, exist_ok=True)
            for p in static_src.iterdir():
                if p.name == "swagger-ui" and (not EXPORT_NON_INTERACTIVE or not vendor_swagger_assets):
                    continue
                if p.is_dir():
                    shutil.copytree(p, static_out / p.name)
                elif p.is_file():
                    shutil.copy2(p, static_out / p.name)
    except Exception:
        pass
    mapping = dict(DOCS_MAPPING)
    if EXPORT_NON_INTERACTIVE:
        for src in INTERACTIVE_SOURCES:
            mapping.pop(src, None)

    rss_feed_url = RSS_FEED_URL
    rss_feed_title = RSS_FEED_TITLE
    source_repo_url = SOURCE_REPO_URL
    source_repo_label = SOURCE_REPO_LABEL
    collab_repo_url = COLLAB_REPO_URL
    collab_repo_label = COLLAB_REPO_LABEL

    def render_link(label: str, href: str, external: bool) -> str:
        attrs = ' target="_blank" rel="noopener"' if external else ""
        return f'<a class="hero-link" href="{href}"{attrs}>{html.escape(label)}</a>'

    index_quick_links = [
        ("Start Here", "start-here.html", False, False),
        ("Overview", "overview.html", False, False),
        ("Live Hive Dashboard", "/dashboard", True, True),
        ("Interactive Lab Playground", "playground.html", True, False),
    ]
    if include_static_swagger:
        index_quick_links.append((STATIC_SWAGGER_LABEL, STATIC_SWAGGER_HREF, False, False))
    if rss_feed_url:
        index_quick_links.append(("RSS Feed", rss_feed_url, False, True))

    networking_links = [
        ("HTTP API", "http-api.html", False, False),
        ("API Shim Compatibility", "apishim-compatibility-matrix.html", False, False),
        ("Ingress", "ingress.html", False, False),
        ("API Auth", "api-auth.html", False, False),
    ]
    if include_static_swagger:
        networking_links.insert(1, (STATIC_SWAGGER_LABEL, STATIC_SWAGGER_HREF, False, False))

    index_sections = [
        {
            "title": "Getting Started",
            "desc": "Fast onboarding paths, demos, and architecture context.",
            "links": [
                ("Start Here", "start-here.html", False, False),
                ("Overview", "overview.html", False, False),
                ("Demos & Examples", "examples.html", False, False),
                ("Architecture", "architecture.html", False, False),
            ],
        },
        {
            "title": "Labs & Concepts",
            "desc": "Hands-on labs and the reconciliation mental model.",
            "links": [
                ("Multi-Node Lab", "multinode-lab.html", False, False),
                ("Concepts", "concepts.html", False, False),
                ("Concepts in Practice", "concepts-in-practice.html", False, False),
            ],
        },
        {
            "title": "Platform Guides",
            "desc": "Storage, rollouts, and runtime configuration guides.",
            "links": [
                ("Runtime Profiles", "runtime-profiles.html", False, False),
                ("Configs & Secrets", "configs-secrets.html", False, False),
                ("Rollouts", "rollouts.html", False, False),
                ("Storage", "storage.html", False, False),
                ("Scheduling", "scheduling.html", False, False),
            ],
        },
        {
            "title": "Networking & API",
            "desc": "Ingress, auth, and API compatibility details.",
            "links": networking_links,
        },
        {
            "title": "Ops & Observability",
            "desc": "Runbooks, benchmarks, and observability surfaces.",
            "links": [
                ("Observability", "observability.html", False, False),
                ("Benchmarks", "benchmarks.html", False, False),
                ("End-to-End Guide", "e2e.html", False, False),
                ("K8s Compliance Status", "k8s-compliance.html", False, False),
            ],
        },
        {
            "title": "Interactive Surfaces",
            "desc": "Live dashboards and interactive playgrounds.",
            "links": [
                ("Live Hive Dashboard", "/dashboard", True, True),
                ("Interactive Lab Playground", "playground.html", True, False),
            ],
        },
    ]

    quick_links_html = []
    for label, href, interactive, external in index_quick_links:
        if interactive and not include_interactive:
            continue
        quick_links_html.append(render_link(label, href, external))

    card_html = []
    for section in index_sections:
        link_bits = []
        for label, href, interactive, external in section["links"]:
            if interactive and not include_interactive:
                continue
            link_bits.append(render_link(label, href, external))
        if not link_bits:
            continue
        card_html.append(
            "\n".join(
                [
                    '  <div class="hero-card hero-card--section">',
                    f'    <h2>{html.escape(section["title"])}</h2>',
                    f'    <p>{html.escape(section["desc"])}</p>',
                    '    <div class="hero-links hero-links--dense">',
                    "      " + "\n      ".join(link_bits),
                    "    </div>",
                    "  </div>",
                ]
            )
        )

    repo_section = ""
    repo_items = []
    if source_repo_url:
        repo_items.append((source_repo_label, source_repo_url))
    if collab_repo_url:
        repo_items.append((collab_repo_label, collab_repo_url))
    if repo_items:
        repo_bits = []
        for label, url in repo_items:
            repo_bits.append(
                "\n".join(
                    [
                        '      <div class="hero-repo-item">',
                        f'        <span class="hero-repo-item-label">{html.escape(label)}</span>',
                        (
                            f'        <a class="hero-repo-link" href="{html.escape(url)}" '
                            'target="_blank" rel="noopener">'
                            f"{html.escape(url)}</a>"
                        ),
                        "      </div>",
                    ]
                )
            )
        repo_section = "\n".join(
            [
                '    <div class="hero-repo">',
                '      <span class="hero-repo-label">Repositories</span>',
                '      <div class="hero-repo-list">',
                "\n".join(repo_bits),
                "      </div>",
                "    </div>",
            ]
        )
    index = "\n".join(
        [
            '<div class="hero hero-index">',
            '  <div class="hero-brand">',
            '    <div class="hero-logo-row">',
            '      <img src="static/k1s-logo-horizontal.svg" alt="k1s logo" class="hero-logo" />',
            '      <span class="hero-pill">Docs Hub</span>',
            "    </div>",
            "    <h1>k1s Documentation</h1>",
            '    <p class="hero-tagline">Guides, labs, and reference for building, operating, and observing k1s clusters.</p>',
            '    <p class="hero-tagline">Status: k1s is under very active development and has not reached a fully stable release. Do not use it in production without thorough security vetting and testing for your environment.</p>',
            '    <div class="hero-links">',
            "      " + "\n      ".join(quick_links_html),
            "    </div>",
            repo_section,
            "  </div>",
            '  <div class="hero-actions">',
            "\n".join(card_html),
            "  </div>",
            "</div>",
        ]
    )
    index_extra_head = ""
    if rss_feed_url:
        index_extra_head = (
            f'<link rel="alternate" type="application/rss+xml" '
            f'title="{html.escape(rss_feed_title)}" href="{html.escape(rss_feed_url)}"/>'
        )
    index_path = OUT / "index.html"
    index_html = render_template(
        title="k1s Docs",
        body=index,
        api_base=API_BASE,
        dashboard_url=DASHBOARD_URL,
        extra_head=index_extra_head,
        footer_text=BUILD_STAMP_PLACEHOLDER,
        nav_html=nav_html,
        api_mode_widget=api_mode_widget,
        api_mode_script=api_mode_script,
    )
    index_html = _apply_stable_build_stamp(index_path, index_html)
    index_path.write_text(index_html, encoding="utf-8")

    if EXPORT_NON_INTERACTIVE:
        copy_openapi_specs()
        build_static_swagger_page(
            nav_html=nav_html_swagger,
            api_mode_widget=api_mode_widget,
            api_mode_script=api_mode_script,
            use_vendor_assets=vendor_swagger_assets,
            title="k1s Controller API",
            spec_path=STATIC_SWAGGER_CONTROLLER_SPEC,
            default_url=SWAGGER_UI_CONTROLLER_DEFAULT_URL,
            base_tag='<base href="../"/>',
            out_path=STATIC_SWAGGER_CONTROLLER_OUT_PATH,
            active_tab="controller",
        )
        build_static_swagger_page(
            nav_html=nav_html_swagger,
            api_mode_widget=api_mode_widget,
            api_mode_script=api_mode_script,
            use_vendor_assets=vendor_swagger_assets,
            title="API Shim",
            spec_path=STATIC_SWAGGER_APISHIM_SPEC,
            default_url=SWAGGER_UI_DEFAULT_URL,
            base_tag='<base href="../../"/>',
            out_path=STATIC_SWAGGER_APISHIM_OUT_PATH,
            active_tab="apishim",
        )

    for src_name, out_name in mapping.items():
        build_one(
            SRC / src_name,
            OUT / out_name,
            nav_html=nav_html,
            strip_interactive_links=EXPORT_NON_INTERACTIVE,
            api_mode_widget=api_mode_widget,
            api_mode_script=api_mode_script,
        )

    # Copy curated example YAMLs to /examples for playground preview
    if not EXPORT_NON_INTERACTIVE:
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
