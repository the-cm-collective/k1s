from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ae.controller.etcd_state import EtcdStateStore
from ae.controller.node_identity import scoped_node_id


@dataclass
class _Proc:
    name: str
    popen: subprocess.Popen
    log_path: Path
    log_handle: object


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _wait_tcp(host: str, port: int, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"timeout waiting for {host}:{port}")


def _wait_etcd(url: str, timeout_s: float = 30.0) -> None:
    import urllib.request

    deadline = time.time() + timeout_s
    health_url = url.rstrip("/") + "/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if payload.get("health") in {"true", True}:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"timeout waiting for etcd health at {health_url}")


def _wait_nats_leaf(url: str, timeout_s: float = 30.0) -> None:
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                count = payload.get("num_leaves")
                if count is None:
                    leafs = payload.get("leafs") or payload.get("leaf_nodes") or []
                    if isinstance(leafs, list):
                        count = len(leafs)
                if isinstance(count, int) and count > 0:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"timeout waiting for nats leaf connection at {url}")


def _write_compose(compose_path: Path, root: Path, state_dir: Path) -> None:
    hub_conf = (root / "ops/dev/nats-hub.conf").as_posix()
    edge_conf = (root / "ops/dev/nats-edge.conf").as_posix()
    etcd_state = (state_dir / "etcd").as_posix()
    nats_state = (state_dir / "nats-hub").as_posix()
    compose = f"""services:
  etcd:
    image: quay.io/coreos/etcd:v3.5.13
    command:
      - /usr/local/bin/etcd
      - --name=etcd0
      - --data-dir=/etcd-data
      - --listen-client-urls=http://0.0.0.0:2379
      - --advertise-client-urls=http://etcd:2379
      - --listen-peer-urls=http://0.0.0.0:2380
      - --initial-advertise-peer-urls=http://etcd:2380
      - --initial-cluster=etcd0=http://etcd:2380
      - --initial-cluster-state=new
    ports:
      - "2379:2379"
    volumes:
      - {etcd_state}:/etcd-data

  nats-hub:
    image: nats:2.10
    command: ["-c", "/etc/nats/nats-hub.conf"]
    ports:
      - "4222:4222"
      - "8222:8222"
      - "7422:7422"
    volumes:
      - {hub_conf}:/etc/nats/nats-hub.conf:ro
      - {nats_state}:/data
    depends_on:
      - etcd

  nats-edge:
    image: nats:2.10
    command: ["-c", "/etc/nats/nats-edge.conf"]
    ports:
      - "4223:4223"
      - "8223:8223"
    volumes:
      - {edge_conf}:/etc/nats/nats-edge.conf:ro
    depends_on:
      - nats-hub
"""
    compose_path.write_text(compose, encoding="utf-8")


def _start_proc(cmd: list[str], env: dict[str, str], log_path: Path) -> _Proc:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    popen = subprocess.Popen(
        cmd,
        env=env,
        stdout=log_handle,
        stderr=log_handle,
        text=True,
        start_new_session=True,
    )
    return _Proc(name=" ".join(cmd[:2]), popen=popen, log_path=log_path, log_handle=log_handle)


def _terminate(proc: _Proc, timeout_s: float = 5.0) -> None:
    if proc.popen.poll() is None:
        try:
            os.killpg(proc.popen.pid, signal.SIGTERM)
        except Exception:
            proc.popen.terminate()
        try:
            proc.popen.wait(timeout=timeout_s)
        except Exception:
            try:
                os.killpg(proc.popen.pid, signal.SIGKILL)
            except Exception:
                proc.popen.kill()
    try:
        proc.log_handle.close()
    except Exception:
        pass


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)


def _wait_work_state(
    store: EtcdStateStore, work_id: str, desired: str, timeout_s: float = 60.0
):
    deadline = time.time() + timeout_s
    last_state = None
    while time.time() < deadline:
        entry = store.get_work_ledger(work_id)
        if entry is not None:
            last_state = entry.state
            if entry.state == desired:
                return entry
        time.sleep(1)
    raise RuntimeError(f"work {work_id} not {desired} (last={last_state})")


def _wait_node_ready(store: EtcdStateStore, node_id: str, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            node = store.get_node(node_id)
        except Exception:
            node = None
        if node is not None:
            return True
        time.sleep(1)
    return False


def run_core_edge_e2e() -> int:
    root = _repo_root()
    run_id = uuid.uuid4().hex[:8]
    base_dir = Path(
        os.getenv("K1S_E2E_DIR", root / ".local" / f"e2e-core-edge-{run_id}")
    ).resolve()
    state_dir = base_dir / "state"
    logs_dir = base_dir / "logs"
    compose_path = base_dir / "compose.yaml"
    specs_dir = base_dir / "specs-empty"
    state_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    specs_dir.mkdir(parents=True, exist_ok=True)

    print(f"[e2e] workspace={base_dir}")
    _write_compose(compose_path, root, state_dir)

    compose_cmd = ["docker", "compose", "-f", str(compose_path)]
    subprocess.run([*compose_cmd, "up", "-d"], check=True)

    controller = gateway = worker = None
    try:
        _wait_tcp("127.0.0.1", 2379, timeout_s=40)
        _wait_tcp("127.0.0.1", 4222, timeout_s=40)
        _wait_tcp("127.0.0.1", 4223, timeout_s=40)
        _wait_etcd("http://127.0.0.1:2379", timeout_s=40)
        _wait_nats_leaf("http://127.0.0.1:8222/leafz", timeout_s=40)

        etcd_prefix = f"k1s/e2e/{run_id}"
        env_base = os.environ.copy()
        env_base.update(
            {
                "AE_STATE_BACKEND": "etcd",
                "AE_ETCD_ENDPOINTS": "http://127.0.0.1:2379",
                "AE_ETCD_PREFIX": etcd_prefix,
                "AE_SITE_ID": "core",
                "AE_TRANSPORT_BACKEND": "nats-js",
                "AE_NATS_URL": "nats://hub-controller:dev@127.0.0.1:4222",
                "AE_JS_DOMAIN": "K1S",
                "AE_OUTBOX_PUBLISH_INTERVAL_S": "0.2",
                "AE_LEASE_TTL_MS": "20000",
                "AE_LEASE_RENEW_AFTER_MS": "5000",
                "AE_PROJECTION_ROOT": str(base_dir / "projections"),
                "PYTHONUNBUFFERED": "1",
            }
        )

        os.environ.update(
            {
                "AE_ETCD_ENDPOINTS": "http://127.0.0.1:2379",
                "AE_ETCD_PREFIX": etcd_prefix,
                "AE_SITE_ID": "core",
            }
        )
        store = EtcdStateStore()

        controller = _start_proc(
            [
                sys.executable,
                "-m",
                "ae.controller",
                "--loop",
                "--interval",
                "2",
                "--specs",
                str(specs_dir),
            ],
            env=env_base,
            log_path=logs_dir / "controller.log",
        )

        worker_env = env_base.copy()
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

        # enqueue work before gateway starts to prove JetStream buffering
        work1 = f"edge-w1-{run_id}"
        payload1 = json.dumps(
            {
                "work_id": work1,
                "attempt": 1,
                "site_id": "sfo-edge-01",
                "op": "deploy",
                "desired_generation": 1,
                "target": {"app": "edge-demo", "replicas": 1},
            }
        )
        res = _run(
            [
                sys.executable,
                "-m",
                "ae.cli",
                "work",
                "enqueue",
                "--site-id",
                "sfo-edge-01",
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
        print("[e2e] enqueue work1:", res.stdout.strip())

        time.sleep(1.5)

        gateway_env = env_base.copy()
        gateway_env.update(
            {
                "AE_NATS_URL": "nats://gateway:dev@127.0.0.1:4223",
                "AE_SITE_ID": "sfo-edge-01",
                "AE_NODE_ID": "edge-node-1",
                "AE_GATEWAY_SPOOL_PATH": str(base_dir / "gateway-spool.db"),
                "AE_GATEWAY_LEASE_TIMEOUT": "10s",
                "AE_JS_DOMAIN": "K1S",
            }
        )
        gateway = _start_proc(
            [sys.executable, "-m", "ae.gateway"],
            env=gateway_env,
            log_path=logs_dir / "gateway.log",
        )
        edge_node_key = scoped_node_id("sfo-edge-01", "edge-node-1")
        if not _wait_node_ready(store, edge_node_key, timeout_s=20):
            _terminate(gateway)
            gateway = _start_proc(
                [sys.executable, "-m", "ae.gateway"],
                env=gateway_env,
                log_path=logs_dir / "gateway.log",
            )
            if not _wait_node_ready(store, edge_node_key, timeout_s=20):
                raise RuntimeError("gateway lease not acquired (node not registered)")
        _wait_work_state(store, work1, "Succeeded", timeout_s=90)
        print("[e2e] work1 succeeded")

        work2 = f"edge-w2-{run_id}"
        payload2 = json.dumps(
            {
                "work_id": work2,
                "attempt": 1,
                "site_id": "sfo-edge-01",
                "op": "deploy",
                "desired_generation": 1,
                "target": {"app": "edge-demo", "replicas": 2},
            }
        )
        res = _run(
            [
                sys.executable,
                "-m",
                "ae.cli",
                "work",
                "enqueue",
                "--site-id",
                "sfo-edge-01",
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
        print("[e2e] enqueue work2:", res.stdout.strip())
        _wait_work_state(store, work2, "Succeeded", timeout_s=90)
        print("[e2e] work2 succeeded")

        print("[e2e] logs:")
        print(f"  controller: {controller.log_path}")
        print(f"  gateway:    {gateway.log_path}")
        print(f"  worker:     {worker.log_path}")
        return 0
    finally:
        for proc in [gateway, worker, controller]:
            if proc is not None:
                _terminate(proc)
        subprocess.run([*compose_cmd, "down"], check=False)


__all__ = ["run_core_edge_e2e"]
