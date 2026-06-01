from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from ae.controller.state import SQLiteStateStore
from ae.fabric.locality import (
    FabricAdvisoryRequestRecord,
    FabricAdvisoryResponseRecord,
    FabricChunkRecord,
    FabricDecisionTraceRecord,
    FabricMovementRecord,
    FabricResidencyRecord,
)
from ae.fabric.phase_assurance import (
    PHASE_REQUIREMENTS,
    assess_fabric_phases,
    f1_evidence_from_nodes,
    f2_evidence_from_store,
    f3_evidence_from_store,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "fabric_phase_assurance.py"


def _complete(*phase_ids: str) -> dict[str, dict[str, bool]]:
    return {
        phase_id: dict.fromkeys(PHASE_REQUIREMENTS[phase_id], True)
        for phase_id in phase_ids
    }


def test_f3_is_blocked_until_f1_and_f2_are_present() -> None:
    report = assess_fabric_phases(_complete("F0", "F3"))

    assert report["phases"]["F3"]["status"] == "present"
    assert report["phases"]["F3"]["gate"]["ready"] is False
    assert report["phases"]["F3"]["gate"]["blocked_by"] == ["F1", "F2"]


def test_f0_ready_does_not_make_f1_or_f2_ready() -> None:
    report = assess_fabric_phases(_complete("F0"))

    assert report["phases"]["F0"]["gate"]["ready"] is True
    assert report["phases"]["F1"]["status"] == "missing"
    assert report["phases"]["F1"]["gate"]["ready"] is False
    assert "typed_link_topology" in report["phases"]["F1"]["missing"]
    assert report["phases"]["F2"]["gate"]["blocked_by"] == ["F1"]


def test_later_phases_keep_dependency_order() -> None:
    report = assess_fabric_phases(_complete("F0", "F1", "F2", "F4", "F5"))

    assert report["phases"]["F2"]["gate"]["ready"] is True
    assert report["phases"]["F4"]["status"] == "present"
    assert report["phases"]["F4"]["gate"]["ready"] is False
    assert report["phases"]["F4"]["gate"]["blocked_by"] == ["F3"]
    assert report["phases"]["F5"]["gate"]["blocked_by"] == ["F3"]


def test_f0n_nvidia_subtrack_does_not_substitute_for_f0() -> None:
    report = assess_fabric_phases(_complete("F0n-nvidia-dev", "F1"))

    assert report["phases"]["F0n-nvidia-dev"]["gate"]["ready"] is True
    assert report["phases"]["F0"]["status"] == "missing"
    assert report["phases"]["F1"]["status"] == "present"
    assert report["phases"]["F1"]["gate"]["ready"] is False
    assert report["phases"]["F1"]["gate"]["blocked_by"] == ["F0"]


def test_f1_evidence_from_nodes_covers_typed_fact_families() -> None:
    evidence = f1_evidence_from_nodes(
        [
            {
                "node_id": "node-a",
                "labels": {
                    "gpu.present": "true",
                    "gpu.count": "1",
                    "gpu.models": "RTX 8000",
                },
                "capabilities": {
                    "accelerators": [
                        {
                            "vendor": "nvidia",
                            "family": "RTX 8000",
                            "deviceCount": 1,
                        }
                    ],
                    "storageDevices": [{"id": "nvme0", "mediaType": "nvme"}],
                    "networkInterfaces": [
                        {
                            "name": "enp1s0",
                            "linkMetrics": [
                                {
                                    "fromSite": "site-a",
                                    "toSite": "site-b",
                                    "rttP95Ms": 5.0,
                                }
                            ],
                        }
                    ],
                    "rdmaDevices": [{"name": "irdma0", "pcieBusId": "0000:01:00.0"}],
                    "identityRoles": {
                        "management": "spiffe://node-a/management",
                        "execution": "spiffe://node-a/execution",
                        "fabric": "spiffe://node-a/fabric",
                    },
                },
            }
        ]
    )

    report = assess_fabric_phases({"F1": evidence})

    assert report["phases"]["F1"]["status"] == "present"
    assert report["phases"]["F1"]["gate"]["ready"] is False
    assert report["phases"]["F1"]["gate"]["blocked_by"] == ["F0"]
    assert report["phases"]["F1"]["evidence"]["typed_accelerators"]["accelerator_count"] == 1
    assert report["phases"]["F1"]["evidence"]["typed_link_topology"]["link_metric_count"] == 1


def test_f2_evidence_from_store_covers_locality_contracts(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    chunk_id = "sha256:" + ("a" * 64)
    store.upsert_fabric_chunk(
        FabricChunkRecord(
            chunk_id=chunk_id,
            namespace="default",
            name="qwen-coder-shard-0",
            digest=chunk_id,
            size_bytes=4096,
            source_kind="model-shard",
            source_ref="hf://qwen/shard-0",
            labels={"model": "qwen"},
            created_at=now,
            updated_at=now,
        )
    )
    store.upsert_fabric_residency(
        FabricResidencyRecord(
            chunk_id=chunk_id,
            node_id="node-a",
            storage_device_id="nvme0n1",
            path="/srv/storage/models/qwen/shard-0",
            state="resident",
            integrity_state="verified",
            epoch=3,
            digest=chunk_id,
            verified_at=now,
            updated_at=now,
        )
    )
    store.upsert_fabric_movement(
        FabricMovementRecord(
            movement_id="move-1",
            chunk_id=chunk_id,
            direction="pull",
            source_node_id="node-a",
            target_node_id="node-b",
            status="complete",
            requested_by="controller",
            digest=chunk_id,
            epoch=3,
            created_at=now,
            updated_at=now,
        )
    )

    evidence = _complete("F0", "F1")
    evidence["F2"] = f2_evidence_from_store(store)
    report = assess_fabric_phases(evidence)

    assert report["phases"]["F2"]["status"] == "present"
    assert report["phases"]["F2"]["gate"]["ready"] is True
    assert report["phases"]["F2"]["evidence"]["content_addressed_chunks"]["chunk_count"] == 1
    integrity_evidence = report["phases"]["F2"]["evidence"]["integrity_epoch_semantics"]
    assert integrity_evidence["invalid_record_count"] == 0


def test_f3_evidence_from_store_covers_advisory_trace_contracts(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    store.record_fabric_advisory_request(
        FabricAdvisoryRequestRecord(
            request_id="req-1",
            subject_type="placement",
            subject_id="cell/qwen",
            intent="advise",
            facts_ref="das://facts/req-1",
            locality_snapshot_ref="fabric://snapshot/req-1",
            max_candidates=4,
            time_budget_ms=250,
            policy_mode="advisory_only",
            created_at=now,
        )
    )
    store.record_fabric_advisory_response(
        FabricAdvisoryResponseRecord(
            request_id="req-1",
            provider="workerbee-ai-router",
            status="ok",
            recommendation="prefer node-a",
            confidence=0.42,
            evidence_refs=["das://facts/req-1"],
            authoritative=False,
            created_at=now,
        )
    )
    store.record_fabric_decision_trace(
        FabricDecisionTraceRecord(
            trace_id="trace-1",
            request_id="req-1",
            deterministic_baseline={"winner": "node-b"},
            advisory_response={"winner": "node-a"},
            accepted=False,
            divergence_reason="lower locality score than deterministic baseline",
            replay_status="recorded",
            continuity_signals={"previous_trace_id": "trace-0"},
            coherence_signals={"retrieval_result_count": 2},
            created_at=now,
        )
    )

    report = assess_fabric_phases(
        {
            "F0": _complete("F0")["F0"],
            "F1": _complete("F1")["F1"],
            "F2": _complete("F2")["F2"],
            "F3": f3_evidence_from_store(store),
        }
    )

    assert report["phases"]["F3"]["status"] == "present"
    assert report["phases"]["F3"]["gate"]["ready"] is True
    assert report["phases"]["F3"]["evidence"]["advisory_contract"]["authoritative"] is False
    assert report["phases"]["F3"]["evidence"]["bounded_planning"]["bounded_request_count"] == 1


def test_assurance_script_emits_json_report(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_complete("F0", "F1", "F2")), encoding="utf-8")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--evidence", str(evidence_path), "--json"],
        check=True,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert report["api_version"] == "k1s.fabric.phase-assurance/v1"
    assert report["phases"]["F2"]["gate"]["ready"] is True
    assert report["phases"]["F3"]["gate"]["blocked_by"] == []
    assert report["phases"]["F3"]["gate"]["ready"] is False
