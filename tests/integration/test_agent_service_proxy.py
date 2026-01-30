"""Integration-ish test for node service proxy snapshot ingestion."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ae.controller.state import SQLiteStateStore
from ae.node.server import _start_service_proxy_loop


def _start_services_api(payload: list[dict]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/services":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _fmt, *_args):  # noqa: ANN001
            return

    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("listener sockets not permitted in sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_agent_service_proxy_ingests_services(tmp_path, monkeypatch):
    services = [
        {
            "app": "echo",
            "cluster_ip": "10.241.0.10",
            "ports": {
                "ports": [
                    {
                        "name": "http",
                        "port": 8080,
                        "targetPort": 8080,
                        "protocol": "TCP",
                        "nodePort": 32080,
                    }
                ]
            },
            "endpoints": [
                {
                    "port": 8080,
                    "ip": "10.1.1.10",
                    "target_port": 8080,
                    "ready": True,
                }
            ],
        }
    ]
    server, thread = _start_services_api(services)
    try:
        monkeypatch.setenv("AE_AGENT_SERVICE_PROXY", "1")
        monkeypatch.setenv("AE_AGENT_SERVICE_PROXY_INTERVAL", "1")
        monkeypatch.setenv("AE_IPTABLES_BIN", "missing-iptables")

        db_path = Path(tmp_path) / "agent-services.db"
        _start_service_proxy_loop(
            controller_url=f"http://127.0.0.1:{server.server_port}",
            interval=1,
            state_db=str(db_path),
        )

        # Wait for the proxy loop to ingest services.
        deadline = time.time() + 5
        while time.time() < deadline:
            store = SQLiteStateStore(db_path)
            svc_items = store.list_services()
            if svc_items:
                endpoints = store.list_service_endpoints("echo")
                assert endpoints
                assert endpoints[0].ip == "10.1.1.10"
                assert endpoints[0].port == 8080
                return
            time.sleep(0.1)
        pytest.fail("service proxy did not ingest services in time")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
