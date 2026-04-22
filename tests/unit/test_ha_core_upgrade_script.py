from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "ha_core_upgrade.py"


@contextmanager
def _serve_json(payload: dict, *, metrics_text: str | None = None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/__ae/version":
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/metrics" and metrics_text is not None:
                data = metrics_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_precheck_dry_run_prints_thresholds() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "precheck",
            "--metrics-url",
            "http://127.0.0.1:9108/metrics",
            "--backlog-threshold",
            "1",
            "--ack-age-threshold",
            "9",
            "--dry-run",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout.strip()
    assert "DRY RUN precheck" in text
    assert "backlog_threshold=1.0" in text
    assert "ack_age_threshold=9.0" in text


def test_node_plan_prints_leader_last_commands() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "node-plan",
            "--node-name",
            "core-a",
            "--service",
            "ae-ha-core.service",
            "--controller-url",
            "http://core-a:9108",
            "--apishim-url",
            "https://core-a:8445",
            "--expected-version",
            "0.1.3.dev0",
            "--expected-sha",
            "sha-123",
            "--leader",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout
    assert "Rolling upgrade plan for node core-a (leader)" in text
    assert "systemctl stop ae-ha-core.service" in text
    assert "curl -fsS http://core-a:9108/__ae/version" in text
    assert "curl -fsSk https://core-a:8445/__ae/version" in text
    assert "Expected build: version=0.1.3.dev0 sha=sha-123" in text


def test_cluster_verify_accepts_two_build_window() -> None:
    metrics_text = "\n".join(
        [
            "ae_controller_authority_healthy 1",
            "ae_controller_build_info{version=\"0.1.3.dev0\",sha=\"sha-target\",date=\"2026-03-18\"} 1",
        ]
    )
    with _serve_json(
        {"component": "controller", "version": "0.1.2.dev0", "sha": "sha-old", "date": "2026-03-17"},
        metrics_text=metrics_text,
    ) as ctrl_old, _serve_json(
        {"component": "apishim", "version": "0.1.2.dev0", "sha": "sha-old", "date": "2026-03-17"}
    ) as api_old, _serve_json(
        {"component": "controller", "version": "0.1.3.dev0", "sha": "sha-target", "date": "2026-03-18"},
        metrics_text=metrics_text,
    ) as ctrl_new, _serve_json(
        {"component": "apishim", "version": "0.1.3.dev0", "sha": "sha-target", "date": "2026-03-18"}
    ) as api_new:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "cluster-verify",
                "--node",
                f"core-a={ctrl_old},{api_old}",
                "--node",
                f"core-b={ctrl_new},{api_new}",
                "--expected-version",
                "0.1.3.dev0",
                "--expected-sha",
                "sha-target",
            ],
            check=True,
            text=True,
            capture_output=True,
        )

    text = proc.stdout
    assert "core-a: version=0.1.2.dev0 sha=sha-old authority_healthy=1" in text
    assert "core-b: version=0.1.3.dev0 sha=sha-target authority_healthy=1" in text
    assert "cluster verify ok: nodes=2 distinct_builds=2" in text
