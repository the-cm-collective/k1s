from __future__ import annotations

import json

from ae.cli.__main__ import main
from ae.controller.state import SQLiteStateStore
from ae.fabric.ai_runtime_profile import (
    AI_RUNTIME_PROFILE_ADVISORY_API_VERSION,
    AI_RUNTIME_PROFILE_TRACK_ANNOTATION,
    evaluate_ai_runtime_profile_admission,
)
from tests.unit.test_ai_runtime_profile import _passing_soak, _valid_profile


def test_ai_runtime_profile_store_tracks_warning_and_latest_records(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    profile = _valid_profile(track="baseline", soak=_passing_soak(60))
    profile["adapter_hotset"] = []
    report = evaluate_ai_runtime_profile_admission(
        profile,
        workerbee_status={"ok": True},
    )

    record = store.upsert_ai_runtime_profile(
        profile,
        report,
        workerbee_status={"ok": True},
    )

    assert record.run_id == "acceptance-closeout-test"
    assert record.track == "baseline"
    assert record.admitted is True
    assert record.promotion_ready is False
    assert record.warning_codes == ["AI_RUNTIME_PROFILE_SOAK_DURATION"]
    assert store.get_ai_runtime_profile(record.run_id) == record
    assert store.latest_ai_runtime_profile("baseline") == record
    assert store.list_ai_runtime_profiles(track="baseline") == [record]


def test_runtime_profile_publish_list_and_show_cli(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    status_path = tmp_path / "workerbee-status.json"
    status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    profile = _valid_profile(track="quality", soak=_passing_soak(1800, track="quality"))
    profile["run_id"] = "quality-full"
    profile["adapter_hotset"] = []
    profile["evidence"]["workerbee_status_ref"] = str(status_path)  # type: ignore[index]
    profile_path = tmp_path / "ai-runtime-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    exit_code = main(
        [
            "fabric",
            "runtime-profile",
            "publish",
            "--profile",
            str(profile_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["stored"] is True
    assert payload["record"]["run_id"] == "quality-full"
    assert payload["record"]["promotion_ready"] is True
    assert payload["record"]["warning_codes"] == []

    assert main(["fabric", "runtime-profile", "list", "--track", "quality", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["run_id"] for item in listed] == ["quality-full"]

    assert (
        main(
            [
                "fabric",
                "runtime-profile",
                "show",
                "--track",
                "quality",
                "--latest",
                "--json",
            ]
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["run_id"] == "quality-full"
    assert shown["profile"]["track"] == "quality"
    assert shown["workerbee_status"]["ok"] is True


def test_runtime_profile_publish_rejects_structural_errors(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    profile = _valid_profile()
    profile["authoritative"] = True
    profile_path = tmp_path / "bad-runtime-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    exit_code = main(
        [
            "fabric",
            "runtime-profile",
            "publish",
            "--profile",
            str(profile_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["stored"] is False
    assert payload["admission"]["admitted"] is False
    assert SQLiteStateStore(db_path).list_ai_runtime_profiles() == []


def test_runtime_profile_advisory_cli_and_status_json(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    status_path = tmp_path / "workerbee-status.json"
    status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    profile = _valid_profile(track="baseline", soak=_passing_soak(1800, track="baseline"))
    profile["run_id"] = "baseline-full"
    profile["adapter_hotset"] = []
    profile["evidence"]["workerbee_status_ref"] = str(status_path)  # type: ignore[index]
    profile_path = tmp_path / "baseline-runtime-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    manifest_path = tmp_path / "echo.yaml"
    manifest_path.write_text(
        f"""
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: echo
  annotations:
    {AI_RUNTIME_PROFILE_TRACK_ANNOTATION}: baseline
spec:
  image: alpine:3.20
  replicas: 1
        """.strip(),
        encoding="utf-8",
    )

    assert (
        main(["fabric", "runtime-profile", "publish", "--profile", str(profile_path)])
        == 0
    )
    capsys.readouterr()

    assert (
        main(["fabric", "runtime-profile", "advisory", "-f", str(manifest_path), "--json"])
        == 0
    )
    advisory = json.loads(capsys.readouterr().out)
    assert advisory["api_version"] == AI_RUNTIME_PROFILE_ADVISORY_API_VERSION
    assert advisory["authoritative"] is False
    assert advisory["track"] == "baseline"
    assert advisory["profile_ref"]["run_id"] == "baseline-full"
    assert advisory["evidence"]["promotion_ready"] is True
    assert advisory["findings"] == []

    assert main(["apply", "-f", str(manifest_path)]) == 0
    capsys.readouterr()
    assert main(["status", "echo", "--json", "--wide"]) == 0
    status = json.loads(capsys.readouterr().out)
    status_advisory = status["runtime_profile_advisory"]
    assert status_advisory["track"] == "baseline"
    assert status_advisory["profile_ref"]["run_id"] == "baseline-full"
    assert status_advisory["evidence"]["promotion_ready"] is True
