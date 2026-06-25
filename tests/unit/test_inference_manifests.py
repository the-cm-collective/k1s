from pathlib import Path

import pytest

from ae.controller.spec import (
    InferenceCellManifest,
    InferenceCellSetManifest,
    ManifestError,
    ai_max_autonomy_initial_state,
    ai_max_autonomy_transition_trace,
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


def _ai_max_installer_payload() -> dict:
    return {
        "profile": "nixos-ai-max-edge-cell-installer-v1",
        "image": "nixos-ai-max-edge-cell-installer",
        "signedBy": "k1s-core-root-of-trust",
        "artifact": {
            "name": "nixos-ai-max-edge-cell-installer",
            "profile": "nixos-ai-max-edge-cell-installer-v1",
            "image": "nixos-ai-max-edge-cell-installer",
            "version": "stage7-local",
            "artifactDigest": (
                "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            ),
            "manifestDigest": (
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            "pathCoverage": ["gateway", "cell-node"],
            "provenance": {
                "builder": "k1s-public-stage7-local-simulator",
                "sourceRevision": "public-dev-stage7",
                "createdAt": "2026-06-25T00:00:00Z",
            },
        },
        "signature": {
            "algorithm": "k1s-local-sim-ed25519-sha256",
            "signingKeyId": "k1s-core-root-of-trust",
            "signedDigest": (
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            "signature": (
                "k1s-sim-signature:3333333333333333333333333333333333333333333333333333333333333333"
            ),
        },
        "roleScaffolds": [
            {
                "role": "gateway",
                "moduleRef": "nixos/modules/ai-max/installer/gateway.nix",
                "configRef": "nixos/configs/ai-max/gateway-installed-system.nix",
                "derivedFromManifestDigest": (
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "postInstall": {
                    "autoBoot": "enabled",
                    "connectTarget": "core",
                    "usbDevicePolicy": "signed-only",
                    "displayMode": "telemetry",
                },
            },
            {
                "role": "cell-node",
                "moduleRef": "nixos/modules/ai-max/installer/cell-node.nix",
                "configRef": "nixos/configs/ai-max/cell-node-installed-system.nix",
                "derivedFromManifestDigest": (
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "postInstall": {
                    "autoBoot": "enabled",
                    "connectTarget": "gateway",
                    "usbDevicePolicy": "limited",
                    "displayMode": "connect-monitor-to-gateway",
                },
            },
        ],
        "bootEvidence": [
            {
                "nodeId": "gateway-1",
                "role": "gateway",
                "installerProfile": "nixos-ai-max-edge-cell-installer-v1",
                "installerImage": "nixos-ai-max-edge-cell-installer",
                "artifactDigest": (
                    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
                ),
                "manifestDigest": (
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "bootMeasurementDigest": (
                    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                ),
                "signingKeyId": "k1s-core-root-of-trust",
                "verifierTrustRoot": "k1s-core-root-of-trust",
                "nonce": "k1s-stage9-nonce-gateway",
                "createdAt": "2026-06-25T00:00:00Z",
                "verification": {
                    "status": "verified",
                    "verifier": "k1s-local-boot-evidence-verifier-v1",
                    "trustRoot": "k1s-core-root-of-trust",
                    "failureReasons": [],
                },
            },
            {
                "nodeId": "cell-node-1",
                "role": "cell-node",
                "installerProfile": "nixos-ai-max-edge-cell-installer-v1",
                "installerImage": "nixos-ai-max-edge-cell-installer",
                "artifactDigest": (
                    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
                ),
                "manifestDigest": (
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "bootMeasurementDigest": (
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
                "signingKeyId": "k1s-core-root-of-trust",
                "verifierTrustRoot": "k1s-core-root-of-trust",
                "nonce": "k1s-stage9-nonce-cell-node",
                "createdAt": "2026-06-25T00:00:00Z",
                "verification": {
                    "status": "verified",
                    "verifier": "k1s-local-boot-evidence-verifier-v1",
                    "trustRoot": "k1s-core-root-of-trust",
                    "failureReasons": [],
                },
            },
        ],
        "assurance": {
            "secureImageValidation": "enabled",
            "bootValidation": "measured-verified",
            "tamperDetection": "enabled",
            "validationFailureAction": "disable-quarantine",
            "coreAlerting": "when-connected",
        },
        "installPaths": [
            {
                "path": "gateway",
                "postInstall": {
                    "autoBoot": "enabled",
                    "connectTarget": "core",
                    "usbDevicePolicy": "signed-only",
                    "displayMode": "telemetry",
                },
            },
            {
                "path": "cell-node",
                "postInstall": {
                    "autoBoot": "enabled",
                    "connectTarget": "gateway",
                    "usbDevicePolicy": "limited",
                    "displayMode": "connect-monitor-to-gateway",
                },
            },
        ],
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
    installer = doc.spec.cell_contract.installer
    assert installer.profile == "nixos-ai-max-edge-cell-installer-v1"
    assert installer.image == "nixos-ai-max-edge-cell-installer"
    assert installer.signed_by == "k1s-core-root-of-trust"
    assert installer.artifact.name == "nixos-ai-max-edge-cell-installer"
    assert installer.artifact.profile == "nixos-ai-max-edge-cell-installer-v1"
    assert installer.artifact.image == "nixos-ai-max-edge-cell-installer"
    assert installer.artifact.version == "stage7-local"
    assert installer.artifact.artifact_digest.startswith("sha256:")
    assert installer.artifact.manifest_digest.startswith("sha256:")
    assert installer.artifact.path_coverage == ["gateway", "cell-node"]
    assert installer.artifact.provenance.builder == "k1s-public-stage7-local-simulator"
    assert installer.artifact.provenance.source_revision == "public-dev-stage7"
    assert installer.artifact.provenance.created_at == "2026-06-25T00:00:00Z"
    assert installer.signature.algorithm == "k1s-local-sim-ed25519-sha256"
    assert installer.signature.signing_key_id == "k1s-core-root-of-trust"
    assert installer.signature.signed_digest == installer.artifact.manifest_digest
    assert installer.signature.signature.startswith("k1s-sim-signature:")
    assert [scaffold.role for scaffold in installer.role_scaffolds] == ["gateway", "cell-node"]
    gateway_scaffold, cell_node_scaffold = installer.role_scaffolds
    assert gateway_scaffold.module_ref == "nixos/modules/ai-max/installer/gateway.nix"
    assert gateway_scaffold.config_ref == "nixos/configs/ai-max/gateway-installed-system.nix"
    assert gateway_scaffold.derived_from_manifest_digest == installer.artifact.manifest_digest
    assert gateway_scaffold.post_install.connect_target == "core"
    assert gateway_scaffold.post_install.usb_device_policy == "signed-only"
    assert gateway_scaffold.post_install.display_mode == "telemetry"
    assert cell_node_scaffold.module_ref == "nixos/modules/ai-max/installer/cell-node.nix"
    assert cell_node_scaffold.config_ref == "nixos/configs/ai-max/cell-node-installed-system.nix"
    assert cell_node_scaffold.derived_from_manifest_digest == installer.artifact.manifest_digest
    assert cell_node_scaffold.post_install.connect_target == "gateway"
    assert cell_node_scaffold.post_install.usb_device_policy == "limited"
    assert cell_node_scaffold.post_install.display_mode == "connect-monitor-to-gateway"
    assert [evidence.role for evidence in installer.boot_evidence] == ["gateway", "cell-node"]
    gateway_evidence, cell_node_evidence = installer.boot_evidence
    assert gateway_evidence.node_id == "gateway-1"
    assert gateway_evidence.artifact_digest == installer.artifact.artifact_digest
    assert gateway_evidence.manifest_digest == installer.artifact.manifest_digest
    assert gateway_evidence.boot_measurement_digest.startswith("sha256:")
    assert gateway_evidence.signing_key_id == "k1s-core-root-of-trust"
    assert gateway_evidence.verifier_trust_root == "k1s-core-root-of-trust"
    assert gateway_evidence.nonce == "k1s-stage9-nonce-gateway"
    assert gateway_evidence.created_at == "2026-06-25T00:00:00Z"
    assert gateway_evidence.verification.status == "verified"
    assert gateway_evidence.verification.verifier == "k1s-local-boot-evidence-verifier-v1"
    assert gateway_evidence.verification.failure_reasons == []
    assert cell_node_evidence.node_id == "cell-node-1"
    assert cell_node_evidence.boot_measurement_digest.startswith("sha256:")
    assert cell_node_evidence.nonce == "k1s-stage9-nonce-cell-node"
    assert installer.assurance.secure_image_validation == "enabled"
    assert installer.assurance.boot_validation == "measured-verified"
    assert installer.assurance.tamper_detection == "enabled"
    assert installer.assurance.validation_failure_action == "disable-quarantine"
    assert installer.assurance.core_alerting == "when-connected"
    assert [path.path for path in installer.install_paths] == ["gateway", "cell-node"]
    assert installer.install_paths[0].post_install.auto_boot == "enabled"
    assert installer.install_paths[0].post_install.connect_target == "core"
    assert installer.install_paths[0].post_install.display_mode == "telemetry"
    assert installer.install_paths[1].post_install.connect_target == "gateway"
    assert installer.install_paths[1].post_install.display_mode == "connect-monitor-to-gateway"
    assert len(doc.spec.members) == 4
    assert [member.role for member in doc.spec.members].count("gateway") == 1
    assert [member.role for member in doc.spec.members].count("cell-node") == 3
    assert all(member.compute_eligible for member in doc.spec.members)


def test_inference_cell_accepts_ai_max_installer_contract() -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"]["cellContract"]["installer"] = _ai_max_installer_payload()

    doc = InferenceCellManifest.model_validate(payload)

    assert doc.spec.cell_contract is not None
    installer = doc.spec.cell_contract.installer
    assert installer.profile == "nixos-ai-max-edge-cell-installer-v1"
    assert installer.image == "nixos-ai-max-edge-cell-installer"
    assert installer.signed_by == "k1s-core-root-of-trust"
    assert installer.artifact.path_coverage == ["gateway", "cell-node"]
    assert installer.signature.signing_key_id == "k1s-core-root-of-trust"
    assert [scaffold.role for scaffold in installer.role_scaffolds] == ["gateway", "cell-node"]
    assert installer.role_scaffolds[0].module_ref.endswith("/gateway.nix")
    assert installer.role_scaffolds[1].module_ref.endswith("/cell-node.nix")
    assert [evidence.role for evidence in installer.boot_evidence] == ["gateway", "cell-node"]
    assert all(evidence.verification.status == "verified" for evidence in installer.boot_evidence)
    assert [path.path for path in installer.install_paths] == ["gateway", "cell-node"]
    gateway, cell_node = installer.install_paths
    assert gateway.post_install.connect_target == "core"
    assert gateway.post_install.usb_device_policy == "signed-only"
    assert gateway.post_install.display_mode == "telemetry"
    assert cell_node.post_install.connect_target == "gateway"
    assert cell_node.post_install.usb_device_policy == "limited"
    assert cell_node.post_install.display_mode == "connect-monitor-to-gateway"


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


def test_ai_max_autonomy_state_defaults_connected_with_cache_ready() -> None:
    doc = InferenceCellManifest.model_validate(_ai_max_edge_cell_payload())

    state = ai_max_autonomy_initial_state(doc.spec.cell_contract)

    assert state is not None
    assert state.state == "connected"
    assert state.local_service_continuity is True
    assert state.cache.ready is True
    assert state.cache.approved_workload_ref == "inferencecell/default/ai-max-edge-cell"
    assert state.cache.model_artifact_ref == "models/llama:stage11-local"
    assert state.cache.service_endpoints == {
        "gateway-api": "http://gateway.local:18080",
        "cell-monitor": "http://gateway.local:19090",
    }
    assert state.cache.last_core_sync == "core-sync-stage11"
    assert state.transition_trace == []


def test_ai_max_autonomy_core_loss_degrades_and_retains_local_services() -> None:
    doc = InferenceCellManifest.model_validate(_ai_max_edge_cell_payload())

    state = ai_max_autonomy_transition_trace(
        doc.spec.cell_contract,
        ["core-link-lost", "local-services-retained"],
    )

    assert state is not None
    assert state.state == "degraded-local-only"
    assert state.local_service_continuity is True
    assert state.transition_trace == [
        {
            "from": "connected",
            "event": "core-link-lost",
            "to": "core-link-unavailable",
        },
        {
            "from": "core-link-unavailable",
            "event": "local-services-retained",
            "to": "degraded-local-only",
        },
    ]


def test_ai_max_autonomy_core_restore_reconciles_to_reconciled() -> None:
    doc = InferenceCellManifest.model_validate(_ai_max_edge_cell_payload())

    state = ai_max_autonomy_transition_trace(
        doc.spec.cell_contract,
        [
            "core-link-lost",
            "local-services-retained",
            "core-link-restored",
            "reconcile-completed",
        ],
    )

    assert state is not None
    assert state.state == "reconciled"
    assert state.local_service_continuity is True
    assert state.transition_trace[-2:] == [
        {
            "from": "degraded-local-only",
            "event": "core-link-restored",
            "to": "reconciling",
        },
        {
            "from": "reconciling",
            "event": "reconcile-completed",
            "to": "reconciled",
        },
    ]


def test_ai_max_autonomy_rejects_unsupported_transition() -> None:
    doc = InferenceCellManifest.model_validate(_ai_max_edge_cell_payload())
    state = ai_max_autonomy_initial_state(doc.spec.cell_contract)
    assert state is not None

    with pytest.raises(ValueError, match="unsupported autonomy transition"):
        state.apply_event("reconcile-completed")


def test_ai_max_autonomy_missing_contract_is_backward_compatible() -> None:
    payload = _ai_max_edge_cell_payload()
    payload["spec"].pop("cellContract")
    doc = InferenceCellManifest.model_validate(payload)

    assert ai_max_autonomy_initial_state(doc.spec.cell_contract) is None
    assert ai_max_autonomy_transition_trace(doc.spec.cell_contract, ["core-link-lost"]) is None


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


def test_inference_cell_rejects_missing_ai_max_installer_path() -> None:
    payload = _ai_max_edge_cell_payload()
    installer = _ai_max_installer_payload()
    installer["installPaths"].pop()
    payload["spec"]["cellContract"]["installer"] = installer

    with pytest.raises(ValueError, match="exactly gateway and cell-node"):
        InferenceCellManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda installer: installer.update({"signedBy": "lab-key"}),
            "k1s-core-root-of-trust",
        ),
        (
            lambda installer: installer["signature"].update({"signingKeyId": "lab-key"}),
            "k1s-core-root-of-trust",
        ),
        (
            lambda installer: installer["artifact"].update({"artifactDigest": ""}),
            "sha256:<64 hex>",
        ),
        (
            lambda installer: installer["artifact"].update({"manifestDigest": "sha256:bad"}),
            "sha256:<64 hex>",
        ),
        (
            lambda installer: installer["signature"].update({"signedDigest": ""}),
            "sha256:<64 hex>",
        ),
        (
            lambda installer: installer["signature"].update({"signature": ""}),
            "signature must be non-empty",
        ),
        (
            lambda installer: installer["artifact"].update({"profile": "lab-profile"}),
            "nixos-ai-max-edge-cell-installer-v1",
        ),
        (
            lambda installer: installer["artifact"].update({"image": "lab-image"}),
            "nixos-ai-max-edge-cell-installer",
        ),
        (
            lambda installer: installer["artifact"].update({"pathCoverage": ["gateway"]}),
            "pathCoverage must cover gateway and cell-node",
        ),
        (
            lambda installer: installer["signature"].update(
                {
                    "signedDigest": (
                        "sha256:4444444444444444444444444444444444444444444444444444444444444444"
                    )
                }
            ),
            "signedDigest must match artifact manifestDigest",
        ),
        (
            lambda installer: installer["roleScaffolds"].pop(),
            "roleScaffolds must contain exactly gateway and cell-node",
        ),
        (
            lambda installer: installer["roleScaffolds"][1].update(
                {
                    "role": "gateway",
                    "postInstall": {
                        "autoBoot": "enabled",
                        "connectTarget": "core",
                        "usbDevicePolicy": "signed-only",
                        "displayMode": "telemetry",
                    },
                }
            ),
            "roleScaffolds must not contain duplicate roles",
        ),
        (
            lambda installer: installer["roleScaffolds"][0]["postInstall"].update(
                {"connectTarget": "gateway"}
            ),
            "gateway role scaffold must connectTarget=core",
        ),
        (
            lambda installer: installer["roleScaffolds"][1]["postInstall"].update(
                {"connectTarget": "core"}
            ),
            "cell-node role scaffold must connectTarget=gateway",
        ),
        (
            lambda installer: installer["roleScaffolds"][0].update({"moduleRef": ""}),
            "moduleRef and configRef must be non-empty",
        ),
        (
            lambda installer: installer["roleScaffolds"][1].update({"configRef": "   "}),
            "moduleRef and configRef must be non-empty",
        ),
        (
            lambda installer: installer["roleScaffolds"][0]["postInstall"].update(
                {"usbDevicePolicy": "unrestricted"}
            ),
            "disabled",
        ),
        (
            lambda installer: installer["roleScaffolds"][0].update(
                {
                    "derivedFromManifestDigest": (
                        "sha256:5555555555555555555555555555555555555555555555555555555555555555"
                    )
                }
            ),
            "derivedFromManifestDigest must match artifact manifestDigest",
        ),
        (
            lambda installer: installer["bootEvidence"].pop(),
            "bootEvidence must contain exactly gateway and cell-node",
        ),
        (
            lambda installer: installer["bootEvidence"][1].update(
                {
                    "role": "gateway",
                    "nonce": "k1s-stage9-nonce-gateway",
                }
            ),
            "bootEvidence must not contain duplicate roles",
        ),
        (
            lambda installer: installer["bootEvidence"][0].update({"signingKeyId": "lab-key"}),
            "k1s-core-root-of-trust",
        ),
        (
            lambda installer: installer["bootEvidence"][0].update({"verifierTrustRoot": "lab-key"}),
            "k1s-core-root-of-trust",
        ),
        (
            lambda installer: installer["bootEvidence"][0].update(
                {
                    "artifactDigest": (
                        "sha256:6666666666666666666666666666666666666666666666666666666666666666"
                    )
                }
            ),
            "artifactDigest must match artifact digest",
        ),
        (
            lambda installer: installer["bootEvidence"][0].update(
                {
                    "manifestDigest": (
                        "sha256:7777777777777777777777777777777777777777777777777777777777777777"
                    )
                }
            ),
            "manifestDigest must match artifact manifestDigest",
        ),
        (
            lambda installer: installer["bootEvidence"][0].update({"bootMeasurementDigest": ""}),
            "sha256:<64 hex>",
        ),
        (
            lambda installer: installer["bootEvidence"][1].update(
                {"bootMeasurementDigest": "sha256:bad"}
            ),
            "sha256:<64 hex>",
        ),
        (
            lambda installer: installer["bootEvidence"][0].update({"nonce": ""}),
            "nodeId, nonce, and createdAt must be non-empty",
        ),
        (
            lambda installer: installer["bootEvidence"][0].update({"nonce": "stale-nonce"}),
            "nonce is stale or does not match role",
        ),
        (
            lambda installer: installer["bootEvidence"][0].update(
                {"installerProfile": "lab-profile"}
            ),
            "nixos-ai-max-edge-cell-installer-v1",
        ),
        (
            lambda installer: installer["bootEvidence"][1].update({"installerImage": "lab-image"}),
            "nixos-ai-max-edge-cell-installer",
        ),
        (
            lambda installer: installer["roleScaffolds"].pop(0),
            "roleScaffolds must contain exactly gateway and cell-node",
        ),
        (
            lambda installer: installer["bootEvidence"][0]["verification"].update(
                {"failureReasons": ["tampered-artifact"]}
            ),
            "failureReasons must be empty when verified",
        ),
        (
            lambda installer: installer["assurance"].update({"secureImageValidation": "disabled"}),
            "enabled",
        ),
        (
            lambda installer: installer["assurance"].update({"bootValidation": "measured-only"}),
            "measured-verified",
        ),
        (
            lambda installer: installer["assurance"].update(
                {"validationFailureAction": "alert-only"}
            ),
            "disable-quarantine",
        ),
        (
            lambda installer: installer["assurance"].update({"coreAlerting": "disabled"}),
            "when-connected",
        ),
        (
            lambda installer: installer["installPaths"][0]["postInstall"].update(
                {"autoBoot": "disabled"}
            ),
            "enabled",
        ),
        (
            lambda installer: installer["installPaths"][0]["postInstall"].update(
                {"connectTarget": "gateway"}
            ),
            "gateway path must connectTarget=core",
        ),
        (
            lambda installer: installer["installPaths"][1]["postInstall"].update(
                {"displayMode": "telemetry"}
            ),
            "cell-node path must displayMode=connect-monitor-to-gateway",
        ),
        (
            lambda installer: installer["installPaths"][1]["postInstall"].update(
                {"usbDevicePolicy": "unrestricted"}
            ),
            "disabled",
        ),
    ],
)
def test_inference_cell_rejects_invalid_ai_max_installer_contract(mutate, message: str) -> None:
    payload = _ai_max_edge_cell_payload()
    installer = _ai_max_installer_payload()
    mutate(installer)
    payload["spec"]["cellContract"]["installer"] = installer

    with pytest.raises(ValueError, match=message):
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
