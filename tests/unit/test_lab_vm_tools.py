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
CRI_IMAGE_MIRROR_SCRIPT = ROOT / "scripts" / "dev" / "cri_image_mirror.sh"
CRI_SEED_BUNDLE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_seed_bundle.sh"
COMMON_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "common.sh"
CRI_SEED_LOCK_FILE = ROOT / "lab" / "variants" / "cri_seed_images.lock.json"
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
    assert payload["k1s"]["apishim_port"] == 8445
    assert payload["transport"]["leaf_uplink_mode"] == "direct_ip"
    assert payload["transport"]["hub_host"] == "192.168.152.10"
    assert payload["transport"]["hub_leaf_port"] == 7422
    assert payload["ha"]["enabled"] is False
    assert payload["ha"]["etcd_endpoints"] == []
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


def test_variant_parser_accepts_ha_core_profile_shape(tmp_path: Path) -> None:
    variant = tmp_path / "ha-variant.yaml"
    variant.write_text(
        """
name: unit-test-ha-variant
test_id: 104
network:
  bridge: k1s-br0
  cidr: 192.168.154.0/24
  gateway: 192.168.154.1
images:
  base: artifacts/images/base.qcow2
  gpu: artifacts/images/gpu.qcow2
hosts:
  - name: core-a
    ip: 192.168.154.10
    role: k1s-ha-core
    node_id: core-a
  - name: core-b
    ip: 192.168.154.11
    role: k1s-ha-core
    node_id: core-b
  - name: edge-a
    ip: 192.168.154.20
    role: k1s-edge-core
    site_id: sea
  - name: edge-a-node
    ip: 192.168.154.21
    role: k1s-edge-node
    site_id: sea
vm:
  memory_mb: 2048
  vcpus: 2
  disk_gb: 20
k1s:
  controller_port: 9208
  apishim_port: 9445
ha:
  etcd_endpoints:
    - http://192.168.154.100:2379
    - http://192.168.154.101:2379
  etcd_prefix: k1s/lab/ha
  nats_url: nats://hub-controller:dev@192.168.154.110:4222
  controller_scheme: http
  apishim_scheme: http
  hub_nodes:
    - name: hub-a
      monitor_url: http://192.168.154.110:8222
  edge_sites:
    - site_id: sea
      monitor_url: http://192.168.154.20:8224
      expected_gateways:
        - edge-a
smoke:
  lanes:
    - ha_control_plane
""".strip(),
        encoding="utf-8",
    )

    res = subprocess.run(  # noqa: S603
        [sys.executable, str(VARIANT_SCRIPT), "--variant", str(variant), "--print-json"],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(res.stdout)
    assert [host["role"] for host in payload["hosts"][:2]] == ["k1s-ha-core", "k1s-ha-core"]
    assert payload["k1s"]["controller_port"] == 9208
    assert payload["k1s"]["apishim_port"] == 9445
    assert payload["ha"]["enabled"] is True
    assert payload["ha"]["etcd_endpoints"] == [
        "http://192.168.154.100:2379",
        "http://192.168.154.101:2379",
    ]
    assert payload["ha"]["hub_nodes"][0]["name"] == "hub-a"
    assert payload["ha"]["edge_sites"][0]["expected_gateways"] == ["edge-a"]
    assert payload["smoke"]["lanes"] == ["ha_control_plane"]


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


def test_smoke_v2_plan_only_supports_ha_control_plane_lane(tmp_path: Path) -> None:
    variant = tmp_path / "smoke-ha-plan.yaml"
    variant.write_text(
        """
name: unit-test-smoke-ha-plan
test_id: 105
network:
  bridge: k1s-br0
  cidr: 192.168.155.0/24
  gateway: 192.168.155.1
images:
  base: artifacts/images/base.qcow2
  gpu: artifacts/images/gpu.qcow2
hosts:
  - name: core-a
    ip: 192.168.155.10
    role: k1s-ha-core
    gpu: false
    node_id: core-a
  - name: core-b
    ip: 192.168.155.11
    role: k1s-ha-core
    gpu: false
    node_id: core-b
  - name: edge-sea
    ip: 192.168.155.20
    role: k1s-edge-core
    gpu: false
    site_id: sea
  - name: edge-sea-node
    ip: 192.168.155.21
    role: k1s-edge-node
    gpu: true
    site_id: sea
vm:
  memory_mb: 2048
  vcpus: 2
  disk_gb: 20
ha:
  etcd_endpoints: http://192.168.155.100:2379
  etcd_prefix: k1s/lab/ha
  nats_url: nats://hub-controller:dev@192.168.155.110:4222
  hub_nodes:
    - name: hub-a
      monitor_url: http://192.168.155.110:8222
  edge_sites:
    - site_id: sea
      monitor_url: http://192.168.155.20:8224
      expected_gateways:
        - edge-sea
smoke:
  lanes:
    - ha_control_plane
""".strip(),
        encoding="utf-8",
    )
    run_id = "20260318T120000Z_ha_unit"
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
    assert [lane["name"] for lane in plan["lanes"]] == ["ha_control_plane"]
    lane = plan["lanes"][0]
    assert lane["host_count"] == 4
    assert lane["skipped"] is False


def test_k1s_bootstrap_core_sets_cri_trust_and_preload_defaults() -> None:
    text = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert "AE_CRI_DATA_ROOT=\\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri}" in text
    assert "AE_CRI_REGISTRY_TRUST_SYSTEM=\\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1}" in text
    assert "AE_CRI_REGISTRY_PRELOAD=\\${AE_CRI_REGISTRY_PRELOAD:-1}" in text
    assert "AE_APISHIM_MODE=\\${AE_APISHIM_MODE:-host}" in text
    assert "bootstrap_seed_cri_cache core" in text
    assert "bootstrap_seed_cri_cache edge" in text
    assert "make k1s-ha-core" in text
    assert "AE_CONTROLLER_ADVERTISE_ADDR=http://${ip}:${controller_port}" in text
    assert "AE_APISHIM_ETCD_ENDPOINTS='${ha_etcd_endpoints}'" in text
    assert "AE_CRI_CACHE_SEED_MODE" in text
    assert "AE_CRI_CACHE_SEED_BUNDLE" in text


def test_run_profile_host_apishim_uses_src_pythonpath() -> None:
    text = RUN_PROFILE_SCRIPT.read_text(encoding="utf-8")
    assert 'local apishim_pythonpath="$ROOT_DIR/src"' in text
    assert 'nohup env PYTHONPATH="$apishim_pythonpath" "$PYTHON_BIN" -m ae.apishim serve' in text


def test_cri_image_mirror_prefers_local_cache() -> None:
    text = CRI_IMAGE_MIRROR_SCRIPT.read_text(encoding="utf-8")
    assert "AE_CRI_IMAGE_MIRROR_ALWAYS_PULL" in text
    assert "[cri-image-mirror] source already cached: ${image}" in text
    assert 'ctr -n "$ctr_namespace" images ls -q' in text
    assert 'grep -Fx -- "$image"' in text


def test_cri_seed_bundle_script_accepts_run_id_and_profile() -> None:
    text = CRI_SEED_BUNDLE_SCRIPT.read_text(encoding="utf-8")
    assert "--run-id <id>" in text
    assert "--profile <name>" in text
    assert "AE_CRI_CACHE_SEED_ENGINE" in text
    assert "[cri-seed] source already cached: $image" in text
    assert "images export" in text or "save -o" in text


def test_lab_vm_scripts_prefer_repo_venv_python() -> None:
    common_text = COMMON_SCRIPT.read_text(encoding="utf-8")
    smoke_text = (ROOT / "scripts" / "lab" / "vm" / "smoke.sh").read_text(encoding="utf-8")
    assert '$ROOT_DIR/.venv/bin/python' in common_text
    assert 'exec "$(lab_python)" "$SCRIPT_DIR/smoke_v2.py" "$@"' in smoke_text


def test_cri_seed_lock_contains_core_and_edge_images() -> None:
    payload = json.loads(CRI_SEED_LOCK_FILE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert "seed_version" in payload
    assert "core" in payload["images"]
    assert "edge" in payload["images"]
    assert "docker.io/library/registry:2" in payload["images"]["core"]
    assert "docker.io/library/registry:2" in payload["images"]["edge"]
    assert "quay.io/coreos/etcd:v3.5.13" in payload["images"]["core"]
    assert "docker.io/library/nats:2.10" in payload["images"]["edge"]


def test_smoke_v2_includes_seed_cache_phase_timeout() -> None:
    assert "seed_cache" in smoke_v2.DEFAULT_PHASE_TIMEOUTS


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


def test_nats_edge_logs_parses_status_marker() -> None:
    original = smoke_v2.run_remote
    seen: dict[str, str] = {}

    def _fake_run_remote(
        ip: str,
        command: str,
        *,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        _ = timeout
        seen["ip"] = ip
        seen["command"] = command
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout="",
            stderr="__NATS_EDGE_LOG_STATUS__:empty_logs\n",
        )

    smoke_v2.run_remote = _fake_run_remote  # type: ignore[assignment]
    try:
        edge = {
            "name": "b-edge-core",
            "role": "k1s-edge-core",
            "site_id": "edge-b",
            "ip": "192.168.152.20",
        }
        logs, status = smoke_v2.nats_edge_logs(edge)
    finally:
        smoke_v2.run_remote = original

    assert logs == ""
    assert status == "empty_logs"
    assert seen["ip"] == "192.168.152.20"
    assert "cids=$({" in seen["command"]
    assert "} 2>/dev/null" in seen["command"]
    assert "}} 2>/dev/null" not in seen["command"]


def test_nats_edge_logs_returns_unsupported_role_for_non_edge_host() -> None:
    edge = {"name": "b-edge-node-1", "role": "k1s-edge-node", "ip": "192.168.152.21"}
    logs, status = smoke_v2.nats_edge_logs(edge)
    assert logs == ""
    assert status == "unsupported_role"


def test_leaf_matches_edge_by_ip() -> None:
    edge = {"name": "b-edge-core", "ip": "192.168.152.20", "site_id": "edge-b"}
    leaf = {"ip": "192.168.152.20", "name": "edge-edge-b"}
    assert smoke_v2.leaf_matches_edge(leaf, edge) is True


def test_leaf_matches_edge_by_site_alias_suffix() -> None:
    edge = {"name": "b-edge-core", "ip": "192.168.152.20", "site_id": "edge-b"}
    leaf = {"ip": "10.0.0.2", "name": "edge-edge-b"}
    assert smoke_v2.leaf_matches_edge(leaf, edge) is True


def test_leaf_matches_edge_false_for_unrelated_leaf() -> None:
    edge = {"name": "b-edge-core", "ip": "192.168.152.20", "site_id": "edge-b"}
    leaf = {"ip": "192.168.152.30", "name": "edge-edge-c"}
    assert smoke_v2.leaf_matches_edge(leaf, edge) is False


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


def test_smoke_v2_removes_partial_log_note_text() -> None:
    text = SMOKE_V2_SCRIPT.read_text(encoding="utf-8")
    assert "ok (leafz+partial-log-signals)" not in text
    assert "signal_gaps_resolved_by_leafz" not in text


def test_ha_closeout_reduced_lane_forces_single_replica_jetstream() -> None:
    text = (ROOT / "tests" / "e2e" / "ha_closeout.py").read_text(encoding="utf-8")
    assert '"AE_JS_REPLICAS": "1"' in text
