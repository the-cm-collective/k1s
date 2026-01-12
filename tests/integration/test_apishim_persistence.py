"""
Integration-ish check: apishim should preserve ClusterIP/NodePort/loadBalancer
status and EndpointSlice projections across restart when using the same
controller SQLite state.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


def run(cmd: list[str], env=None, cwd=None, timeout=20):
    env_all = os.environ.copy()
    if env:
        env_all.update(env)
    return subprocess.run(cmd, env=env_all, cwd=cwd, check=True, capture_output=True, text=True, timeout=timeout)


@pytest.fixture()
def state_db(tmp_path: Path) -> Path:
    return tmp_path / "ctrl.db"


@pytest.fixture()
def apishim_db(tmp_path: Path) -> Path:
    return tmp_path / "apishim.db"


def _seed_state(state_db: Path):
    script = f"""
from ae.controller.state import SQLiteStateStore, ServiceEndpoint
store = SQLiteStateStore("{state_db}")
store.upsert_service("default--echo-svc", "10.96.0.77", {{"8080": {{"port": 8080, "targetPort": 18080, "protocol": "TCP", "nodePort": 31080}}}})
store.upsert_service_endpoints("default--echo-svc", [
    ServiceEndpoint(app_name="default--echo-svc", port=8080, ip="10.0.0.21", target_port=18080, ready=True),
])
"""
    run(["python", "-c", script])


def _start_apishim(state_db: Path, apishim_db: Path):
    env = {
        "AE_APISHIM_ENABLE": "1",
        "AE_APISHIM_TOKEN": "test-token",
        "AE_APISHIM_DB": str(apishim_db),
        "AE_STATE_DB": str(state_db),
        "AE_APISHIM_RUNTIME": "stub",
        "PYTHONPATH": "src",
    }
    proc = subprocess.Popen(
        ["python", "-m", "ae.apishim", "serve", "--host", "127.0.0.1", "--port", "8845", "--token", "test-token"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2)
    return proc


def _kubectl(args: list[str]):
    base = ["kubectl", "--server", "http://127.0.0.1:8845", "--token", "test-token", "--insecure-skip-tls-verify"]
    return run(base + args)


def _service_get() -> dict:
    out = _kubectl(["get", "svc", "echo-svc", "-o", "json"]).stdout
    return json.loads(out)


def _endpointslice_get() -> dict:
    out = _kubectl(["get", "endpointslice", "-l", "kubernetes.io/service-name=echo-svc", "-o", "json"]).stdout
    return json.loads(out)


def test_apishim_persists_service_and_endpoints(state_db: Path, apishim_db: Path):
    _seed_state(state_db)
    proc1 = _start_apishim(state_db, apishim_db)
    try:
        # create Service through API (spec without clusterIP/nodePort)
        svc_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "echo-svc"},
            "spec": {"selector": {"app": "echo"}, "ports": [{"port": 8080, "targetPort": 18080}]},
        }
        p = subprocess.run(
            ["kubectl", "--server", "http://127.0.0.1:8845", "--token", "test-token", "--insecure-skip-tls-verify", "apply", "-f", "-"],
            input=json.dumps(svc_manifest),
            text=True,
            capture_output=True,
            env=os.environ.copy(),
        )
        assert p.returncode == 0, p.stderr
        svc = _service_get()
        assert svc["spec"]["clusterIP"] == "10.96.0.77"
        assert svc["spec"]["ports"][0]["nodePort"] == 31080
        assert svc["status"]["loadBalancer"]["ingress"][0]["ip"] == "10.96.0.77"
        eps = _endpointslice_get()
        assert eps["items"][0]["endpoints"][0]["addresses"][0] == "10.0.0.21"
    finally:
        proc1.kill()
        proc1.wait(timeout=5)

    # restart apishim, ensure data remains consistent
    proc2 = _start_apishim(state_db, apishim_db)
    try:
        svc = _service_get()
        assert svc["spec"]["clusterIP"] == "10.96.0.77"
        assert svc["spec"]["ports"][0]["nodePort"] == 31080
        assert svc["status"]["loadBalancer"]["ingress"][0]["ip"] == "10.96.0.77"
        eps = _endpointslice_get()
        assert eps["items"][0]["endpoints"][0]["addresses"][0] == "10.0.0.21"
    finally:
        proc2.kill()
        proc2.wait(timeout=5)
