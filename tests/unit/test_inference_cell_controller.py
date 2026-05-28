import pytest

from ae.controller.inference_cell import (
    InferenceCellController,
    InferenceCellSetController,
    StagePlacement,
)
from ae.controller.spec import InferenceCellManifest, InferenceCellSetManifest
from ae.controller.state import SQLiteStateStore


def _cell_manifest(name: str = "demo-cell") -> InferenceCellManifest:
    return InferenceCellManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCell",
            "metadata": {"name": name, "namespace": "default"},
            "spec": {
                "model": {"modelId": "llama", "localPath": "/models/llama"},
                "parallelism": {"tp": 1, "pp": 2},
                "members": [
                    {"siteId": "site-a", "nodeId": "node-a", "gpuCount": 1},
                    {"siteId": "site-b", "nodeId": "node-b", "gpuCount": 1},
                ],
                "linkMetrics": [
                    {
                        "fromSite": "site-a",
                        "toSite": "site-b",
                        "rttP95Ms": 5.0,
                        "jitterP95Ms": 0.2,
                        "lossPct": 0.0,
                    }
                ],
            },
        }
    )


def _single_node_ray_manifest(name: str = "single-node-ray") -> InferenceCellManifest:
    return InferenceCellManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCell",
            "metadata": {"name": name, "namespace": "default"},
            "spec": {
                "model": {"modelId": "llama", "localPath": "/models/llama"},
                "parallelism": {"tp": 1, "pp": 1},
                "members": [
                    {"siteId": "site-a", "nodeId": "node-a", "gpuCount": 1},
                ],
                "executor": {
                    "type": "ray",
                    "fallbackMode": "mp_on_failure",
                    "launcherImage": "ray-launcher:test",
                    "rayImage": "ray-head:test",
                },
            },
        }
    )


class _JoinRuntime:
    def __init__(self, app_names: list[str], *, running: bool = True):
        self._app_names = list(app_names)
        self._running = bool(running)

    def list_containers_info(self):
        return [{"labels": {"ae.app": name}, "running": self._running} for name in self._app_names]


def _joining_alloc(name: str) -> dict:
    return {
        "master_addr": "10.0.0.10",
        "api_port": 18080,
        "api_endpoint": "10.0.0.10:18080",
        "execution": {
            "workloads": [
                {"node_id": "node-a", "app_name": f"default/{name}-ray-head"},
                {"node_id": "node-a", "app_name": f"default/{name}-ray-launcher"},
            ]
        },
    }


def test_inference_cell_reconcile_ready(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    rec = ctrl.reconcile_manifest(_cell_manifest(), source="test")
    assert rec.phase == "READY"
    assert rec.allocations.get("fabric_session_id")
    sessions = store.list_fabric_sessions(cell_name="demo-cell", namespace="default")
    assert len(sessions) == 1
    events = store.list_inference_cell_events("demo-cell", namespace="default")
    assert any(ev.event_type == "CellReady" for ev in events)


def test_inference_cell_admission_failure_without_metrics(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    manifest = _cell_manifest(name="bad-cell")
    payload = manifest.model_dump(by_alias=True)
    payload["spec"]["linkMetrics"] = []
    bad = InferenceCellManifest.model_validate(payload)
    rec = ctrl.reconcile_manifest(bad, source="test")
    assert rec.phase == "FAILED"
    assert rec.last_error == "ADMISSION_REJECTED"


def test_inference_cellset_scale_to_zero(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    set_ctrl = InferenceCellSetController(store)
    cellset = InferenceCellSetManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCellSet",
            "metadata": {"name": "set-a", "namespace": "default"},
            "spec": {
                "replicas": 1,
                "template": _cell_manifest().spec.model_dump(by_alias=True),
            },
        }
    )
    rec = set_ctrl.reconcile_manifest(cellset, source="test")
    assert rec.desired == 1
    assert rec.current >= 1
    scaled = set_ctrl.scale("set-a", 0, namespace="default")
    assert scaled is not None
    assert scaled.desired == 0
    cells = [
        c
        for c in store.list_inference_cells(namespace="default")
        if (c.manifest.metadata.labels or {}).get("k1s.cellset") == "set-a"
    ]
    assert cells == []
    set_ctrl.delete_cellset("set-a", namespace="default")
    assert store.get_inference_cellset("set-a", namespace="default") is None


def test_inference_cell_execution_mode_requires_registered_members(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_EXPERIMENTAL", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    rec = ctrl.reconcile_manifest(_cell_manifest(name="exec-cell"), source="test")
    assert rec.phase == "FAILED"
    assert rec.last_error == "ADMISSION_MEMBER_INVALID"


def test_inference_cell_execution_mode_accepts_typed_accelerators(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_EXPERIMENTAL", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    for node_id, site_id in (("node-a", "site-a"), ("node-b", "site-b")):
        store.upsert_node(
            node_id,
            name=node_id,
            labels={"site": site_id},
            capabilities={
                "accelerators": [
                    {
                        "id": f"{node_id}-gpu-0",
                        "kind": "discrete_gpu",
                        "vendor": "nvidia",
                        "family": "RTX 8000",
                        "device_count": 1,
                        "memory_model": "dedicated",
                        "memory_bytes_per_device": 49152 * 1024 * 1024,
                        "runtime_handlers": ["nvidia"],
                        "partitioning_mode": "none",
                        "backing_device_id": None,
                        "execution_role": "execution",
                    }
                ]
            },
            endpoint=f"http://{node_id}.lan:9109",
        )
        store.record_heartbeat(node_id, "Ready")

    ctrl = InferenceCellController(store)
    errors = ctrl._validate_members_for_execution(_cell_manifest(name="typed-exec-cell").spec)
    assert errors == []


def test_inference_stage_manifest_sets_runtime_class(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    payload = _cell_manifest().model_dump(by_alias=True)
    payload["spec"]["executor"]["runtimeClassName"] = "nvidia"
    manifest = InferenceCellManifest.model_validate(payload)

    stage0 = StagePlacement(stage=0, site_id="site-a", node_id="node-a", gpu_indices=[0])
    alloc = {
        "master_addr": "10.255.0.10",
        "master_port": 22000,
        "api_port": 18080,
    }
    mp_manifest = ctrl._mp_stage_manifest(manifest, stage0, alloc)
    assert mp_manifest.spec.runtime_class_name == "nvidia"


def test_inference_mp_stage_manifest_mounts_model_path(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    manifest = _cell_manifest()
    stage0 = StagePlacement(stage=0, site_id="site-a", node_id="node-a", gpu_indices=[0])
    alloc = {
        "master_addr": "10.255.0.10",
        "master_port": 22000,
        "api_port": 18080,
    }

    mp_manifest = ctrl._mp_stage_manifest(manifest, stage0, alloc)

    assert mp_manifest.spec.volumes is not None
    assert len(mp_manifest.spec.volumes) == 1
    assert mp_manifest.spec.volumes[0].host_path == "/models/llama"
    assert mp_manifest.spec.volumes[0].mount_path == "/models/llama"
    assert mp_manifest.spec.volumes[0].read_only is True
    assert mp_manifest.spec.service is not None
    assert mp_manifest.spec.service.port == 18080
    assert mp_manifest.spec.service.target_port == 18080
    assert mp_manifest.spec.args is not None
    assert "python3 -m vllm.entrypoints.openai.api_server" in mp_manifest.spec.args[0]
    assert "--distributed-executor-backend mp" in mp_manifest.spec.args[0]
    assert "--dtype" not in mp_manifest.spec.args[0]
    assert "--nnodes" not in mp_manifest.spec.args[0]
    assert "--node-rank" not in mp_manifest.spec.args[0]
    assert "--master-addr" not in mp_manifest.spec.args[0]
    assert "--master-port" not in mp_manifest.spec.args[0]


def test_inference_mp_stage_manifest_includes_dtype_when_configured(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    payload = _cell_manifest().model_dump(by_alias=True)
    payload["spec"]["executor"]["dtype"] = "half"
    manifest = InferenceCellManifest.model_validate(payload)
    stage0 = StagePlacement(stage=0, site_id="site-a", node_id="node-a", gpu_indices=[0])
    alloc = {
        "master_addr": "10.255.0.10",
        "master_port": 22000,
        "api_port": 18080,
    }

    mp_manifest = ctrl._mp_stage_manifest(manifest, stage0, alloc)

    assert mp_manifest.spec.args is not None
    assert '--dtype "$DTYPE"' in mp_manifest.spec.args[0]
    assert mp_manifest.spec.env is not None
    assert {"name": "DTYPE", "value": "half"} in mp_manifest.spec.env


def test_inference_mp_stage_manifest_does_not_publish_service_for_non_leader(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    manifest = _cell_manifest()
    stage1 = StagePlacement(stage=1, site_id="site-b", node_id="node-b", gpu_indices=[0])
    alloc = {
        "master_addr": "10.255.0.10",
        "master_port": 22000,
        "api_port": 18080,
    }

    mp_manifest = ctrl._mp_stage_manifest(manifest, stage1, alloc)

    assert mp_manifest.spec.service is None


def test_inference_launcher_manifest_publishes_api_service_port(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    manifest = _cell_manifest()
    stage0 = StagePlacement(stage=0, site_id="site-a", node_id="node-a", gpu_indices=[0])
    alloc = {"api_port": 18080}

    launcher = ctrl._ray_launcher_manifest(manifest, stage0, alloc)

    assert launcher.spec.service is not None
    assert launcher.spec.service.port == 18080
    assert launcher.spec.service.target_port == 18080
    assert launcher.spec.volumes is not None
    assert len(launcher.spec.volumes) == 1
    assert launcher.spec.volumes[0].host_path == "/models/llama"
    assert launcher.spec.volumes[0].mount_path == "/models/llama"
    assert launcher.spec.volumes[0].read_only is True
    assert launcher.spec.args is not None
    assert "python3 -m vllm.entrypoints.openai.api_server" in launcher.spec.args[0]
    assert "--dtype" not in launcher.spec.args[0]
    assert "sleep infinity" not in launcher.spec.args[0]


def test_inference_launcher_manifest_includes_dtype_when_configured(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    payload = _cell_manifest().model_dump(by_alias=True)
    payload["spec"]["executor"]["dtype"] = "half"
    manifest = InferenceCellManifest.model_validate(payload)
    stage0 = StagePlacement(stage=0, site_id="site-a", node_id="node-a", gpu_indices=[0])
    alloc = {"api_port": 18080}

    launcher = ctrl._ray_launcher_manifest(manifest, stage0, alloc)

    assert launcher.spec.args is not None
    assert '--dtype "$DTYPE"' in launcher.spec.args[0]
    assert launcher.spec.env is not None
    assert {"name": "DTYPE", "value": "half"} in launcher.spec.env


def test_inference_launcher_manifest_can_hold_on_failure_in_debug_mode(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_DEBUG_HOLD_ON_FAILURE", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    manifest = _cell_manifest()
    stage0 = StagePlacement(stage=0, site_id="site-a", node_id="node-a", gpu_indices=[0])
    alloc = {"api_port": 18080}

    launcher = ctrl._ray_launcher_manifest(manifest, stage0, alloc)

    assert launcher.spec.args is not None
    assert "sleep infinity" in launcher.spec.args[0]


def test_inference_ray_head_manifest_does_not_publish_api_service_port(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    manifest = _cell_manifest()
    stage0 = StagePlacement(stage=0, site_id="site-a", node_id="node-a", gpu_indices=[0])
    alloc = {
        "api_port": 18080,
        "master_addr": "10.255.0.10",
        "member_fabric_ips": {"node-a": "10.250.0.10"},
    }

    ray_head = ctrl._ray_worker_manifest(manifest, stage0, alloc, leader=True)

    assert ray_head.spec.service is None
    assert ray_head.spec.health is None
    assert ray_head.spec.volumes == []


def test_single_node_lan_direct_ray_uses_launcher_as_leader_and_hostport_endpoint(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_EXPERIMENTAL", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_node(
        "node-a",
        name="node-a",
        labels={"site": "site-a"},
        capabilities={},
        endpoint="http://192.168.29.148:9109",
    )
    store.record_heartbeat("node-a", "Ready")
    manifest = _single_node_ray_manifest()
    store.register_inference_cell(manifest, source="test")
    store.update_inference_cell_status(
        manifest.metadata.name,
        namespace=manifest.metadata.namespace,
        phase="STARTING_LEADER",
        allocations={
            "placements": [
                {
                    "stage": 0,
                    "site_id": "site-a",
                    "node_id": "node-a",
                    "gpu_indices": [0],
                }
            ],
            "api_port": 18080,
            "master_addr": "10.250.0.10",
            "member_fabric_ips": {"node-a": "10.250.0.10"},
            "fabric_mode": "lan_direct",
            "active_executor": "ray",
            "execution": {"workloads": []},
        },
        conditions={},
        last_error=None,
    )
    ctrl = InferenceCellController(store)
    applied: list[str] = []

    def fake_apply(node_id, applied_manifest, *, revision=1):  # type: ignore[no-untyped-def]
        _ = (node_id, revision)
        applied.append(applied_manifest.metadata.name)
        return {
            "node_id": "node-a",
            "endpoint": "http://192.168.29.148:9109",
            "app_name": f"default/{applied_manifest.metadata.name}",
            "pod_states": [],
        }

    monkeypatch.setattr(ctrl, "_apply_manifest_to_node", fake_apply)

    rec = ctrl._run_once(manifest)

    assert rec.phase == "JOINING"
    assert applied == [f"{manifest.metadata.name}-ray-launcher"]
    assert rec.allocations["api_endpoint"] == "192.168.29.148:18080"
    assert rec.allocations["leader_node_id"] == "node-a"
    assert rec.allocations["workloads"]["leader"]["type"] == "ray-launcher"
    assert [item["role"] for item in rec.allocations["execution"]["workloads"]] == ["ray-launcher"]


def test_inference_cell_joining_waits_for_leader_api_health(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_EXPERIMENTAL", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    manifest = _cell_manifest(name="join-pending")
    store.register_inference_cell(manifest, source="test")
    alloc = _joining_alloc("join-pending")
    store.update_inference_cell_status(
        manifest.metadata.name,
        namespace=manifest.metadata.namespace,
        phase="JOINING",
        allocations=alloc,
        conditions={},
        last_error=None,
    )
    ctrl = InferenceCellController(store)
    runtime = _JoinRuntime(
        [
            f"default/{manifest.metadata.name}-ray-head",
            f"default/{manifest.metadata.name}-ray-launcher",
        ]
    )
    monkeypatch.setattr(
        ctrl,
        "_runtime_for_node",
        lambda node_id: (runtime, "http://node-a:9109"),
    )
    monkeypatch.setattr(
        ctrl,
        "_probe_leader_api",
        lambda current_alloc: (False, "http://10.0.0.10:18080/health status=503"),
    )

    rec = ctrl._run_once(manifest)
    assert rec.phase == "JOINING"
    assert rec.last_error == "JOIN_API_NOT_READY"
    assert rec.conditions["ApiReady"]["status"] is False
    assert "leader api not ready" in rec.conditions["ApiReady"]["message"]


def test_inference_cell_joining_requires_leader_api_health_for_ready(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_EXPERIMENTAL", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    manifest = _cell_manifest(name="join-ready")
    store.register_inference_cell(manifest, source="test")
    alloc = _joining_alloc("join-ready")
    store.update_inference_cell_status(
        manifest.metadata.name,
        namespace=manifest.metadata.namespace,
        phase="JOINING",
        allocations=alloc,
        conditions={},
        last_error="JOIN_API_NOT_READY",
    )
    ctrl = InferenceCellController(store)
    runtime = _JoinRuntime(
        [
            f"default/{manifest.metadata.name}-ray-head",
            f"default/{manifest.metadata.name}-ray-launcher",
        ]
    )
    monkeypatch.setattr(
        ctrl,
        "_runtime_for_node",
        lambda node_id: (runtime, "http://node-a:9109"),
    )
    monkeypatch.setattr(
        ctrl,
        "_probe_leader_api",
        lambda current_alloc: (True, "http://10.0.0.10:18080/health"),
    )

    rec = ctrl._run_once(manifest)
    assert rec.phase == "READY"
    assert rec.last_error is None
    assert rec.conditions["ApiReady"]["status"] is True
    assert rec.conditions["ApiReady"]["message"] == "http://10.0.0.10:18080/health"


def test_inference_cell_joining_falls_back_to_mp_when_ray_runtime_stops(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_EXPERIMENTAL", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    payload = _cell_manifest(name="join-fallback").model_dump(by_alias=True)
    payload["spec"]["executor"] = {"fallbackMode": "mp_on_failure"}
    manifest = InferenceCellManifest.model_validate(payload)
    store.register_inference_cell(manifest, source="test")
    alloc = _joining_alloc("join-fallback")
    store.update_inference_cell_status(
        manifest.metadata.name,
        namespace=manifest.metadata.namespace,
        phase="JOINING",
        allocations=alloc,
        conditions={},
        last_error=None,
    )
    ctrl = InferenceCellController(store)
    runtime = _JoinRuntime(
        [
            f"default/{manifest.metadata.name}-ray-head",
            f"default/{manifest.metadata.name}-ray-launcher",
        ],
        running=False,
    )
    monkeypatch.setattr(
        ctrl,
        "_runtime_for_node",
        lambda node_id: (runtime, "http://node-a:9109"),
    )

    rec = ctrl._run_once(manifest)

    assert rec.phase == "STARTING_WORKERS"
    assert rec.allocations["active_executor"] == "mp"
    assert rec.conditions["ExecutorFallback"]["status"] is True
