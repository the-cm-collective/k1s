from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

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
from ae.observability.http_api import FABRIC_ADVISORY_STATE_API_VERSION, _ApiHandler

CHUNK_ID = "sha256:" + ("b" * 64)


def _now() -> datetime:
    return datetime.now(UTC)


def _seed_locality(store: SQLiteStateStore) -> None:
    now = _now()
    store.upsert_fabric_chunk(
        FabricChunkRecord(
            chunk_id=CHUNK_ID,
            namespace="ai",
            name="qwen-coordinator-shard-0",
            digest=CHUNK_ID,
            size_bytes=8192,
            source_kind="model-shard",
            source_ref="hf://qwen/coordinator/shard-0",
            labels={"lane": "coordinator"},
            created_at=now,
            updated_at=now,
        )
    )
    store.upsert_fabric_residency(
        FabricResidencyRecord(
            chunk_id=CHUNK_ID,
            node_id="node-a",
            storage_device_id="nvme0",
            path="/srv/storage/models/qwen/coordinator/shard-0",
            state="resident",
            integrity_state="verified",
            epoch=7,
            digest=CHUNK_ID,
            verified_at=now,
            updated_at=now,
        )
    )
    store.upsert_fabric_movement(
        FabricMovementRecord(
            movement_id="move-qwen-0",
            chunk_id=CHUNK_ID,
            direction="push",
            source_node_id="node-a",
            target_node_id="node-b",
            status="queued",
            requested_by="controller",
            digest=CHUNK_ID,
            epoch=7,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_advisory(store: SQLiteStateStore) -> None:
    now = _now()
    store.record_fabric_advisory_request(
        FabricAdvisoryRequestRecord(
            request_id="req-qwen-0",
            subject_type="placement",
            subject_id="cell/qwen",
            intent="rank_nodes",
            facts_ref="das://facts/req-qwen-0",
            locality_snapshot_ref="fabric://snapshot/req-qwen-0",
            max_candidates=4,
            time_budget_ms=500,
            policy_mode="advisory_only",
            created_at=now,
        )
    )
    store.record_fabric_advisory_response(
        FabricAdvisoryResponseRecord(
            request_id="req-qwen-0",
            provider="workerbee-ai-router",
            status="ok",
            recommendation="prefer node-a",
            confidence=0.7,
            evidence_refs=["das://facts/req-qwen-0"],
            authoritative=False,
            created_at=now,
        )
    )
    store.record_fabric_decision_trace(
        FabricDecisionTraceRecord(
            trace_id="trace-qwen-0",
            request_id="req-qwen-0",
            deterministic_baseline={"winner": "node-b"},
            advisory_response={"winner": "node-a"},
            accepted=None,
            divergence_reason="pending_operator_review",
            replay_status="recorded",
            continuity_signals={"request_id": "req-qwen-0"},
            coherence_signals={"model_ok": True},
            created_at=now,
        )
    )


def _seed_f4(store: SQLiteStateStore) -> None:
    now = _now()
    store.upsert_fabric_transfer_capability(
        FabricTransferCapabilityRecord(
            capability_id="cap-roce-node-a-node-b",
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
            zone_id="zone-node-b-models",
            node_id="node-b",
            path="/srv/storage/landing/models",
            capacity_bytes=10_000,
            reserved_bytes=4096,
            safety_state="ready",
            cleanup_policy="lease_expiry",
            created_at=now,
            updated_at=now,
        )
    )
    store.upsert_fabric_transfer_lease(
        FabricTransferLeaseRecord(
            lease_id="lease-qwen-0",
            chunk_id=CHUNK_ID,
            source_node_id="node-a",
            target_node_id="node-b",
            transport="roce",
            status="active",
            holder="controller",
            landing_zone_id="zone-node-b-models",
            digest=CHUNK_ID,
            epoch=7,
            expires_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    store.record_fabric_transport_attempt(
        FabricTransportAttemptRecord(
            attempt_id="attempt-qwen-0",
            lease_id="lease-qwen-0",
            chunk_id=CHUNK_ID,
            transport="roce",
            status="fallback",
            fallback_used=True,
            fallback_transport="lan_direct",
            error="roce path not enabled in development lab",
            started_at=now,
            finished_at=now,
            created_at=now,
        )
    )


def _seed_f5(store: SQLiteStateStore) -> None:
    now = _now()
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
            trace_id="das-query-0",
            bundle_id="das-site-a-runtime",
            site_id="site-a",
            query_id="query-0",
            query_kind="advisory",
            local_first=True,
            warmed_refs=["das://site-a/runtime/facts.jsonl"],
            promoted_refs=["qdrant://ai_fabric_corpus/k1s"],
            fallback_sites=[],
            result_ref="trace://das-query-0",
            created_at=now,
        )
    )
    store.record_fabric_das_replication(
        FabricDasReplicationRecord(
            replication_id="replicate-das-site-a-site-b",
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
            signal_id="cog-signal-0",
            subject_type="das-cell",
            subject_id="das-site-a-runtime",
            signal_kind="continuity",
            continuity_ref="trace://das-query-0",
            coherence_score=0.91,
            overload_state="nominal",
            review_gate="operator_review",
            advisory_trace_id="trace-qwen-0",
            created_at=now,
        )
    )


def _make_handler(path: str, store: SQLiteStateStore) -> tuple[_ApiHandler, list[int]]:
    handler = object.__new__(_ApiHandler)
    statuses: list[int] = []
    headers: dict[str, str] = {}
    handler.path = path
    handler.store = store
    handler.wfile = BytesIO()
    handler.send_response = lambda code: statuses.append(code)  # type: ignore[method-assign]
    handler.send_header = lambda key, value: headers.__setitem__(key, value)  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]
    return handler, statuses


def _payload(handler: _ApiHandler) -> dict[str, Any]:
    handler.wfile.seek(0)
    return json.loads(handler.wfile.read().decode("utf-8"))


def test_sqlite_fabric_locality_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_locality(store)

    chunk = store.get_fabric_chunk(CHUNK_ID)
    assert chunk is not None
    assert chunk.namespace == "ai"
    assert chunk.labels == {"lane": "coordinator"}
    assert store.list_fabric_chunks(namespace="ai")[0].chunk_id == CHUNK_ID
    assert store.list_fabric_residencies(chunk_id=CHUNK_ID)[0].epoch == 7
    assert store.list_fabric_movements(chunk_id=CHUNK_ID)[0].direction == "push"


def test_sqlite_fabric_advisory_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_advisory(store)

    requests = store.list_fabric_advisory_requests(subject_type="placement")
    responses = store.list_fabric_advisory_responses(request_id="req-qwen-0")
    traces = store.list_fabric_decision_traces(request_id="req-qwen-0")

    assert requests[0].max_candidates == 4
    assert responses[0].authoritative is False
    assert traces[0].divergence_reason == "pending_operator_review"


def test_sqlite_fabric_f4_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_f4(store)

    capabilities = store.list_fabric_transfer_capabilities(node_id="node-a")
    leases = store.list_fabric_transfer_leases(chunk_id=CHUNK_ID)
    zones = store.list_fabric_landing_zones(node_id="node-b")
    attempts = store.list_fabric_transport_attempts(lease_id="lease-qwen-0")

    assert capabilities[0].transport == "roce"
    assert capabilities[0].fallback_transport == "lan_direct"
    assert leases[0].landing_zone_id == "zone-node-b-models"
    assert zones[0].safety_state == "ready"
    assert attempts[0].fallback_used is True


def test_sqlite_fabric_f5_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_f5(store)

    bundles = store.list_fabric_das_cell_bundles(site_id="site-a")
    traces = store.list_fabric_das_query_traces(bundle_id="das-site-a-runtime")
    replications = store.list_fabric_das_replications(bundle_id="das-site-a-runtime")
    signals = store.list_fabric_cognitive_signals(subject_type="das-cell")

    assert bundles[0].facts_ref == "das://site-a/runtime/facts.jsonl"
    assert traces[0].local_first is True
    assert traces[0].promoted_refs == ["qdrant://ai_fabric_corpus/k1s"]
    assert replications[0].approved_by == "operator"
    assert signals[0].review_gate == "operator_review"


def test_fabric_read_api_lists_locality_and_advisory_payloads(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_locality(store)
    _seed_advisory(store)

    chunks_handler, chunk_statuses = _make_handler("/fabric/chunks?namespace=ai", store)
    _ApiHandler._handle_fabric_chunks_list(chunks_handler)  # type: ignore[arg-type]
    traces_handler, trace_statuses = _make_handler(
        "/fabric/advisory/traces?request_id=req-qwen-0",
        store,
    )
    _ApiHandler._handle_fabric_advisory_traces_list(traces_handler)  # type: ignore[arg-type]

    chunks = _payload(chunks_handler)
    traces = _payload(traces_handler)
    assert chunk_statuses == [200]
    assert trace_statuses == [200]
    assert chunks["items"][0]["chunk_id"] == CHUNK_ID
    assert chunks["items"][0]["labels"]["lane"] == "coordinator"
    assert traces["items"][0]["trace_id"] == "trace-qwen-0"
    assert traces["items"][0]["replay_status"] == "recorded"


def test_fabric_advisory_state_api_reports_empty_optional_state(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")

    handler, statuses = _make_handler("/fabric/advisory/state", store)
    _ApiHandler._handle_fabric_advisory_state(handler)  # type: ignore[arg-type]

    payload = _payload(handler)
    assert statuses == [200]
    assert payload["api_version"] == FABRIC_ADVISORY_STATE_API_VERSION
    assert payload["mode"] == "advisory_only"
    assert payload["authoritative"] is False
    assert payload["controller_authority"] == "k1s"
    assert payload["data_present"] is False
    assert payload["runtime_profiles"]["count"] == 0
    assert payload["hyperon"]["status"] == "disabled"
    assert payload["hyperon"]["status_label"] == "none attached"
    assert "No Hyperon/DAS evidence is attached" in payload["hyperon"]["status_message"]
    assert "k1s advisory-only review remains available" in payload["hyperon"]["status_message"]
    assert payload["warnings"] == []


def test_fabric_advisory_state_api_reports_runtime_profile_only(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_ai_runtime_profile(
        {"run_id": "quality-promotion-test", "track": "quality"},
        {"ok": True, "admitted": True, "findings": []},
        workerbee_status={"ok": True},
    )

    handler, statuses = _make_handler("/fabric/advisory/state", store)
    _ApiHandler._handle_fabric_advisory_state(handler)  # type: ignore[arg-type]

    payload = _payload(handler)
    assert statuses == [200]
    assert payload["data_present"] is True
    assert payload["runtime_profiles"]["count"] == 1
    assert payload["runtime_profiles"]["promotion_ready_count"] == 1
    assert payload["runtime_profiles"]["tracks"][0]["track"] == "quality"
    assert payload["hyperon"]["status"] == "disabled"
    assert payload["hyperon"]["status_label"] == "none attached"
    assert "No Hyperon/DAS evidence is attached" in payload["hyperon"]["status_message"]
    assert "k1s advisory-only review remains available" in payload["hyperon"]["status_message"]
    assert payload["warnings"] == []


def test_fabric_advisory_state_api_reports_hyperon_das_evidence(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_advisory(store)
    _seed_f5(store)

    handler, statuses = _make_handler("/fabric/advisory/state", store)
    _ApiHandler._handle_fabric_advisory_state(handler)  # type: ignore[arg-type]

    payload = _payload(handler)
    warning_codes = {item["code"] for item in payload["warnings"]}
    assert statuses == [200]
    assert payload["data_present"] is True
    assert payload["advisory"]["traces_count"] == 1
    assert payload["advisory"]["pending_review_count"] == 1
    assert payload["advisory"]["latest_trace"]["trace_id"] == "trace-qwen-0"
    assert payload["hyperon"]["enabled"] is True
    assert payload["hyperon"]["available"] is True
    assert payload["hyperon"]["status"] == "experimental"
    assert payload["hyperon"]["status_label"] == "experimental active"
    assert "k1s remains authoritative" in payload["hyperon"]["status_message"]
    assert payload["hyperon"]["latest_das_query_trace"]["query_kind"] == "advisory"
    assert "AI_RUNTIME_PROFILE_EVIDENCE_MISSING" in warning_codes
    assert "FABRIC_ADVISORY_PENDING_REVIEW" in warning_codes


def test_fabric_read_api_lists_f4_and_f5_payloads(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_f4(store)
    _seed_f5(store)

    leases_handler, lease_statuses = _make_handler(
        "/fabric/transfer-leases?chunk_id=" + CHUNK_ID,
        store,
    )
    _ApiHandler._handle_fabric_transfer_leases_list(leases_handler)  # type: ignore[arg-type]
    das_handler, das_statuses = _make_handler(
        "/fabric/das-query-traces?bundle_id=das-site-a-runtime",
        store,
    )
    _ApiHandler._handle_fabric_das_query_traces_list(das_handler)  # type: ignore[arg-type]

    leases = _payload(leases_handler)
    traces = _payload(das_handler)
    assert lease_statuses == [200]
    assert das_statuses == [200]
    assert leases["items"][0]["lease_id"] == "lease-qwen-0"
    assert leases["items"][0]["transport"] == "roce"
    assert traces["items"][0]["local_first"] is True
    assert traces["items"][0]["warmed_refs"] == ["das://site-a/runtime/facts.jsonl"]
