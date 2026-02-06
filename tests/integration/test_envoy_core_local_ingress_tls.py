import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4
import ssl

import pytest

from ae.controller.spec import app_key
from ae.controller.state import ServiceEndpoint, SQLiteStateStore
from ae.ingress.edge_core_proxy import EdgeCoreProxyConfig, EdgeCoreProxyRenderer


@pytest.mark.integration
def test_envoy_core_local_tls_handshake(tmp_path: Path) -> None:
    if os.getenv("AE_E2E_ENVOY_TLS", "0") != "1":
        pytest.skip("set AE_E2E_ENVOY_TLS=1 to run envoy TLS handshake test")
    engine = _detect_engine()
    if engine is None:
        pytest.skip("docker/podman not available")
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")

    app_name = app_key("demo", "default")
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.upsert_service_endpoints(
        app_name,
        [
            ServiceEndpoint(
                app_name=app_name,
                port=8080,
                ip="127.0.0.1",
                target_port=8080,
                ready=True,
            )
        ],
    )

    tls_root = tmp_path / "tls"
    tls_root.mkdir(parents=True, exist_ok=True)
    _write_self_signed_cert(tls_root, "demo-cert", "demo.local")

    store.upsert_edge_ingress_route(
        name="demo-ingress",
        namespace="default",
        site_id="core",
        policy_name=None,
        policy_namespace=None,
        document={
            "host": "demo.local",
            "paths": [
                {
                    "path": "/",
                    "serviceRef": {"name": "demo", "namespace": "default", "port": 8080},
                }
            ],
            "exposure": {
                "mode": "core-local",
                "tls": {
                    "mode": "terminate-core",
                    "terminateCore": {
                        "secretName": "demo-cert",
                        "redirectHttpToHttps": True,
                    },
                },
            },
        },
    )

    http_port = _free_port()
    tls_port = _free_port()
    config_dir = tmp_path / "edge-ingress"
    envoy_path = config_dir / "envoy.yaml"
    cfg = EdgeCoreProxyConfig(
        config_dir=config_dir,
        envoy_config_path=envoy_path,
        rathole_server_path=config_dir / "rathole-server.toml",
        rathole_client_dir=None,
        site_domain_suffix="edge.local",
        http_listen_port=http_port,
        tls_listen_port=tls_port,
        tls_root=tls_root,
        tls_default_secret=None,
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )

    renderer = EdgeCoreProxyRenderer(store, cfg)
    renderer.render()

    image = os.getenv("AE_ENVOY_IMAGE", "envoyproxy/envoy:v1.29-latest")
    container = f"k1s-envoy-{uuid4().hex[:8]}"
    try:
        _run([engine, "rm", "-f", container], check=False)
        _run(
            [
                engine,
                "run",
                "-d",
                "--name",
                container,
                "--network",
                "host",
                "-v",
                f"{envoy_path}:/etc/envoy/envoy.yaml:ro",
                image,
                "-c",
                "/etc/envoy/envoy.yaml",
                "--log-level",
                "info",
            ]
        )
        _wait_for_port("127.0.0.1", tls_port, timeout_s=10.0)
        _tls_http_probe("127.0.0.1", tls_port, "demo.local")
    finally:
        _run([engine, "rm", "-f", container], check=False)


def _detect_engine() -> str | None:
    for key in ("AE_CONTAINER_CLI", "STACK_BIN"):
        val = os.getenv(key)
        if val:
            return val
    for cand in ("docker", "podman"):
        if shutil.which(cand):
            return cand
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_self_signed_cert(root: Path, name: str, cn: str) -> None:
    crt = root / f"{name}.crt"
    key = root / f"{name}.key"
    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-days",
        "1",
        "-nodes",
        "-subj",
        f"/CN={cn}",
        "-keyout",
        str(key),
        "-out",
        str(crt),
        "-addext",
        f"subjectAltName=DNS:{cn}",
    ]
    subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603
    crt.chmod(0o600)
    key.chmod(0o600)


def _wait_for_port(host: str, port: int, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {host}:{port} did not open")


def _tls_http_probe(host: str, port: int, server_name: str) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=2.0) as sock:
        with ctx.wrap_socket(sock, server_hostname=server_name) as ssock:
            ssock.sendall(
                b"GET / HTTP/1.1\r\nHost: demo.local\r\nConnection: close\r\n\r\n"
            )
            data = ssock.recv(64)
            assert data.startswith(b"HTTP/")


def _run(cmd: list[str], *, check: bool = True) -> None:
    subprocess.run(cmd, check=check, capture_output=True, text=True)  # noqa: S603
