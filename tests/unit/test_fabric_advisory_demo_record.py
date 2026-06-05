from __future__ import annotations

from scripts.dev.fabric_advisory_demo_record import (
    build_demo_import_payload,
    dashboard_url_for_project,
    infer_project_from_dashboard_url,
)


def test_dashboard_project_inference_round_trips_workerbee_url() -> None:
    project = "k1s-workerbee-dev-2592c13f5e"
    url = dashboard_url_for_project(project)

    assert url == "https://k1s.k1s-workerbee-dev-2592c13f5e.workerbee.home.arpa:19443/dashboard"
    assert infer_project_from_dashboard_url(url) == project


def test_demo_import_payload_links_pending_trace_to_das_and_signal_evidence() -> None:
    payload = build_demo_import_payload(
        run_id="20260605T000000Z-test",
        created_at="2026-06-05T00:00:00+00:00",
    )

    trace = payload["decision_traces"][0]
    records = payload["records"]
    das_query = records["das_query_traces"][0]
    signal = records["cognitive_signals"][0]

    assert trace["accepted"] is None
    assert trace["divergence_reason"] == "pending_operator_review"
    assert trace["advisory_response"]["authoritative"] is False
    assert trace["continuity_signals"]["das_query_trace_id"] == das_query["trace_id"]
    assert das_query["query_id"] == trace["request_id"]
    assert das_query["local_first"] is True
    assert signal["advisory_trace_id"] == trace["trace_id"]
    assert signal["review_status"] == "pending"
