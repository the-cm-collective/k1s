import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ae.apishim.store import ObjectStore
from ae.storage.state import ApishimHttpStorageState, ApishimStorageState
from ae.storage.types import PvcRef


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def test_apishim_storage_state_decodes_secret_data(tmp_path) -> None:
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "secrets",
        "demo",
        "creds",
        {"name": "creds", "namespace": "demo"},
        {"username": _b64("user"), "password": _b64("pass")},
        status={},
    )
    state = ApishimStorageState(store)
    secret = state.get_secret("demo", "creds")
    assert secret == {"username": "user", "password": "pass"}


def test_apishim_storage_state_reads_configmap(tmp_path) -> None:
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "demo",
        "app-config",
        {"name": "app-config", "namespace": "demo"},
        {"MODE": "auto", "LOG_LEVEL": "debug"},
        status={},
    )
    state = ApishimStorageState(store)
    cfg = state.get_config_map("demo", "app-config")
    assert cfg == {"MODE": "auto", "LOG_LEVEL": "debug"}


def _start_http_state_server(responses: dict[str, dict]) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = responses.get(self.path)
            if payload is None:
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
    return server


def test_apishim_http_storage_state_reads_secret_and_pv() -> None:
    responses = {
        "/api/v1/namespaces/demo/secrets/creds": {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "creds", "namespace": "demo"},
            "data": {"username": _b64("user"), "password": _b64("pass")},
        },
        "/api/v1/namespaces/demo/persistentvolumeclaims/claim": {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "claim", "namespace": "demo"},
            "spec": {"volumeName": "pv-demo"},
        },
        "/api/v1/persistentvolumes/pv-demo": {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": "pv-demo", "uid": "pv-uid"},
            "spec": {"nfs": {"server": "127.0.0.1", "path": "/exports/netfs"}},
        },
    }
    server = _start_http_state_server(responses)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        state = ApishimHttpStorageState(base)
        secret = state.get_secret("demo", "creds")
        assert secret == {"username": "user", "password": "pass"}
        pv = state.get_pv_for_pvc(PvcRef(name="claim", namespace="demo"))
        assert pv is not None
        assert pv.name == "pv-demo"
        assert pv.driver == "k1s.io/nfs"
        assert pv.uid == "pv-uid"
    finally:
        server.shutdown()


def test_apishim_http_storage_state_reads_configmap() -> None:
    responses = {
        "/api/v1/namespaces/demo/configmaps/app-config": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "app-config", "namespace": "demo"},
            "data": {"MODE": "auto", "LOG_LEVEL": "debug"},
        }
    }
    server = _start_http_state_server(responses)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        state = ApishimHttpStorageState(base)
        cfg = state.get_config_map("demo", "app-config")
        assert cfg == {"MODE": "auto", "LOG_LEVEL": "debug"}
    finally:
        server.shutdown()
