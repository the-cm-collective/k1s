from __future__ import annotations

import json

from ae.fabric.ai_runtime_profile import (
    AI_RUNTIME_PROFILE_ADMISSION_API_VERSION,
    AI_RUNTIME_PROFILE_ADMISSION_KIND,
    AI_RUNTIME_PROFILE_API_VERSION,
    AI_RUNTIME_PROFILE_KIND,
    AI_RUNTIME_PROFILE_REQUIRED_FIELDS,
    AI_RUNTIME_PROFILE_REQUIRED_LANES,
    evaluate_ai_runtime_profile_admission,
    validate_ai_runtime_profile,
)


def _valid_profile() -> dict[str, object]:
    return {
        "api_version": AI_RUNTIME_PROFILE_API_VERSION,
        "kind": AI_RUNTIME_PROFILE_KIND,
        "run_id": "acceptance-closeout-test",
        "track": "lora-adapter-smoke",
        "model_lanes": {
            "coordinator": {
                "model": "Qwen/Qwen2.5-3B-Instruct-AWQ",
                "revision": "a" * 40,
                "served_model_name": "general-coordinator",
                "context_budget_tokens": 4096,
            },
            "expert": {
                "model": "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
                "revision": "b" * 40,
                "served_model_name": "k1s-code-expert",
                "context_budget_tokens": 4096,
            },
        },
        "context_budget_tokens": {"coordinator": 4096, "expert": 4096},
        "adapter_hotset": [
            {
                "lane": "expert",
                "name": "k1s-code-expert-lora-smoke",
                "claim_scope": "runtime-smoke-only",
            }
        ],
        "observed_vram_growth_mib": 12,
        "evidence": {
            "runtime_validation_ref": "runs/acceptance/summary.json",
            "workerbee_status_ref": "runs/acceptance/workerbee-status.json",
            "f5_evidence_ref": "runs/acceptance/f5-evidence.json",
            "advisor_scenarios_ref": "runs/acceptance/advisor-scenarios.json",
            "advisory_trace_refs": [{"trace_id": "trace-1"}],
            "das_fact_count": 12,
            "retrieval_corpus_count": {"document_count": 3, "chunk_count": 9},
        },
        "authoritative": False,
        "controller_authority": "k1s",
    }


def test_ai_runtime_profile_contract_constants_are_stable() -> None:
    assert AI_RUNTIME_PROFILE_API_VERSION == "k1s.fabric.ai-runtime-profile/v1"
    assert AI_RUNTIME_PROFILE_KIND == "AIFabricRuntimeProfile"
    assert (
        AI_RUNTIME_PROFILE_ADMISSION_API_VERSION
        == "k1s.fabric.ai-runtime-profile-admission/v1"
    )
    assert AI_RUNTIME_PROFILE_ADMISSION_KIND == "AIFabricRuntimeProfileAdmissionReport"
    assert "model_lanes" in AI_RUNTIME_PROFILE_REQUIRED_FIELDS
    assert AI_RUNTIME_PROFILE_REQUIRED_LANES == ("coordinator", "expert")


def test_validate_ai_runtime_profile_accepts_workerbee_acceptance_shape() -> None:
    result = validate_ai_runtime_profile(_valid_profile())

    assert result["ok"] is True
    assert result["findings"] == []


def test_validate_ai_runtime_profile_rejects_authoritative_or_missing_lane() -> None:
    profile = _valid_profile()
    profile["authoritative"] = True
    profile["model_lanes"] = {"coordinator": profile["model_lanes"]["coordinator"]}  # type: ignore[index]

    result = validate_ai_runtime_profile(profile)
    codes = {item["code"] for item in result["findings"]}

    assert result["ok"] is False
    assert "AI_RUNTIME_PROFILE_AUTHORITY" in codes
    assert "AI_RUNTIME_PROFILE_LANE" in codes


def test_runtime_profile_admission_report_is_dry_run_and_non_authoritative() -> None:
    workerbee_status = {"ok": True, "app_status": {"ready_workload_count": 8}}

    report = evaluate_ai_runtime_profile_admission(
        _valid_profile(), workerbee_status=workerbee_status
    )
    warning_codes = {item["code"] for item in report["findings"]}

    assert report["api_version"] == AI_RUNTIME_PROFILE_ADMISSION_API_VERSION
    assert report["kind"] == AI_RUNTIME_PROFILE_ADMISSION_KIND
    assert report["mode"] == "dry-run"
    assert report["ok"] is True
    assert report["admitted"] is True
    assert report["authoritative"] is False
    assert report["controller_authority"] == "k1s"
    assert report["profile_ref"]["run_id"] == "acceptance-closeout-test"
    assert report["requirements"]["required_lanes"] == ["coordinator", "expert"]
    assert (
        report["requirements"]["lanes"]["expert"]["served_model_name"]
        == "k1s-code-expert"
    )
    assert report["requirements"]["adapter_hotset"][0]["claim_scope"] == "runtime-smoke-only"
    assert report["evidence"]["workerbee_status_ok"] is True
    assert "AI_RUNTIME_PROFILE_ADAPTER_SCOPE" in warning_codes
    assert "AI_RUNTIME_PROFILE_WORKERBEE_STATUS" not in warning_codes


def test_runtime_profile_admission_blocks_structural_errors_only() -> None:
    profile = _valid_profile()
    profile["authoritative"] = True
    profile["model_lanes"] = {"coordinator": profile["model_lanes"]["coordinator"]}  # type: ignore[index]

    report = evaluate_ai_runtime_profile_admission(profile)
    codes = {item["code"] for item in report["findings"]}

    assert report["ok"] is False
    assert report["admitted"] is False
    assert "AI_RUNTIME_PROFILE_AUTHORITY" in codes
    assert "AI_RUNTIME_PROFILE_LANE" in codes
    assert "AI_RUNTIME_PROFILE_WORKERBEE_STATUS_UNVERIFIED" in codes


def test_runtime_profile_admission_warns_on_placeholder_workerbee_status() -> None:
    report = evaluate_ai_runtime_profile_admission(
        _valid_profile(),
        workerbee_status={
            "ok": None,
            "note": "capture WorkerBee MCP project_status after runtime validation",
        },
    )
    codes = {item["code"] for item in report["findings"]}

    assert report["ok"] is True
    assert "AI_RUNTIME_PROFILE_WORKERBEE_STATUS" in codes


def test_runtime_profile_admission_cli_emits_json(tmp_path, capsys) -> None:
    from ae.cli.__main__ import main

    status_path = tmp_path / "workerbee-status.json"
    profile = _valid_profile()
    profile["evidence"]["workerbee_status_ref"] = str(status_path)  # type: ignore[index]
    profile_path = tmp_path / "ai-runtime-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")

    exit_code = main(
        [
            "fabric",
            "runtime-profile-admission",
            "--profile",
            str(profile_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["api_version"] == AI_RUNTIME_PROFILE_ADMISSION_API_VERSION
    assert payload["evidence"]["workerbee_status_ok"] is True
