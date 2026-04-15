from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "ha_edge_transport.py"


@contextmanager
def _serve_endpoints(payloads: dict[str, dict | str]):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = urlsplit(self.path).path
            key = self.path if self.path in payloads else path
            payload = payloads.get(key)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            if isinstance(payload, str):
                data = payload.encode("utf-8")
                content_type = "text/plain"
            else:
                data = json.dumps(payload).encode("utf-8")
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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


def _edge_payloads(*, version: str = "2.10.18", commit: str = "sha-edge", leaf_count: int = 1) -> dict[str, dict]:
    return {
        "/varz": {
            "server_name": "edge-sea",
            "server_id": "srv-edge-sea",
            "version": version,
            "git_commit": commit,
        },
        "/leafz": {"num_leafs": leaf_count},
    }


def _metrics_text(
    *,
    site: str = "sea",
    stale: float = 0.0,
    backlog: float = 0.0,
    ack_age: float = 0.0,
    gateways: dict[str, tuple[float, str, str, str]] | None = None,
) -> str:
    gateways = gateways or {
        "edge-1": (5.0, "0.1.3.dev0", "sha-a", "2026-03-18"),
        "edge-2": (7.0, "0.1.3.dev0", "sha-a", "2026-03-18"),
    }
    lines = [
        "ae_controller_authority_healthy 1",
        f'ae_site_stale{{site="{site}"}} {stale}',
        f'ae_gateway_result_replay_backlog{{site="{site}"}} {backlog}',
        f'ae_route_bundle_ack_age_seconds{{site="{site}"}} {ack_age}',
    ]
    for node_id, (last_seen, version, sha, date) in gateways.items():
        lines.append(f'ae_site_gateway_last_seen_seconds{{site="{site}",node="{node_id}"}} {last_seen}')
        lines.append(
            f'ae_site_gateway_build_info{{site="{site}",node="{node_id}",version="{version}",sha="{sha}",date="{date}"}} 1'
        )
    return "\n".join(lines)


def test_precheck_dry_run_prints_thresholds() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "precheck",
            "--site",
            "sea=http://127.0.0.1:8223",
            "--controller-metrics-url",
            "http://127.0.0.1:9108/metrics",
            "--expected-gateway",
            "edge-1",
            "--gateway-last-seen-threshold",
            "30",
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
    assert "expected_gateways=1" in text
    assert "gateway_last_seen_threshold=30.0" in text
    assert "backlog_threshold=1.0" in text
    assert "ack_age_threshold=9.0" in text


def test_precheck_rejects_missing_leaf_connectivity() -> None:
    with _serve_endpoints(_edge_payloads(leaf_count=0)) as edge, _serve_endpoints({"/metrics": _metrics_text(gateways={"edge-1": (5.0, "0.1.3.dev0", "sha-a", "2026-03-18")})}) as metrics:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "precheck",
                "--site",
                f"sea={edge}",
                "--controller-metrics-url",
                f"{metrics}/metrics",
                "--expected-gateway",
                "edge-1",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    assert proc.returncode != 0
    assert "leaf_count:sea:0/1" in proc.stderr


def test_site_verify_accepts_two_gateway_build_window() -> None:
    gateways = {
        "edge-1": (5.0, "0.1.3.dev0", "sha-old", "2026-03-18"),
        "edge-2": (6.0, "0.1.3.dev1", "sha-target", "2026-03-19"),
    }
    with _serve_endpoints(_edge_payloads(version="2.10.18", commit="sha-edge", leaf_count=1)) as edge, _serve_endpoints(
        {"/metrics": _metrics_text(gateways=gateways)}
    ) as metrics:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "site-verify",
                "--site",
                f"sea={edge}",
                "--controller-metrics-url",
                f"{metrics}/metrics",
                "--expected-gateway",
                "edge-1",
                "--expected-gateway",
                "edge-2",
                "--expected-edge-version",
                "2.10.18",
                "--expected-edge-commit",
                "sha-edge",
                "--expected-gateway-version",
                "0.1.3.dev1",
                "--expected-gateway-sha",
                "sha-target",
            ],
            check=True,
            text=True,
            capture_output=True,
        )

    text = proc.stdout
    assert "edge-site=sea: version=2.10.18 commit=sha-edge leaf_count=1" in text
    assert "edge-1: last_seen=5.0 version=0.1.3.dev0 sha=sha-old" in text
    assert "edge-2: last_seen=6.0 version=0.1.3.dev1 sha=sha-target" in text
    assert "edge transport site verify ok: site=sea gateways=2 distinct_gateway_builds=2" in text


def test_gateway_plan_prints_one_gateway_at_a_time_guidance() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "gateway-plan",
            "--site-id",
            "sea",
            "--gateway-node",
            "edge-1",
            "--controller-metrics-url",
            "http://core-a:9108/metrics",
            "--expected-version",
            "0.1.3.dev1",
            "--expected-sha",
            "sha-target",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout
    assert "Edge gateway rolling plan for site sea gateway edge-1" in text
    assert "Restart one gateway at a time" in text
    assert 'ae_site_gateway_last_seen_seconds{site="sea",node="edge-1"}' in text
    assert "Expected gateway build: version=0.1.3.dev1 sha=sha-target" in text


def test_leader_replace_plan_prints_checklist() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "leader-replace-plan",
            "--site-id",
            "sea",
            "--failed-node",
            "edge-nats-a",
            "--replacement-node",
            "edge-nats-b",
            "--replacement-monitor-url",
            "http://edge-nats-b:8223",
            "--controller-metrics-url",
            "http://core-a:9108/metrics",
            "--expected-gateway",
            "edge-1",
            "--expected-gateway",
            "edge-2",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout
    assert "Edge transport leader replacement plan for site sea" in text
    assert "Confirm the failed edge leader is isolated: edge-nats-a" in text
    assert "curl -fsS http://edge-nats-b:8223/varz" in text
    assert "Confirm expected gateways are visible again: edge-1, edge-2" in text
    assert "Non-goals: this helper does not install edge services" in text
