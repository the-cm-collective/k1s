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
    FabricCognitiveSignalRecord,
    FabricDasCellBundleRecord,
    FabricDasQueryTraceRecord,
    FabricDasReplicationRecord,
    FabricDecisionTraceRecord,
    FabricLandingZoneRecord,
    FabricMovementRecord,
    FabricResidencyRecord,
    FabricTransferCapabilityRecord,
    FabricTransferLeaseRecord,
    FabricTransportAttemptRecord,
)
from ae.fabric.phase_assurance import (
    PHASE_REQUIREMENTS,
    assess_fabric_phases,
    f1_evidence_from_nodes,
    f2_evidence_from_store,
    f3_evidence_from_store,
    f4_evidence_from_store,
    f5_evidence_from_store,
)
from ae.fabric.symbolic import (
    SYMBOLIC_ADVISORY_DECISION_API_VERSION,
    SYMBOLIC_ADVISORY_DECISION_REQUIRED_FIELDS,
    SYMBOLIC_FACT_NAMESPACE,
    SYMBOLIC_FACT_SOURCE_WORKERBEE_AI_FABRIC,
    SYMBOLIC_RELATIONSHIP_PREDICATES,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "fabric_phase_assurance.py"


def _complete(*phase_ids: str) -> dict[str, dict[str, bool]]:
    return {
        phase_id: dict.fromkeys(PHASE_REQUIREMENTS[phase_id], True)
        for phase_id in phase_ids
    }


def test_symbolic_relationship_vocabulary_matches_workerbee_runtime_facts() -> None:
    assert SYMBOLIC_FACT_NAMESPACE == "runtime"
    assert SYMBOLIC_FACT_SOURCE_WORKERBEE_AI_FABRIC == (
        "workerbee.ai-fabric.runtime-facts/v1"
    )
    assert SYMBOLIC_ADVISORY_DECISION_API_VERSION == (
        "workerbee.ai-fabric.advisory-decision/v1"
    )
    assert SYMBOLIC_ADVISORY_DECISION_REQUIRED_FIELDS == (
        "subject",
        "intent",
        "recommended_action",
        "confidence",
        "evidence_refs",
        "risks",
        "blocked_conditions",
        "authoritative",
    )
    assert SYMBOLIC_RELATIONSHIP_PREDICATES == (
        "owns_service",
        "depends_on",
        "serves_model",
        "requires_resource",
        "produced_artifact",
        "supports_advisory",
    )


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


def test_f4_evidence_from_store_covers_accelerated_movement_readiness(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    chunk_id = "sha256:" + ("c" * 64)
    store.upsert_fabric_transfer_capability(
        FabricTransferCapabilityRecord(
            capability_id="cap-node-a-node-b-roce",
            node_id="node-a",
            peer_node_id="node-b",
            transport="roce",
            status="negotiated",
            priority=100,
            capabilities={"rnic": "e810", "path": "development"},
            fallback_transport="lan_direct",
            created_at=now,
            updated_at=now,
        )
    )
    store.upsert_fabric_landing_zone(
        FabricLandingZoneRecord(
            zone_id="zone-node-b",
            node_id="node-b",
            path="/srv/storage/landing",
            capacity_bytes=8192,
            reserved_bytes=4096,
            safety_state="ready",
            cleanup_policy="lease_expiry",
            created_at=now,
            updated_at=now,
        )
    )
    store.upsert_fabric_transfer_lease(
        FabricTransferLeaseRecord(
            lease_id="lease-1",
            chunk_id=chunk_id,
            source_node_id="node-a",
            target_node_id="node-b",
            transport="roce",
            status="active",
            holder="controller",
            landing_zone_id="zone-node-b",
            digest=chunk_id,
            epoch=4,
            expires_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    store.record_fabric_transport_attempt(
        FabricTransportAttemptRecord(
            attempt_id="attempt-1",
            lease_id="lease-1",
            chunk_id=chunk_id,
            transport="roce",
            status="fallback",
            fallback_used=True,
            fallback_transport="lan_direct",
            error="roce path disabled in dev lab",
            started_at=now,
            finished_at=now,
            created_at=now,
        )
    )

    report = assess_fabric_phases(
        {
            "F0": _complete("F0")["F0"],
            "F1": _complete("F1")["F1"],
            "F2": _complete("F2")["F2"],
            "F3": _complete("F3")["F3"],
            "F4": f4_evidence_from_store(store),
        }
    )

    assert report["phases"]["F4"]["status"] == "present"
    assert report["phases"]["F4"]["gate"]["ready"] is True
    f4_evidence = report["phases"]["F4"]["evidence"]
    assert f4_evidence["capability_negotiation"]["negotiated_count"] == 1
    assert f4_evidence["standard_transport_fallback"]["fallback_attempt_count"] == 1


def test_f5_evidence_from_store_covers_das_cell_readiness(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    store.upsert_fabric_das_cell_bundle(
        FabricDasCellBundleRecord(
            bundle_id="das-site-a-runtime",
            site_id="site-a",
            cell_id="runtime",
            version="2026-06-01",
            storage_ref="/srv/storage/k1s/ai-fabric-lab/das",
            facts_ref="das://site-a/runtime/facts.jsonl",
            status="ready",
            labels={"lab": "ai-fabric"},
            created_at=now,
            updated_at=now,
        )
    )
    store.record_fabric_das_query_trace(
        FabricDasQueryTraceRecord(
            trace_id="query-trace-1",
            bundle_id="das-site-a-runtime",
            site_id="site-a",
            query_id="query-1",
            query_kind="advisory",
            local_first=True,
            warmed_refs=["das://site-a/runtime/facts.jsonl"],
            promoted_refs=["qdrant://ai_fabric_corpus/k1s"],
            fallback_sites=[],
            result_ref="trace://query-trace-1",
            created_at=now,
        )
    )
    store.record_fabric_das_replication(
        FabricDasReplicationRecord(
            replication_id="replication-1",
            bundle_id="das-site-a-runtime",
            source_site_id="site-a",
            target_site_id="site-b",
            mode="controlled",
            status="planned",
            approved_by="operator",
            reason="warm secondary DAS cell",
            created_at=now,
            updated_at=now,
        )
    )
    store.record_fabric_cognitive_signal(
        FabricCognitiveSignalRecord(
            signal_id="signal-1",
            subject_type="das-cell",
            subject_id="das-site-a-runtime",
            signal_kind="continuity",
            continuity_ref="trace://query-trace-1",
            coherence_score=0.87,
            overload_state="nominal",
            review_gate="operator_review",
            advisory_trace_id="trace-1",
            created_at=now,
        )
    )

    report = assess_fabric_phases(
        {
            "F0": _complete("F0")["F0"],
            "F1": _complete("F1")["F1"],
            "F2": _complete("F2")["F2"],
            "F3": _complete("F3")["F3"],
            "F5": f5_evidence_from_store(store),
        }
    )

    assert report["phases"]["F5"]["status"] == "present"
    assert report["phases"]["F5"]["gate"]["ready"] is True
    f5_evidence = report["phases"]["F5"]["evidence"]
    assert f5_evidence["das_cell_bundles"]["bundle_count"] == 1
    assert f5_evidence["cognitive_fabric_substrate"]["review_gates"] == ["operator_review"]


def test_f4_and_f5_store_evidence_report_missing_keys_when_incomplete(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")

    report = assess_fabric_phases(
        {
            "F0": _complete("F0")["F0"],
            "F1": _complete("F1")["F1"],
            "F2": _complete("F2")["F2"],
            "F3": _complete("F3")["F3"],
            "F4": f4_evidence_from_store(store),
            "F5": f5_evidence_from_store(store),
        }
    )

    assert report["phases"]["F4"]["status"] == "missing"
    assert report["phases"]["F4"]["missing"] == list(PHASE_REQUIREMENTS["F4"])
    assert report["phases"]["F5"]["status"] == "missing"
    assert report["phases"]["F5"]["missing"] == list(PHASE_REQUIREMENTS["F5"])


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
