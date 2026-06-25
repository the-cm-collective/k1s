from pathlib import Path

import pytest

from ae.controller.spec import (
    InferenceCellManifest,
    InferenceCellSetManifest,
    ManifestError,
    load_any_manifest,
    load_manifest,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _ai_max_edge_cell_payload() -> dict:
    return {
        "apiVersion": "ae.dev/v1alpha1",
        "kind": "InferenceCell",
        "metadata": {"name": "ai-max-edge-cell"},
        "spec": {
            "cellContract": {"profile": "ai-max-edge-cell-v1"},
            "model": {"modelId": "llama", "localPath": "/models/llama"},
            "parallelism": {"tp": 1, "pp": 4},
            "members": [
                {
                    "siteId": "edge-cell",
                    "nodeId": "gateway-1",
                    "gpuCount": 1,
                    "role": "gateway",
                    "computeEligible": True,
                },
                {
                    "siteId": "edge-cell",
                    "nodeId": "cell-node-1",
                    "gpuCount": 1,
                    "role": "cell-node",
                    "computeEligible": True,
                },
                {
                    "siteId": "edge-cell",
                    "nodeId": "cell-node-2",
                    "gpuCount": 1,
                    "role": "cell-node",
                    "computeEligible": True,
                },
                {
                    "siteId": "edge-cell",
                    "nodeId": "cell-node-3",
                    "gpuCount": 1,
                    "role": "cell-node",
                    "computeEligible": True,
                },
            ],
        },
    }


def test_load_any_manifest_inference_cell(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "cell.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: InferenceCell
metadata:
  name: demo-cell
spec:
  model:
    modelId: llama
    localPath: /models/llama
  parallelism:
    tp: 1
    pp: 2
  executor:
    type: ray
    fallbackMode: mp_on_failure
    dtype: half
    runtimeClassName: nvidia
  members:
    - siteId: site-a
      nodeId: node-a
      gpuCount: 1
    - siteId: site-b
      nodeId: node-b
      gpuCount: 1
  linkMetrics:
    - fromSite: site-a
      toSite: site-b
      rttP95Ms: 5
      jitterP95Ms: 0.3
      lossPct: 0.0
        """,
    )
    doc = load_any_manifest(p)
    assert isinstance(doc, InferenceCellManifest)
    assert doc.metadata.name == "demo-cell"
    assert doc.spec.executor.type == "ray"
    assert doc.spec.executor.fallback_mode == "mp_on_failure"
    assert doc.spec.executor.dtype == "half"
    assert doc.spec.executor.runtime_class_name == "nvidia"
    assert doc.spec.fabric.mode == "lan_direct"


def test_load_any_manifest_inference_cell_normalizes_blank_dtype(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "cell-blank-dtype.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: InferenceCell
metadata:
  name: demo-cell
spec:
  model:
    modelId: llama
    localPath: /models/llama
  executor:
    dtype: "   "
  members:
    - siteId: site-a
      nodeId: node-a
      gpuCount: 1
  linkMetrics: []
        """,
    )
    doc = load_any_manifest(p)
    assert isinstance(doc, InferenceCellManifest)
    assert doc.spec.executor.dtype is None


def test_load_any_manifest_inference_cellset(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "cellset.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: InferenceCellSet
metadata:
  name: demo-set
spec:
  replicas: 0
  template:
    model:
      modelId: llama
      localPath: /models/llama
    parallelism:
      tp: 1
      pp: 1
    members:
      - siteId: site-a
        nodeId: node-a
        gpuCount: 1
        """,
    )
    doc = load_any_manifest(p)
    assert isinstance(doc, InferenceCellSetManifest)
    assert doc.spec.replicas == 0


def test_inference_cell_accepts_ai_max_edge_cell_contract() -> None:
    doc = InferenceCellManifest.model_validate(_ai_max_edge_cell_payload())

    assert doc.spec.cell_contract is not None
    assert doc.spec.cell_contract.profile == "ai-max-edge-cell-v1"
    assert doc.spec.cell_contract.gateway_reserved_gpu_fraction == 0.0
    assert doc.spec.cell_contract.autonomy.connected_mode == "normal-connected"
    assert doc.spec.cell_contract.autonomy.core_link_unavailable_mode == "degraded-local-only"
    assert doc.spec.cell_contract.autonomy.reconnect_mode == "reconcile-on-restore"
    assert doc.spec.cell_contract.autonomy.core_link_uptime_threshold_pct == 80.0
    assert doc.spec.cell_contract.gateway_discovery.mode == "lan-local"
    assert doc.spec.cell_contract.gateway_discovery.fabric_cell_count == 1
    assert doc.spec.cell_contract.gateway_discovery.lan_scope == "default-lan"
    assert doc.spec.cell_contract.gateway_discovery.gateway_peer_ids == []
    assert len(doc.spec.members) == 4
    assert [member.role for member in doc.spec.members].count("gateway") == 1
    assert [member.role for member in doc.spec.members].count("cell-node") == 3
    assert all(member.compute_eligible for member in doc.spec.members)


def test_inference_cell_accepts_ai_max_gateway_reservation() -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"]["cellContract"]["gatewayReservedGpuFraction"] = 0.25

    doc = InferenceCellManifest.model_validate(payload)

    assert doc.spec.cell_contract is not None
    assert doc.spec.cell_contract.gateway_reserved_gpu_fraction == 0.25


def test_inference_cell_accepts_ai_max_disconnected_autonomy_policy() -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"]["cellContract"]["autonomy"] = {
        "connectedMode": "normal-connected",
        "coreLinkUnavailableMode": "degraded-local-only",
        "reconnectMode": "reconcile-on-restore",
        "coreLinkUptimeThresholdPct": 75,
    }

    doc = InferenceCellManifest.model_validate(payload)

    assert doc.spec.cell_contract is not None
    autonomy = doc.spec.cell_contract.autonomy
    assert autonomy.connected_mode == "normal-connected"
    assert autonomy.core_link_unavailable_mode == "degraded-local-only"
    assert autonomy.reconnect_mode == "reconcile-on-restore"
    assert autonomy.core_link_uptime_threshold_pct == 75


@pytest.mark.parametrize("fabric_cell_count", [1, 2, 4, 8])
def test_inference_cell_accepts_ai_max_gateway_discovery_fabric_sizes(
    fabric_cell_count: int,
) -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"]["cellContract"]["gatewayDiscovery"] = {
        "mode": "lan-local",
        "fabricCellCount": fabric_cell_count,
        "lanScope": "floor-a",
        "gatewayPeerIds": [f"gateway-peer-{idx}" for idx in range(1, fabric_cell_count)],
    }

    doc = InferenceCellManifest.model_validate(payload)

    assert doc.spec.cell_contract is not None
    discovery = doc.spec.cell_contract.gateway_discovery
    assert discovery.mode == "lan-local"
    assert discovery.fabric_cell_count == fabric_cell_count
    assert discovery.lan_scope == "floor-a"
    assert discovery.gateway_peer_ids == [
        f"gateway-peer-{idx}" for idx in range(1, fabric_cell_count)
    ]
    assert len(doc.spec.members) == 4
    assert [member.role for member in doc.spec.members].count("gateway") == 1
    assert [member.role for member in doc.spec.members].count("cell-node") == 3
    assert all(member.compute_eligible for member in doc.spec.members)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["spec"]["members"].pop(),
            "requires exactly 4 total members",
        ),
        (
            lambda payload: payload["spec"]["members"][1].update({"role": "gateway"}),
            "requires exactly 1 gateway member",
        ),
        (
            lambda payload: payload["spec"]["members"][1].update({"role": None}),
            "requires exactly 3 cell-node members",
        ),
        (
            lambda payload: payload["spec"]["members"][0].update({"computeEligible": False}),
            "requires all members to be compute eligible: gateway-1",
        ),
    ],
)
def test_inference_cell_rejects_invalid_ai_max_edge_cell_contract(mutate, message: str) -> None:
    payload = _ai_max_edge_cell_payload()
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        InferenceCellManifest.model_validate(payload)


def test_inference_cell_rejects_invalid_ai_max_gateway_reservation() -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"]["cellContract"]["gatewayReservedGpuFraction"] = 1.0

    with pytest.raises(ValueError, match="less than 1"):
        InferenceCellManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("connectedMode", "always-online", "normal-connected"),
        ("coreLinkUnavailableMode", "fail-closed", "degraded-local-only"),
        ("reconnectMode", "discard-local-state", "reconcile-on-restore"),
        ("coreLinkUptimeThresholdPct", 95, "less than or equal to 80"),
    ],
)
def test_inference_cell_rejects_invalid_ai_max_autonomy_policy(
    field: str, value: object, message: str
) -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"]["cellContract"]["autonomy"] = {
        "connectedMode": "normal-connected",
        "coreLinkUnavailableMode": "degraded-local-only",
        "reconnectMode": "reconcile-on-restore",
        "coreLinkUptimeThresholdPct": 80,
    }
    payload["spec"]["cellContract"]["autonomy"][field] = value

    with pytest.raises(ValueError, match=message):
        InferenceCellManifest.model_validate(payload)


@pytest.mark.parametrize("fabric_cell_count", [0, -1, 3])
def test_inference_cell_rejects_invalid_gateway_discovery_fabric_cell_count(
    fabric_cell_count: int,
) -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"]["cellContract"]["gatewayDiscovery"] = {
        "mode": "lan-local",
        "fabricCellCount": fabric_cell_count,
        "lanScope": "floor-a",
        "gatewayPeerIds": [],
    }

    with pytest.raises(ValueError, match="fabricCellCount must be one of 1, 2, 4, or 8"):
        InferenceCellManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("discovery", "message"),
    [
        (
            {
                "mode": "mdns",
                "fabricCellCount": 1,
                "lanScope": "floor-a",
                "gatewayPeerIds": [],
            },
            "lan-local",
        ),
        (
            {
                "mode": "lan-local",
                "fabricCellCount": 1,
                "lanScope": "   ",
                "gatewayPeerIds": [],
            },
            "lanScope must be non-empty",
        ),
        (
            {
                "mode": "lan-local",
                "fabricCellCount": 2,
                "lanScope": "floor-a",
                "gatewayPeerIds": [],
            },
            "exactly 1 peer id",
        ),
        (
            {
                "mode": "lan-local",
                "fabricCellCount": 2,
                "lanScope": "floor-a",
                "gatewayPeerIds": ["gateway-peer-1", "gateway-peer-1"],
            },
            "gatewayPeerIds must be unique",
        ),
        (
            {
                "mode": "lan-local",
                "fabricCellCount": 2,
                "lanScope": "floor-a",
                "gatewayPeerIds": ["   "],
            },
            "must not contain blank ids",
        ),
    ],
)
def test_inference_cell_rejects_malformed_gateway_discovery_config(
    discovery: dict, message: str
) -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"]["cellContract"]["gatewayDiscovery"] = discovery

    with pytest.raises(ValueError, match=message):
        InferenceCellManifest.model_validate(payload)


def test_load_manifest_rejects_non_deployment(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "bad.yaml",
        """
apiVersion: ae.dev/v1alpha1
kind: InferenceCell
metadata:
  name: bad
spec:
  model:
    modelId: m
    localPath: /m
  members: []
        """,
    )
    with pytest.raises(ManifestError):
        load_manifest(p)


@pytest.mark.parametrize(
    ("rel_path", "expected_name", "expected_dtype"),
    [
        ("specs/examples/inference/cell-a-single.yaml", "cell-a-single", "half"),
        ("specs/examples/inference/cell-b-single.yaml", "cell-b-single", None),
        ("specs/examples/inference/cell-ab-pp2-ray.yaml", "cell-ab-pp2-ray", "half"),
        ("specs/examples/inference/cell-ab-pp2-mp.yaml", "cell-ab-pp2-mp", "half"),
    ],
)
def test_checked_in_inference_examples_load(
    rel_path: str, expected_name: str, expected_dtype: str | None
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    doc = load_any_manifest(repo_root / rel_path)
    assert isinstance(doc, InferenceCellManifest)
    assert doc.metadata.name == expected_name
    assert doc.spec.executor.runtime_class_name == "nvidia"
    assert doc.spec.executor.dtype == expected_dtype
    assert doc.spec.executor.ray_image == "rayproject/ray:latest"
    assert doc.spec.executor.mp_image == "vllm/vllm-openai:latest"
    assert doc.spec.executor.launcher_image == "vllm/vllm-openai:latest"
