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
    FabricDecisionTraceRecord,
    FabricMovementRecord,
    FabricResidencyRecord,
)
from ae.observability.http_api import _ApiHandler

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
