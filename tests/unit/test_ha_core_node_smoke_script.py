from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

from ae.controller.state import SQLiteStateStore


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "ha_core_node_smoke.py"

_SPEC = spec_from_file_location("ha_core_node_smoke_script", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
ha_core_node_smoke = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ha_core_node_smoke
_SPEC.loader.exec_module(ha_core_node_smoke)


def test_find_ready_node_accepts_expected_labels(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    store.upsert_node(
        "attached-node-1",
        name="attached-node-1",
        labels={"role": "worker", "site": "core"},
    )
    store.record_heartbeat("attached-node-1", "ready")

    ok, detail = ha_core_node_smoke.find_ready_node(
        store,
        "attached-node-1",
        {"role": "worker", "site": "core"},
    )

    assert ok is True
    assert detail == "node ready: attached-node-1"


def test_find_ready_node_rejects_label_mismatch(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    store.upsert_node(
        "attached-node-1",
        name="attached-node-1",
        labels={"role": "worker", "site": "lab"},
    )
    store.record_heartbeat("attached-node-1", "ready")

    ok, detail = ha_core_node_smoke.find_ready_node(
        store,
        "attached-node-1",
        {"role": "worker", "site": "core"},
    )

    assert ok is False
    assert detail == "node labels mismatch: site=core"


def test_load_smoke_manifest_overrides_name() -> None:
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "shell-demo-node-hub.yaml",
        "ha-core-node-smoke",
    )

    assert manifest.metadata.name == "ha-core-node-smoke"
    assert manifest.spec.image == "docker.io/library/demo-shell:latest"
    assert manifest.spec.node_selector == {"role": "hub", "site": "hub"}


def test_load_edge_smoke_manifest_exposes_core_proxy_service_port() -> None:
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "ha-web-smoke-edge.yaml",
        "ha-edge-web-smoke",
    )

    assert manifest.spec.service is not None
    assert manifest.spec.service.port == 18081
    assert manifest.spec.service.target_port == 8080


def test_load_retained_attached_node_smoke_manifest_targets_core_worker() -> None:
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "ha-web-smoke.yaml",
        "ha-web-smoke",
    )

    assert manifest.spec.node_selector == {"role": "worker", "site": "core"}


def test_run_workload_smoke_cleans_up_registered_state(monkeypatch, tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "shell-demo-node-hub.yaml",
        "ha-core-node-smoke",
    )
    app_name = manifest.metadata.name
    cleanup_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(ha_core_node_smoke, "state_store_from_env", lambda: store)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_node_ready",
        lambda *args, **kwargs: "node ready: attached-node-1",
    )
    monkeypatch.setattr(ha_core_node_smoke, "load_smoke_manifest", lambda path, name: manifest)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_workload_ready",
        lambda *args, **kwargs: "workload ready: app=ha-core-node-smoke desired=1 ready=1 live=1",
    )

    original_cleanup = ha_core_node_smoke.cleanup_workload

    def _wrapped_cleanup(store_obj, app_key: str, *, timeout_s: int, poll_s: float, purge_history: bool) -> None:
        cleanup_calls.append((app_key, purge_history))
        original_cleanup(
            store_obj,
            app_key,
            timeout_s=timeout_s,
            poll_s=poll_s,
            purge_history=purge_history,
        )

    monkeypatch.setattr(ha_core_node_smoke, "cleanup_workload", _wrapped_cleanup)

    args = SimpleNamespace(
        node_id="hub-1",
        label=["role=hub", "site=hub"],
        manifest=ROOT / "docs" / "site" / "examples" / "shell-demo-node-hub.yaml",
        app_name=app_name,
        timeout=5,
        poll=0.01,
        purge_history=True,
    )

    rc = ha_core_node_smoke.run_workload_smoke(args)

    assert rc == 0
    assert store.get_registered_entry(app_name) is None
    assert cleanup_calls == [(app_name, True)]


def test_run_ingress_smoke_verifies_ingress_and_cleans_up(monkeypatch, tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "ha-web-smoke.yaml",
        "ha-web-smoke",
    )
    app_name = manifest.metadata.name
    cleanup_calls: list[tuple[str, bool]] = []
    ingress_calls: list[dict[str, object]] = []

    monkeypatch.setattr(ha_core_node_smoke, "state_store_from_env", lambda: store)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_node_ready",
        lambda *args, **kwargs: "node ready: attached-node-1",
    )
    monkeypatch.setattr(ha_core_node_smoke, "load_smoke_manifest", lambda path, name: manifest)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_workload_ready",
        lambda *args, **kwargs: "workload ready: app=ha-web-smoke desired=1 ready=1 live=1",
    )
    monkeypatch.setattr(
        ha_core_node_smoke,
        "verify_workload_endpoint_cidr",
        lambda *args, **kwargs: "pod endpoint ok: app=ha-web-smoke pod=ha-web-smoke-rev1-0 endpoint=10.42.0.3:8080 pod_cidr=10.42.0.0/24",
    )

    def _wait_for_ingress_response(**kwargs):
        ingress_calls.append(kwargs)
        return f"ingress ok: host={kwargs['host']} path={kwargs['path']} status=200"

    monkeypatch.setattr(ha_core_node_smoke, "wait_for_ingress_response", _wait_for_ingress_response)

    original_cleanup = ha_core_node_smoke.cleanup_workload

    def _wrapped_cleanup(store_obj, app_key: str, *, timeout_s: int, poll_s: float, purge_history: bool) -> None:
        cleanup_calls.append((app_key, purge_history))
        original_cleanup(
            store_obj,
            app_key,
            timeout_s=timeout_s,
            poll_s=poll_s,
            purge_history=purge_history,
        )

    monkeypatch.setattr(ha_core_node_smoke, "cleanup_workload", _wrapped_cleanup)

    args = SimpleNamespace(
        node_id="attached-node-1",
        label=["role=worker", "site=core"],
        manifest=ROOT / "docs" / "site" / "examples" / "ha-web-smoke.yaml",
        app_name=app_name,
        timeout=5,
        poll=0.01,
        purge_history=True,
        ingress_host="ha-web-smoke.home.arpa",
        ingress_port=10443,
        resolve_ip="192.168.155.10",
        health_path="/healthz",
        root_path="/",
        expected_text="Shell + Port-Forward Smoke",
    )

    rc = ha_core_node_smoke.run_ingress_smoke(args)

    assert rc == 0
    assert store.get_registered_entry(app_name) is None
    assert cleanup_calls == [(app_name, True)]
    assert [call["path"] for call in ingress_calls] == ["/healthz", "/"]
    assert ingress_calls[0].get("expected_text") is None
    assert ingress_calls[1]["expected_text"] == "Shell + Port-Forward Smoke"


def test_run_ingress_smoke_direct_probe_precedes_ingress(monkeypatch, tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "ha-web-smoke.yaml",
        "ha-web-smoke",
    )
    call_order: list[str] = []

    monkeypatch.setattr(ha_core_node_smoke, "state_store_from_env", lambda: store)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_node_ready",
        lambda *args, **kwargs: "node ready: attached-node-1",
    )
    monkeypatch.setattr(ha_core_node_smoke, "load_smoke_manifest", lambda path, name: manifest)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_workload_ready",
        lambda *args, **kwargs: "workload ready: app=ha-web-smoke desired=1 ready=1 live=1",
    )

    def _verify(*args, **kwargs):
        call_order.append("endpoint")
        return "pod endpoint ok: app=ha-web-smoke pod=ha-web-smoke-rev1-0 endpoint=10.42.0.3:8080 pod_cidr=10.42.0.0/24"

    monkeypatch.setattr(ha_core_node_smoke, "verify_workload_endpoint_cidr", _verify)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "select_workload_endpoint",
        lambda *args, **kwargs: ha_core_node_smoke.ReadyWorkloadEndpoint(
            pod_name="ha-web-smoke-rev1-0",
            endpoint="10.42.0.3:8080",
            host="10.42.0.3",
            port=8080,
            pod_cidr="10.42.0.0/24",
        ),
    )

    def _direct_probe(**kwargs):
        call_order.append("direct")
        return "direct probe ok: core=192.168.155.10 endpoint=10.42.0.3:8080 path=/healthz status=200"

    monkeypatch.setattr(ha_core_node_smoke, "wait_for_direct_endpoint_response", _direct_probe)

    def _ingress(**kwargs):
        call_order.append(f"ingress:{kwargs['path']}")
        return f"ingress ok: host={kwargs['host']} path={kwargs['path']} status=200"

    monkeypatch.setattr(ha_core_node_smoke, "wait_for_ingress_response", _ingress)

    args = SimpleNamespace(
        node_id="attached-node-1",
        label=["role=worker", "site=core"],
        manifest=ROOT / "docs" / "site" / "examples" / "ha-web-smoke.yaml",
        app_name="ha-web-smoke",
        timeout=5,
        poll=0.01,
        purge_history=True,
        ingress_host="ha-web-smoke.home.arpa",
        ingress_port=10443,
        resolve_ip="192.168.155.10",
        health_path="/healthz",
        root_path="/",
        expected_text="Shell + Port-Forward Smoke",
        direct_probe_host="192.168.155.10",
        direct_probe_user="ae",
    )

    rc = ha_core_node_smoke.run_ingress_smoke(args)

    assert rc == 0
    assert call_order == ["endpoint", "direct", "ingress:/healthz", "ingress:/"]


def test_run_ingress_smoke_target_probe_precedes_ingress(monkeypatch, tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "ha-web-smoke-edge.yaml",
        "ha-edge-web-smoke",
    )
    call_order: list[str] = []

    monkeypatch.setattr(ha_core_node_smoke, "state_store_from_env", lambda: store)
    monkeypatch.setattr(
        ha_core_node_smoke, "wait_for_node_ready", lambda *args, **kwargs: "node ready: sea-node-1"
    )
    monkeypatch.setattr(ha_core_node_smoke, "load_smoke_manifest", lambda path, name: manifest)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_workload_ready",
        lambda *args, **kwargs: "workload ready: app=ha-edge-web-smoke desired=1 ready=1 live=1",
    )

    def _verify(*args, **kwargs):
        call_order.append("endpoint")
        return "pod endpoint ok: app=ha-edge-web-smoke pod=ha-edge-web-smoke-rev1-0 endpoint=10.42.1.2:8080 pod_cidr=10.42.1.0/24"

    monkeypatch.setattr(ha_core_node_smoke, "verify_workload_endpoint_cidr", _verify)

    def _target_probe(**kwargs):
        call_order.append("target")
        return "target probe ok: host=192.168.155.20 url=http://192.168.155.21:18081/healthz status=200"

    monkeypatch.setattr(ha_core_node_smoke, "wait_for_target_probe_response", _target_probe)

    def _ingress(**kwargs):
        call_order.append(f"ingress:{kwargs['path']}")
        return f"ingress ok: host={kwargs['host']} path={kwargs['path']} status=200"

    monkeypatch.setattr(ha_core_node_smoke, "wait_for_ingress_response", _ingress)

    args = SimpleNamespace(
        node_id="sea-node-1",
        label=["role=worker", "site=sea"],
        manifest=ROOT / "docs" / "site" / "examples" / "ha-web-smoke-edge.yaml",
        app_name="ha-edge-web-smoke",
        timeout=5,
        poll=0.01,
        purge_history=True,
        ingress_host="ha-edge-web-smoke.home.arpa",
        ingress_port=10443,
        resolve_ip="192.168.155.10",
        health_path="/healthz",
        root_path="/",
        expected_text="Shell + Port-Forward Smoke",
        target_probe_host="192.168.155.20",
        target_probe_user="ae",
        target_probe_url="http://192.168.155.21:18081/healthz",
        target_probe_timeout=60,
    )

    rc = ha_core_node_smoke.run_ingress_smoke(args)

    assert rc == 0
    assert call_order == ["endpoint", "target", "ingress:/healthz", "ingress:/"]


def test_run_ingress_smoke_cleans_up_on_ingress_failure(monkeypatch, tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "ha-web-smoke.yaml",
        "ha-web-smoke",
    )
    app_name = manifest.metadata.name
    cleanup_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(ha_core_node_smoke, "state_store_from_env", lambda: store)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_node_ready",
        lambda *args, **kwargs: "node ready: attached-node-1",
    )
    monkeypatch.setattr(ha_core_node_smoke, "load_smoke_manifest", lambda path, name: manifest)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_workload_ready",
        lambda *args, **kwargs: "workload ready: app=ha-web-smoke desired=1 ready=1 live=1",
    )
    monkeypatch.setattr(
        ha_core_node_smoke,
        "verify_workload_endpoint_cidr",
        lambda *args, **kwargs: "pod endpoint ok: app=ha-web-smoke pod=ha-web-smoke-rev1-0 endpoint=10.42.0.3:8080 pod_cidr=10.42.0.0/24",
    )

    def _wait_for_ingress_response(**kwargs):
        if kwargs["path"] == "/healthz":
            return "ingress ok: host=ha-web-smoke.home.arpa path=/healthz status=200"
        raise SystemExit("ingress status mismatch: host=ha-web-smoke.home.arpa path=/ expected=200 actual=503")

    monkeypatch.setattr(ha_core_node_smoke, "wait_for_ingress_response", _wait_for_ingress_response)

    original_cleanup = ha_core_node_smoke.cleanup_workload

    def _wrapped_cleanup(store_obj, app_key: str, *, timeout_s: int, poll_s: float, purge_history: bool) -> None:
        cleanup_calls.append((app_key, purge_history))
        original_cleanup(
            store_obj,
            app_key,
            timeout_s=timeout_s,
            poll_s=poll_s,
            purge_history=purge_history,
        )

    monkeypatch.setattr(ha_core_node_smoke, "cleanup_workload", _wrapped_cleanup)

    args = SimpleNamespace(
        node_id="attached-node-1",
        label=["role=worker", "site=core"],
        manifest=ROOT / "docs" / "site" / "examples" / "ha-web-smoke.yaml",
        app_name=app_name,
        timeout=5,
        poll=0.01,
        purge_history=True,
        ingress_host="ha-web-smoke.home.arpa",
        ingress_port=10443,
        resolve_ip="192.168.155.10",
        health_path="/healthz",
        root_path="/",
        expected_text="Shell + Port-Forward Smoke",
    )

    with pytest.raises(SystemExit, match="ingress status mismatch"):
        ha_core_node_smoke.run_ingress_smoke(args)

    assert store.get_registered_entry(app_name) is None
    assert cleanup_calls == [(app_name, True)]


def test_run_ingress_smoke_cleans_up_on_target_probe_failure(monkeypatch, tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "ha-web-smoke-edge.yaml",
        "ha-edge-web-smoke",
    )
    app_name = manifest.metadata.name
    cleanup_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(ha_core_node_smoke, "state_store_from_env", lambda: store)
    monkeypatch.setattr(
        ha_core_node_smoke, "wait_for_node_ready", lambda *args, **kwargs: "node ready: sea-node-1"
    )
    monkeypatch.setattr(ha_core_node_smoke, "load_smoke_manifest", lambda path, name: manifest)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_workload_ready",
        lambda *args, **kwargs: "workload ready: app=ha-edge-web-smoke desired=1 ready=1 live=1",
    )
    monkeypatch.setattr(
        ha_core_node_smoke,
        "verify_workload_endpoint_cidr",
        lambda *args, **kwargs: "pod endpoint ok: app=ha-edge-web-smoke pod=ha-edge-web-smoke-rev1-0 endpoint=10.42.1.2:8080 pod_cidr=10.42.1.0/24",
    )
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_target_probe_response",
        lambda **kwargs: (_ for _ in ()).throw(
            SystemExit(
                "target probe failed: host=192.168.155.20 url=http://192.168.155.21:18081/healthz status=503"
            )
        ),
    )

    original_cleanup = ha_core_node_smoke.cleanup_workload

    def _wrapped_cleanup(store_obj, app_key: str, *, timeout_s: int, poll_s: float, purge_history: bool) -> None:
        cleanup_calls.append((app_key, purge_history))
        original_cleanup(
            store_obj,
            app_key,
            timeout_s=timeout_s,
            poll_s=poll_s,
            purge_history=purge_history,
        )

    monkeypatch.setattr(ha_core_node_smoke, "cleanup_workload", _wrapped_cleanup)

    args = SimpleNamespace(
        node_id="sea-node-1",
        label=["role=worker", "site=sea"],
        manifest=ROOT / "docs" / "site" / "examples" / "ha-web-smoke-edge.yaml",
        app_name=app_name,
        timeout=5,
        poll=0.01,
        purge_history=True,
        ingress_host="ha-edge-web-smoke.home.arpa",
        ingress_port=10443,
        resolve_ip="192.168.155.10",
        health_path="/healthz",
        root_path="/",
        expected_text="Shell + Port-Forward Smoke",
        target_probe_host="192.168.155.20",
        target_probe_user="ae",
        target_probe_url="http://192.168.155.21:18081/healthz",
        target_probe_timeout=60,
    )

    with pytest.raises(SystemExit, match="target probe failed"):
        ha_core_node_smoke.run_ingress_smoke(args)

    assert store.get_registered_entry(app_name) is None
    assert cleanup_calls == [(app_name, True)]


def test_verify_workload_endpoint_cidr_accepts_ready_pod_within_node_cidr() -> None:
    store = SimpleNamespace(
        list_nodes=lambda: [
            (
                SimpleNamespace(node_id="attached-node-1", pod_cidr="10.42.0.0/24"),
                SimpleNamespace(status="ready"),
            )
        ],
        list_pods=lambda _app_name: [
            SimpleNamespace(
                pod_name="ha-web-smoke-rev1-0",
                ready=True,
                endpoint="10.42.0.3:8080",
            )
        ],
    )

    detail = ha_core_node_smoke.verify_workload_endpoint_cidr(
        store, "default/ha-web-smoke", "attached-node-1"
    )

    assert "endpoint=10.42.0.3:8080" in detail


def test_verify_workload_endpoint_cidr_rejects_pod_outside_node_cidr() -> None:
    store = SimpleNamespace(
        list_nodes=lambda: [
            (
                SimpleNamespace(node_id="attached-node-1", pod_cidr="10.42.0.0/24"),
                SimpleNamespace(status="ready"),
            )
        ],
        list_pods=lambda _app_name: [
            SimpleNamespace(
                pod_name="ha-web-smoke-rev1-0",
                ready=True,
                endpoint="10.88.0.3:8080",
            )
        ],
    )

    with pytest.raises(SystemExit, match="pod endpoint outside node pod CIDR"):
        ha_core_node_smoke.verify_workload_endpoint_cidr(
            store, "default/ha-web-smoke", "attached-node-1"
        )
