from __future__ import annotations

# ruff: noqa: S603
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VARIANT_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "variant.py"
GATE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "throughput_gate.py"
VARIANT_FILE = ROOT / "lab" / "variants" / "test3-abc-pp2.yaml"


def test_variant_parser_prints_normalized_json() -> None:
    res = subprocess.run(  # noqa: S603
        [sys.executable, str(VARIANT_SCRIPT), "--variant", str(VARIANT_FILE), "--print-json"],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(res.stdout)
    assert payload["name"] == "test3-abc-pp2"
    assert payload["test_id"] == 3
    assert len(payload["hosts"]) == 5
    assert any(h["role"] == "k1s-core" for h in payload["hosts"])
    assert any(h["gpu"] for h in payload["hosts"])


def test_throughput_gate_pass_and_fail(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    current_ok = tmp_path / "current-ok.json"
    current_bad = tmp_path / "current-bad.json"

    baseline.write_text(
        json.dumps({"tokens_out_per_s": 100.0, "latency_p95": 1.0, "error_rate": 0.0}),
        encoding="utf-8",
    )
    current_ok.write_text(
        json.dumps({"tokens_out_per_s": 92.0, "latency_p95": 1.1, "error_rate": 0.0001}),
        encoding="utf-8",
    )
    current_bad.write_text(
        json.dumps({"tokens_out_per_s": 70.0, "latency_p95": 1.6, "error_rate": 0.002}),
        encoding="utf-8",
    )

    ok_out = tmp_path / "gate-ok.json"
    bad_out = tmp_path / "gate-bad.json"

    ok = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(GATE_SCRIPT),
            "--baseline",
            str(baseline),
            "--current",
            str(current_ok),
            "--out",
            str(ok_out),
        ],
        text=True,
        capture_output=True,
    )
    assert ok.returncode == 0
    assert json.loads(ok_out.read_text(encoding="utf-8"))["passed"] is True

    bad = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(GATE_SCRIPT),
            "--baseline",
            str(baseline),
            "--current",
            str(current_bad),
            "--out",
            str(bad_out),
        ],
        text=True,
        capture_output=True,
    )
    assert bad.returncode == 1
    assert json.loads(bad_out.read_text(encoding="utf-8"))["passed"] is False
