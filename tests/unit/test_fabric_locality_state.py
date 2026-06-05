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
from ae.observability.http_api import (
    FABRIC_ADVISORY_IMPORT_API_VERSION,
    FABRIC_ADVISORY_STATE_API_VERSION,
    FABRIC_PHASE_ASSURANCE_API_VERSION,
    _ApiHandler,
    _import_fabric_advisory_payload,
)

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


def _make_handler(
    path: str,
    store: SQLiteStateStore,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[_ApiHandler, list[int]]:
    handler = object.__new__(_ApiHandler)
    statuses: list[int] = []
    response_headers: dict[str, str] = {}
    payload = json.dumps(body or {}).encode("utf-8")
    request_headers = dict(headers or {})
    request_headers.setdefault("Content-Length", str(len(payload)))
    request_headers.setdefault("Content-Type", "application/json")
    handler.path = path
    handler.command = method
    handler.headers = request_headers
    handler.rfile = BytesIO(payload)
    handler.store = store
    handler.wfile = BytesIO()
    handler.send_response = lambda code: statuses.append(code)  # type: ignore[method-assign]
    handler.send_header = lambda key, value: response_headers.__setitem__(key, value)  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]
    return handler, statuses


def _payload(handler: _ApiHandler) -> dict[str, Any]:
    handler.wfile.seek(0)
    return json.loads(handler.wfile.read().decode("utf-8"))


def _build_state_payload(store: SQLiteStateStore) -> dict[str, Any]:
    handler, statuses = _make_handler("/fabric/advisory/state", store)
    _ApiHandler._handle_fabric_advisory_state(handler)  # type: ignore[arg-type]
    assert statuses == [200]
    return _payload(handler)


def _build_phase_payload(store: SQLiteStateStore) -> dict[str, Any]:
    handler, statuses = _make_handler("/fabric/phase-assurance", store)
    _ApiHandler._handle_fabric_phase_assurance(handler)  # type: ignore[arg-type]
    assert statuses == [200]
    return _payload(handler)


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


def test_sqlite_fabric_advisory_review_updates_trace_signal_and_event(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_advisory(store)
    _seed_f5(store)

    result = store.record_fabric_advisory_review(
        trace_id="trace-qwen-0",
        decision="accept",
        reviewer="operator-a",
        note="matches deterministic replay",
        checked_steps=["authority", "request", "response", "trace", "signals"],
        client_seen={"request_id": "req-qwen-0", "signal_ids": ["cog-signal-0"]},
    )

    trace = store.get_fabric_decision_trace("trace-qwen-0")
    signals = store.list_fabric_cognitive_signals(advisory_trace_id="trace-qwen-0")
    events = store.list_fabric_advisory_review_events(trace_id="trace-qwen-0")
    state = _build_state_payload(store)

    assert result["accepted"] is True
    assert result["updated_signal_ids"] == ["cog-signal-0"]
    assert trace is not None
    assert trace.accepted is True
    assert trace.divergence_reason is None
    assert signals[0].review_status == "accepted"
    assert signals[0].reviewed_by == "operator-a"
    assert signals[0].review_note == "matches deterministic replay"
    assert signals[0].reviewed_at is not None
    assert events[0].decision == "accept"
    assert events[0].checked_steps == ["authority", "request", "response", "trace", "signals"]
    assert events[0].signal_ids == ["cog-signal-0"]
    assert state["advisory"]["pending_review_count"] == 0
    assert state["advisory"]["accepted_count"] == 1
    assert state["hyperon"]["latest_cognitive_signal"]["review_status"] == "accepted"

    _seed_advisory(store)
    _seed_f5(store)
    trace_after_reimport = store.get_fabric_decision_trace("trace-qwen-0")
    signal_after_reimport = store.list_fabric_cognitive_signals(
        advisory_trace_id="trace-qwen-0"
    )[0]

    assert trace_after_reimport is not None
    assert trace_after_reimport.accepted is True
    assert trace_after_reimport.divergence_reason is None
    assert signal_after_reimport.review_status == "accepted"
    assert signal_after_reimport.reviewed_by == "operator-a"


def test_fabric_advisory_review_api_requires_admin_and_records_review(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AE_API_MUTATIONS", "1")
    monkeypatch.setenv("AE_API_RBAC", "1")
    monkeypatch.setenv("AE_API_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("AE_API_READ_TOKEN", "read")
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_advisory(store)
    _seed_f5(store)

    reader, reader_statuses = _make_handler(
        "/fabric/advisory/traces/trace-qwen-0/review",
        store,
        method="POST",
        body={"decision": "accept"},
        headers={"Authorization": "Bearer read"},
    )
    reader.do_POST()

    assert 403 in reader_statuses
    assert store.get_fabric_decision_trace("trace-qwen-0").accepted is None  # type: ignore[union-attr]

    admin, admin_statuses = _make_handler(
        "/fabric/advisory/traces/trace-qwen-0/review",
        store,
        method="POST",
        body={
            "decision": "diverge",
            "reviewer": "operator-b",
            "note": "advisory conflicts with current policy",
            "checked_steps": ["authority", "trace"],
            "client_seen": {"request_id": "req-qwen-0", "signal_ids": ["cog-signal-0"]},
        },
        headers={"Authorization": "Bearer admin"},
    )
    admin.do_POST()
    payload = _payload(admin)
    trace = store.get_fabric_decision_trace("trace-qwen-0")
    signals = store.list_fabric_cognitive_signals(advisory_trace_id="trace-qwen-0")

    assert admin_statuses == [200]
    assert payload["ok"] is True
    assert payload["decision"] == "diverge"
    assert payload["accepted"] is False
    assert payload["pending_review_count"] == 0
    assert trace is not None
    assert trace.accepted is False
    assert trace.divergence_reason == "operator_diverged"
    assert signals[0].review_status == "diverged"
    assert signals[0].reviewed_by == "operator-b"

    invalid, invalid_statuses = _make_handler(
        "/fabric/advisory/traces/trace-qwen-0/review",
        store,
        method="POST",
        body={"decision": "defer"},
        headers={"Authorization": "Bearer admin"},
    )
    invalid.do_POST()
    assert invalid_statuses == [400]

    missing, missing_statuses = _make_handler(
        "/fabric/advisory/traces/missing-trace/review",
        store,
        method="POST",
        body={"decision": "accept"},
        headers={"Authorization": "Bearer admin"},
    )
    missing.do_POST()
    assert missing_statuses == [404]


def test_fabric_advisory_import_ingests_grouped_f3_records(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    payload = {
        "source": "workerbee.live.review-validation/v1",
        "records": {
            "advisory_requests": [
                {
                    "request_id": "req-grouped-0",
                    "subject_type": "phase_gate",
                    "subject_id": "F3",
                    "intent": "review",
                    "facts_ref": "facts://grouped",
                    "locality_snapshot_ref": "fabric://grouped",
                    "max_candidates": 1,
                    "time_budget_ms": 100,
                    "policy_mode": "advisory_only",
                    "created_at": "2026-06-05T00:00:00+00:00",
                }
            ],
            "advisory_responses": [
                {
                    "request_id": "req-grouped-0",
                    "provider": "workerbee-test",
                    "status": "review",
                    "recommendation": "operator review",
                    "confidence": 0.5,
                    "evidence_refs": ["facts://grouped"],
                    "authoritative": False,
                    "created_at": "2026-06-05T00:00:00+00:00",
                }
            ],
            "decision_traces": [
                {
                    "trace_id": "trace-grouped-0",
                    "request_id": "req-grouped-0",
                    "deterministic_baseline": {"winner": "node-a"},
                    "advisory_response": {"winner": "node-b", "authoritative": False},
                    "accepted": None,
                    "divergence_reason": "pending_operator_review",
                    "replay_status": "recorded",
                    "continuity_signals": {"request_id": "req-grouped-0"},
                    "coherence_signals": {"model_ok": True},
                    "created_at": "2026-06-05T00:00:00+00:00",
                }
            ],
        },
    }

    result = _import_fabric_advisory_payload(store, payload)

    assert result["ok"] is True
    assert result["counts"]["advisory_requests"] == 1
    assert result["counts"]["advisory_responses"] == 1
    assert result["counts"]["decision_traces"] == 1
    assert store.get_fabric_decision_trace("trace-grouped-0") is not None


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


def test_fabric_phase_assurance_api_reports_empty_controller_state(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")

    payload = _build_phase_payload(store)

    assert payload["api_version"] == FABRIC_PHASE_ASSURANCE_API_VERSION
    assert payload["source"] == "k1s-controller-state"
    assert payload["controller_authority"] == "k1s"
    assert payload["authoritative"] is True
    assert payload["advisory_authoritative"] is False
    assert payload["phases"]["F3"]["status"] == "missing"
    assert payload["phases"]["F3"]["gate"]["ready"] is False
    assert payload["phases"]["F3"]["gate"]["blocked_by"] == ["F1", "F2"]
    assert "bounded_planning" in payload["phases"]["F3"]["missing"]


def test_fabric_phase_assurance_api_derives_f3_from_advisory_records(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    _seed_advisory(store)

    payload = _build_phase_payload(store)
    f3 = payload["phases"]["F3"]

    assert f3["status"] == "present"
    assert f3["gate"]["ready"] is False
    assert f3["gate"]["blocked_by"] == ["F1", "F2"]
    assert f3["missing"] == []
    assert f3["evidence"]["advisory_contract"]["authoritative"] is False
    assert f3["evidence"]["bounded_planning"]["bounded_request_count"] == 1
    assert f3["evidence"]["continuity_coherence_signals"]["signal_trace_count"] == 1


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


def test_fabric_advisory_import_ingests_router_trace_and_grouped_f5(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    payload = {
        "source": "workerbee.ai-fabric.runtime-validation/v1",
        "decision_traces": [
            {
                "trace_id": "trace-import-0",
                "request_id": "req-import-0",
                "created_at": "2026-06-04T00:00:00+00:00",
                "request_contract": {
                    "subject_type": "advisory_query",
                    "subject_id": "Explain k1s advisory routing",
                    "intent": "advise",
                    "facts_ref": "http://das.example",
                    "locality_snapshot_ref": "http://retrieval.example",
                    "max_candidates": 5,
                    "time_budget_ms": 3000,
                },
                "response_contract": {
                    "provider": "workerbee-ai-router",
                    "status": "ok",
                    "recommendation": "keep k1s authoritative",
                    "confidence": 0.8,
                    "evidence_refs": ["das-fact://fact-1"],
                    "authoritative": True,
                },
                "deterministic_baseline": {"selected_lane": "coordinator"},
                "accepted": None,
                "divergence_reason": "pending_operator_review",
                "replay_status": "recorded",
                "continuity_signals": {"request_id": "req-import-0"},
                "coherence_signals": {"authoritative": False},
            }
        ],
        "records": {
            "das_cell_bundles": [
                {
                    "bundle_id": "das-import-runtime",
                    "site_id": "site-a",
                    "cell_id": "runtime",
                    "version": "2026-06-04",
                    "storage_ref": "/srv/storage/k1s/ai-fabric-lab/das",
                    "facts_ref": "das://site-a/runtime/facts.jsonl",
                    "status": "ready",
                    "labels": {"workerbee_lab": "ai-fabric"},
                    "created_at": "2026-06-04T00:00:00+00:00",
                    "updated_at": "2026-06-04T00:00:00+00:00",
                }
            ],
            "das_query_traces": [
                {
                    "trace_id": "das-query-import-0",
                    "bundle_id": "das-import-runtime",
                    "site_id": "site-a",
                    "query_id": "req-import-0",
                    "query_kind": "advisory",
                    "local_first": True,
                    "warmed_refs": ["das-fact://fact-1"],
                    "promoted_refs": ["qdrant://ai_fabric_corpus/smoke"],
                    "fallback_sites": [],
                    "result_ref": "das://site-a/runtime/query/das-query-import-0",
                    "created_at": "2026-06-04T00:00:00+00:00",
                }
            ],
            "cognitive_signals": [
                {
                    "signal_id": "cognitive-import-0",
                    "subject_type": "das-cell",
                    "subject_id": "das-import-runtime",
                    "signal_kind": "continuity",
                    "continuity_ref": "das://site-a/runtime/query/das-query-import-0",
                    "coherence_score": 1.0,
                    "overload_state": "nominal",
                    "review_gate": "operator_review",
                    "advisory_trace_id": "trace-import-0",
                    "created_at": "2026-06-04T00:00:00+00:00",
                }
            ],
        },
    }

    result = _import_fabric_advisory_payload(store, payload)
    state = _build_state_payload(store)

    assert result["api_version"] == FABRIC_ADVISORY_IMPORT_API_VERSION
    assert result["ok"] is True
    assert result["authoritative"] is False
    assert result["counts"]["advisory_requests"] == 1
    assert result["counts"]["advisory_responses"] == 1
    assert result["counts"]["decision_traces"] == 1
    assert result["counts"]["das_cell_bundles"] == 1
    assert state["advisory"]["latest_response"]["authoritative"] is False
    assert state["advisory"]["latest_trace"]["trace_id"] == "trace-import-0"
    assert state["hyperon"]["enabled"] is True
    assert state["hyperon"]["status_label"] == "experimental active"


def test_fabric_advisory_import_ingests_f2_locality_records(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    payload = {
        "source": "workerbee.ai-fabric.f1-f2-locality-closeout/v1",
        "records": {
            "fabric_nodes": [
                {
                    "node_id": "node-a",
                    "name": "node-a",
                    "status": "Ready",
                    "labels": {
                        "gpu.present": "true",
                        "gpu.count": "1",
                        "gpu.models": "RTX 8000",
                    },
                    "capabilities": {
                        "accelerators": [
                            {
                                "id": "gpu-0",
                                "vendor": "nvidia",
                                "family": "RTX 8000",
                                "device_count": 1,
                                "execution_role": "execution",
                            }
                        ],
                        "storage_devices": [{"id": "workerbee-local-nvme", "medium": "nvme"}],
                        "network_interfaces": [
                            {
                                "id": "workerbee-lan",
                                "name": "workerbee-lan",
                                "link_metrics": [
                                    {
                                        "from_site": "site-a",
                                        "to_site": "site-a",
                                        "rtt_p95_ms": 1.0,
                                    }
                                ],
                            }
                        ],
                        "rdma_devices": [{"id": "rdma-0", "name": "rdma-0"}],
                        "identity_roles": {
                            "management": "spiffe://node-a/management",
                            "execution": "spiffe://node-a/execution",
                            "fabric": "spiffe://node-a/fabric",
                        },
                    },
                }
            ],
            "fabric_chunks": [
                {
                    "chunk_id": CHUNK_ID,
                    "namespace": "ai-fabric-lab",
                    "name": "coordinator-adapter-hotset",
                    "digest": CHUNK_ID,
                    "size_bytes": 8192,
                    "source_kind": "workerbee-artifact",
                    "source_ref": "/srv/storage/k1s/ai-fabric-lab/adapters/expert",
                    "labels": {"suite": "f1-f2-locality-closeout"},
                    "created_at": "2026-06-04T00:00:00+00:00",
                    "updated_at": "2026-06-04T00:00:00+00:00",
                }
            ],
            "fabric_residencies": [
                {
                    "chunk_id": CHUNK_ID,
                    "node_id": "node-a",
                    "storage_device_id": "workerbee-local-nvme",
                    "path": "/srv/storage/k1s/ai-fabric-lab/adapters/expert",
                    "state": "resident",
                    "integrity_state": "verified",
                    "epoch": 1,
                    "digest": CHUNK_ID,
                    "verified_at": "2026-06-04T00:00:00+00:00",
                    "updated_at": "2026-06-04T00:00:00+00:00",
                }
            ],
            "fabric_movements": [
                {
                    "movement_id": "movement-f2-closeout",
                    "chunk_id": CHUNK_ID,
                    "direction": "pull",
                    "source_node_id": "workerbee-storage",
                    "target_node_id": "node-a",
                    "status": "complete",
                    "requested_by": "workerbee.ai-fabric.closeout",
                    "digest": CHUNK_ID,
                    "epoch": 1,
                    "created_at": "2026-06-04T00:00:00+00:00",
                    "updated_at": "2026-06-04T00:00:00+00:00",
                }
            ],
        },
    }

    result = _import_fabric_advisory_payload(store, payload)
    phase = _build_phase_payload(store)

    assert result["ok"] is True
    assert result["counts"]["fabric_nodes"] == 1
    assert result["counts"]["fabric_chunks"] == 1
    assert result["counts"]["fabric_residencies"] == 1
    assert result["counts"]["fabric_movements"] == 1
    assert store.list_fabric_chunks(namespace="ai-fabric-lab")[0].chunk_id == CHUNK_ID
    assert phase["phases"]["F1"]["status"] == "present"
    assert phase["phases"]["F2"]["status"] == "present"
    assert phase["phases"]["F2"]["present"] == [
        "content_addressed_chunks",
        "residency_state",
        "controlled_push_pull",
        "integrity_epoch_semantics",
    ]


def test_fabric_advisory_import_accepts_live_das_evidence_stream(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    payload = {
        "source": "workerbee.das-bridge",
        "records": [
            {
                "kind": "das_cell_bundle",
                "payload": {
                    "bundle_id": "das-stream-runtime",
                    "site_id": "site-a",
                    "cell_id": "runtime",
                    "version": "2026-06-04",
                    "storage_ref": "/srv/storage/k1s/ai-fabric-lab/das",
                    "facts_ref": "das://site-a/runtime/facts.jsonl",
                    "status": "ready",
                    "labels": {"workerbee_lab": "ai-fabric"},
                    "created_at": "2026-06-04T00:00:00+00:00",
                    "updated_at": "2026-06-04T00:00:00+00:00",
                },
            },
            {
                "kind": "das_query_trace",
                "payload": {
                    "trace_id": "das-stream-query-0",
                    "bundle_id": "das-stream-runtime",
                    "site_id": "site-a",
                    "query_id": "query-0",
                    "query_kind": "advisory",
                    "local_first": True,
                    "warmed_refs": ["das://site-a/runtime/facts.jsonl"],
                    "promoted_refs": [],
                    "fallback_sites": [],
                    "result_ref": "das://site-a/runtime/query/das-stream-query-0",
                    "created_at": "2026-06-04T00:00:00+00:00",
                },
            },
            {
                "kind": "cognitive_signal",
                "payload": {
                    "signal_id": "cognitive-stream-0",
                    "subject_type": "das-cell",
                    "subject_id": "das-stream-runtime",
                    "signal_kind": "continuity",
                    "continuity_ref": "das://site-a/runtime/query/das-stream-query-0",
                    "coherence_score": 1.0,
                    "overload_state": "nominal",
                    "review_gate": "operator_review",
                    "advisory_trace_id": "trace-import-0",
                    "created_at": "2026-06-04T00:00:00+00:00",
                },
            },
        ],
    }

    result = _import_fabric_advisory_payload(store, payload)
    state = _build_state_payload(store)

    assert result["ok"] is True
    assert result["counts"]["das_cell_bundles"] == 1
    assert result["counts"]["das_query_traces"] == 1
    assert result["counts"]["cognitive_signals"] == 1
    assert state["hyperon"]["latest_das_query_trace"]["trace_id"] == "das-stream-query-0"
    assert state["hyperon"]["latest_cognitive_signal"]["review_gate"] == "operator_review"
