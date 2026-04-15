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
SCRIPT = ROOT / "scripts" / "dev" / "ha_transport_upgrade.py"


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


def _node_payloads(*, name: str, version: str, commit: str, leader: str, route_count: int) -> dict[str, dict]:
    if name != leader:
        replica_name = name
    else:
        replica_name = "hub-b" if name != "hub-b" else "hub-a"
    return {
        "/varz": {
            "server_name": name,
            "server_id": f"id-{name}",
            "version": version,
            "git_commit": commit,
            "cluster": {"name": "k1s-hub"},
            "jetstream": {"config": {"domain": "K1S"}},
        },
        "/routez": {
            "num_routes": route_count,
            "routes": [{"remote_name": "peer-a"} for _ in range(route_count)],
        },
        "/jsz?streams=true&consumers=true&config=true": {
            "meta_cluster": {"leader": leader},
            "streams": [
                {
                    "config": {"name": "K1S_WORK"},
                    "cluster": {"leader": leader, "replicas": [{"name": replica_name}]},
                    "consumer_detail": [
                        {
                            "config": {"durable_name": "WORK_SITE_sea"},
                            "cluster": {"leader": leader, "replicas": [{"name": replica_name}]},
                        }
                    ],
                }
            ],
        },
        "/leafz": {"num_leafs": 1},
    }


def _metrics_text() -> str:
    return "\n".join(
        [
            "ae_controller_authority_healthy 1",
            'ae_gateway_result_replay_backlog{site="sea"} 0',
            'ae_route_bundle_ack_age_seconds{site="sea"} 0',
            'ae_site_stale{site="sea"} 0',
            'ae_js_consumer_pending{stream="K1S_WORK",consumer="WORK_SITE_sea",site="sea"} 0',
        ]
    )


def test_precheck_dry_run_prints_thresholds() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "precheck",
            "--node",
            "hub-a=http://127.0.0.1:8222",
            "--controller-metrics-url",
            "http://127.0.0.1:9108/metrics",
            "--expected-replicas",
            "2",
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
    assert "replicas=2" in text
    assert "backlog_threshold=1.0" in text
    assert "ack_age_threshold=9.0" in text


def test_node_plan_prints_meta_leader_last_commands() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "node-plan",
            "--node-name",
            "hub-a",
            "--monitor-url",
            "http://hub-a:8222",
            "--controller-metrics-url",
            "http://core-a:9108/metrics",
            "--expected-version",
            "2.10.18",
            "--expected-commit",
            "sha-target",
            "--meta-leader",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout
    assert "Hub transport rolling upgrade plan for node hub-a (meta leader)" in text
    assert "Confirm all non-meta-leader hub nodes already report the target build" in text
    assert "curl -fsS http://hub-a:8222/varz" in text
    assert "Expected NATS build: version=2.10.18 commit=sha-target" in text


def test_cluster_verify_accepts_two_build_window() -> None:
    with _serve_endpoints(_node_payloads(name="hub-a", version="2.10.17", commit="sha-old", leader="hub-a", route_count=1)) as hub_a, _serve_endpoints(
        _node_payloads(name="hub-b", version="2.10.18", commit="sha-target", leader="hub-a", route_count=1)
    ) as hub_b, _serve_endpoints({"/metrics": _metrics_text()}) as metrics:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "cluster-verify",
                "--node",
                f"hub-a={hub_a}",
                "--node",
                f"hub-b={hub_b}",
                "--controller-metrics-url",
                f"{metrics}/metrics",
                "--expected-version",
                "2.10.18",
                "--expected-commit",
                "sha-target",
                "--expected-replicas",
                "2",
            ],
            check=True,
            text=True,
            capture_output=True,
        )

    text = proc.stdout
    assert "hub-a: version=2.10.17 commit=sha-old routes=1 meta_leader=hub-a" in text
    assert "hub-b: version=2.10.18 commit=sha-target routes=1 meta_leader=hub-a" in text
    assert "hub transport cluster verify ok: nodes=2 distinct_builds=2 expected_consumers=1" in text


def test_precheck_rejects_route_mesh_loss() -> None:
    with _serve_endpoints(_node_payloads(name="hub-a", version="2.10.18", commit="sha-a", leader="hub-a", route_count=1)) as hub_a, _serve_endpoints(
        _node_payloads(name="hub-b", version="2.10.18", commit="sha-a", leader="hub-a", route_count=0)
    ) as hub_b, _serve_endpoints({"/metrics": _metrics_text()}) as metrics:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "precheck",
                "--node",
                f"hub-a={hub_a}",
                "--node",
                f"hub-b={hub_b}",
                "--controller-metrics-url",
                f"{metrics}/metrics",
                "--expected-replicas",
                "2",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    assert proc.returncode != 0
    assert "route_mesh:hub-b:0/1" in proc.stderr


def test_member_replace_plan_prints_checklist() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "member-replace-plan",
            "--failed-node",
            "hub-b",
            "--replacement-node",
            "hub-d",
            "--replacement-monitor-url",
            "http://hub-d:8222",
            "--controller-metrics-url",
            "http://core-a:9108/metrics",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout
    assert "Hub transport member replacement plan" in text
    assert "Confirm the failed node is isolated: hub-b" in text
    assert "curl -fsS http://hub-d:8222/varz" in text
    assert "Non-goals: this helper does not generate NATS configs" in text
