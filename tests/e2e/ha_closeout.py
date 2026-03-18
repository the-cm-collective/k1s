from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from ae.controller.etcd_state import EtcdStateStore
from ae.controller.node_identity import scoped_node_id
from ae.ha.ops import read_etcd_leader
from tests.e2e.core_edge import (
    _repo_root,
    _run,
    _start_proc,
    _terminate,
    _wait_etcd,
    _wait_nats_leaf,
    _wait_node_ready,
    _wait_tcp,
    _wait_work_state,
    _write_compose,
)


def _wait_http_ok(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if 200 <= int(resp.status) < 500:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"timeout waiting for {url}")


def _wait_leader(
    endpoints: list[str],
    prefix: str,
    *,
    timeout_s: float = 45.0,
    min_epoch: int = 0,
    exclude_controller_id: str | None = None,
):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        leader = read_etcd_leader(endpoints, prefix, timeout_s=3.0)
        if leader is None:
            time.sleep(0.5)
            continue
        if leader.controller_epoch <= min_epoch:
            time.sleep(0.5)
            continue
        if exclude_controller_id and leader.controller_id == exclude_controller_id:
            time.sleep(0.5)
            continue
        return leader
    raise RuntimeError("timeout waiting for controller leader")


def _apishim_request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
) -> dict:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = resp.read().decode("utf-8")
    return json.loads(payload or "{}")


def _create_namespace(apishim_base: str, name: str) -> dict:
    return _apishim_request(
        f"{apishim_base}/api/v1/namespaces/{name}",
        method="PUT",
        body={
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": name},
        },
    )


def run_ha_closeout_e2e() -> int:
    root = _repo_root()
    run_id = uuid.uuid4().hex[:8]
    base_dir = Path(
        os.getenv("K1S_E2E_HA_DIR", root / ".local" / f"e2e-ha-closeout-{run_id}")
    ).resolve()
    state_dir = base_dir / "state"
    logs_dir = base_dir / "logs"
    compose_path = base_dir / "compose.yaml"
    state_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    print(f"[ha-e2e] workspace={base_dir}")
    _write_compose(compose_path, root, state_dir)
    compose_cmd = ["docker", "compose", "-f", str(compose_path)]
    subprocess.run([*compose_cmd, "up", "-d"], check=True)

    controller_a = controller_b = gateway = worker = apishim = None
    try:
        _wait_tcp("127.0.0.1", 2379, timeout_s=40)
        _wait_tcp("127.0.0.1", 4222, timeout_s=40)
        _wait_tcp("127.0.0.1", 4223, timeout_s=40)
        _wait_etcd("http://127.0.0.1:2379", timeout_s=40)
        _wait_nats_leaf("http://127.0.0.1:8222/leafz", timeout_s=40)

        etcd_prefix = f"k1s/e2e/ha-closeout/{run_id}"
        endpoints = ["http://127.0.0.1:2379"]
        env_base = os.environ.copy()
        env_base.update(
            {
                "AE_HA_MODE": "1",
                "AE_STATE_BACKEND": "etcd",
                "AE_ETCD_ENDPOINTS": endpoints[0],
                "AE_ETCD_PREFIX": etcd_prefix,
                "AE_SITE_ID": "core",
                "AE_TRANSPORT_BACKEND": "nats-js",
                "AE_NATS_URL": "nats://hub-controller:dev@127.0.0.1:4222",
                "AE_JS_DOMAIN": "K1S",
                "AE_OUTBOX_PUBLISH_INTERVAL_S": "0.2",
                "AE_PROJECTION_ROOT": str(base_dir / "projections"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        os.environ.update(
            {
                "AE_ETCD_ENDPOINTS": endpoints[0],
                "AE_ETCD_PREFIX": etcd_prefix,
                "AE_SITE_ID": "core",
            }
        )
        store = EtcdStateStore()

        controller_a_env = dict(env_base)
        controller_a_env.update(
            {
                "AE_CONTROLLER_ID": "core-a",
                "AE_CONTROLLER_ADVERTISE_ADDR": "http://127.0.0.1:9108",
            }
        )
        controller_b_env = dict(env_base)
        controller_b_env.update(
            {
                "AE_CONTROLLER_ID": "core-b",
                "AE_CONTROLLER_ADVERTISE_ADDR": "http://127.0.0.1:9109",
            }
        )
        controller_a = _start_proc(
            [sys.executable, "-m", "ae.controller", "--loop", "--interval", "2", "--metrics-port", "9108"],
            env=controller_a_env,
            log_path=logs_dir / "controller-a.log",
        )
        controller_b = _start_proc(
            [sys.executable, "-m", "ae.controller", "--loop", "--interval", "2", "--metrics-port", "9109"],
            env=controller_b_env,
            log_path=logs_dir / "controller-b.log",
        )

        apishim_env = dict(env_base)
        apishim_env.update(
            {
                "AE_APISHIM_ENABLE": "1",
                "AE_APISHIM_ALLOW_ANON": "1",
                "AE_APISHIM_RUNTIME": "stub",
                "AE_APISHIM_DB": str(base_dir / "apishim.db"),
                "AE_APISHIM_ETCD_ENDPOINTS": endpoints[0],
            }
        )
        apishim = _start_proc(
            [
                sys.executable,
                "-m",
                "ae.apishim",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8845",
                "--allow-anonymous",
            ],
            env=apishim_env,
            log_path=logs_dir / "apishim.log",
        )
        _wait_http_ok("http://127.0.0.1:8845/version", timeout_s=30)

        first_leader = _wait_leader(endpoints, etcd_prefix, timeout_s=45)
        print(
            "[ha-e2e] initial leader:",
            f"{first_leader.controller_id}:{first_leader.controller_epoch}",
        )

        ns_a = _create_namespace("http://127.0.0.1:8845", "ha-before-failover")
        assert ns_a["metadata"]["name"] == "ha-before-failover"
        assert int(ns_a["metadata"]["resourceVersion"]) >= 1

        worker_env = dict(env_base)
        worker_env.update({"AE_NATS_URL": "nats://worker:dev@127.0.0.1:4223"})
        worker = _start_proc(
            [
                sys.executable,
                "-m",
                "ae.worker_stub",
                "--node-id",
                "edge-node-1",
                "--nats-url",
                "nats://worker:dev@127.0.0.1:4223",
                "--delay-ms",
                "50",
                "--progress-interval",
                "1",
            ],
            env=worker_env,
            log_path=logs_dir / "worker.log",
        )
        gateway_env = dict(env_base)
        gateway_env.update(
            {
                "AE_NATS_URL": "nats://gateway:dev@127.0.0.1:4223",
                "AE_SITE_ID": "sea-edge-01",
                "AE_NODE_ID": "edge-node-1",
                "AE_GATEWAY_SPOOL_PATH": str(base_dir / "gateway-spool.db"),
                "AE_GATEWAY_FENCE_DB": str(base_dir / "gateway-fence.db"),
                "AE_GATEWAY_LEASE_TIMEOUT": "10s",
            }
        )
        gateway = _start_proc(
            [sys.executable, "-m", "ae.gateway"],
            env=gateway_env,
            log_path=logs_dir / "gateway.log",
        )
        edge_node_key = scoped_node_id("sea-edge-01", "edge-node-1")
        if not _wait_node_ready(store, edge_node_key, timeout_s=25):
            raise RuntimeError("gateway lease not acquired in HA e2e")

        work1 = f"ha-w1-{run_id}"
        payload1 = json.dumps(
            {
                "work_id": work1,
                "attempt": 1,
                "site_id": "sea-edge-01",
                "op": "deploy",
                "desired_generation": 1,
                "target": {"app": "ha-edge-demo", "replicas": 1},
            }
        )
        _run(
            [
                sys.executable,
                "-m",
                "ae.cli",
                "work",
                "enqueue",
                "--site-id",
                "sea-edge-01",
                "--work-id",
                work1,
                "--mode",
                "outbox",
                "--preferred-node",
                "edge-node-1",
                "--payload",
                payload1,
            ],
            env=env_base,
        )
        _wait_work_state(store, work1, "Succeeded", timeout_s=90)
        print("[ha-e2e] work1 succeeded")

        leaders = {
            "core-a": controller_a,
            "core-b": controller_b,
        }
        old_leader_proc = leaders[first_leader.controller_id]
        if old_leader_proc is None:
            raise RuntimeError(f"missing leader process for {first_leader.controller_id}")
        _terminate(old_leader_proc)
        if first_leader.controller_id == "core-a":
            controller_a = None
        else:
            controller_b = None
        second_leader = _wait_leader(
            endpoints,
            etcd_prefix,
            timeout_s=60,
            min_epoch=first_leader.controller_epoch,
            exclude_controller_id=first_leader.controller_id,
        )
        print(
            "[ha-e2e] leader failover:",
            f"{first_leader.controller_id}:{first_leader.controller_epoch} -> "
            f"{second_leader.controller_id}:{second_leader.controller_epoch}",
        )

        ns_b = _create_namespace("http://127.0.0.1:8845", "ha-after-failover")
        assert ns_b["metadata"]["name"] == "ha-after-failover"
        assert int(ns_b["metadata"]["resourceVersion"]) > int(ns_a["metadata"]["resourceVersion"])

        _terminate(gateway)
        gateway = None
        work2 = f"ha-w2-{run_id}"
        payload2 = json.dumps(
            {
                "work_id": work2,
                "attempt": 1,
                "site_id": "sea-edge-01",
                "op": "deploy",
                "desired_generation": 2,
                "target": {"app": "ha-edge-demo", "replicas": 2},
            }
        )
        _run(
            [
                sys.executable,
                "-m",
                "ae.cli",
                "work",
                "enqueue",
                "--site-id",
                "sea-edge-01",
                "--work-id",
                work2,
                "--mode",
                "outbox",
                "--preferred-node",
                "edge-node-1",
                "--payload",
                payload2,
            ],
            env=env_base,
        )
        time.sleep(1.0)
        gateway = _start_proc(
            [sys.executable, "-m", "ae.gateway"],
            env=gateway_env,
            log_path=logs_dir / "gateway.log",
        )
        if not _wait_node_ready(store, edge_node_key, timeout_s=25):
            raise RuntimeError("gateway did not recover after restart")
        _wait_work_state(store, work2, "Succeeded", timeout_s=90)
        print("[ha-e2e] work2 succeeded after gateway replay recovery")

        print("[ha-e2e] logs:")
        for proc in (controller_a, controller_b, apishim, gateway, worker):
            if proc is not None:
                print(f"  {proc.name}: {proc.log_path}")
        return 0
    finally:
        for proc in [gateway, worker, apishim, controller_b, controller_a]:
            if proc is not None:
                _terminate(proc)
        subprocess.run([*compose_cmd, "down"], check=False)


__all__ = ["run_ha_closeout_e2e"]
