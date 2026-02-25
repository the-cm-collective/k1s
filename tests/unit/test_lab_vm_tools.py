from __future__ import annotations

# ruff: noqa: S603
import json
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VARIANT_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "variant.py"
GATE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "throughput_gate.py"
SMOKE_V2_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "smoke_v2.py"
BOOTSTRAP_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "k1s_bootstrap.sh"
RUN_PROFILE_SCRIPT = ROOT / "scripts" / "dev" / "run_profile.sh"
VARIANT_FILE = ROOT / "lab" / "variants" / "test3-abc-pp2.yaml"

_SMOKE_V2_SPEC = spec_from_file_location("smoke_v2_script", SMOKE_V2_SCRIPT)
assert _SMOKE_V2_SPEC is not None and _SMOKE_V2_SPEC.loader is not None
smoke_v2 = module_from_spec(_SMOKE_V2_SPEC)
# Dataclasses consult sys.modules during class decoration on Python 3.13+.
sys.modules[_SMOKE_V2_SPEC.name] = smoke_v2
_SMOKE_V2_SPEC.loader.exec_module(smoke_v2)


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
    assert Path(payload["images"]["base"]).is_absolute()
    assert Path(payload["images"]["gpu"]).is_absolute()
    assert payload["transport"]["leaf_uplink_mode"] == "direct_ip"
    assert payload["transport"]["hub_host"] == "192.168.152.10"
    assert payload["transport"]["hub_leaf_port"] == 7422
    assert payload["smoke"]["lanes"] == [
        "single_non_gpu",
        "single_gpu",
        "multi_non_gpu",
        "multi_gpu",
    ]
    assert payload["smoke"]["checks"]["functional_advanced"] is False
    assert payload["environments"]["local_vm"] == {}
    assert payload["environments"]["remote_lab"] == {}
    assert payload["secrets"]["refs"] == {}


def test_variant_parser_validate_images_fails_when_files_missing(tmp_path: Path) -> None:
    variant = tmp_path / "missing-images.yaml"
    variant.write_text(
        """
name: unit-test-missing-images
test_id: 99
network:
  bridge: k1s-br0
  cidr: 192.168.152.0/24
  gateway: 192.168.152.1
images:
  base: artifacts/images/does-not-exist-base.qcow2
  gpu: artifacts/images/does-not-exist-gpu.qcow2
hosts:
  - name: a-core
    ip: 192.168.152.10
    role: k1s-core
vm:
  memory_mb: 2048
  vcpus: 2
  disk_gb: 20
""".strip(),
        encoding="utf-8",
    )

    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(VARIANT_SCRIPT),
            "--variant",
            str(variant),
            "--validate-images",
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 2
    assert "images.base not found" in res.stderr


def test_variant_parser_rejects_invalid_transport_mode(tmp_path: Path) -> None:
    variant = tmp_path / "invalid-transport.yaml"
    variant.write_text(
        """
name: unit-test-invalid-transport
test_id: 101
network:
  bridge: k1s-br0
  cidr: 192.168.152.0/24
  gateway: 192.168.152.1
images:
  base: artifacts/images/base.qcow2
  gpu: artifacts/images/gpu.qcow2
hosts:
  - name: a-core
    ip: 192.168.152.10
    role: k1s-core
vm:
  memory_mb: 2048
  vcpus: 2
  disk_gb: 20
transport:
  leaf_uplink_mode: invalid-mode
""".strip(),
        encoding="utf-8",
    )

    res = subprocess.run(  # noqa: S603
        [sys.executable, str(VARIANT_SCRIPT), "--variant", str(variant)],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 2
    assert "transport.leaf_uplink_mode must be direct_ip or local_tunnel" in res.stderr


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


def test_smoke_v2_plan_only_writes_lane_plan(tmp_path: Path) -> None:
    variant = tmp_path / "smoke-plan.yaml"
    variant.write_text(
        """
name: unit-test-smoke-plan
test_id: 103
network:
  bridge: k1s-br0
  cidr: 192.168.152.0/24
  gateway: 192.168.152.1
images:
  base: artifacts/images/base.qcow2
  gpu: artifacts/images/gpu.qcow2
hosts:
  - name: a-core
    ip: 192.168.152.10
    role: k1s-core
    gpu: false
  - name: b-edge-core
    ip: 192.168.152.20
    role: k1s-edge-core
    gpu: false
    site_id: edge-b
  - name: b-edge-node-1
    ip: 192.168.152.21
    role: k1s-edge-node
    gpu: true
    site_id: edge-b
vm:
  memory_mb: 2048
  vcpus: 2
  disk_gb: 20
smoke:
  lanes:
    - single_non_gpu
    - single_gpu
    - multi_non_gpu
    - multi_gpu
""".strip(),
        encoding="utf-8",
    )
    run_id = "20260225T120000Z_unit"
    out_root = tmp_path / "runs"

    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SMOKE_V2_SCRIPT),
            "--variant",
            str(variant),
            "--run-id",
            run_id,
            "--plan-only",
            "--output-root",
            str(out_root),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr

    plan = json.loads((out_root / run_id / "plan.json").read_text(encoding="utf-8"))
    lane_names = [lane["name"] for lane in plan["lanes"]]
    assert lane_names == ["single_non_gpu", "single_gpu", "multi_non_gpu", "multi_gpu"]

    lane_map = {lane["name"]: lane for lane in plan["lanes"]}
    assert lane_map["single_non_gpu"]["host_count"] == 1
    assert lane_map["single_gpu"]["host_count"] == 2
    assert lane_map["multi_non_gpu"]["host_count"] == 2
    assert lane_map["multi_gpu"]["host_count"] == 3


def test_k1s_bootstrap_core_sets_cri_trust_and_preload_defaults() -> None:
    text = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert "AE_CRI_DATA_ROOT=\\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri}" in text
    assert "AE_CRI_REGISTRY_TRUST_SYSTEM=\\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1}" in text
    assert "AE_CRI_REGISTRY_PRELOAD=\\${AE_CRI_REGISTRY_PRELOAD:-1}" in text
    assert "AE_APISHIM_MODE=\\${AE_APISHIM_MODE:-host}" in text


def test_run_profile_host_apishim_uses_src_pythonpath() -> None:
    text = RUN_PROFILE_SCRIPT.read_text(encoding="utf-8")
    assert 'local apishim_pythonpath="$ROOT_DIR/src"' in text
    assert 'nohup env PYTHONPATH="$apishim_pythonpath" "$PYTHON_BIN" -m ae.apishim serve' in text


def test_smoke_with_retry_handles_check_exceptions() -> None:
    calls = {"n": 0}

    def _flaky_check():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return True, "ok", {"calls": calls["n"]}

    ok, attempts, detail, payload = smoke_v2.with_retry(
        _flaky_check,
        timeout_s=2,
        retry_policy={"initial_backoff_s": 0.01, "max_backoff_s": 0.01, "jitter_s": 0.0},
    )
    assert ok is True
    assert attempts == 3
    assert detail == "ok"
    assert payload["calls"] == 3


def test_nats_hub_logs_parses_status_marker() -> None:
    original = smoke_v2.run_remote
    smoke_v2.run_remote = lambda *_args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[assignment]
        args=["ssh"],
        returncode=0,
        stdout="",
        stderr="__NATS_LOG_STATUS__:empty_logs\n",
    )
    try:
        logs, status = smoke_v2.nats_hub_logs("192.168.152.10")
    finally:
        smoke_v2.run_remote = original

    assert logs == ""
    assert status == "empty_logs"


def test_nats_hub_logs_marks_ssh_failure_without_status_marker() -> None:
    original = smoke_v2.run_remote
    smoke_v2.run_remote = lambda *_args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[assignment]
        args=["ssh"],
        returncode=255,
        stdout="",
        stderr="ssh: connect to host 192.168.152.10 port 22: Connection refused\n",
    )
    try:
        logs, status = smoke_v2.nats_hub_logs("192.168.152.10")
    finally:
        smoke_v2.run_remote = original

    assert logs == ""
    assert status == "ssh_failed"


def test_nats_hub_logs_returns_log_output_with_logpath_status() -> None:
    original = smoke_v2.run_remote
    smoke_v2.run_remote = lambda *_args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[assignment]
        args=["ssh"],
        returncode=0,
        stdout="line-from-logpath\n",
        stderr="__NATS_LOG_STATUS__:logpath_ok\n",
    )
    try:
        logs, status = smoke_v2.nats_hub_logs("192.168.152.10")
    finally:
        smoke_v2.run_remote = original

    assert "line-from-logpath" in logs
    assert status == "logpath_ok"


def test_lane_status_rollup_mixed_pass_and_skip_is_passed() -> None:
    phase_status = [
        {"phase": "service_ready", "status": "passed"},
        {"phase": "fabric_validate", "status": "skipped"},
        {"phase": "functional_basic", "status": "passed"},
        {"phase": "functional_advanced", "status": "skipped"},
    ]
    assert smoke_v2.lane_status_from_phases(phase_status) == "passed"


def test_lane_status_rollup_all_skipped_is_skipped() -> None:
    phase_status = [
        {"phase": "service_ready", "status": "skipped"},
        {"phase": "fabric_validate", "status": "skipped"},
    ]
    assert smoke_v2.lane_status_from_phases(phase_status) == "skipped"


def test_lane_status_rollup_failed_takes_precedence() -> None:
    phase_status = [
        {"phase": "service_ready", "status": "passed"},
        {"phase": "functional_basic", "status": "failed"},
        {"phase": "functional_advanced", "status": "skipped"},
    ]
    assert smoke_v2.lane_status_from_phases(phase_status) == "failed"
