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
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def run(cmd: list[str], env=None, cwd=None, timeout=20):
    env_all = os.environ.copy()
    if env:
        env_all.update(env)
    # Ensure local src is importable when spawning helper scripts
    env_all.setdefault("PYTHONPATH", "src")
    return subprocess.run(
        cmd, env=env_all, cwd=cwd, check=True, capture_output=True, text=True, timeout=timeout
    )


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
store.upsert_service("default--echo", "10.96.0.77", {{"8080": {{"port": 8080, "targetPort": 18080, "protocol": "TCP", "nodePort": 31080}}}})
store.upsert_service_endpoints("default--echo", [
    ServiceEndpoint(app_name="default--echo", port=8080, ip="10.0.0.21", target_port=18080, ready=True),
])
"""
    run(["python", "-c", script])


def _start_apishim(state_db: Path, apishim_db: Path):
    env = os.environ.copy()
    env.update(
        {
            "AE_APISHIM_ENABLE": "1",
            "AE_APISHIM_TOKEN": "test-token",
            "AE_APISHIM_DB": str(apishim_db),
            "AE_STATE_DB": str(state_db),
            "AE_APISHIM_RUNTIME": "stub",
            "PYTHONPATH": "src",
        }
    )
    proc = subprocess.Popen(
        [
            "python",
            "-m",
            "ae.apishim",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "8845",
            "--token",
            "test-token",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2)
    return proc


def _kubectl(args: list[str]):
    base = [
        "kubectl",
        "--server",
        "http://127.0.0.1:8845",
        "--token",
        "test-token",
        "--insecure-skip-tls-verify",
    ]
    return run(base + args)


def _service_get() -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:8845/api/v1/namespaces/default/services/echo-svc",
        headers={"Authorization": "Bearer test-token"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_apishim_persists_service_and_endpoints(state_db: Path, apishim_db: Path):
    _seed_state(state_db)
    proc1 = _start_apishim(state_db, apishim_db)
    try:
        # create Service through API (spec without clusterIP/nodePort)
        svc_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "echo-svc"},
            "spec": {
                "type": "LoadBalancer",
                "selector": {"app": "echo"},
                "ports": [{"port": 8080, "targetPort": 18080}],
            },
        }
        req = urllib.request.Request(
            "http://127.0.0.1:8845/api/v1/namespaces/default/services",
            data=json.dumps(svc_manifest).encode(),
            method="POST",
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status in {200, 201}
        except urllib.error.HTTPError as exc:  # pragma: no cover - surface helpful error
            raise AssertionError(f"service create failed: {exc.read().decode()}") from exc
            svc = _service_get()
            assert svc["spec"]["clusterIP"] == "10.96.0.77"
            assert svc["spec"]["ports"][0]["nodePort"] == 31080
            assert svc["status"]["loadBalancer"]["ingress"][0]["ip"] == "10.96.0.77"
            from ae.controller.state import SQLiteStateStore

            s = SQLiteStateStore(state_db)
            eps = s.list_service_endpoints("default--echo")
            assert eps and eps[0].ip == "10.0.0.21"
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
        from ae.controller.state import SQLiteStateStore

        s = SQLiteStateStore(state_db)
        eps = s.list_service_endpoints("default--echo")
        assert eps and eps[0].ip == "10.0.0.21"
    finally:
        proc2.kill()
        proc2.wait(timeout=5)


# ruff: noqa: E501,S603,S607,S310
