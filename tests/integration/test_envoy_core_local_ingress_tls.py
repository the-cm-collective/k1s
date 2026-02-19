import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

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
    while tls_port == http_port:
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
        tls_default_secret="demo-cert",
        tls_fallback=False,
        tls_fallback_cn="edge.local",
        tls_fallback_days=7,
        rathole_bind_addr="0.0.0.0:2333",
        rathole_default_token="dev",
        rathole_server_addr="127.0.0.1:2333",
        edge_local_addr="127.0.0.1:18081",
        reload_cmd=None,
    )

    image = os.getenv("AE_ENVOY_IMAGE", "envoyproxy/envoy:v1.29-latest")
    last_exc: Exception | None = None
    for attempt in range(2):
        ca_file, used_caddy = _setup_tls(tls_root, prefer_caddy=attempt == 0)
        renderer = EdgeCoreProxyRenderer(store, cfg)
        renderer.render()
        _ensure_admin_port_free(envoy_path)
        bundle_dir = tmp_path / "envoy-bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        container_root = "/work"
        envoy_path = _prepare_envoy_bundle(envoy_path, tls_root, bundle_dir, container_root)
        container = f"k1s-envoy-{uuid4().hex[:8]}"
        try:
            _run([engine, "rm", "-f", container], check=False)
            mount = _mount_arg(bundle_dir, container_root, engine)
            run_cmd = [
                engine,
                "run",
                "-d",
                "--name",
                container,
                "-v",
                mount,
                "--user",
                "0",
            ]
            if "podman" in engine:
                run_cmd.extend(
                    ["-p", f"{http_port}:{http_port}", "-p", f"{tls_port}:{tls_port}"]
                )
            else:
                run_cmd.extend(["--network", "host"])
            run_cmd.extend(
                [
                    image,
                    "envoy",
                    "-c",
                    f"{container_root}/envoy.yaml",
                    "--log-level",
                    "info",
                ]
            )
            _run(run_cmd)
            try:
                _wait_for_port("127.0.0.1", tls_port, timeout_s=10.0)
            except TimeoutError as exc:
                logs = _container_logs(engine, container)
                raise TimeoutError(f"{exc}\n\nEnvoy logs:\n{logs}") from exc
            last_exc = None
            for _ in range(5):
                try:
                    _tls_http_probe("127.0.0.1", tls_port, "demo.local", ca_file)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.5)
            if last_exc is None:
                break
            logs = _container_logs(engine, container)
            last_exc = AssertionError(
                f"TLS probe failed: {last_exc}\n\nEnvoy logs:\n{logs}"
            )
        except Exception as exc:
            last_exc = exc
        finally:
            _run([engine, "rm", "-f", container], check=False)
        if not used_caddy:
            break
    if last_exc is not None:
        raise last_exc


def _detect_engine() -> str | None:
    for key in ("AE_CONTAINER_CLI", "STACK_BIN"):
        val = os.getenv(key)
        if val:
            return val
    for cand in ("docker", "podman"):
        if shutil.which(cand):
            return cand
    return None


def _mount_arg(bundle_dir: Path, container_root: str, engine: str) -> str:
    suffix = ""
    if "podman" in engine:
        suffix = ":Z"
    return f"{bundle_dir}:{container_root}:ro{suffix}"


def _setup_tls(tls_root: Path, *, prefer_caddy: bool) -> tuple[Path, bool]:
    if tls_root.exists():
        shutil.rmtree(tls_root)
    tls_root.mkdir(parents=True, exist_ok=True)
    if prefer_caddy:
        ca_cert, ca_key = _find_caddy_ca()
        if ca_cert and ca_key:
            _write_signed_cert(tls_root, "demo-cert", "demo.local", ca_cert, ca_key)
            return ca_cert, True
    ca_file = _write_ca_and_signed_cert(tls_root, "demo-cert", "demo.local")
    return ca_file, False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_signed_cert(
    root: Path, name: str, cn: str, ca_cert: Path, ca_key: Path
) -> None:
    crt = root / f"{name}.crt"
    key = root / f"{name}.key"
    csr = root / f"{name}.csr"
    conf = root / f"{name}.cnf"
    conf.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name = req_distinguished_name",
                "prompt = no",
                "req_extensions = v3_req",
                "",
                "[req_distinguished_name]",
                f"CN = {cn}",
                "",
                "[v3_req]",
                f"subjectAltName = DNS:{cn}",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-config",
            str(conf),
        ],
        check=True,
        capture_output=True,
    )  # noqa: S603
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(crt),
            "-days",
            "1",
            "-sha256",
            "-extensions",
            "v3_req",
            "-extfile",
            str(conf),
        ],
        check=True,
        capture_output=True,
    )  # noqa: S603
    crt.chmod(0o644)
    key.chmod(0o644)


def _write_ca_and_signed_cert(root: Path, name: str, cn: str) -> Path:
    ca_crt = root / "ca.crt"
    ca_key = root / "ca.key"
    subprocess.run(
        [
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
            "/CN=k1s-local-ca",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_crt),
        ],
        check=True,
        capture_output=True,
    )  # noqa: S603
    ca_crt.chmod(0o644)
    ca_key.chmod(0o644)
    _write_signed_cert(root, name, cn, ca_crt, ca_key)
    return ca_crt


def _wait_for_port(host: str, port: int, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {host}:{port} did not open")


def _tls_http_probe(host: str, port: int, server_name: str, ca_file: Path) -> None:
    cmd = [
        "openssl",
        "s_client",
        "-connect",
        f"{host}:{port}",
        "-servername",
        server_name,
        "-CAfile",
        str(ca_file),
        "-brief",
        "-verify_return_error",
    ]
    cp = subprocess.run(
        cmd,
        input=b"",
        capture_output=True,
        timeout=5,
    )  # noqa: S603
    if cp.returncode != 0:
        out = (cp.stdout or b"")[-2000:] + (cp.stderr or b"")[-2000:]
        raise AssertionError(out.decode(errors="ignore"))


def _run(cmd: list[str], *, check: bool = True) -> None:
    subprocess.run(cmd, check=check, capture_output=True, text=True)  # noqa: S603


def _find_caddy_ca() -> tuple[Path | None, Path | None]:
    root = Path(__file__).resolve().parents[2]
    candidates = [
        (
            root / "state" / "caddy-data" / "pki" / "authorities" / "local" / "root.crt",
            root / "state" / "caddy-data" / "pki" / "authorities" / "local" / "root.key",
        ),
        (
            root / "state" / "caddy-data" / "caddy" / "pki" / "authorities" / "local" / "root.crt",
            root / "state" / "caddy-data" / "caddy" / "pki" / "authorities" / "local" / "root.key",
        ),
    ]
    for crt, key in candidates:
        if crt.exists() and key.exists():
            return crt, key
    return None, None


def _ensure_admin_port_free(envoy_path: Path) -> None:
    admin_port = 9901
    if _port_available(admin_port):
        return
    new_port = _free_port()
    lines = envoy_path.read_text(encoding="utf-8").splitlines()
    in_admin = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "admin:":
            in_admin = True
            continue
        if in_admin and stripped.startswith("port_value:"):
            prefix = line.split("port_value:")[0]
            lines[idx] = f"{prefix}port_value: {int(new_port)}"
            break
    envoy_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _container_logs(engine: str, name: str) -> str:
    try:
        cp = subprocess.run(
            [engine, "logs", name],
            check=False,
            capture_output=True,
            text=True,
        )
        return (cp.stdout or "")[-2000:] + (cp.stderr or "")[-2000:]
    except Exception:
        return ""


def _prepare_envoy_bundle(
    envoy_path: Path, host_tls_root: Path, bundle_dir: Path, container_root: str
) -> Path:
    tls_dst = bundle_dir / "tls"
    if tls_dst.exists():
        shutil.rmtree(tls_dst)
    shutil.copytree(host_tls_root, tls_dst)
    text = envoy_path.read_text(encoding="utf-8")
    text = text.replace(str(host_tls_root), f"{container_root}/tls")
    bundled_cfg = bundle_dir / "envoy.yaml"
    bundled_cfg.write_text(text, encoding="utf-8")
    return bundled_cfg
