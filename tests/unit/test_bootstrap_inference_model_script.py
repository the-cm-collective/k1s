from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "bootstrap_inference_model.py"
_SPEC = spec_from_file_location("bootstrap_inference_model_script", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
bootstrap_inference_model = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bootstrap_inference_model
_SPEC.loader.exec_module(bootstrap_inference_model)


def test_ensure_model_reuses_existing_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "models" / "smollm2-1.7b-instruct"
    target.mkdir(parents=True)
    (target / "config.json").write_text("{}", encoding="utf-8")
    calls: list[tuple[str, str | None, str]] = []

    def fake_snapshot_download(*, repo_id, revision=None, local_dir):  # type: ignore[no-untyped-def]
        calls.append((repo_id, revision, local_dir))
        return local_dir

    monkeypatch.setattr(bootstrap_inference_model, "snapshot_download", fake_snapshot_download)

    payload = bootstrap_inference_model.ensure_model(
        model_id="HuggingFaceTB/SmolLM2-1.7B-Instruct",
        revision=None,
        local_path=target,
    )

    assert payload["status"] == "ready"
    assert payload["result"] == "reused"
    assert calls == []


def test_ensure_model_downloads_when_snapshot_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "models" / "smollm2-360m-instruct"
    calls: list[tuple[str, str | None, str]] = []

    def fake_snapshot_download(*, repo_id, revision=None, local_dir):  # type: ignore[no-untyped-def]
        calls.append((repo_id, revision, local_dir))
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / "config.json").write_text("{}", encoding="utf-8")
        return local_dir

    monkeypatch.setattr(bootstrap_inference_model, "snapshot_download", fake_snapshot_download)

    payload = bootstrap_inference_model.ensure_model(
        model_id="HuggingFaceTB/SmolLM2-360M-Instruct",
        revision="main",
        local_path=target,
    )

    assert payload["status"] == "ready"
    assert payload["result"] == "downloaded"
    assert calls == [
        (
            "HuggingFaceTB/SmolLM2-360M-Instruct",
            "main",
            str(target),
        )
    ]


def test_main_emits_failure_json_on_download_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def fake_snapshot_download(*, repo_id, revision=None, local_dir):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(bootstrap_inference_model, "snapshot_download", fake_snapshot_download)

    rc = bootstrap_inference_model.main(
        [
            "--model-id",
            "HuggingFaceTB/SmolLM2-1.7B-Instruct",
            "--local-path",
            str(tmp_path / "models" / "smollm2-1.7b-instruct"),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["status"] == "failed"
    assert "RuntimeError: boom" in payload["error"]
