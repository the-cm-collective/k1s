#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

DEFAULT_PLAYWRIGHT_VERSION = "1.60.0"
DEFAULT_CONTROLLER_URL = "http://127.0.0.1:19108"
DEFAULT_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "k1s-playwright-demo"
DEFAULT_NPM_ROOT = Path(tempfile.gettempdir()) / "k1s-playwright-demo-npm"


def utc_run_id() -> str:
    now = datetime.now(timezone.utc)  # noqa: UP017 - keep this dev script Python 3.10 compatible.
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"


def infer_project_from_dashboard_url(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    parts = host.split(".")
    if len(parts) >= 2 and parts[0] == "k1s":
        return parts[1]
    return ""


def dashboard_url_for_project(project: str) -> str:
    return f"https://k1s.{project}.workerbee.home.arpa:19443/dashboard"


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def read_stack_admin_token(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("admin_token") or "")


def read_stack_admin_token_with_sudo(path: Path) -> str:
    sudo = require_tool("sudo")
    python = require_tool("python3")
    code = (
        "import json,sys;"
        "from pathlib import Path;"
        "print(json.loads(Path(sys.argv[1]).read_text()).get('admin_token',''))"
    )
    completed = subprocess.run(  # noqa: S603
        [sudo, "-n", python, "-c", code, str(path)],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def resolve_admin_token(
    *,
    project: str,
    state_root: Path,
    token_env_file: Path | None,
    sudo_stack_token: bool,
) -> str:
    token = os.environ.get("K1S_ADMIN_TOKEN") or os.environ.get("AE_API_ADMIN_TOKEN") or ""
    if token:
        return token
    if token_env_file is not None:
        values = parse_env_file(token_env_file)
        token = values.get("AE_API_ADMIN_TOKEN") or values.get("AE_LABS_TOKEN") or ""
        if token:
            return token
    if project:
        stack_json = state_root / project / "stack.json"
        token = read_stack_admin_token(stack_json)
        if token:
            return token
        if sudo_stack_token:
            token = read_stack_admin_token_with_sudo(stack_json)
            if token:
                return token
        values = parse_env_file(state_root / project / "apishim.env")
        token = values.get("AE_API_ADMIN_TOKEN") or values.get("AE_LABS_TOKEN") or ""
        if token:
            return token
    return ""


def build_demo_import_payload(*, run_id: str, created_at: str) -> dict:
    trace_id = f"demo-fabric-advisory-review-{run_id}"
    request_id = f"demo-fabric-advisory-request-{run_id}"
    bundle_id = f"demo-das-runtime-{run_id}"
    das_query_trace_id = f"demo-das-query-{run_id}"
    signal_id = f"demo-cognitive-signal-{run_id}"
    facts_ref = f"das://site-a/runtime/{run_id}/facts.jsonl"
    result_ref = f"das://site-a/runtime/{run_id}/query/{das_query_trace_id}"
    evidence_refs = [
        facts_ref,
        result_ref,
        f"k1s://fabric/phase-assurance/F3/{run_id}",
    ]
    return {
        "source": "k1s.dev.fabric-advisory-demo/v1",
        "api_version": "k1s.fabric-advisory-demo/v1",
        "decision_traces": [
            {
                "trace_id": trace_id,
                "request_id": request_id,
                "created_at": created_at,
                "request_contract": {
                    "subject_type": "fabric_phase_gate",
                    "subject_id": "F3",
                    "intent": "review_pending_advisory_trace",
                    "facts_ref": facts_ref,
                    "locality_snapshot_ref": f"k1s://fabric/locality/snapshot/{run_id}",
                    "max_candidates": 5,
                    "time_budget_ms": 3000,
                    "policy_mode": "advisory_only",
                },
                "response_contract": {
                    "provider": "hyperon-das-demo",
                    "status": "review",
                    "recommendation": (
                        "Diverge until F0/F1 blockers are cleared; use Hyperon/DAS "
                        "evidence as advisory context while k1s remains authoritative."
                    ),
                    "confidence": 0.74,
                    "evidence_refs": evidence_refs,
                    "authoritative": False,
                },
                "deterministic_baseline": {
                    "controller_authority": "k1s",
                    "phase": "F3",
                    "gate_ready": False,
                    "blocked_by": ["F0", "F1"],
                    "operator_action": "hold_authoritative_k1s_path",
                },
                "advisory_response": {
                    "provider": "hyperon-das-demo",
                    "status": "review",
                    "recommendation": (
                        "Diverge until F0/F1 blockers are cleared; advisory evidence "
                        "does not change k1s authority."
                    ),
                    "confidence": 0.74,
                    "evidence_refs": evidence_refs,
                    "authoritative": False,
                },
                "accepted": None,
                "divergence_reason": "pending_operator_review",
                "replay_status": "recorded",
                "continuity_signals": {
                    "request_id": request_id,
                    "das_query_trace_id": das_query_trace_id,
                    "facts_ref": facts_ref,
                    "result_ref": result_ref,
                },
                "coherence_signals": {
                    "model_ok": True,
                    "local_first": True,
                    "coherence_score": 0.91,
                    "overload_state": "nominal",
                },
            }
        ],
        "records": {
            "das_cell_bundles": [
                {
                    "bundle_id": bundle_id,
                    "site_id": "site-a",
                    "cell_id": "runtime",
                    "version": run_id,
                    "storage_ref": f"/srv/storage/k1s/ai-fabric-lab/das/{run_id}",
                    "facts_ref": facts_ref,
                    "status": "ready",
                    "labels": {
                        "workerbee_lab": "ai-fabric",
                        "demo": "fabric-advisory-review",
                    },
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            ],
            "das_query_traces": [
                {
                    "trace_id": das_query_trace_id,
                    "bundle_id": bundle_id,
                    "site_id": "site-a",
                    "query_id": request_id,
                    "query_kind": "advisory",
                    "local_first": True,
                    "warmed_refs": [facts_ref],
                    "promoted_refs": ["qdrant://ai_fabric_corpus/demo-review"],
                    "fallback_sites": [],
                    "result_ref": result_ref,
                    "created_at": created_at,
                }
            ],
            "cognitive_signals": [
                {
                    "signal_id": signal_id,
                    "subject_type": "das-cell",
                    "subject_id": bundle_id,
                    "signal_kind": "continuity",
                    "continuity_ref": result_ref,
                    "coherence_score": 0.91,
                    "overload_state": "nominal",
                    "review_gate": "operator_review",
                    "advisory_trace_id": trace_id,
                    "created_at": created_at,
                    "review_status": "pending",
                }
            ],
        },
    }


def post_json(url: str, payload: dict, *, token: str, timeout: float) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme for POST: {parsed.scheme or '<missing>'}")
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - scheme is validated above.
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed with HTTP {exc.code}: {detail}") from exc
    return json.loads(body or "{}")


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required executable not found: {name}")
    return path


def run_command(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)  # noqa: S603


def ensure_playwright(*, npm_root: Path, version: str) -> Path:
    npm = require_tool("npm")
    node = require_tool("node")
    package_dir = npm_root / "node_modules" / "playwright"
    npm_root.mkdir(parents=True, exist_ok=True)
    if not (npm_root / "package.json").exists():
        run_command([npm, "init", "-y"], cwd=npm_root)
    if not package_dir.exists():
        run_command([npm, "install", "--no-save", f"playwright@{version}"], cwd=npm_root)
    cli = package_dir / "cli.js"
    if not cli.exists():
        raise RuntimeError(f"Playwright CLI not found after install: {cli}")
    run_command([node, str(cli), "install", "chromium"])
    return npm_root / "node_modules"


PLAYWRIGHT_RECORDER = r"""
const fs = require('fs');
const path = require('path');
const { chromium } = require(path.join(process.env.PLAYWRIGHT_NODE_MODULES, 'playwright'));

const dashboardUrl = process.env.K1S_DASHBOARD_URL;
const traceId = process.env.K1S_DEMO_TRACE_ID;
const adminToken = process.env.K1S_ADMIN_TOKEN || '';
const artifactDir = process.env.K1S_DEMO_ARTIFACT_DIR;
const decision = process.env.K1S_DEMO_REVIEW_DECISION || 'diverge';
const reviewer = process.env.K1S_DEMO_REVIEWER || 'dashboard-operator';
const note = process.env.K1S_DEMO_REVIEW_NOTE || '';
const hostAddress = process.env.K1S_DEMO_HOST_ADDRESS || '127.0.0.1';
const stepDelayMs = Number.parseInt(process.env.K1S_DEMO_STEP_DELAY_MS || '1400', 10);

if (!dashboardUrl || !traceId || !artifactDir) {
  throw new Error('missing dashboard URL, trace id, or artifact dir');
}

fs.mkdirSync(artifactDir, { recursive: true });
fs.mkdirSync(path.join(artifactDir, 'video'), { recursive: true });

function screenshotName(step) {
  return path.join(artifactDir, step + '.png');
}

async function pause(page, multiplier = 1) {
  const delay = Number.isFinite(stepDelayMs) ? Math.max(0, stepDelayMs * multiplier) : 0;
  if (delay > 0) await page.waitForTimeout(delay);
}

function isIgnoredConsoleError(text) {
  return /net::ERR_NETWORK_CHANGED/.test(text) ||
    /document is sandboxed and lacks the 'allow-same-origin' flag/.test(text);
}

async function clickReviewButton(page) {
  await page.waitForFunction((id) => {
    return Array.from(document.querySelectorAll('.fabric-advisory-review-btn'))
      .some((btn) => btn.getAttribute('data-trace-id') === id);
  }, traceId, { timeout: 30000 });
  await page.evaluate((id) => {
    const btn = Array.from(document.querySelectorAll('.fabric-advisory-review-btn'))
      .find((candidate) => candidate.getAttribute('data-trace-id') === id);
    if (!btn) throw new Error('review button missing for ' + id);
    btn.scrollIntoView({ block: 'center', inline: 'nearest' });
    btn.click();
  }, traceId);
  await pause(page);
}

async function clickTab(page, tabId, stepName, screenshots, optional = false) {
  const clicked = await page.evaluate((id) => {
    const btn = Array.from(document.querySelectorAll('#fabric-advisory-review-tabs button'))
      .find((candidate) => candidate.getAttribute('data-fabric-review-tab') === id);
    if (!btn) return false;
    btn.click();
    return true;
  }, tabId);
  if (!clicked) {
    if (optional) return false;
    throw new Error('review tab missing: ' + tabId);
  }
  await pause(page);
  const file = screenshotName(stepName);
  await page.screenshot({ path: file, fullPage: false });
  screenshots.push(file);
  return true;
}

(async () => {
  const parsed = new URL(dashboardUrl);
  const browser = await chromium.launch({
    headless: true,
    args: [`--host-resolver-rules=MAP ${parsed.hostname} ${hostAddress}`],
  });
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 960 },
    recordVideo: {
      dir: path.join(artifactDir, 'video'),
      size: { width: 1440, height: 960 },
    },
  });
  if (adminToken) {
    await context.addInitScript((token) => {
      try {
        window.localStorage.setItem('ae_token', token);
        window.localStorage.removeItem('ae_token_disable_bootstrap');
      } catch (_) {}
    }, adminToken);
  }
  const page = await context.newPage();
  const consoleErrors = [];
  const consoleWarnings = [];
  const ignoredConsoleErrors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      if (isIgnoredConsoleError(msg.text())) {
        ignoredConsoleErrors.push(msg.text());
      } else {
        consoleErrors.push(msg.text());
      }
    }
    if (msg.type() === 'warning') consoleWarnings.push(msg.text());
  });
  page.on('pageerror', (err) => {
    if (isIgnoredConsoleError(err.message)) {
      ignoredConsoleErrors.push(err.message);
    } else {
      consoleErrors.push(err.message);
    }
  });

  const screenshots = [];
  let videoPath = null;
  let submitResult = null;
  try {
    await page.goto(dashboardUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForSelector('#fabric-advisory-section:not(.hidden)', { timeout: 30000 });
    await page.waitForFunction((id) => document.body.textContent.includes(id), traceId, {
      timeout: 30000,
    });
    await pause(page, 1.4);
    let file = screenshotName('01-dashboard-fabric-advisory');
    await page.screenshot({ path: file, fullPage: false });
    screenshots.push(file);

    await clickReviewButton(page);
    await page.waitForSelector('#fabric-advisory-review-modal:not(.hidden)', { timeout: 30000 });
    await page.waitForFunction(() => {
      const tabs = document.querySelector('#fabric-advisory-review-tabs');
      return tabs && tabs.textContent.includes('Request') && tabs.textContent.includes('Response');
    }, { timeout: 30000 });
    await pause(page);
    file = screenshotName('02-review-modal-trace');
    await page.screenshot({ path: file, fullPage: false });
    screenshots.push(file);

    await clickTab(page, 'request', '03-review-modal-request', screenshots);
    await clickTab(page, 'response', '04-review-modal-response', screenshots);
    const hasDas = await clickTab(page, 'das', '05-review-modal-das', screenshots, true);
    const hasSignals = await clickTab(
      page,
      'signals',
      '06-review-modal-signals',
      screenshots,
      true,
    );
    await clickTab(page, 'phase', '07-review-modal-f3-gate', screenshots);

    await page.fill('#fabric-advisory-review-reviewer', reviewer);
    await pause(page, 0.4);
    await page.fill('#fabric-advisory-review-operator-note', note);
    await pause(page, 0.8);
    const checkedSteps = await page.$$eval(
      '#fabric-advisory-review-checklist input[type=checkbox]',
      (inputs) => {
        inputs.forEach((input) => {
          if (!input.checked) input.click();
        });
        return inputs.map((input) => input.value);
      },
      );
    file = screenshotName('08-review-modal-ready-to-submit');
    await pause(page);
    await page.screenshot({ path: file, fullPage: false });
    screenshots.push(file);

    if (decision !== 'none') {
      const buttonId = decision === 'accept'
        ? '#fabric-advisory-review-accept'
        : '#fabric-advisory-review-diverge';
      await page.click(buttonId);
      await page.waitForFunction(() => {
        const noteEl = document.querySelector('#fabric-advisory-review-note');
        return noteEl && /Recorded (accept|diverge) review event/.test(noteEl.textContent || '');
      }, { timeout: 30000 });
      await pause(page, 1.2);
      file = screenshotName('09-review-modal-recorded');
      await page.screenshot({ path: file, fullPage: false });
      screenshots.push(file);
      submitResult = await page.evaluate(() => ({
        note: document.querySelector('#fabric-advisory-review-note')?.textContent || '',
        status: document.querySelector('#fabric-advisory-review-status')?.textContent || '',
      }));
    }

    const tabs = await page.$$eval(
      '#fabric-advisory-review-tabs button',
      (buttons) => buttons.map((button) => button.textContent.trim()),
    );
    const status = await page.$eval(
      '#fabric-advisory-review-status',
      (el) => el.textContent.trim(),
    );
    const video = page.video();
    await context.close();
    if (video) videoPath = await video.path();
    await browser.close();
    const summary = {
      ok: consoleErrors.length === 0,
      trace_id: traceId,
      decision,
      status,
      tabs,
      has_das_tab: hasDas,
      has_signals_tab: hasSignals,
      checked_steps: checkedSteps,
      screenshots,
      video: videoPath,
      submit_result: submitResult,
      console_errors: consoleErrors,
      console_warnings: consoleWarnings,
      ignored_console_errors: ignoredConsoleErrors,
    };
    fs.writeFileSync(
      path.join(artifactDir, 'playwright-summary.json'),
      JSON.stringify(summary, null, 2),
    );
    console.log(JSON.stringify(summary, null, 2));
  } catch (err) {
    const video = page.video();
    await context.close().catch(() => {});
    if (video) {
      try { videoPath = await video.path(); } catch (_) {}
    }
    await browser.close().catch(() => {});
    const summary = {
      ok: false,
      trace_id: traceId,
      decision,
      screenshots,
      video: videoPath,
      console_errors: consoleErrors.concat([err.message]),
      console_warnings: consoleWarnings,
      ignored_console_errors: ignoredConsoleErrors,
    };
    fs.writeFileSync(
      path.join(artifactDir, 'playwright-summary.json'),
      JSON.stringify(summary, null, 2),
    );
    console.error(JSON.stringify(summary, null, 2));
    process.exit(1);
  }
})();
"""


def run_playwright_recorder(
    *,
    node_modules: Path,
    artifact_dir: Path,
    dashboard_url: str,
    trace_id: str,
    admin_token: str,
    decision: str,
    reviewer: str,
    note: str,
    host_address: str,
    step_delay_ms: int,
) -> dict:
    node = require_tool("node")
    recorder = artifact_dir / "fabric-advisory-recorder.cjs"
    recorder.write_text(PLAYWRIGHT_RECORDER, encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PLAYWRIGHT_NODE_MODULES": str(node_modules),
            "K1S_DASHBOARD_URL": dashboard_url,
            "K1S_DEMO_TRACE_ID": trace_id,
            "K1S_ADMIN_TOKEN": admin_token,
            "K1S_DEMO_ARTIFACT_DIR": str(artifact_dir),
            "K1S_DEMO_REVIEW_DECISION": decision,
            "K1S_DEMO_REVIEWER": reviewer,
            "K1S_DEMO_REVIEW_NOTE": note,
            "K1S_DEMO_HOST_ADDRESS": host_address,
            "K1S_DEMO_STEP_DELAY_MS": str(step_delay_ms),
        }
    )
    completed = subprocess.run(  # noqa: S603
        [node, str(recorder)],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )
    summary_path = artifact_dir / "playwright-summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    else:
        summary = {
            "ok": False,
            "trace_id": trace_id,
            "decision": decision,
            "console_errors": [completed.stderr.strip() or "Playwright recorder failed"],
        }
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        summary.setdefault("console_errors", []).append(detail)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        raise RuntimeError(f"Playwright recorder failed: {summary_path}")
    return summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed and record a k1s Fabric Advisory trace review demo with Playwright."
    )
    parser.add_argument("--project", default=os.environ.get("WORKERBEE_PROJECT", ""))
    parser.add_argument(
        "--controller-url",
        default=os.environ.get("K1S_CONTROLLER_URL", DEFAULT_CONTROLLER_URL),
    )
    parser.add_argument("--dashboard-url", default=os.environ.get("K1S_DASHBOARD_URL", ""))
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".local" / "share" / "workerbee" / "projects",
    )
    parser.add_argument("--token-env-file", type=Path, default=None)
    parser.add_argument(
        "--sudo-stack-token",
        action="store_true",
        help=(
            "Use sudo -n to read the WorkerBee stack.json admin token when the "
            "stack is root-owned."
        ),
    )
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--npm-root", type=Path, default=DEFAULT_NPM_ROOT)
    parser.add_argument("--playwright-version", default=DEFAULT_PLAYWRIGHT_VERSION)
    parser.add_argument("--host-address", default="127.0.0.1")
    parser.add_argument("--step-delay-ms", type=int, default=1400)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument(
        "--review-decision",
        choices=("diverge", "accept", "none"),
        default="diverge",
        help="Use 'none' for a non-mutating modal walkthrough after optional seed import.",
    )
    parser.add_argument("--reviewer", default="dashboard-operator")
    parser.add_argument(
        "--note",
        default=(
            "Demo review: diverge until F0/F1 blockers are cleared; Hyperon/DAS "
            "evidence is advisory and k1s remains authoritative."
        ),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    project = args.project
    dashboard_url = args.dashboard_url
    if not dashboard_url:
        if not project:
            raise RuntimeError("--project or --dashboard-url is required")
        dashboard_url = dashboard_url_for_project(project)
    if not project:
        project = infer_project_from_dashboard_url(dashboard_url)

    run_id = args.run_id or utc_run_id()
    created_at = datetime.now(timezone.utc).isoformat()  # noqa: UP017
    artifact_dir = args.artifact_root / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    token_required = not args.skip_seed or args.review_decision != "none"
    admin_token = resolve_admin_token(
        project=project,
        state_root=args.state_root,
        token_env_file=args.token_env_file,
        sudo_stack_token=args.sudo_stack_token,
    )
    if token_required and not admin_token:
        raise RuntimeError(
            "admin token required; set K1S_ADMIN_TOKEN/AE_API_ADMIN_TOKEN or pass --token-env-file"
        )

    payload: dict | None = None
    import_result: dict | None = None
    trace_id = args.trace_id
    if not args.skip_seed:
        payload = build_demo_import_payload(run_id=run_id, created_at=created_at)
        trace_id = payload["decision_traces"][0]["trace_id"]
        import_url = args.controller_url.rstrip("/") + "/fabric/advisory/import"
        import_result = post_json(import_url, payload, token=admin_token, timeout=args.timeout)
        if not import_result.get("ok"):
            raise RuntimeError(f"fabric advisory import failed: {import_result}")
    elif not trace_id:
        raise RuntimeError("--trace-id is required when --skip-seed is used")

    node_modules = ensure_playwright(npm_root=args.npm_root, version=args.playwright_version)
    playwright_summary = run_playwright_recorder(
        node_modules=node_modules,
        artifact_dir=artifact_dir,
        dashboard_url=dashboard_url,
        trace_id=trace_id,
        admin_token=admin_token,
        decision=args.review_decision,
        reviewer=args.reviewer,
        note=args.note,
        host_address=args.host_address,
        step_delay_ms=args.step_delay_ms,
    )

    summary = {
        "ok": bool(playwright_summary.get("ok")),
        "project": project,
        "run_id": run_id,
        "trace_id": trace_id,
        "dashboard_url": dashboard_url,
        "controller_url": args.controller_url,
        "artifact_dir": str(artifact_dir),
        "review_decision": args.review_decision,
        "import_result": import_result,
        "playwright": playwright_summary,
    }
    (artifact_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(textwrap.dedent(f"fabric advisory demo recording failed: {exc}"), file=sys.stderr)
        raise SystemExit(1) from exc
