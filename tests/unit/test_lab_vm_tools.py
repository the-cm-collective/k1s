from __future__ import annotations

# ruff: noqa: S603
import json
import os
import re
import shutil
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VARIANT_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "variant.py"
GATE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "throughput_gate.py"
SMOKE_V2_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "smoke_v2.py"
BOOTSTRAP_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "k1s_bootstrap.sh"
HA_SHARED_INFRA_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "ha_shared_infra.sh"
COMMON_BOOTSTRAP_SCRIPT = ROOT / "lab" / "packer" / "http" / "common-bootstrap.sh"
PACKER_TEMPLATE = ROOT / "lab" / "packer" / "ubuntu-22.04-ga.pkr.hcl"
IMAGE_BUILD_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_build.sh"
IMAGE_VERIFY_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_verify.sh"
INSPECT_QCOW_BOOT_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "inspect_qcow_boot.sh"
ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "assert_image_boot_contract.sh"
RUN_PROFILE_SCRIPT = ROOT / "scripts" / "dev" / "run_profile.sh"
CRI_NODE_CNI_HELPER_SCRIPT = ROOT / "scripts" / "dev" / "ensure_cri_node_cni.sh"
HA_CLOSEOUT_E2E_SCRIPT = ROOT / "scripts" / "dev" / "ha_closeout_e2e.sh"
STRICT_CRI_SMOKE_SCRIPT = ROOT / "scripts" / "dev" / "strict_cri_smoke.sh"
CRI_IMAGE_MIRROR_SCRIPT = ROOT / "scripts" / "dev" / "cri_image_mirror.sh"
CRI_SEED_BUNDLE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_seed_bundle.sh"
HA_DASHBOARD_SMOKE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "ha_dashboard_smoke.sh"
HOST_PREPARE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "host_prepare.sh"
VARIANT_UP_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "variant_up.sh"
VARIANT_DOWN_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "variant_down.sh"
COMMON_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "common.sh"
GUEST_PREREQS_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "guest_prereqs.sh"
CRI_PREFLIGHT_SCRIPT = ROOT / "scripts" / "cri_preflight.sh"
ENSURE_APISHIM_ENV_SCRIPT = ROOT / "scripts" / "ensure_apishim_env.sh"
ENSURE_APISHIM_CLI_ENV_SCRIPT = ROOT / "scripts" / "ensure_apishim_cli_env.sh"
ENSURE_CONTROLLER_ENV_SCRIPT = ROOT / "scripts" / "ensure_controller_env.sh"
HA_DRILL_ACTIONS_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "ha_drill_actions.sh"
MAKEFILE = ROOT / "Makefile"
CRI_SEED_LOCK_FILE = ROOT / "lab" / "variants" / "cri_seed_images.lock.json"
VARIANT_FILE = ROOT / "lab" / "variants" / "test3-abc-pp2.yaml"
HA_VARIANT_FILE = ROOT / "lab" / "variants" / "ha-control-plane-core.yaml"
HA_ATTACHED_NODE_VARIANT_FILE = ROOT / "lab" / "variants" / "ha-control-plane-attached-node.yaml"
HA_DRILL_VARIANT_FILE = ROOT / "lab" / "variants" / "ha-control-plane-core-drills.yaml"
HA_BRING_UP_DOC = ROOT / "docs" / "ops" / "ha-cluster-bring-up.md"
VM_VARIANT_RUNBOOK_DOC = ROOT / "docs" / "ops" / "vm-variant-runbook.md"
OPS_RUNBOOK_DOC = ROOT / "docs" / "ops" / "runbook.md"
VM_GOLDEN_IMAGE_PIPELINE_DOC = ROOT / "docs" / "ops" / "vm-golden-image-pipeline.md"

_SMOKE_V2_SPEC = spec_from_file_location("smoke_v2_script", SMOKE_V2_SCRIPT)
assert _SMOKE_V2_SPEC is not None and _SMOKE_V2_SPEC.loader is not None
smoke_v2 = module_from_spec(_SMOKE_V2_SPEC)
# Dataclasses consult sys.modules during class decoration on Python 3.13+.
sys.modules[_SMOKE_V2_SPEC.name] = smoke_v2
_SMOKE_V2_SPEC.loader.exec_module(smoke_v2)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


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
    assert payload["vm"] == {"memory_mb": 6144, "vcpus": 4, "disk_gb": 50}
    assert all(host["vm"] == {"memory_mb": 6144, "vcpus": 4, "disk_gb": 50} for host in payload["hosts"])
    assert payload["k1s"]["apishim_port"] == 8445
    assert payload["k1s"]["agent_api_port"] == 9110
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


def test_variant_parser_allows_partial_host_vm_overrides(tmp_path: Path) -> None:
    variant = tmp_path / "host-vm-overrides.yaml"
    variant.write_text(
        """
name: unit-test-host-vm-overrides
test_id: 77
network:
  bridge: k1s-br0
  cidr: 192.168.155.0/24
  gateway: 192.168.155.1
images:
  base: artifacts/images/ubuntu-22.04-k1s-base.qcow2
  gpu: artifacts/images/ubuntu-22.04-k1s-gpu.qcow2
vm:
  memory_mb: 4096
  vcpus: 4
  disk_gb: 50
hosts:
  - name: core-a
    ip: 192.168.155.10
    role: k1s-ha-core
    vm:
      memory_mb: 5120
  - name: edge-a
    ip: 192.168.155.20
    role: k1s-edge-node
    vm:
      memory_mb: 2048
      vcpus: 2
      disk_gb: 25
ha:
  etcd_endpoints:
    - http://192.168.155.10:2379
  etcd_prefix: k1s/lab/unit-test
  nats_url: nats://unit:dev@192.168.155.10:4222
  hub_nodes:
    - name: core-a
      monitor_url: http://192.168.155.10:8222
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
    assert payload["vm"] == {"memory_mb": 4096, "vcpus": 4, "disk_gb": 50}
    assert payload["hosts"][0]["vm"] == {"memory_mb": 5120, "vcpus": 4, "disk_gb": 50}
    assert payload["hosts"][1]["vm"] == {"memory_mb": 2048, "vcpus": 2, "disk_gb": 25}


def test_checked_in_ha_variant_normalizes_for_closeout_lane() -> None:
    res = subprocess.run(  # noqa: S603
        [sys.executable, str(VARIANT_SCRIPT), "--variant", str(HA_VARIANT_FILE), "--print-json"],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(res.stdout)
    assert payload["name"] == "ha-control-plane-core"
    assert payload["vm"] == {"memory_mb": 5120, "vcpus": 4, "disk_gb": 50}
    assert [host["role"] for host in payload["hosts"][:3]] == [
        "k1s-ha-core",
        "k1s-ha-core",
        "k1s-ha-core",
    ]
    assert payload["hosts"][0]["vm"] == {"memory_mb": 5120, "vcpus": 4, "disk_gb": 50}
    assert payload["hosts"][3]["vm"] == {"memory_mb": 3072, "vcpus": 2, "disk_gb": 50}
    assert payload["hosts"][4]["vm"] == {"memory_mb": 3072, "vcpus": 2, "disk_gb": 50}
    assert payload["ha"]["enabled"] is True
    assert payload["ha"]["etcd_endpoints"] == [
        "http://192.168.155.10:2379",
        "http://192.168.155.11:2379",
        "http://192.168.155.12:2379",
    ]
    assert payload["ha"]["apishim_scheme"] == "https"
    assert [item["name"] for item in payload["ha"]["hub_nodes"]] == ["core-a", "core-b", "core-c"]
    assert payload["ha"]["edge_sites"][0]["monitor_url"] == "http://192.168.155.20:8223"
    assert payload["ha"]["edge_sites"][0]["expected_gateways"] == ["sea--sea-gw"]
    assert payload["hosts"][4]["pod_cidr"] == "10.42.1.0/24"
    assert payload["smoke"]["lanes"] == ["ha_control_plane"]


def test_checked_in_ha_attached_node_variant_normalizes_for_manual_smoke_lane() -> None:
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(VARIANT_SCRIPT),
            "--variant",
            str(HA_ATTACHED_NODE_VARIANT_FILE),
            "--print-json",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(res.stdout)
    assert payload["name"] == "ha-control-plane-attached-node"
    assert payload["vm"] == {"memory_mb": 5120, "vcpus": 4, "disk_gb": 50}
    assert [host["role"] for host in payload["hosts"][:3]] == [
        "k1s-ha-core",
        "k1s-ha-core",
        "k1s-ha-core",
    ]
    assert payload["hosts"][3]["role"] == "k1s-core-node"
    assert payload["hosts"][3]["node_id"] == "attached-node-1"
    assert payload["hosts"][3]["node_labels"] == "role=worker,site=core"
    assert payload["hosts"][3]["pod_cidr"] == "10.42.0.0/24"
    assert payload["hosts"][3]["vm"] == {"memory_mb": 3072, "vcpus": 2, "disk_gb": 50}
    assert payload["k1s"]["agent_api_port"] == 9110
    assert payload["ha"]["enabled"] is True
    assert payload["ha"]["edge_sites"] == []
    assert payload["smoke"]["lanes"] == ["ha_control_plane"]


def test_checked_in_ha_drill_variant_exposes_optional_commands() -> None:
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(VARIANT_SCRIPT),
            "--variant",
            str(HA_DRILL_VARIANT_FILE),
            "--print-json",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(res.stdout)
    assert payload["name"] == "ha-control-plane-core-drills"
    assert payload["vm"] == {"memory_mb": 5120, "vcpus": 4, "disk_gb": 50}
    assert payload["hosts"][3]["vm"] == {"memory_mb": 3072, "vcpus": 2, "disk_gb": 50}
    assert payload["hosts"][4]["vm"] == {"memory_mb": 3072, "vcpus": 2, "disk_gb": 50}
    assert payload["hosts"][4]["pod_cidr"] == "10.42.1.0/24"
    assert payload["k1s"]["agent_api_port"] == 9110
    assert payload["ha"]["drills"]["leader_failover_command"] == (
        "./scripts/lab/vm/ha_drill_actions.sh leader-failover "
        "--variant lab/variants/ha-control-plane-core-drills.yaml"
    )
    assert payload["ha"]["drills"]["etcd_restart_command"] == (
        "./scripts/lab/vm/ha_drill_actions.sh etcd-restart "
        "--variant lab/variants/ha-control-plane-core-drills.yaml"
    )
    assert payload["ha"]["drills"]["transport_recovery_command"] == (
        "./scripts/lab/vm/ha_drill_actions.sh transport-recovery "
        "--variant lab/variants/ha-control-plane-core-drills.yaml --site sea"
    )


def test_ha_drill_actions_dry_run_works_without_live_vms() -> None:
    res = subprocess.run(  # noqa: S603
        [
            str(HA_DRILL_ACTIONS_SCRIPT),
            "leader-failover",
            "--variant",
            str(HA_DRILL_VARIANT_FILE),
            "--dry-run",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "dry-run leader-failover target=current controller leader via etcd" in res.stdout


def test_ha_drill_actions_transport_recovery_dry_run_preserves_split_edge_targets() -> None:
    res = subprocess.run(  # noqa: S603
        [
            str(HA_DRILL_ACTIONS_SCRIPT),
            "transport-recovery",
            "--variant",
            str(HA_DRILL_VARIANT_FILE),
            "--site",
            "sea",
            "--dry-run",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "AE_EDGE_INGRESS_LOCAL_ADDR=${AE_EDGE_INGRESS_LOCAL_ADDR:-192.168.155.21:18081}" in res.stdout
    assert "AE_RATHOLE_SERVER_ADDR=${AE_RATHOLE_SERVER_ADDR:-192.168.155.10:2333}" in res.stdout


def test_ha_drill_actions_require_guest_prereqs() -> None:
    text = HA_DRILL_ACTIONS_SCRIPT.read_text(encoding="utf-8")
    assert "source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh" in text
    assert "ensure_vm_bootstrap_prereqs" in text


def test_ha_drill_actions_restart_processes_without_profile_reentry() -> None:
    text = HA_DRILL_ACTIONS_SCRIPT.read_text(encoding="utf-8")
    assert "python3 -m ae.controller --loop --metrics-port" in text
    assert "wait_for_local_pid() {" in text
    assert "local_tcp_listener_pids() {" in text
    assert "sudo ss -ltnpH" in text
    assert "old_pids=" in text
    assert "pattern_pids=" in text
    assert "port_pids=" in text
    assert 'sudo pkill -TERM -f -- "\\$controller_pattern"' in text
    assert 'sudo kill -TERM "\\$pid"' in text
    assert 'sudo kill -KILL "\\$pid"' in text
    assert 'current_port_pids="\\$(local_tcp_listener_pids ${controller_port} | tr' in text
    assert "controller port ${controller_port} is still busy after stop attempt; pids=" in text
    assert 'wait_for_local_pid "\\$new_pid" 45 1' in text
    assert "AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS=\\${AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS:-1}" in text
    assert "AE_EDGE_INGRESS_CONFIG_DIR=\\${AE_EDGE_INGRESS_CONFIG_DIR:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress}" in text
    assert "python3 -m ae.gateway" in text
    assert "wait_for_local_tcp_port" in text
    assert "wait_for_local_process" in text
    assert "wait_for_local_etcd_health" in text
    assert "edge_runtime_host_json() {" in text
    assert 'local edge_runtime_ip="${3:-}"' in text
    assert 'local edge_rathole_server_addr="${4:-}"' in text
    assert 'edge_local_target_addr="${edge_runtime_ip}:18081"' in text
    assert 'edge_rathole_server_addr="${edge_rathole_server_addr:-127.0.0.1:2333}"' in text
    assert "make k1s-ha-core > /home/ae/k1s-ha-core.log 2>&1 </dev/null &" not in text
    assert "make k1s-edge-core-cri > /home/ae/k1s-edge-core.log 2>&1 </dev/null &" not in text


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
  agent_api_port: 9210
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
    assert payload["k1s"]["agent_api_port"] == 9210
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
      monitor_url: http://192.168.155.20:8223
      expected_gateways:
        - sea--sea-gw
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


def test_host_prepare_parse_args_accepts_variant() -> None:
    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'source "{HOST_PREPARE_SCRIPT}"; '
                f'parse_args --variant "{HA_VARIANT_FILE}" --apply; '
                'printf "%s|%s" "$VARIANT" "$APPLY"'
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == f"{HA_VARIANT_FILE}|1"


def test_host_prepare_rejects_variant_with_manual_network_flags() -> None:
    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'source "{HOST_PREPARE_SCRIPT}"; '
                f'parse_args --variant "{HA_VARIANT_FILE}" --cidr 192.168.155.0/24'
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 2
    assert "--variant cannot be combined with --bridge, --cidr, or --gateway" in res.stderr


def test_host_prepare_detects_existing_bridge_cidr_mismatch(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nexec "$@"\n',
    )
    _write_executable(
        fake_bin / "ip",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "link" && "$2" == "show" && "$3" == "k1s-br0" ]]; then
  exit 0
fi
if [[ "$1" == "-o" && "$2" == "-4" && "$3" == "addr" && "$4" == "show" && "$5" == "dev" && "$6" == "k1s-br0" ]]; then
  echo "344: k1s-br0    inet 192.168.152.1/24 brd 192.168.152.255 scope global k1s-br0"
  exit 0
fi
echo "unexpected ip args: $*" >&2
exit 9
""",
    )

    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'PATH="{fake_bin}:$PATH"; '
                f'source "{HOST_PREPARE_SCRIPT}"; '
                'BRIDGE="k1s-br0"; NET_CIDR="192.168.155.0/24"; GATEWAY="192.168.155.1"; '
                "validate_existing_bridge"
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 1
    assert (
        "bridge=k1s-br0 already exists with IPv4 192.168.152.1/24; expected 192.168.155.1/24"
        in res.stderr
    )
    assert "--destroy-network" in res.stderr


def test_host_prepare_allows_matching_existing_bridge(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nexec "$@"\n',
    )
    _write_executable(
        fake_bin / "ip",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "link" && "$2" == "show" && "$3" == "k1s-br0" ]]; then
  exit 0
fi
if [[ "$1" == "-o" && "$2" == "-4" && "$3" == "addr" && "$4" == "show" && "$5" == "dev" && "$6" == "k1s-br0" ]]; then
  echo "344: k1s-br0    inet 192.168.155.1/24 brd 192.168.155.255 scope global k1s-br0"
  exit 0
fi
echo "unexpected ip args: $*" >&2
exit 9
""",
    )

    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'PATH="{fake_bin}:$PATH"; '
                f'source "{HOST_PREPARE_SCRIPT}"; '
                'BRIDGE="k1s-br0"; NET_CIDR="192.168.155.0/24"; GATEWAY="192.168.155.1"; '
                "validate_existing_bridge && echo ok"
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"


def test_host_prepare_adds_bridge_and_pod_cidr_nat_exemptions() -> None:
    text = HOST_PREPARE_SCRIPT.read_text(encoding="utf-8")
    assert 'declare -a POD_CIDRS=()' in text
    assert 'declare -a POD_ROUTE_ROWS=()' in text
    assert 'declare -a TAP_INTERFACES=()' in text
    assert 'BRIDGE_FORWARD_CHAIN="${BRIDGE_FORWARD_CHAIN:-K1S_VM_BRIDGE_FORWARD}"' in text
    assert "mapfile -t POD_CIDRS" in text
    assert "mapfile -t POD_ROUTE_ROWS" in text
    assert 'select(.role=="k1s-core-node" and (.pod_cidr // "") != "")' in text
    assert 'ensure_nat_return_rule -s "$NET_CIDR" -o "$BRIDGE" -j RETURN' in text
    assert 'ensure_nat_return_rule -s "$NET_CIDR" -d "$pod_cidr" -j RETURN' in text
    assert 'sudo ip route replace "$route_cidr" via "$route_ip" dev "$BRIDGE"' in text
    assert 'sudo iptables -t nat -I POSTROUTING 1 "$@"' in text
    assert 'sudo iptables -N "$FORWARD_CHAIN" 2>/dev/null || true' in text
    assert 'sudo iptables -I FORWARD 1 -j "$FORWARD_CHAIN"' in text
    assert 'sudo ebtables -t filter -N "$BRIDGE_FORWARD_CHAIN" 2>/dev/null || true' in text
    assert 'sudo ebtables -t filter -I FORWARD 1 -j "$BRIDGE_FORWARD_CHAIN"' in text
    assert 'ensure_forward_chain_rule -i "$tap" -j ACCEPT' in text
    assert 'ensure_forward_chain_rule -o "$tap" -j ACCEPT' in text
    assert 'ensure_bridge_forward_chain_rule -i "$tap" -j ACCEPT' in text
    assert 'ensure_bridge_forward_chain_rule -o "$tap" -j ACCEPT' in text


def test_host_prepare_installs_forward_chain_before_host_firewall_jumps(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    iptables_log = tmp_path / "iptables.log"
    ebtables_log = tmp_path / "ebtables.log"

    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nexec "$@"\n',
    )
    _write_executable(
        fake_bin / "iptables",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "{iptables_log}"
for arg in "$@"; do
  if [[ "$arg" == "-C" ]]; then
    exit 1
  fi
done
exit 0
""",
    )
    _write_executable(
        fake_bin / "ebtables",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "{ebtables_log}"
for arg in "$@"; do
  if [[ "$arg" == "-D" ]]; then
    exit 1
  fi
done
exit 0
""",
    )

    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'PATH="{fake_bin}:$PATH"; '
                f'source "{HOST_PREPARE_SCRIPT}"; '
                'FORWARD_CHAIN="K1S_VM_FORWARD"; '
                'BRIDGE="k1s-br0"; NET_CIDR="192.168.155.0/24"; '
                'POD_CIDRS=("10.42.0.0/24"); '
                'TAP_INTERFACES=("k1s0" "k1s1" "k1s2" "k1s3"); '
                "ensure_nat_rules"
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr
    iptables_calls = iptables_log.read_text(encoding="utf-8")
    assert '-N K1S_VM_FORWARD' in iptables_calls
    assert '-F K1S_VM_FORWARD' in iptables_calls
    assert '-A K1S_VM_FORWARD -i k1s0 -j ACCEPT' in iptables_calls
    assert '-A K1S_VM_FORWARD -o k1s3 -j ACCEPT' in iptables_calls
    assert '-C K1S_VM_FORWARD ' not in iptables_calls
    assert '-D K1S_VM_FORWARD ' not in iptables_calls
    assert '-I FORWARD 1 -j K1S_VM_FORWARD' in iptables_calls
    assert '-A FORWARD -j K1S_VM_FORWARD' not in iptables_calls
    ebtables_calls = ebtables_log.read_text(encoding="utf-8")
    assert '-t filter -N K1S_VM_BRIDGE_FORWARD' in ebtables_calls
    assert '-t filter -F K1S_VM_BRIDGE_FORWARD' in ebtables_calls
    assert '-t filter -A K1S_VM_BRIDGE_FORWARD -i k1s0 -j ACCEPT' in ebtables_calls
    assert '-t filter -A K1S_VM_BRIDGE_FORWARD -o k1s3 -j ACCEPT' in ebtables_calls
    assert '-t filter -D K1S_VM_BRIDGE_FORWARD ' not in ebtables_calls
    assert '-t filter -I FORWARD 1 -j K1S_VM_BRIDGE_FORWARD' in ebtables_calls
    assert '-t filter -A FORWARD -j K1S_VM_BRIDGE_FORWARD' not in ebtables_calls


def test_host_prepare_installs_pod_routes_on_bridge(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ip_log = tmp_path / "ip.log"

    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nexec "$@"\n',
    )
    _write_executable(
        fake_bin / "ip",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "{ip_log}"
exit 0
""",
    )

    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'PATH="{fake_bin}:$PATH"; '
                f'source "{HOST_PREPARE_SCRIPT}"; '
                'BRIDGE="k1s-br0"; '
                "POD_ROUTE_ROWS=(); "
                "POD_ROUTE_ROWS+=($'10.42.0.0/24\\t192.168.155.20'); "
                "POD_ROUTE_ROWS+=($'10.42.1.0/24\\t192.168.155.21'); "
                "ensure_pod_routes"
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr
    ip_calls = ip_log.read_text(encoding="utf-8")
    assert "route replace 10.42.0.0/24 via 192.168.155.20 dev k1s-br0" in ip_calls
    assert "route replace 10.42.1.0/24 via 192.168.155.21 dev k1s-br0" in ip_calls


def test_variant_up_uses_variant_aware_host_prepare() -> None:
    text = VARIANT_UP_SCRIPT.read_text(encoding="utf-8")
    assert 'CLOUD_INIT_WAIT_TIMEOUT="${CLOUD_INIT_WAIT_TIMEOUT:-300}"' in text
    assert '"$ROOT_DIR/scripts/lab/vm/host_prepare.sh" --variant "$VARIANT" --apply' in text
    assert 'pod_route_rows="$(' in text
    assert 'select(.role=="k1s-core-node" and (.pod_cidr // "") != "")' in text
    assert 'render_guest_route_yaml() {' in text
    assert "printf '      routes:" in text
    assert 'route_yaml="$(render_guest_route_yaml "$role")"' in text
    assert 'tap="$(lane_tap_name "$index")"' in text
    assert 'log "resetting stale tap=${tap}"' in text
    assert 'sudo ip link set "$tap" nomaster || true' in text
    assert 'err "tap ${tap} still exists after reset"' in text
    assert 'host_disk_gb="$(echo "$row" | jq -r \'.vm.disk_gb\')"' in text
    assert 'validate_overlay_disk_size() {' in text
    assert 'backing image ${backing_image} has virtual size ${backing_size_gib}GiB' in text
    assert 'existing overlay ${overlay} has virtual size ${overlay_size_gib}GiB' in text
    assert 'validate_overlay_disk_size "$name" "$host_disk_gb" "$img" "$overlay"' in text
    assert 'host_mem="$(echo "$row" | jq -r \'.vm.memory_mb\')"' in text
    assert 'host_cpus="$(echo "$row" | jq -r \'.vm.vcpus\')"' in text
    assert 'qemu-img create -f qcow2 -F qcow2 -b "$img" "$overlay" "${host_disk_gb}G" >/dev/null' in text
    assert '-m "$host_mem" -smp "$host_cpus"' in text
    assert 'wait_for_cloud_init "$ip" "$CLOUD_INIT_WAIT_TIMEOUT"' in text
    assert 'err "cloud-init did not complete for ${name} (${ip})"' in text


def test_variant_down_uses_run_inventory_fallback() -> None:
    text = VARIANT_DOWN_SCRIPT.read_text(encoding="utf-8")
    assert "[--purge] [--destroy-network] [--best-effort]" in text
    assert 'run_inventory="$(run_dir "$RUN_ID")/qemu_inventory.json"' in text
    assert 'pod_route_rows="$(' in text
    assert 'select(.role=="k1s-core-node" and (.pod_cidr // "") != "")' in text
    assert 'log "using run inventory fallback for run_id=${RUN_ID}: $inventory"' in text
    assert 'log "continuing with best-effort cleanup derived from variant topology"' in text
    assert 'FORWARD_CHAIN="${FORWARD_CHAIN:-K1S_VM_FORWARD}"' in text
    assert 'BRIDGE_FORWARD_CHAIN="${BRIDGE_FORWARD_CHAIN:-K1S_VM_BRIDGE_FORWARD}"' in text
    assert "cleanup_expected_lane_taps() {" in text
    assert 'tap="$(lane_tap_name "$i")"' in text
    assert 'pid_file="$state_dir/pids/${name}.pid"' in text
    assert "while sudo iptables -t nat -D POSTROUTING -s \"$cidr\" -d \"$pod_cidr\" -j RETURN 2>/dev/null; do" in text
    assert "while sudo iptables -t nat -D POSTROUTING -s \"$cidr\" -o \"$bridge\" -j RETURN 2>/dev/null; do" in text
    assert 'while sudo iptables -D FORWARD -j "$FORWARD_CHAIN" 2>/dev/null; do' in text
    assert 'sudo iptables -F "$FORWARD_CHAIN" 2>/dev/null || true' in text
    assert 'sudo iptables -X "$FORWARD_CHAIN" 2>/dev/null || true' in text
    assert 'while sudo ebtables -t filter -D FORWARD -j "$BRIDGE_FORWARD_CHAIN" 2>/dev/null; do' in text
    assert 'sudo ebtables -t filter -F "$BRIDGE_FORWARD_CHAIN" 2>/dev/null || true' in text
    assert 'sudo ebtables -t filter -X "$BRIDGE_FORWARD_CHAIN" 2>/dev/null || true' in text
    assert 'sudo ip route del "$route_cidr" via "$route_ip" dev "$bridge" 2>/dev/null || true' in text
    assert 'sudo ip link set "$tap" nomaster || true' in text
    assert (
        'elif pids="$(pgrep -f -- "$overlay" 2>/dev/null || true)" && [[ -n "$pids" ]]; then'
        in text
    )
    assert 'if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then' in text


def test_variant_down_best_effort_handles_missing_inventories(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    fake_root.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ip_log = tmp_path / "ip.log"

    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nexec "$@"\n',
    )
    _write_executable(
        fake_bin / "ip",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "{ip_log}"
exit 0
""",
    )
    _write_executable(
        fake_bin / "pgrep",
        """#!/usr/bin/env bash
set -euo pipefail
pattern="${*: -1}"
    if [[ "$pattern" == *"core-a.qcow2"* ]]; then
      echo 4321
      exit 0
    fi
    exit 1
    """,
    )

    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'PATH="{fake_bin}:$PATH"; '
                f'source "{VARIANT_DOWN_SCRIPT}"; '
                f'ROOT_DIR="{fake_root}"; '
                "variant_to_json() { "
                "cat <<'EOF'\n"
                '{"network":{"bridge":"k1s-br0","cidr":"192.168.155.0/24"},'
                '"hosts":[{"name":"core-a"},{"name":"core-b"}]}'
                "\nEOF\n"
                "}; "
                'main --variant fake-variant.yaml --run-id retained-test --best-effort'
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr
    assert "continuing with best-effort cleanup derived from variant topology" in res.stdout
    assert "stopped core-a" in res.stdout
    assert "stopped core-b" in res.stdout
    assert "variant down complete run_id=retained-test" in res.stdout
    ip_calls = ip_log.read_text(encoding="utf-8")
    assert "link show k1s0" in ip_calls
    assert "link delete k1s0" in ip_calls
    assert "link show k1s1" in ip_calls
    assert "link delete k1s1" in ip_calls


def test_variant_down_removes_forward_chain_when_destroying_network(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    fake_root.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    iptables_log = tmp_path / "iptables.log"
    ebtables_log = tmp_path / "ebtables.log"
    ip_log = tmp_path / "ip.log"

    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nexec "$@"\n',
    )
    _write_executable(
        fake_bin / "iptables",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "{iptables_log}"
for arg in "$@"; do
  if [[ "$arg" == "-D" ]]; then
    exit 1
  fi
done
exit 0
""",
    )
    _write_executable(
        fake_bin / "ebtables",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "{ebtables_log}"
for arg in "$@"; do
  if [[ "$arg" == "-D" ]]; then
    exit 1
  fi
done
exit 0
""",
    )
    _write_executable(
        fake_bin / "ip",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "{ip_log}"
exit 0
""",
    )
    _write_executable(
        fake_bin / "pgrep",
        """#!/usr/bin/env bash
set -euo pipefail
exit 1
""",
    )

    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'PATH="{fake_bin}:$PATH"; '
                f'source "{VARIANT_DOWN_SCRIPT}"; '
                f'ROOT_DIR="{fake_root}"; '
                "variant_to_json() { "
                "cat <<'EOF'\n"
                '{"network":{"bridge":"k1s-br0","cidr":"192.168.155.0/24"},'
                '"hosts":[{"name":"core-a","role":"k1s-core-node","ip":"192.168.155.20","pod_cidr":"10.42.0.0/24"},{"name":"core-b"}]}'
                "\nEOF\n"
                "}; "
                'main --variant fake-variant.yaml --run-id retained-test --best-effort --destroy-network'
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr
    iptables_calls = iptables_log.read_text(encoding="utf-8")
    assert '-D FORWARD -j K1S_VM_FORWARD' in iptables_calls
    assert '-F K1S_VM_FORWARD' in iptables_calls
    assert '-X K1S_VM_FORWARD' in iptables_calls
    ebtables_calls = ebtables_log.read_text(encoding="utf-8")
    assert '-t filter -D FORWARD -j K1S_VM_BRIDGE_FORWARD' in ebtables_calls
    assert '-t filter -F K1S_VM_BRIDGE_FORWARD' in ebtables_calls
    assert '-t filter -X K1S_VM_BRIDGE_FORWARD' in ebtables_calls
    ip_calls = ip_log.read_text(encoding="utf-8")
    assert "route del 10.42.0.0/24 via 192.168.155.20 dev k1s-br0" in ip_calls


def test_variant_down_without_best_effort_rejects_missing_inventories(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    fake_root.mkdir()

    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            (
                f'source "{VARIANT_DOWN_SCRIPT}"; '
                f'ROOT_DIR="{fake_root}"; '
                "variant_to_json() { "
                "cat <<'EOF'\n"
                '{"network":{"bridge":"k1s-br0","cidr":"192.168.155.0/24"},'
                '"hosts":[{"name":"core-a"}]}'
                "\nEOF\n"
                "}; "
                'main --variant fake-variant.yaml --run-id retained-test'
            ),
        ],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 1
    assert "inventory not found for run_id=retained-test" in res.stderr
    assert "run inventory fallback also missing" in res.stderr


def test_k1s_bootstrap_core_sets_cri_trust_and_preload_defaults() -> None:
    text = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert 'ha_profile_dir="$ROOT_DIR/state/profiles/k1s-ha-core"' in text
    assert 'ha_apishim_ca_file="$ha_profile_dir/apishim.ca.crt"' in text
    assert "ensure_ha_shared_apishim_tls() {" in text
    assert 'APISHIM_ENV_FILE="$ha_apishim_env_file"' in text
    assert 'APISHIM_CA_FILE="$ha_apishim_ca_file"' in text
    assert '"$ROOT_DIR/scripts/ensure_apishim_env.sh"' in text
    assert '"$ROOT_DIR/scripts/ensure_apishim_cli_env.sh"' in text
    assert "ensure_ha_shared_apishim_tls" in text
    assert "AE_CRI_DATA_ROOT=\\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri}" in text
    assert "AE_CRI_REGISTRY_TRUST_SYSTEM=\\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1}" in text
    assert "AE_CRI_REGISTRY_PRELOAD=\\${AE_CRI_REGISTRY_PRELOAD:-1}" in text
    assert "source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh" in text
    assert "ensure_vm_bootstrap_prereqs" in text
    assert "AE_APISHIM_MODE=\\${AE_APISHIM_MODE:-host}" in text
    assert "bootstrap_seed_cri_cache core" in text
    assert "bootstrap_seed_cri_cache edge" in text
    assert text.count("bootstrap_seed_cri_cache edge") == 2
    assert "make k1s-core-node" in text
    assert "make k1s-ha-core" in text
    assert "AE_CONTROLLER_URL=http://${controller_ip}:${controller_agent_port}" in text
    assert "AE_CONTROLLER_ADVERTISE_ADDR=http://${ip}:${controller_port}" in text
    assert "AE_AGENT_API_PORT=${controller_agent_port}" in text
    assert "AE_AGENT_API_TOKEN=${token}" in text
    assert "AE_APISHIM_ETCD_ENDPOINTS='${ha_etcd_endpoints}'" in text
    assert "AE_APISHIM_PRESEEDED=1" in text
    assert "AE_APISHIM_IMAGE=\\${AE_APISHIM_IMAGE:-localhost:5001/k1s-apishim:dev}" in text
    assert "AE_APISHIM_STARTUP_TIMEOUT=\\${AE_APISHIM_STARTUP_TIMEOUT:-60}" in text
    assert "APISHIM_HOST=\\${APISHIM_HOST:-0.0.0.0}" in text
    assert "APISHIM_CERT_SANS='${ha_apishim_cert_sans}'" in text
    assert "print_ha_bootstrap_failure_context() {" in text
    assert 'ha_profile_owner_path="/mnt/host/state/profiles/k1s-ha-core"' in text
    assert 'stat -c \'%u %g\' "\\$ha_profile_owner_path"' in text
    assert "failed to resolve strict-CRI target ownership" in text
    assert 'sudo tail -n 80 /home/ae/k1s-ha-core.log' in text
    assert 'sudo crictl ps -a 2>/dev/null || true' in text
    assert 'bootstrap_pid=\\$!' in text
    assert 'deadline=\\$((SECONDS + 90))' in text
    assert 'AE_STRICT_CRI_TARGET_UID=\\${strict_cri_target_uid}' in text
    assert 'AE_STRICT_CRI_TARGET_GID=\\${strict_cri_target_gid}' in text
    assert "controller startup failed on ${name} (${ip})" in text
    assert "AE_ROSENPASS_ENABLED=\\${AE_ROSENPASS_ENABLED:-0}" in text
    assert "AE_NODE_PORT=${agent_port}" in text
    assert "AE_GATEWAY_SPOOL_PATH=/var/lib/ae/gateway/gateway-${site_id}-${node_id}.db" in text
    assert "AE_GATEWAY_FENCE_DB=/var/lib/ae/gateway/fence-${site_id}-${node_id}.db" in text
    assert "sudo mkdir -p /var/lib/ae/gateway" in text
    assert 'edge_local_target_addr="${edge_site_runtime_ip:+${edge_site_runtime_ip}:18081}"' in text
    assert 'edge_local_target_addr="\\${edge_local_target_addr:-127.0.0.1:18081}"' in text
    assert 'edge_rathole_server_addr="${edge_hub_leaf_host}:2333"' in text
    assert 'edge_profile_owner_path="/mnt/host/state/profiles/k1s-edge"' in text
    assert 'edge_profile_owner_ids="\\$(stat -c \'%u %g\' "\\$edge_profile_owner_path" 2>/dev/null || true)"' in text
    assert "failed to resolve strict-CRI target ownership for \\$edge_profile_owner_path" in text
    assert 'expected_gateway_node="${site_id}--${node_id}"' in text
    assert 'edge_metrics_url="http://${controller_ip}:${controller_port}/metrics"' in text
    assert 'expected_gateway_metric="ae_site_gateway_last_seen_seconds{site=\\"${site_id}\\",node=\\"\\${expected_gateway_node}\\"}"' in text
    assert "print_edge_bootstrap_failure_context() {" in text
    assert "gateway startup failed on ${name} (${ip})" in text
    assert "edge_gateway_metric_present() {" in text
    assert "urllib.request.urlopen(url, timeout=2)" in text
    assert 'sudo tail -n 80 /home/ae/k1s-edge-core.log' in text
    assert "pgrep -a -f 'python(3)? -m ae\\.gateway|run_profile\\.sh k1s-edge|ae\\.worker_stub'" in text
    assert 'sudo crictl pods -a 2>/dev/null | sed' in text
    assert 'deadline=\\$((SECONDS + 120))' in text
    assert 'AE_STRICT_CRI_TARGET_UID=\\${strict_cri_target_uid}' in text
    assert 'AE_STRICT_CRI_TARGET_GID=\\${strict_cri_target_gid}' in text
    assert 'AE_EDGE_INGRESS_LOCAL_ADDR=\\${AE_EDGE_INGRESS_LOCAL_ADDR:-\\${edge_local_target_addr}}' in text
    assert 'AE_RATHOLE_SERVER_ADDR=\\${AE_RATHOLE_SERVER_ADDR:-\\${edge_rathole_server_addr}}' in text
    assert "AE_CRI_CACHE_SEED_MODE" in text
    assert "AE_CRI_CACHE_SEED_BUNDLE" in text
    assert 'pod_cidr="$(echo "$host_json" | jq -r \'.pod_cidr // empty\')"' in text
    assert "pod_cidr_env=$'  AE_POD_CIDR='" in text
    assert "pod_cidr_env+=$'  AE_CNI_SUBNET='" in text
    assert "AE_CNI_FORCE=\\${AE_CNI_FORCE:-1}" in text
    assert "AE_POD_BRIDGE=\\${AE_POD_BRIDGE:-cni0}" in text
    assert "AE_CNI_BRIDGE_NAME=\\${AE_CNI_BRIDGE_NAME:-\\${AE_POD_BRIDGE:-cni0}}" in text
    assert text.count("REGISTER_ONLY=1 SITE_ID") == 1

    helper_text = CRI_NODE_CNI_HELPER_SCRIPT.read_text(encoding="utf-8")
    assert 'pod_cidr="${AE_POD_CIDR:-}"' in helper_text
    assert 'export AE_CNI_SUBNET="${AE_CNI_SUBNET:-$pod_cidr}"' in helper_text
    assert 'export AE_CNI_FORCE="${AE_CNI_FORCE:-1}"' in helper_text
    assert 'export AE_CNI_BRIDGE_NAME="${AE_CNI_BRIDGE_NAME:-cni0}"' in helper_text
    assert 'bash "${ROOT_DIR}/scripts/cni_init.sh"' in helper_text

    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    assert "AE_CNI_SUBNET=$${AE_CNI_SUBNET:-$${AE_POD_CIDR:-10.42.0.0/24}}" in makefile_text
    assert "AE_CNI_SUBNET=$${AE_CNI_SUBNET:-$${AE_POD_CIDR:-10.42.1.0/24}}" in makefile_text
    assert "AE_CNI_FORCE=$${AE_CNI_FORCE:-1}" in makefile_text
    assert "AE_POD_BRIDGE=$${AE_POD_BRIDGE:-cni0}" in makefile_text
    assert "AE_CNI_BRIDGE_NAME=$${AE_CNI_BRIDGE_NAME:-$${AE_POD_BRIDGE:-cni0}}" in makefile_text
    assert "./scripts/dev/ensure_cri_node_cni.sh && PYTHONPATH=src python -m ae.node --ensure-pod-net" in makefile_text

    run_profile_text = RUN_PROFILE_SCRIPT.read_text(encoding="utf-8")
    assert "STRICT_CRI_OWNERSHIP_HELPER_ARGS=()" in run_profile_text
    assert "strict_cri_explicit_target_configured()" in run_profile_text
    assert "AE_STRICT_CRI_TARGET_UID and AE_STRICT_CRI_TARGET_GID must be set together." in run_profile_text
    assert 'STRICT_CRI_OWNERSHIP_HELPER_ARGS=(--target-uid "$target_uid" --target-gid "$target_gid")' in run_profile_text
    assert (
        'export AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS="${AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS:-1}"'
        in run_profile_text
    )
    assert (
        'export AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS="${AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS:-0}"'
        in run_profile_text
    )


def test_guest_prereqs_script_requires_ready_image_by_default() -> None:
    text = GUEST_PREREQS_SCRIPT.read_text(encoding="utf-8")
    assert "AE_VM_BOOTSTRAP_AUTOFIX:-0" in text
    assert 'missing+=("python")' in text
    assert 'missing+=("crictl")' in text
    assert 'missing+=("/etc/crictl.yaml")' in text
    assert 'missing+=("/opt/cni/bin")' in text
    assert 'missing+=("/etc/cni/net.d")' in text
    assert 'missing+=("containerd-config-valid")' in text
    assert "sudo find /etc/cni/net.d -maxdepth 1 -type f" in text
    assert "containerd --config /etc/containerd/config.toml config dump" in text
    assert "stale VM image; missing prerequisites" in text
    assert "scripts/lab/vm/labctl.sh image build --variant all" in text
    assert "scripts/lab/vm/labctl.sh image verify --variant all" in text
    assert "AE_VM_BOOTSTRAP_AUTOFIX=1" in text


def test_guest_prereqs_blank_output_does_not_trigger_stale_image() -> None:
    res = subprocess.run(  # noqa: S603
        [
            "bash",
            "-lc",
            f"""
source "{GUEST_PREREQS_SCRIPT}"
vm_bootstrap_missing_prereqs() {{
  printf '\\n'
}}
ensure_vm_bootstrap_prereqs
""",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "[vm-prereqs] ready" in res.stdout


def test_ensure_apishim_env_regenerates_when_requested_sans_are_missing() -> None:
    text = ENSURE_APISHIM_ENV_SCRIPT.read_text(encoding="utf-8")
    assert 'CA_FILE="${APISHIM_CA_FILE:-$(dirname "$CERT_FILE")/apishim.ca.crt}"' in text
    assert 'CA_KEY_FILE="${APISHIM_CA_KEY_FILE:-$(dirname "$KEY_FILE")/apishim.ca.key}"' in text
    assert 'san="${APISHIM_CERT_SANS:-DNS:apishim,DNS:localhost,IP:127.0.0.1,IP:::1}"' in text
    assert 'grep -q "CA:TRUE" <<<"$ca_text"' in text
    assert 'grep -q "CA:FALSE" <<<"$cert_text"' in text
    assert "IFS=',' read -r -a san_entries <<<\"$san\"" in text
    assert 'cert_pattern="IP Address:${san_entry#IP:}"' in text
    assert '-addext "basicConstraints=critical,CA:TRUE"' in text
    assert "extendedKeyUsage=serverAuth" in text
    assert 'openssl x509 -req -in "$csr_file" -CA "$CA_FILE" -CAkey "$CA_KEY_FILE"' in text


def test_ensure_apishim_cli_env_uses_dedicated_ca_file() -> None:
    text = ENSURE_APISHIM_CLI_ENV_SCRIPT.read_text(encoding="utf-8")
    assert 'CA_FILE="${APISHIM_CA_FILE:-$(dirname "$CERT_FILE")/apishim.ca.crt}"' in text
    assert 'if [[ -f "$CA_FILE" ]]; then' in text
    assert "warning: CA file missing; skipping CA bundle export: $CA_FILE" in text


def test_ensure_controller_env_preserves_controller_tokens_and_profile_metadata() -> None:
    text = ENSURE_CONTROLLER_ENV_SCRIPT.read_text(encoding="utf-8")
    assert 'ENV_FILE="${CONTROLLER_ENV_FILE:-$ROOT_DIR/state/env.sh}"' in text
    assert 'admin_token="$(read_env_var "AE_API_ADMIN_TOKEN" "$APISHIM_ENV_FILE" || true)"' in text
    assert 'scaler_token="${AE_API_SCALER_TOKEN:-}"' in text
    assert 'read_token="${AE_API_READ_TOKEN:-}"' in text
    assert 'state_db="${AE_STATE_DB:-}"' in text
    assert 'state_backend="${AE_STATE_BACKEND:-}"' in text
    assert 'etcd_endpoints="${AE_ETCD_ENDPOINTS:-}"' in text
    assert 'etcd_prefix="${AE_ETCD_PREFIX:-}"' in text
    assert 'printf \'AE_API_SCALER_TOKEN=%s\\n\' "$scaler_token"' in text
    assert 'printf \'AE_API_READ_TOKEN=%s\\n\' "$read_token"' in text
    assert 'printf \'AE_STATE_BACKEND=%s\\n\' "$state_backend"' in text
    assert 'printf \'AE_APISHIM_SERVER=%s\\n\' "$apishim_server"' in text


def test_ha_shared_infra_script_bootstraps_clustered_backends() -> None:
    text = HA_SHARED_INFRA_SCRIPT.read_text(encoding="utf-8")
    assert "ha shared infra requires exactly 3 hosts with role=k1s-ha-core" in text
    assert 'seed_manifest_default="/mnt/host/lab/variants/cri_seed_images.lock.json"' in text
    assert 'seed_bundle_default="/mnt/host/state/lab-vm/${RUN_ID}/seeds/cri-seed-images.oci.tar"' in text
    assert "bootstrap_seed_cri_cache() {" in text
    assert "source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh" in text
    assert "ensure_vm_bootstrap_prereqs" in text
    assert "bootstrap_seed_cri_cache core" in text
    assert "python3 /mnt/host/scripts/dev/cri_stack.py up-etcd \\" in text
    assert 'AE_CRI_IMAGE_POLICY=\\${AE_CRI_IMAGE_POLICY:-fail}' in text
    assert "--initial-cluster '" in text
    assert "python3 /mnt/host/scripts/dev/cri_stack.py up-nats-hub \\" in text
    assert "HA shared infra NATS cluster did not converge" in text


def test_common_bootstrap_bakes_vm_prereqs_into_images() -> None:
    text = COMMON_BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert "containernetworking-plugins" in text
    assert "python-is-python3" in text
    assert 'echo "[image-bootstrap] installing crictl ${crictl_version} binary"' in text
    assert "install_crictl_binary()" in text
    assert "containerd --config /etc/containerd/config.toml config dump" in text
    assert "write_containerd_bootstrap_config()" in text
    assert "/etc/containerd/conf.d/10-k1s-bootstrap.toml" in text
    assert "sandbox = '${sandbox_image}'" in text
    assert "sandbox_image = '${sandbox_image}'" in text
    assert "/etc/crictl.yaml" in text
    assert "/opt/cni/bin" in text
    assert "10-k1s-bridge.conflist" in text
    assert "systemctl enable containerd" in text
    assert "systemctl restart containerd" in text
    assert 'ctr -n k8s.io images import "$seed_bundle"' in text
    assert 'AE_CRI_SANDBOX_IMAGE="$sandbox_image" AE_CRI_SMOKE_PULL=0 "$cri_smoke_script"' in text
    assert "guest_root_uuid()" in text
    assert "guest_root_label()" in text
    assert "guest_fstab_root_source()" in text
    assert "guest_grub_root_uuids()" in text
    assert "assert_guest_boot_contract()" in text
    assert "ensure_initramfs_module()" in text
    assert "write_virtio_root_modules()" in text
    assert "ensure_initramfs_module virtio_blk" in text
    assert "ensure_initramfs_module virtio_pci" in text
    assert "update-initramfs -u -k all" in text
    assert "update-grub" in text
    assert "assert_guest_boot_contract" in text
    assert "systemctl enable containerd qemu-guest-agent" not in text
    assert "systemctl restart containerd qemu-guest-agent" not in text
    assert '"vm_bootstrap_ready": true' in text
    assert '"python_alias": true' in text
    assert '"crictl_ready": true' in text
    assert '"cni_ready": true' in text


def test_image_build_writes_vm_bootstrap_metadata_flags() -> None:
    text = IMAGE_BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'SEED_BUNDLE_SCRIPT="${SEED_BUNDLE_SCRIPT:-$ROOT_DIR/scripts/lab/vm/image_seed_bundle.sh}"' in text
    assert (
        'ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT="${ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT:-$ROOT_DIR/scripts/lab/vm/assert_image_boot_contract.sh}"'
        in text
    )
    assert "prepare_seed_bundle() {" in text
    assert 'SEED_BUNDLE="$ROOT_DIR/state/lab-vm/$SEED_RUN_ID/seeds/cri-seed-images.oci.tar"' in text
    assert 'bash "$SEED_BUNDLE_SCRIPT" \\' in text
    assert '-var "seed_bundle=${SEED_BUNDLE}" \\' in text
    assert 'build_dir="$OUTPUT_DIR/build-${variant}"' in text
    assert 'rm -rf "$build_dir"' in text
    assert 'rm -f "$sha_file" "$meta_file"' in text
    assert 'bash "$ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" "$image"' in text
    assert "vm_bootstrap_ready:true" in text
    assert "python_alias:true" in text
    assert "crictl_ready:true" in text
    assert "cni_ready:true" in text


def test_packer_template_pins_virtio_disk_interface() -> None:
    text = PACKER_TEMPLATE.read_text(encoding="utf-8")
    assert 'disk_interface   = "virtio"' in text


@pytest.mark.skipif(
    not shutil.which("qemu-img") or not shutil.which("sha256sum") or not shutil.which("jq"),
    reason="image verify dependencies not available",
)
def test_image_verify_requires_vm_bootstrap_metadata_flags(tmp_path: Path) -> None:
    qemu_img = shutil.which("qemu-img")
    sha256sum = shutil.which("sha256sum")
    assert qemu_img is not None
    assert sha256sum is not None
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    image = image_dir / "ubuntu-22.04-k1s-base.qcow2"
    subprocess.run(  # noqa: S603
        [qemu_img, "create", "-f", "qcow2", str(image), "16M"],
        check=True,
        text=True,
        capture_output=True,
    )
    sha_file = Path(f"{image}.sha256")
    meta_file = Path(f"{image}.meta.json")
    sha_file.write_text(
        subprocess.run(  # noqa: S603
            [sha256sum, str(image)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout,
        encoding="utf-8",
    )
    meta_file.write_text(
        json.dumps(
            {
                "image": image.name,
                "variant": "base",
                "kernel_track": "ga-5.15",
                "checksum": sha_file.read_text(encoding="utf-8").split()[0],
                "created_at": "2026-03-19T18:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    res = subprocess.run(  # noqa: S603
        [str(IMAGE_VERIFY_SCRIPT), "--image-dir", str(image_dir), "--variant", "base"],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 1

    meta_file.write_text(
        json.dumps(
            {
                "image": image.name,
                "variant": "base",
                "kernel_track": "ga-5.15",
                "checksum": sha_file.read_text(encoding="utf-8").split()[0],
                "created_at": "2026-03-19T18:00:00Z",
                "bootstrap_contract_version": "20260324-cni-0.4.0-smoke-v1",
                "cni_version": "0.4.0",
                "vm_bootstrap_ready": True,
                "python_alias": True,
                "crictl_ready": True,
                "cni_ready": True,
            }
        ),
        encoding="utf-8",
    )

    res = subprocess.run(  # noqa: S603
        [str(IMAGE_VERIFY_SCRIPT), "--image-dir", str(image_dir), "--variant", "base", "--metadata-only"],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr


@pytest.mark.skipif(
    not shutil.which("qemu-img") or not shutil.which("sha256sum") or not shutil.which("jq"),
    reason="image verify dependencies not available",
)
def test_image_verify_boots_ephemeral_vm_by_default(tmp_path: Path) -> None:
    qemu_img = shutil.which("qemu-img")
    sha256sum = shutil.which("sha256sum")
    assert qemu_img is not None
    assert sha256sum is not None

    image_dir = tmp_path / "images"
    image_dir.mkdir()
    image = image_dir / "ubuntu-22.04-k1s-base.qcow2"
    subprocess.run(  # noqa: S603
        [qemu_img, "create", "-f", "qcow2", str(image), "16M"],
        check=True,
        text=True,
        capture_output=True,
    )
    sha_file = Path(f"{image}.sha256")
    meta_file = Path(f"{image}.meta.json")
    sha_file.write_text(
        subprocess.run(  # noqa: S603
            [sha256sum, str(image)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout,
        encoding="utf-8",
    )
    meta_file.write_text(
        json.dumps(
            {
                "image": image.name,
                "variant": "base",
                "kernel_track": "ga-5.15",
                "checksum": sha_file.read_text(encoding="utf-8").split()[0],
                "created_at": "2026-04-06T19:00:00Z",
                "bootstrap_contract_version": "20260324-cni-0.4.0-smoke-v1",
                "cni_version": "0.4.0",
                "vm_bootstrap_ready": True,
                "python_alias": True,
                "crictl_ready": True,
                "cni_ready": True,
            }
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    verify_log = tmp_path / "verify.log"
    up_log = tmp_path / "up.log"
    down_log = tmp_path / "down.log"
    contract_log = tmp_path / "contract.log"
    variant_log = tmp_path / "variant.log"
    fake_up = tmp_path / "variant_up.sh"
    fake_down = tmp_path / "variant_down.sh"
    fake_assert = tmp_path / "assert_image_boot_contract.sh"

    _write_executable(
        fake_up,
        f"""#!/usr/bin/env bash
set -euo pipefail
variant_file=""
printf '%s\\n' "$*" >> "{up_log}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) variant_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
cat "$variant_file" > "{variant_log}"
""",
    )
    _write_executable(
        fake_down,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{down_log}"
""",
    )
    _write_executable(
        fake_assert,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{contract_log}"
""",
    )
    _write_executable(
        fake_bin / "ssh",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{verify_log}"
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["VARIANT_UP_SCRIPT"] = str(fake_up)
    env["VARIANT_DOWN_SCRIPT"] = str(fake_down)
    env["ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT"] = str(fake_assert)

    res = subprocess.run(  # noqa: S603
        [str(IMAGE_VERIFY_SCRIPT), "--image-dir", str(image_dir), "--variant", "base"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    assert str(image) in contract_log.read_text(encoding="utf-8")
    assert "--run-id image-verify-base-" in up_log.read_text(encoding="utf-8")
    assert "disk_gb: 1" in variant_log.read_text(encoding="utf-8")
    assert "ae@192.168.251.10" in verify_log.read_text(encoding="utf-8")
    assert "ensure_vm_bootstrap_prereqs" in verify_log.read_text(encoding="utf-8")
    down_text = down_log.read_text(encoding="utf-8")
    assert "--destroy-network" in down_text
    assert "--best-effort" in down_text
    assert "--purge" in down_text


@pytest.mark.skipif(not Path("/dev/kvm").exists(), reason="/dev/kvm missing")
def test_variant_up_rejects_overlay_smaller_than_backing_image(tmp_path: Path) -> None:
    variant = tmp_path / "variant.yaml"
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    base = image_dir / "base.qcow2"
    gpu = image_dir / "gpu.qcow2"
    base.write_bytes(b"base")
    gpu.write_bytes(b"gpu")

    for image in (base, gpu):
        Path(f"{image}.meta.json").write_text(
            json.dumps(
                {
                    "vm_bootstrap_ready": True,
                    "python_alias": True,
                    "crictl_ready": True,
                    "cni_ready": True,
                    "bootstrap_contract_version": "20260324-cni-0.4.0-smoke-v1",
                    "cni_version": "0.4.0",
                }
            ),
            encoding="utf-8",
        )

    variant.write_text(
        f"""
name: overlay-too-small
test_id: 9001
network:
  bridge: k1s-br-test
  cidr: 192.168.255.0/24
  gateway: 192.168.255.1
vm:
  memory_mb: 4096
  vcpus: 2
  disk_gb: 30
images:
  base: {base}
  gpu: {gpu}
hosts:
  - name: core-a
    ip: 192.168.255.10
    role: k1s-core
    gpu: false
""".strip(),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    qemu_img_log = tmp_path / "qemu-img.log"
    ssh_key = tmp_path / "id_rsa"
    ssh_key.write_text("private", encoding="utf-8")
    ssh_key.with_suffix(".pub").write_text("ssh-ed25519 AAAATEST test@example\n", encoding="utf-8")
    run_id = "unit-overlay-too-small"
    state_dir = ROOT / "state" / "lab-vm" / run_id
    run_dir = ROOT / "runs" / run_id

    _write_executable(
        fake_bin / "qemu-img",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{qemu_img_log}"
case "${{1:-}}" in
  info)
    printf '%s\\n' '{{"virtual-size":42949672960,"format":"qcow2"}}'
    ;;
  create)
    echo "unexpected qemu-img create" >&2
    exit 99
    ;;
  *)
    echo "unexpected qemu-img args: $*" >&2
    exit 2
    ;;
esac
""",
    )
    _write_executable(fake_bin / "qemu-system-x86_64", "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    _write_executable(fake_bin / "cloud-localds", "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    _write_executable(fake_bin / "ssh", "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    _write_executable(fake_bin / "crictl", "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    _write_executable(
        fake_bin / "ip",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "link" && "${2:-}" == "show" ]]; then
  exit 1
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "iptables",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" -C "* ]] || [[ " $* " == *" -D "* ]]; then
  exit 1
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "ebtables",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" -D "* ]]; then
  exit 1
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "sudo",
        """#!/usr/bin/env bash
set -euo pipefail
exec "$@"
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["SSH_KEY_PATH"] = str(ssh_key)

    try:
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)
        res = subprocess.run(
            [str(VARIANT_UP_SCRIPT), "--variant", str(variant), "--run-id", run_id],
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        assert res.returncode == 1
        assert "host core-a requested vm.disk_gb=30GiB" in res.stderr
        assert f"backing image {base}" in res.stderr
        assert "requires at least 40GiB" in res.stderr
        qemu_img_calls = qemu_img_log.read_text(encoding="utf-8")
        assert "info --output=json" in qemu_img_calls
        assert "create" not in qemu_img_calls
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.skipif(
    not shutil.which("qemu-img") or not shutil.which("sha256sum") or not shutil.which("jq"),
    reason="image verify dependencies not available",
)
def test_image_verify_tears_down_ephemeral_vm_after_boot_failure(tmp_path: Path) -> None:
    qemu_img = shutil.which("qemu-img")
    sha256sum = shutil.which("sha256sum")
    assert qemu_img is not None
    assert sha256sum is not None

    image_dir = tmp_path / "images"
    image_dir.mkdir()
    image = image_dir / "ubuntu-22.04-k1s-base.qcow2"
    subprocess.run(  # noqa: S603
        [qemu_img, "create", "-f", "qcow2", str(image), "16M"],
        check=True,
        text=True,
        capture_output=True,
    )
    sha_file = Path(f"{image}.sha256")
    meta_file = Path(f"{image}.meta.json")
    sha_file.write_text(
        subprocess.run(  # noqa: S603
            [sha256sum, str(image)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout,
        encoding="utf-8",
    )
    meta_file.write_text(
        json.dumps(
            {
                "image": image.name,
                "variant": "base",
                "kernel_track": "ga-5.15",
                "checksum": sha_file.read_text(encoding="utf-8").split()[0],
                "created_at": "2026-04-06T19:00:00Z",
                "bootstrap_contract_version": "20260324-cni-0.4.0-smoke-v1",
                "cni_version": "0.4.0",
                "vm_bootstrap_ready": True,
                "python_alias": True,
                "crictl_ready": True,
                "cni_ready": True,
            }
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    down_log = tmp_path / "down.log"
    inspect_log = tmp_path / "inspect.log"
    fake_up = tmp_path / "variant_up.sh"
    fake_down = tmp_path / "variant_down.sh"
    fake_assert = tmp_path / "assert_image_boot_contract.sh"
    fake_inspect = tmp_path / "inspect_qcow_boot.sh"
    run_dirs_before = {
        path.name
        for path in (ROOT / "state" / "lab-vm").glob("image-verify-base-*")
        if path.is_dir()
    }

    _write_executable(
        fake_up,
        """#!/usr/bin/env bash
set -euo pipefail
""",
    )
    _write_executable(
        fake_down,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{down_log}"
""",
    )
    _write_executable(
        fake_assert,
        """#!/usr/bin/env bash
set -euo pipefail
""",
    )
    _write_executable(
        fake_inspect,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'inspect %s\\n' "$1" >> "{inspect_log}"
printf 'root_uuid=test-root\\n'
""",
    )
    _write_executable(
        fake_bin / "ssh",
        """#!/usr/bin/env bash
set -euo pipefail
echo "boot smoke failed" >&2
exit 1
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["VARIANT_UP_SCRIPT"] = str(fake_up)
    env["VARIANT_DOWN_SCRIPT"] = str(fake_down)
    env["ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT"] = str(fake_assert)
    env["INSPECT_QCOW_BOOT_SCRIPT"] = str(fake_inspect)

    res = subprocess.run(  # noqa: S603
        [str(IMAGE_VERIFY_SCRIPT), "--image-dir", str(image_dir), "--variant", "base"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert res.returncode == 1
    assert "boot smoke failed" in res.stderr
    assert "--destroy-network --best-effort" in down_log.read_text(encoding="utf-8")
    assert "--purge" not in down_log.read_text(encoding="utf-8")
    assert not inspect_log.exists()

    run_dirs_after = {
        path.name
        for path in (ROOT / "state" / "lab-vm").glob("image-verify-base-*")
        if path.is_dir()
    }
    new_run_dirs = sorted(run_dirs_after - run_dirs_before)
    try:
        assert "preserved failed verifier state unavailable" in res.stderr
        assert not new_run_dirs
    finally:
        for run_dir in new_run_dirs:
            shutil.rmtree(ROOT / "state" / "lab-vm" / run_dir, ignore_errors=True)


@pytest.mark.skipif(
    not shutil.which("qemu-img") or not shutil.which("sha256sum") or not shutil.which("jq"),
    reason="image verify dependencies not available",
)
def test_image_verify_preserves_failed_vm_logs_and_classifies_root_mount_failures(tmp_path: Path) -> None:
    qemu_img = shutil.which("qemu-img")
    sha256sum = shutil.which("sha256sum")
    assert qemu_img is not None
    assert sha256sum is not None

    image_dir = tmp_path / "images"
    image_dir.mkdir()
    image = image_dir / "ubuntu-22.04-k1s-base.qcow2"
    subprocess.run(
        [qemu_img, "create", "-f", "qcow2", str(image), "16M"],
        check=True,
        text=True,
        capture_output=True,
    )
    sha_file = Path(f"{image}.sha256")
    meta_file = Path(f"{image}.meta.json")
    sha_file.write_text(
        subprocess.run(
            [sha256sum, str(image)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout,
        encoding="utf-8",
    )
    meta_file.write_text(
        json.dumps(
            {
                "image": image.name,
                "variant": "base",
                "kernel_track": "ga-5.15",
                "checksum": sha_file.read_text(encoding="utf-8").split()[0],
                "created_at": "2026-04-06T19:00:00Z",
                "bootstrap_contract_version": "20260324-cni-0.4.0-smoke-v1",
                "cni_version": "0.4.0",
                "vm_bootstrap_ready": True,
                "python_alias": True,
                "crictl_ready": True,
                "cni_ready": True,
            }
        ),
        encoding="utf-8",
    )

    down_log = tmp_path / "down.log"
    inspect_log = tmp_path / "inspect.log"
    fake_up = tmp_path / "variant_up.sh"
    fake_down = tmp_path / "variant_down.sh"
    fake_assert = tmp_path / "assert_image_boot_contract.sh"
    fake_inspect = tmp_path / "inspect_qcow_boot.sh"
    run_dirs_before = {
        path.name
        for path in (ROOT / "state" / "lab-vm").glob("image-verify-base-*")
        if path.is_dir()
    }

    _write_executable(
        fake_up,
        f"""#!/usr/bin/env bash
set -euo pipefail
run_id=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) run_id="$2"; shift 2 ;;
    *) shift ;;
  esac
done
state_dir="{ROOT}/state/lab-vm/$run_id"
mkdir -p "$state_dir/logs" "$state_dir/pids"
printf 'Gave up waiting for root file system device.\\nALERT! UUID=test-root does not exist.  Dropping to a shell!\\n' > "$state_dir/logs/image-verify-base.console.log"
printf '' > "$state_dir/logs/image-verify-base.qemu.log"
: > "$state_dir/image-verify-base.qcow2"
echo "1234" > "$state_dir/pids/image-verify-base.pid"
exit 1
""",
    )
    _write_executable(
        fake_down,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{down_log}"
""",
    )
    _write_executable(
        fake_assert,
        """#!/usr/bin/env bash
set -euo pipefail
""",
    )
    _write_executable(
        fake_inspect,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$1" >> "{inspect_log}"
printf 'root_uuid=test-root\\n'
""",
    )

    env = os.environ.copy()
    env["VARIANT_UP_SCRIPT"] = str(fake_up)
    env["VARIANT_DOWN_SCRIPT"] = str(fake_down)
    env["ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT"] = str(fake_assert)
    env["INSPECT_QCOW_BOOT_SCRIPT"] = str(fake_inspect)

    res = subprocess.run(
        [str(IMAGE_VERIFY_SCRIPT), "--image-dir", str(image_dir), "--variant", "base"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    run_dirs_after = {
        path.name
        for path in (ROOT / "state" / "lab-vm").glob("image-verify-base-*")
        if path.is_dir()
    }
    new_run_dirs = sorted(run_dirs_after - run_dirs_before)
    try:
        assert res.returncode == 1
        assert new_run_dirs, res.stderr
        state_dir = ROOT / "state" / "lab-vm" / new_run_dirs[0]
        assert (state_dir / "boot-contract.txt").exists()
        assert "boot failed before ssh; root filesystem did not mount" in res.stderr
        assert str(state_dir) in res.stderr
        assert str(state_dir / "image-verify-base.qcow2") in inspect_log.read_text(encoding="utf-8")
        assert "--purge" not in down_log.read_text(encoding="utf-8")
    finally:
        for run_dir in new_run_dirs:
            shutil.rmtree(ROOT / "state" / "lab-vm" / run_dir, ignore_errors=True)


@pytest.mark.skipif(
    not shutil.which("qemu-img") or not shutil.which("sha256sum") or not shutil.which("jq"),
    reason="image verify dependencies not available",
)
def test_image_verify_can_purge_failed_runs_on_request(tmp_path: Path) -> None:
    qemu_img = shutil.which("qemu-img")
    sha256sum = shutil.which("sha256sum")
    assert qemu_img is not None
    assert sha256sum is not None

    image_dir = tmp_path / "images"
    image_dir.mkdir()
    image = image_dir / "ubuntu-22.04-k1s-base.qcow2"
    subprocess.run(
        [qemu_img, "create", "-f", "qcow2", str(image), "16M"],
        check=True,
        text=True,
        capture_output=True,
    )
    sha_file = Path(f"{image}.sha256")
    meta_file = Path(f"{image}.meta.json")
    sha_file.write_text(
        subprocess.run(
            [sha256sum, str(image)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout,
        encoding="utf-8",
    )
    meta_file.write_text(
        json.dumps(
            {
                "image": image.name,
                "variant": "base",
                "kernel_track": "ga-5.15",
                "checksum": sha_file.read_text(encoding="utf-8").split()[0],
                "created_at": "2026-04-06T19:00:00Z",
                "bootstrap_contract_version": "20260324-cni-0.4.0-smoke-v1",
                "cni_version": "0.4.0",
                "vm_bootstrap_ready": True,
                "python_alias": True,
                "crictl_ready": True,
                "cni_ready": True,
            }
        ),
        encoding="utf-8",
    )

    down_log = tmp_path / "down.log"
    fake_up = tmp_path / "variant_up.sh"
    fake_down = tmp_path / "variant_down.sh"
    fake_assert = tmp_path / "assert_image_boot_contract.sh"

    _write_executable(
        fake_up,
        """#!/usr/bin/env bash
set -euo pipefail
exit 1
""",
    )
    _write_executable(
        fake_down,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{down_log}"
""",
    )
    _write_executable(
        fake_assert,
        """#!/usr/bin/env bash
set -euo pipefail
""",
    )

    env = os.environ.copy()
    env["VARIANT_UP_SCRIPT"] = str(fake_up)
    env["VARIANT_DOWN_SCRIPT"] = str(fake_down)
    env["ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT"] = str(fake_assert)
    env["INSPECT_QCOW_BOOT_SCRIPT"] = str(fake_assert)

    res = subprocess.run(
        [str(IMAGE_VERIFY_SCRIPT), "--image-dir", str(image_dir), "--variant", "base", "--purge-failed"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert res.returncode == 1
    assert "--destroy-network --best-effort --purge" in down_log.read_text(encoding="utf-8")


def test_inspect_qcow_boot_reports_expected_root_uuid(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    rootfs = fixture / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "boot" / "grub").mkdir(parents=True)
    (rootfs / "boot").mkdir(exist_ok=True)
    (fixture / "lsblk.txt").write_text(
        "NAME         SIZE FSTYPE UUID       PARTUUID MOUNTPOINT\n"
        "nbd0          40G                         \n"
        "├─nbd0p1    39.9G ext4   test-root  part-1  \n"
        "└─nbd0p15    106M vfat   EFI-0001   part-15 \n",
        encoding="utf-8",
    )
    (fixture / "blkid.txt").write_text(
        '/dev/nbd0p1: LABEL="cloudimg-rootfs" UUID="test-root" TYPE="ext4" PARTUUID="part-1"\n'
        '/dev/nbd0p15: UUID="EFI-0001" TYPE="vfat" PARTUUID="part-15"\n',
        encoding="utf-8",
    )
    (rootfs / "etc" / "fstab").write_text("UUID=test-root / ext4 defaults 0 1\n", encoding="utf-8")
    (rootfs / "boot" / "grub" / "grub.cfg").write_text(
        "menuentry 'Ubuntu' {\n linux /boot/vmlinuz root=UUID=test-root ro quiet splash\n}\n",
        encoding="utf-8",
    )
    (rootfs / "boot" / "vmlinuz-5.15.0").write_text("", encoding="utf-8")
    (rootfs / "boot" / "initrd.img-5.15.0").write_text("", encoding="utf-8")
    (rootfs / "boot" / "vmlinuz").symlink_to("vmlinuz-5.15.0")
    (rootfs / "boot" / "initrd.img").symlink_to("initrd.img-5.15.0")

    env = os.environ.copy()
    env["IMAGE_BOOT_CONTRACT_FIXTURE_DIR"] = str(fixture)

    res = subprocess.run(
        [str(INSPECT_QCOW_BOOT_SCRIPT), "dummy.qcow2"],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    assert "root_uuid=test-root" in res.stdout
    assert "root_label=cloudimg-rootfs" in res.stdout
    assert "fstab_root_uuid=test-root" in res.stdout
    assert "grub_root_uuids=test-root" in res.stdout


def test_assert_image_boot_contract_accepts_matching_root_label(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    rootfs = fixture / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "boot" / "grub").mkdir(parents=True)
    (fixture / "lsblk.txt").write_text(
        "NAME         SIZE FSTYPE UUID       PARTUUID MOUNTPOINT\n"
        "nbd0          40G                         \n"
        "└─nbd0p1    39.9G ext4   test-root  part-1  \n",
        encoding="utf-8",
    )
    (fixture / "blkid.txt").write_text(
        '/dev/nbd0p1: LABEL="cloudimg-rootfs" UUID="test-root" TYPE="ext4" PARTUUID="part-1"\n',
        encoding="utf-8",
    )
    (rootfs / "etc" / "fstab").write_text(
        "LABEL=cloudimg-rootfs / ext4 discard,errors=remount-ro 0 1\n",
        encoding="utf-8",
    )
    (rootfs / "boot" / "grub" / "grub.cfg").write_text(
        "menuentry 'Ubuntu' {\n linux /boot/vmlinuz root=UUID=test-root ro quiet splash\n}\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["IMAGE_BOOT_CONTRACT_FIXTURE_DIR"] = str(fixture)

    res = subprocess.run(
        [str(ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT), "dummy.qcow2"],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    assert "[image-boot-contract] ok: dummy.qcow2" in res.stdout


def test_assert_image_boot_contract_rejects_uuid_mismatch(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    rootfs = fixture / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "boot" / "grub").mkdir(parents=True)
    (fixture / "lsblk.txt").write_text(
        "NAME         SIZE FSTYPE UUID       PARTUUID MOUNTPOINT\n"
        "nbd0          40G                         \n"
        "└─nbd0p1    39.9G ext4   test-root  part-1  \n",
        encoding="utf-8",
    )
    (fixture / "blkid.txt").write_text(
        '/dev/nbd0p1: UUID="test-root" TYPE="ext4" PARTUUID="part-1"\n',
        encoding="utf-8",
    )
    (rootfs / "etc" / "fstab").write_text("UUID=wrong-root / ext4 defaults 0 1\n", encoding="utf-8")
    (rootfs / "boot" / "grub" / "grub.cfg").write_text(
        "menuentry 'Ubuntu' {\n linux /boot/vmlinuz root=UUID=wrong-root ro quiet splash\n}\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["IMAGE_BOOT_CONTRACT_FIXTURE_DIR"] = str(fixture)

    res = subprocess.run(
        [str(ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT), "dummy.qcow2"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert res.returncode == 1
    assert "fstab root UUID mismatch" in res.stderr or "grub root UUID mismatch" in res.stderr


def test_run_profile_host_apishim_uses_src_pythonpath() -> None:
    text = RUN_PROFILE_SCRIPT.read_text(encoding="utf-8")
    assert 'local apishim_pythonpath="$ROOT_DIR/src"' in text
    assert 'nohup env PYTHONPATH="$apishim_pythonpath" "$PYTHON_BIN" -m ae.apishim serve' in text
    assert 'local ca_file="${APISHIM_CA_FILE:-$profile_dir/apishim.ca.crt}"' in text
    assert 'local ca_key_file="${APISHIM_CA_KEY_FILE:-$profile_dir/apishim.ca.key}"' in text
    assert 'if is_truthy "${AE_APISHIM_PRESEEDED:-0}"' in text
    assert (
        '&& [[ -f "$env_file" && -f "$cert_file" && -f "$key_file" '
        '&& -f "$ca_file" && -f "$ca_key_file" ]]; then'
    ) in text
    assert 'APISHIM_CA_FILE="$ca_file" APISHIM_CA_KEY_FILE="$ca_key_file"' in text


def test_cri_image_mirror_prefers_local_cache() -> None:
    text = CRI_IMAGE_MIRROR_SCRIPT.read_text(encoding="utf-8")
    assert "AE_CRI_IMAGE_MIRROR_ALWAYS_PULL" in text
    assert "AE_CRI_IMAGE_MIRROR_BACKEND" in text
    assert "[cri-image-mirror] source already cached: ${image}" in text
    assert 'ctr -n "$ctr_namespace" images ls -q' in text
    assert 'grep -Fx -- "$image"' in text
    assert (
        'ctr -n "$ctr_namespace" images convert --platform "$ctr_platform" "$source" "$target_image"'
        in text
    )
    assert 'crictl --runtime-endpoint "$cri_endpoint" rmi "$target_image"' in text
    assert 'ctr -n "$ctr_namespace" images delete "$target_image"' in text
    assert "evicting CRI image cache" in text
    assert "evicting ctr image ref" in text
    assert 'engine_push "$target_image"' in text
    assert "k1s-ctr-stage" not in text
    assert "ctr backend requires 'ctr images convert' support" in text


def test_cri_seed_bundle_script_accepts_run_id_and_profile() -> None:
    text = CRI_SEED_BUNDLE_SCRIPT.read_text(encoding="utf-8")
    assert "--run-id <id>" in text
    assert "--profile <name>" in text
    assert "AE_CRI_CACHE_SEED_ENGINE" in text
    assert "[cri-seed] source already cached: $image" in text
    assert "build_cri_apishim_image.sh" in text
    assert "samples/servers/shell-demo" in text
    assert "local seed image already cached" in text
    assert "images export" in text or "save -o" in text


def test_ha_docs_use_variant_aware_host_prepare() -> None:
    bring_up = HA_BRING_UP_DOC.read_text(encoding="utf-8")
    runbook = VM_VARIANT_RUNBOOK_DOC.read_text(encoding="utf-8")
    assert (
        "scripts/lab/vm/labctl.sh host prepare \\\n"
        "  --variant lab/variants/ha-control-plane-attached-node.yaml \\\n"
        "  --apply"
    ) in bring_up
    assert (
        "scripts/lab/vm/labctl.sh host prepare \\\n"
        "  --variant lab/variants/ha-control-plane-attached-node.yaml \\\n"
        "  --apply"
    ) in runbook
    assert "lab/variants/ha-control-plane-attached-node.yaml" in bring_up
    assert "lab/variants/ha-control-plane-attached-node.yaml" in runbook
    assert "--purge" in bring_up
    assert "--purge" in runbook
    assert "`--destroy-network` only when you want full bridge cleanup" in bring_up
    assert "`--destroy-network` only when you want full bridge cleanup" in runbook
    assert "source <(ae auth local --strict)" in bring_up
    assert ("Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}") in bring_up
    assert (
        "source <(APISHIM_ENV_FILE=state/profiles/k1s-ha-core/apishim.env CONTROLLER_ENV_FILE=state/profiles/k1s-ha-core/controller.env bash scripts/ae-env.sh local)"
    ) in bring_up
    assert (
        "source <(APISHIM_ENV_FILE=state/profiles/k1s-ha-core/apishim.env CONTROLLER_ENV_FILE=state/profiles/k1s-ha-core/controller.env bash scripts/ae-env.sh local)"
    ) in runbook
    assert ("Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}") in runbook
    assert "scripts/lab/vm/labctl.sh image verify --variant all" in bring_up
    assert "scripts/lab/vm/labctl.sh image verify --variant all" in runbook
    assert (
        "Normal reruns now auto-clean the matching per-variant Packer work directory." in bring_up
    )
    assert "Normal reruns now auto-clean the matching per-variant Packer work directory." in runbook
    assert "artifacts/images/build-base" in bring_up
    assert "artifacts/images/build-base" in runbook
    assert 'LAB_VM_SMOKE_ARGS="--teardown never"' in bring_up
    assert 'LAB_VM_SMOKE_ARGS="--teardown never"' in runbook
    assert "https://dash.home.arpa:10443/dashboard" in bring_up
    assert "https://dash.home.arpa:10443/dashboard" in runbook
    assert "https://docs.home.arpa:10443/" in bring_up
    assert "https://docs.home.arpa:10443/" in runbook
    assert "https://api.home.arpa:10443/swagger" in bring_up
    assert "https://api.home.arpa:10443/swagger" in runbook
    assert "getent hosts dash.home.arpa docs.home.arpa api.home.arpa" in bring_up
    assert "getent hosts dash.home.arpa docs.home.arpa api.home.arpa" in runbook
    assert "no separate `nixos-rebuild` should be required" in bring_up
    assert "no separate `nixos-rebuild` should be required" in runbook
    assert "restore the prior localhost-oriented mapping on purge/reset" in bring_up
    assert "restore the prior localhost-oriented mapping on purge/reset" in runbook
    assert "remove the retained managed mapping instead" in bring_up
    assert "remove the retained managed mapping instead" in runbook
    assert "Use the same bearer token in the dashboard `Bearer` field" in bring_up
    assert "use the same bearer token for the dashboard data panels" in runbook


def test_retained_ha_attached_node_docs_use_make_helper_targets() -> None:
    bring_up = HA_BRING_UP_DOC.read_text(encoding="utf-8")
    runbook = VM_VARIANT_RUNBOOK_DOC.read_text(encoding="utf-8")
    ops = OPS_RUNBOOK_DOC.read_text(encoding="utf-8")
    for text in (bring_up, runbook, ops):
        assert "make lab-vm-ha-attached-node-up" in text
        assert "make lab-vm-ha-attached-node-status" in text
        assert "make lab-vm-ha-attached-node-workload-smoke" in text
        assert "make lab-vm-ha-attached-node-refresh-all" in text
        assert "make lab-vm-ha-attached-node-down" in text
        assert "make lab-vm-ha-attached-node-purge" in text
        assert "make lab-vm-ha-attached-node-reset" in text
    assert 'LAB_VM_HA_ATTACHED_NODE_ARGS="--purge"' not in bring_up
    assert 'LAB_VM_HA_ATTACHED_NODE_ARGS="--purge"' not in runbook
    assert 'LAB_VM_HA_ATTACHED_NODE_ARGS="--purge"' not in ops
    assert 'LAB_VM_HA_ATTACHED_NODE_ARGS="--target all"' in bring_up
    assert 'LAB_VM_HA_ATTACHED_NODE_ARGS="--rebuild-images --destroy-network"' in runbook
    assert 'retained-VM "rebuild and restart all" path' in ops
    assert "ha-web-smoke.home.arpa" in bring_up
    assert "ha-web-smoke.home.arpa" in runbook
    assert "ha-web-smoke.home.arpa" in ops
    assert "ha-web-smoke.home.arpa:10443:192.168.155.10" in bring_up
    assert "ha-web-smoke.home.arpa:10443:192.168.155.10" in runbook


def test_vm_golden_image_pipeline_docs_cover_auto_cleanup_and_manual_recovery() -> None:
    text = VM_GOLDEN_IMAGE_PIPELINE_DOC.read_text(encoding="utf-8")
    assert "Repeated `image build` runs now auto-clean the matching per-variant Packer work" in text
    assert "manually remove `artifacts/images/build-base` and `artifacts/images/build-gpu`" in text
    assert "scripts/lab/vm/labctl.sh image build --variant all" in text
    assert "scripts/lab/vm/labctl.sh image verify --variant all" in text
    assert "normal reruns do not require a manual cleanup" in text


def test_lab_vm_scripts_prefer_repo_venv_python() -> None:
    common_text = COMMON_SCRIPT.read_text(encoding="utf-8")
    smoke_text = (ROOT / "scripts" / "lab" / "vm" / "smoke.sh").read_text(encoding="utf-8")
    assert "$ROOT_DIR/.venv/bin/python" in common_text
    assert 'exec "$(lab_python)" "$SCRIPT_DIR/smoke_v2.py" "$@"' in smoke_text


def test_make_lab_vm_smoke_uses_smoke_helper_wrapper() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "lab-vm-smoke:" in text
    assert "./scripts/lab/vm/smoke_helper.py" in text
    assert "./scripts/lab/vm/labctl.sh smoke" not in text
    assert "$${VARIANT:-lab/variants/test3-abc-pp2.yaml}" in text
    assert "$${LAB_VM_SMOKE_ARGS:-}" in text


def test_make_lab_vm_ha_attached_node_targets_use_helper_wrapper() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    for target in (
        "lab-vm-ha-attached-node-up:",
        "lab-vm-ha-attached-node-status:",
        "lab-vm-ha-attached-node-down:",
        "lab-vm-ha-attached-node-purge:",
        "lab-vm-ha-attached-node-workload-smoke:",
        "lab-vm-ha-core-workload-smoke:",
        "lab-vm-ha-attached-node-reseed-core:",
        "lab-vm-ha-attached-node-restart-core:",
        "lab-vm-ha-attached-node-restart-apishim:",
        "lab-vm-ha-attached-node-restart-node:",
        "lab-vm-ha-attached-node-refresh-all:",
        "lab-vm-ha-attached-node-reset:",
    ):
        assert target in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh up" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh status" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh down" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh purge" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh workload-smoke" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh core-workload-smoke" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh reseed-core" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh restart-core" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh restart-apishim" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh restart-node" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh refresh-all" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh reset" in text
    assert "RUN_ID=$${RUN_ID:-ha-attached-node-local}" in text
    assert "VARIANT=$${VARIANT:-lab/variants/ha-control-plane-attached-node.yaml}" in text
    assert "VARIANT=$${VARIANT:-lab/variants/ha-control-plane-core.yaml}" in text
    assert "$${LAB_VM_HA_ATTACHED_NODE_ARGS:-}" in text


def test_ha_dashboard_smoke_helper_wires_retained_refresh_and_reset_paths() -> None:
    text = HA_DASHBOARD_SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert 'DEFAULT_VARIANT="$ROOT_DIR/lab/variants/ha-control-plane-attached-node.yaml"' in text
    assert 'DEFAULT_RUN_ID="ha-attached-node-local"' in text
    assert 'source "$ROOT_DIR/scripts/lib/nixos_bridge.sh"' in text
    assert "workload-smoke" in text
    assert "core-workload-smoke" in text
    assert "reseed-core" in text
    assert "restart-core" in text
    assert "restart-apishim" in text
    assert "restart-node" in text
    assert "refresh-all" in text
    assert "purge" in text
    assert "reset" in text
    assert '"$SCRIPT_DIR/image_verify.sh" --variant all' in text
    assert 'if [[ -f "$seed_bundle_path" ]]; then' in text
    assert text.index('"$SCRIPT_DIR/image_seed_bundle.sh"') < text.index(
        '"$SCRIPT_DIR/k1s_bootstrap.sh" --variant "$VARIANT" --run-id "$RUN_ID" --execute'
    )
    assert (
        '"$SCRIPT_DIR/ha_shared_infra.sh" --variant "$VARIANT" --run-id "$RUN_ID" --execute' in text
    )
    assert (
        '"$SCRIPT_DIR/k1s_bootstrap.sh" --variant "$VARIANT" --run-id "$RUN_ID" --execute' in text
    )
    assert '"$SCRIPT_DIR/image_seed_bundle.sh" \\' in text
    assert '--run-id "$RUN_ID" \\' in text
    assert "--profile core \\" in text
    assert '--output "$seed_bundle_path"' in text
    assert "python3 /mnt/host/scripts/dev/cri_stack.py up-apishim" in text
    assert "make k1s-core-node > /home/ae/k1s-core-node.log 2>&1 </dev/null &" in text
    assert '"$SCRIPT_DIR/image_build.sh" --variant all' in text
    assert "cmd_purge\n    return 0" in text
    assert 'local down_args=(--variant "$VARIANT" --run-id "$RUN_ID" --best-effort)' in text
    assert 'purge_retained_artifacts() {' in text
    assert 'cmd_workload_smoke() {' in text
    assert 'purge_retained_artifacts\n  cmd_up' in text
    assert 'rm -rf "$run_path"' in text
    assert "localhost:5001/k1s-apishim:dev" in text
    assert "docker.io/library/demo-shell:latest" in text
    assert 'retained_workload_smoke_manifest="${AE_RETAINED_WORKLOAD_SMOKE_MANIFEST:-$ROOT_DIR/docs/site/examples/ha-web-smoke.yaml}"' in text
    assert 'retained_workload_smoke_host="${AE_RETAINED_WORKLOAD_SMOKE_HOST:-ha-web-smoke.home.arpa}"' in text
    assert 'retained_workload_smoke_expected_text="${AE_RETAINED_WORKLOAD_SMOKE_EXPECTED_TEXT:-Shell + Port-Forward Smoke}"' in text
    assert 'attached_node_expected_labels="${attached_node_labels:-role=worker,site=core}"' in text
    assert "wait_for_attached_node_registration() {" in text
    assert 'ha_core_workload_smoke_manifest="${AE_HA_CORE_WORKLOAD_SMOKE_MANIFEST:-$ROOT_DIR/docs/site/examples/ha-web-smoke-edge.yaml}"' in text
    assert 'ha_core_workload_smoke_host="${AE_HA_CORE_WORKLOAD_SMOKE_HOST:-ha-edge-web-smoke.home.arpa}"' in text
    assert 'edge_runtime_site_id="$(echo "$variant_json" | jq -r \'.hosts[] | select(.role=="k1s-edge-node") | (.site_id // "")\' | head -n1)"' in text
    assert 'edge_gateway_ip="$(' in text
    assert '.hosts[] | select(.role=="k1s-edge-core" and (.site_id // "") == $site) | .ip' in text
    assert "print_remote_ha_failure_context() {" in text
    assert "snapshot_local_dev_hosts_state() {" in text
    assert "render_retained_local_dev_apply_map() {" in text
    assert "apply_retained_local_dev_hosts() {" in text
    assert "restore_retained_local_dev_hosts() {" in text
    assert "verify_retained_local_dev_hosts_applied() {" in text
    assert 'local_dev_hosts_dir="$(run_dir "$RUN_ID")"' in text
    assert 'local_dev_hosts_state_file="$local_dev_hosts_dir/local-dev-hosts.env"' in text
    assert 'local_dev_hosts_snapshot_file="$local_dev_hosts_dir/local-dev-hosts.snapshot"' in text
    assert 'local_dev_hosts_apply_file="$local_dev_hosts_dir/local-dev-hosts.apply"' in text
    assert 'AE_NIXOS_REBUILD=always \\' in text
    assert 'DEV_LOCAL_HOSTS_MAP_FILE="$local_dev_hosts_apply_file"' in text
    assert 'DEV_LOCAL_HOSTS_MAP_FILE="$local_dev_hosts_snapshot_file"' in text
    assert 'AE_DEV_LOCAL_ACTION=clean "$ROOT_DIR/scripts/dev/ensure_dev_local.sh"' in text
    assert 'current_output="$(getent hosts "${RETAINED_LOCAL_DEV_TARGET_HOSTS[@]}" 2>/dev/null || true)"' in text
    assert "check_stack_ready\n  apply_retained_local_dev_hosts\n  verify_retained_local_dev_hosts_applied\n  cmd_status" in text
    assert "append_label_args_from_csv() {" in text
    assert '"$ROOT_DIR/scripts/dev/ha_core_node_smoke.py" ingress-smoke' in text
    assert '--manifest "$retained_workload_smoke_manifest"' in text
    assert '--app-name "$retained_workload_smoke_app"' in text
    assert 'log "verifying core-local Envoy ingress host=${retained_workload_smoke_host}:${ingress_tls_port} via ${first_core_ip}"' in text
    assert '--direct-probe-host "$first_core_ip"' in text
    assert 'cmd_core_workload_smoke() {' in text
    assert '--manifest "$ha_core_workload_smoke_manifest"' in text
    assert '--app-name "$ha_core_workload_smoke_app"' in text
    assert '--ingress-host "$ha_core_workload_smoke_host"' in text
    assert '--target-probe-host "$edge_gateway_ip"' in text
    assert '--target-probe-user ae' in text
    assert '--target-probe-url "http://${edge_runtime_ip}:18081/healthz"' in text
    assert '--target-probe-timeout 60' in text
    assert '--ingress-host "$retained_workload_smoke_host"' in text
    assert '--resolve-ip "$first_core_ip"' in text
    assert '--expected-text "$retained_workload_smoke_expected_text"' in text
    assert 'sudo tail -n 80 /home/ae/k1s-ha-core.log' in text
    assert 'sudo crictl ps -a --name k1s-core-apishim -q' in text
    assert 'focus="${3:-controller}"' in text
    assert 'print_remote_ha_failure_context "$name" "$ip" controller' in text
    assert 'print_remote_ha_failure_context "$name" "$ip" apishim' in text
    assert 'mapfile -t selected_rows < <(core_target_rows "$TARGET")' in text
    assert 'for row in "${selected_rows[@]}"; do' in text
    assert "old_pids=" in text
    assert 'sudo pkill -TERM -f -- "\\$controller_pattern"' in text
    assert 'sudo kill -KILL "\\$pid"' in text
    assert 'new_pid=\\$!' in text
    assert 'kill -0 "\\$new_pid"' in text
    assert "AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS=\\${AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS:-1}" in text
    assert "AE_EDGE_INGRESS_RELOAD_CMD=\"python3 /mnt/host/scripts/dev/cri_stack.py up-envoy --profile k1s-ha-core" in text
    assert "workload-smoke requires a live retained HA run; run 'make lab-vm-ha-attached-node-up' first" in text
    assert "requires a live retained HA run; run 'make lab-vm-ha-attached-node-up' first" in text


def test_ha_dashboard_smoke_status_guidance_covers_auth_and_api_only_ingress() -> None:
    text = HA_DASHBOARD_SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert 'ingress_tls_port="${AE_EDGE_INGRESS_TLS_PORT:-10443}"' in text
    assert 'dash_host="${AE_CONTROLPLANE_DASH_HOST:-dash.home.arpa}"' in text
    assert "probe_resolved_https_status() {" in text
    assert "read_api_system_summary_with_retry() {" in text
    assert 'local attempts="${2:-6}"' in text
    assert 'sleep "$delay_s"' in text
    assert 'dash_code="$(probe_resolved_https_status "$dash_host" "/dashboard" "$ip")"' in text
    assert 'docs_code="$(probe_resolved_https_status "$docs_host" "/" "$ip")"' in text
    assert 'api_swagger_code="$(probe_resolved_https_status "$api_host" "/swagger" "$ip")"' in text
    assert 'api_redoc_code="$(probe_resolved_https_status "$api_host" "/redoc" "$ip")"' in text
    assert 'api_dashboard_code="$(probe_resolved_https_status "$api_host" "/dashboard" "$ip")"' in text
    assert 'system_result="$(read_api_system_summary_with_retry "$ip" || true)"' in text
    assert 'local controller_env_file="$ROOT_DIR/state/profiles/k1s-ha-core/controller.env"' in text
    assert "system=000 unavailable" in text
    assert "system=401 auth_required" in text
    assert "system=403 forbidden" in text
    assert "system=200 ha=redacted_or_converging" in text
    assert (
        "source <(APISHIM_ENV_FILE=state/profiles/k1s-ha-core/apishim.env CONTROLLER_ENV_FILE=state/profiles/k1s-ha-core/controller.env bash scripts/ae-env.sh local)"
        in text
    )
    assert 'printf \'  dashboard bearer: %s\\n\' "$AE_API_ADMIN_TOKEN"' in text
    assert "dashboard bearer: unavailable" in text
    assert "paste the dashboard bearer value into the dashboard Bearer field." in text
    assert (
        'curl -sk --resolve %s:%s:%s -H "Authorization: Bearer ${AE_API_READ_TOKEN:-$AE_API_ADMIN_TOKEN}" https://%s:%s/system | jq .'
        in text
    )
    assert (
        "note: test dash/docs/api without auth first; bearer auth is only required for API reads like /system."
        in text
    )
    assert (
        "note: https://%s:%s/dashboard is expected to return 404; dashboard lives on %s."
        in text
    )
    assert "Local host mapping" in text
    assert 'getent hosts %s %s %s' in text
    assert "expected after up: dash/docs/api resolve to %s from this host" in text
    assert "successful retained up verifies this host mapping before reporting success" in text
    assert "purge/reset restore the prior managed local-dev mapping when one was captured" in text


def test_make_ha_closeout_e2e_uses_wrapper_script() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "ha-closeout-e2e:" in text
    assert "./scripts/dev/ha_closeout_e2e.sh" in text
    assert "$${HA_CLOSEOUT_E2E_ARGS:-}" in text


def test_ha_closeout_e2e_wrapper_prepares_runtime_and_runs_pytest() -> None:
    text = HA_CLOSEOUT_E2E_SCRIPT.read_text(encoding="utf-8")
    assert 'source "${ROOT_DIR}/scripts/lib/python_runtime.sh"' in text
    assert 'PYTHON_BIN="$(k1s_find_python "$ROOT_DIR")"' in text
    assert "k1s_ensure_runtime_libs" in text
    assert 'k1s_grpc_preflight "$PYTHON_BIN" "[ha-closeout-e2e]"' in text
    assert "AE_E2E_HA_CLOSEOUT=1" in text
    assert "-m pytest -q tests/integration/test_ha_closeout_e2e.py" in text


def test_make_strict_cri_smoke_uses_wrapper_script() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "strict-cri-smoke:" in text
    assert "./scripts/dev/strict_cri_smoke.sh" in text
    assert "AE_CRI_REQUIRE_RUNTIME_READY=1 ./scripts/cri_preflight.sh" in text


def test_strict_cri_smoke_wrapper_prepares_runtime_and_runs_pytest() -> None:
    text = STRICT_CRI_SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert 'source "${ROOT_DIR}/scripts/lib/python_runtime.sh"' in text
    assert 'PYTHON_BIN="$(k1s_find_python "$ROOT_DIR")"' in text
    assert "k1s_ensure_runtime_libs" in text
    assert 'k1s_grpc_preflight "$PYTHON_BIN" "[strict-cri-smoke]"' in text
    assert 'AE_STRICT_CRI_PROFILE_SMOKE="${AE_STRICT_CRI_PROFILE_SMOKE:-1}"' in text
    assert 'AE_CRI_IT="${AE_CRI_IT:-1}"' in text
    assert 'AE_CRI_SMOKE_PULL="${AE_CRI_SMOKE_PULL:-1}"' in text
    assert "-m pytest --maxfail=1 --disable-warnings -q" in text
    assert "tests/integration/test_strict_cri_profile_smoke.py" in text
    assert "tests/integration/test_cri_smoke.py" in text
    assert "tests/integration/test_cri_runtime_integration.py" in text


def test_cri_preflight_resolves_python3_fallback() -> None:
    text = CRI_PREFLIGHT_SCRIPT.read_text(encoding="utf-8")
    assert "if command -v python3 >/dev/null 2>&1; then" in text
    assert 'python_bin="$(command -v python3)"' in text
    assert '"$python_bin" - "$info_tmp"' in text
    assert "containerd_socket_access.sh --grant" in text
    assert "CNI plugins detected on PATH but not at" in text


def test_cri_ci_setup_uses_supported_containerd_config_validation() -> None:
    text = (ROOT / "scripts" / "cri_ci_setup.sh").read_text(encoding="utf-8")
    assert "containerd --config /etc/containerd/config.toml config dump" in text


def test_cri_seed_lock_contains_core_and_edge_images() -> None:
    payload = json.loads(CRI_SEED_LOCK_FILE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert "seed_version" in payload
    assert "core" in payload["images"]
    assert "edge" in payload["images"]
    assert "docker.io/library/registry:2" in payload["images"]["core"]
    assert "docker.io/library/registry:2" in payload["images"]["edge"]
    assert "registry.k8s.io/pause:3.9" in payload["images"]["core"]
    assert "registry.k8s.io/pause:3.9" in payload["images"]["edge"]
    assert "quay.io/coreos/etcd:v3.5.13" in payload["images"]["core"]
    assert "docker.io/library/nats:2.10" in payload["images"]["edge"]
    assert "docker.io/library/demo-shell:latest" in payload["images"]["core"]
    assert "docker.io/library/demo-shell:latest" in payload["images"]["edge"]
    assert "localhost:5001/k1s-apishim:dev" in payload["images"]["core"]
    assert "docker.io/library/caddy:2.8" in payload["images"]["core"]


def test_cri_seed_lock_covers_default_ha_core_preload_images() -> None:
    payload = json.loads(CRI_SEED_LOCK_FILE.read_text(encoding="utf-8"))
    core_images = set(payload["images"]["core"])
    text = RUN_PROFILE_SCRIPT.read_text(encoding="utf-8")
    marker = 'elif [[ "${PROFILE:-}" == "k1s-ha-core" ]]; then'
    assert marker in text
    block = text.split(marker, 1)[1].split("else", 1)[0]
    raw_images = re.findall(r'"([^"]+)"', block)

    preload_images = []
    for raw in raw_images:
        match = re.fullmatch(r"\$\{[^:}]+:-(.+)\}", raw)
        preload_images.append(match.group(1) if match else raw)

    missing = sorted(set(preload_images) - core_images)
    assert not missing, (
        "core seed manifest is missing default HA preload images: "
        + ", ".join(missing)
    )


def test_smoke_v2_includes_seed_cache_phase_timeout() -> None:
    assert "seed_cache" in smoke_v2.DEFAULT_PHASE_TIMEOUTS


def test_smoke_v2_includes_ha_shared_infra_phase_timeout() -> None:
    assert "ha_shared_infra" in smoke_v2.DEFAULT_PHASE_TIMEOUTS


def test_smoke_v2_select_failure_detail_ignores_known_hosts_warning() -> None:
    stderr = "\n".join(
        [
            "Warning: Permanently added '192.168.155.10' (ED25519) to the list of known hosts.",
            "python: command not found",
        ]
    )
    assert smoke_v2.select_failure_detail(stderr, "") == "python: command not found"


def test_smoke_v2_detects_vm_managed_ha_infra() -> None:
    variant = json.loads(
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(VARIANT_SCRIPT),
                "--variant",
                str(HA_VARIANT_FILE),
                "--print-json",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
    )
    assert smoke_v2.uses_vm_managed_ha_infra(variant, ["ha_control_plane"]) is True


def test_smoke_v2_ha_env_sets_ha_defaults_and_ca_bundle(tmp_path: Path, monkeypatch) -> None:
    ca_bundle = tmp_path / "state" / "profiles" / "k1s-ha-core" / "apishim.ca.crt"
    ca_bundle.parent.mkdir(parents=True, exist_ok=True)
    ca_bundle.write_text("fake-ca", encoding="utf-8")
    monkeypatch.setattr(smoke_v2, "ROOT", tmp_path)

    env = smoke_v2._ha_env(
        {
            "core_nodes": [
                {
                    "name": "core-a",
                    "node_id": "core-a",
                    "controller_url": "http://192.168.155.10:9108",
                }
            ],
            "etcd_endpoints": [
                "http://192.168.155.10:2379",
                "http://192.168.155.11:2379",
                "http://192.168.155.12:2379",
            ],
            "etcd_prefix": "k1s/lab/ha-control-plane",
            "nats_url": "nats://hub-controller:dev@192.168.155.10:4222",
        }
    )

    assert env["AE_CONTROLLER_ID"] == "core-a"
    assert env["AE_CONTROLLER_ADVERTISE_ADDR"] == "http://192.168.155.10:9108"
    assert env["AE_HA_MODE"] == "1"
    assert env["AE_JS_REPLICAS"] == "3"
    assert env["AE_APISHIM_CA_BUNDLE"] == str(ca_bundle)


def test_run_ha_acceptance_checks_passes_ha_env_to_required_helpers(
    tmp_path: Path, monkeypatch
) -> None:
    ca_bundle = tmp_path / "state" / "profiles" / "k1s-ha-core" / "apishim.ca.crt"
    ca_bundle.parent.mkdir(parents=True, exist_ok=True)
    ca_bundle.write_text("fake-ca", encoding="utf-8")
    monkeypatch.setattr(smoke_v2, "ROOT", tmp_path)

    captured_env: dict[str, dict[str, str] | None] = {}

    def _fake_run_helper_check(
        name: str,
        command: list[str],
        *,
        timeout_s: int,
        optional: bool = False,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        _ = command, timeout_s, optional
        captured_env[name] = env
        return {
            "name": name,
            "status": "passed",
            "optional": optional,
            "detail": "ok",
            "command": command,
        }

    monkeypatch.setattr(smoke_v2, "_run_helper_check", _fake_run_helper_check)

    result = smoke_v2.run_ha_acceptance_checks(
        {
            "core_nodes": [
                {
                    "name": "core-a",
                    "node_id": "core-a",
                    "controller_url": "http://192.168.155.10:9108",
                    "apishim_url": "https://192.168.155.10:8445",
                }
            ],
            "controller_metrics_url": "http://192.168.155.10:9108/metrics",
            "etcd_endpoints": [
                "http://192.168.155.10:2379",
                "http://192.168.155.11:2379",
                "http://192.168.155.12:2379",
            ],
            "etcd_prefix": "k1s/lab/ha-control-plane",
            "nats_url": "nats://hub-controller:dev@192.168.155.10:4222",
            "hub_nodes": [{"name": "core-a", "monitor_url": "http://192.168.155.10:8222"}],
            "edge_core_sites": ["sea"],
            "edge_sites": [
                {
                    "site_id": "sea",
                    "monitor_url": "http://192.168.155.20:8223",
                    "expected_gateways": ["sea--sea-gw"],
                }
            ],
            "expected_version": "0.1.3.dev0",
        },
        timeout_s=30,
    )

    assert result["status"] == "passed"
    for name in [
        "ha_core_preflight",
        "ha_core_precheck",
        "ha_core_cluster_verify",
        "ha_hub_transport_precheck",
        "ha_edge_precheck:sea",
        "ha_edge_verify:sea",
    ]:
        env = captured_env[name]
        assert env is not None
        assert env["AE_HA_MODE"] == "1"
        assert env["AE_JS_REPLICAS"] == "3"
        assert env["AE_APISHIM_CA_BUNDLE"] == str(ca_bundle)


def test_run_ha_acceptance_checks_passes_ha_discovery_to_transport_drill(monkeypatch) -> None:
    captured_commands: dict[str, list[str]] = {}

    def _fake_run_helper_check(
        name: str,
        command: list[str],
        *,
        timeout_s: int,
        optional: bool = False,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        _ = timeout_s, optional, env
        captured_commands[name] = command
        return {
            "name": name,
            "status": "passed",
            "optional": optional,
            "detail": "ok",
            "command": command,
        }

    monkeypatch.setattr(smoke_v2, "_run_helper_check", _fake_run_helper_check)

    result = smoke_v2.run_ha_acceptance_checks(
        {
            "core_nodes": [
                {
                    "name": "core-a",
                    "node_id": "core-a",
                    "controller_url": "http://192.168.155.10:9108",
                    "apishim_url": "https://192.168.155.10:8445",
                }
            ],
            "controller_metrics_url": "http://192.168.155.10:9108/metrics",
            "etcd_endpoints": [
                "http://192.168.155.10:2379",
                "http://192.168.155.11:2379",
                "http://192.168.155.12:2379",
            ],
            "etcd_prefix": "k1s/lab/ha-control-plane",
            "nats_url": "nats://hub-controller:dev@192.168.155.10:4222",
            "hub_nodes": [{"name": "core-a", "monitor_url": "http://192.168.155.10:8222"}],
            "edge_core_sites": ["sea"],
            "edge_sites": [
                {
                    "site_id": "sea",
                    "monitor_url": "http://192.168.155.20:8223",
                    "expected_gateways": ["sea--sea-gw"],
                }
            ],
            "drills": {
                "transport_recovery_command": "./scripts/lab/vm/ha_drill_actions.sh transport-recovery --variant lab/variants/ha-control-plane-core-drills.yaml --site sea",
            },
            "expected_version": "0.1.3.dev0",
        },
        timeout_s=30,
    )

    assert result["status"] == "passed"
    command = captured_commands["ha_drill_transport_recovery"]
    assert "--etcd-endpoints" in command
    assert command[command.index("--etcd-endpoints") + 1] == (
        "http://192.168.155.10:2379,http://192.168.155.11:2379,http://192.168.155.12:2379"
    )
    assert "--etcd-prefix" in command
    assert command[command.index("--etcd-prefix") + 1] == "k1s/lab/ha-control-plane"


def test_run_ha_acceptance_checks_caps_internal_helper_timeouts(monkeypatch) -> None:
    captured_timeouts: dict[str, int] = {}

    def _fake_run_helper_check(
        name: str,
        command: list[str],
        *,
        timeout_s: int,
        optional: bool = False,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        _ = command, optional, env
        captured_timeouts[name] = timeout_s
        return {
            "name": name,
            "status": "passed",
            "optional": optional,
            "detail": "ok",
            "command": command,
        }

    monkeypatch.setattr(smoke_v2, "_run_helper_check", _fake_run_helper_check)

    result = smoke_v2.run_ha_acceptance_checks(
        {
            "core_nodes": [
                {
                    "name": "core-a",
                    "node_id": "core-a",
                    "ip": "192.168.155.10",
                    "controller_url": "http://192.168.155.10:9108",
                    "apishim_url": "https://192.168.155.10:8445",
                }
            ],
            "runtime_nodes": [
                {
                    "name": "attached-node-1",
                    "node_id": "attached-node-1",
                    "ip": "192.168.155.20",
                    "agent_url": "http://192.168.155.20:9111",
                    "labels": {"role": "worker", "site": "core"},
                }
            ],
            "edge_runtime_nodes": [
                {
                    "name": "edge-sea-node",
                    "node_id": "sea-node-1",
                    "ip": "192.168.155.21",
                    "site_id": "sea",
                    "agent_url": "http://192.168.155.21:9112",
                    "labels": {"role": "worker", "site": "sea"},
                }
            ],
            "controller_metrics_url": "http://192.168.155.10:9108/metrics",
            "etcd_endpoints": [
                "http://192.168.155.10:2379",
                "http://192.168.155.11:2379",
                "http://192.168.155.12:2379",
            ],
            "etcd_prefix": "k1s/lab/ha-control-plane",
            "nats_url": "nats://hub-controller:dev@192.168.155.10:4222",
            "hub_nodes": [{"name": "core-a", "monitor_url": "http://192.168.155.10:8222"}],
            "edge_core_sites": ["sea"],
            "edge_sites": [
                {
                    "site_id": "sea",
                    "monitor_url": "http://192.168.155.20:8223",
                    "expected_gateways": ["sea--sea-gw"],
                }
            ],
            "drills": {
                "leader_failover_command": "./scripts/lab/vm/ha_drill_actions.sh leader-failover",
                "etcd_restart_command": "./scripts/lab/vm/ha_drill_actions.sh etcd-restart",
                "transport_recovery_command": "./scripts/lab/vm/ha_drill_actions.sh transport-recovery --site sea",
            },
            "expected_version": "0.1.3.dev0",
        },
        timeout_s=900,
    )

    assert result["status"] == "passed"
    assert captured_timeouts["ha_core_preflight"] == 30
    assert captured_timeouts["ha_core_precheck"] == 60
    assert captured_timeouts["ha_core_cluster_verify"] == 60
    assert captured_timeouts["ha_hub_transport_precheck"] == 45
    assert captured_timeouts["ha_attached_node_precheck"] == 45
    assert captured_timeouts["ha_attached_node_ingress_smoke"] == 180
    assert captured_timeouts["ha_edge_precheck:sea"] == 45
    assert captured_timeouts["ha_edge_verify:sea"] == 45
    assert captured_timeouts["ha_edge_runtime_ingress_smoke:sea"] == 180
    assert captured_timeouts["ha_drill_leader_failover"] == 90
    assert captured_timeouts["ha_drill_etcd_restart"] == 90
    assert captured_timeouts["ha_drill_transport_recovery"] == 90


def test_run_ha_acceptance_checks_includes_core_node_smoke_when_runtime_node_present(
    monkeypatch,
) -> None:
    captured_commands: dict[str, list[str]] = {}

    def _fake_run_helper_check(
        name: str,
        command: list[str],
        *,
        timeout_s: int,
        optional: bool = False,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        _ = timeout_s, optional, env
        captured_commands[name] = command
        return {
            "name": name,
            "status": "passed",
            "optional": optional,
            "detail": "ok",
            "command": command,
        }

    monkeypatch.setattr(smoke_v2, "_run_helper_check", _fake_run_helper_check)

    result = smoke_v2.run_ha_acceptance_checks(
        {
            "core_nodes": [
                {
                    "name": "core-a",
                    "node_id": "core-a",
                    "controller_url": "http://192.168.155.10:9108",
                    "apishim_url": "https://192.168.155.10:8445",
                }
            ],
            "runtime_nodes": [
                {
                    "name": "attached-node-1",
                    "node_id": "attached-node-1",
                    "ip": "192.168.155.20",
                    "agent_url": "http://192.168.155.20:9111",
                    "labels": {"role": "worker", "site": "core"},
                }
            ],
            "controller_metrics_url": "http://192.168.155.10:9108/metrics",
            "etcd_endpoints": [
                "http://192.168.155.10:2379",
                "http://192.168.155.11:2379",
                "http://192.168.155.12:2379",
            ],
            "etcd_prefix": "k1s/lab/ha-control-plane",
            "nats_url": "nats://hub-controller:dev@192.168.155.10:4222",
            "hub_nodes": [{"name": "core-a", "monitor_url": "http://192.168.155.10:8222"}],
            "edge_core_sites": [],
            "edge_sites": [],
            "expected_version": "0.1.3.dev0",
        },
        timeout_s=30,
    )

    assert result["status"] == "passed"
    assert captured_commands["ha_attached_node_precheck"][:4] == [
        sys.executable,
        str(ROOT / "scripts" / "dev" / "ha_core_node_smoke.py"),
        "precheck",
        "--node-id",
    ]
    assert captured_commands["ha_attached_node_precheck"][4] == "attached-node-1"
    assert "--label" in captured_commands["ha_attached_node_precheck"]
    assert "role=worker" in captured_commands["ha_attached_node_precheck"]
    assert "site=core" in captured_commands["ha_attached_node_precheck"]
    assert captured_commands["ha_attached_node_ingress_smoke"][:3] == [
        sys.executable,
        str(ROOT / "scripts" / "dev" / "ha_core_node_smoke.py"),
        "ingress-smoke",
    ]
    assert "--manifest" in captured_commands["ha_attached_node_ingress_smoke"]
    assert (
        "ha-web-smoke.yaml"
        in captured_commands["ha_attached_node_ingress_smoke"][
            captured_commands["ha_attached_node_ingress_smoke"].index("--manifest") + 1
        ]
    )
    assert "--ingress-host" in captured_commands["ha_attached_node_ingress_smoke"]
    assert "ha-web-smoke.home.arpa" in captured_commands["ha_attached_node_ingress_smoke"]
    assert "--direct-probe-host" in captured_commands["ha_attached_node_ingress_smoke"]
    assert "192.168.155.10" in captured_commands["ha_attached_node_ingress_smoke"]
    assert (
        captured_commands["ha_attached_node_ingress_smoke"][
            captured_commands["ha_attached_node_ingress_smoke"].index("--timeout") + 1
        ]
        == "30"
    )
    assert (
        captured_commands["ha_attached_node_ingress_smoke"][
            captured_commands["ha_attached_node_ingress_smoke"].index("--direct-probe-timeout") + 1
        ]
        == "30"
    )
    assert (
        captured_commands["ha_attached_node_ingress_smoke"][
            captured_commands["ha_attached_node_ingress_smoke"].index("--ingress-timeout") + 1
        ]
        == "30"
    )


def test_run_ha_acceptance_checks_includes_edge_runtime_ingress_smoke_when_edge_worker_present(
    monkeypatch,
) -> None:
    captured_commands: dict[str, list[str]] = {}

    def _fake_run_helper_check(
        name: str,
        command: list[str],
        *,
        timeout_s: int,
        optional: bool = False,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        _ = timeout_s, optional, env
        captured_commands[name] = command
        return {
            "name": name,
            "status": "passed",
            "optional": optional,
            "detail": "ok",
            "command": command,
        }

    monkeypatch.setattr(smoke_v2, "_run_helper_check", _fake_run_helper_check)

    result = smoke_v2.run_ha_acceptance_checks(
        {
            "core_nodes": [
                {
                    "name": "core-a",
                    "node_id": "core-a",
                    "ip": "192.168.155.10",
                    "controller_url": "http://192.168.155.10:9108",
                    "apishim_url": "https://192.168.155.10:8445",
                }
            ],
            "runtime_nodes": [],
            "edge_runtime_nodes": [
                {
                    "name": "edge-sea-node",
                    "node_id": "sea-node-1",
                    "ip": "192.168.155.21",
                    "site_id": "sea",
                    "agent_url": "http://192.168.155.21:9112",
                    "labels": {"role": "worker", "site": "sea"},
                }
            ],
            "controller_metrics_url": "http://192.168.155.10:9108/metrics",
            "etcd_endpoints": [
                "http://192.168.155.10:2379",
                "http://192.168.155.11:2379",
                "http://192.168.155.12:2379",
            ],
            "etcd_prefix": "k1s/lab/ha-control-plane",
            "nats_url": "nats://hub-controller:dev@192.168.155.10:4222",
            "hub_nodes": [{"name": "core-a", "monitor_url": "http://192.168.155.10:8222"}],
            "edge_core_sites": ["sea"],
            "edge_sites": [
                {
                    "site_id": "sea",
                    "monitor_url": "http://192.168.155.20:8223",
                    "expected_gateways": ["sea--sea-gw"],
                }
            ],
            "expected_version": "0.1.3.dev0",
        },
        timeout_s=30,
    )

    assert result["status"] == "passed"
    command = captured_commands["ha_edge_runtime_ingress_smoke:sea"]
    assert command[:3] == [
        sys.executable,
        str(ROOT / "scripts" / "dev" / "ha_core_node_smoke.py"),
        "ingress-smoke",
    ]
    assert command[command.index("--node-id") + 1] == "sea-node-1"
    assert "role=worker" in command
    assert "site=sea" in command
    assert "ha-web-smoke-edge.yaml" in command[command.index("--manifest") + 1]
    assert command[command.index("--ingress-host") + 1] == "ha-edge-web-smoke.home.arpa"
    assert command[command.index("--target-probe-host") + 1] == "192.168.155.20"
    assert command[command.index("--target-probe-user") + 1] == "ae"
    assert command[command.index("--target-probe-url") + 1] == "http://192.168.155.21:18081/healthz"
    assert command[command.index("--target-probe-timeout") + 1] == "30"
    assert command[command.index("--ingress-timeout") + 1] == "30"


def test_run_ha_acceptance_checks_skips_optional_drills_after_required_failure(monkeypatch) -> None:
    executed_checks: list[str] = []

    def _fake_run_helper_check(
        name: str,
        command: list[str],
        *,
        timeout_s: int,
        optional: bool = False,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        _ = command, timeout_s, optional, env
        executed_checks.append(name)
        status = "failed" if name == "ha_edge_runtime_ingress_smoke:sea" else "passed"
        return {
            "name": name,
            "status": status,
            "optional": optional,
            "detail": "edge ingress timed out" if status == "failed" else "ok",
            "command": command,
        }

    monkeypatch.setattr(smoke_v2, "_run_helper_check", _fake_run_helper_check)

    result = smoke_v2.run_ha_acceptance_checks(
        {
            "core_nodes": [
                {
                    "name": "core-a",
                    "node_id": "core-a",
                    "ip": "192.168.155.10",
                    "controller_url": "http://192.168.155.10:9108",
                    "apishim_url": "https://192.168.155.10:8445",
                }
            ],
            "runtime_nodes": [],
            "edge_runtime_nodes": [
                {
                    "name": "edge-sea-node",
                    "node_id": "sea-node-1",
                    "ip": "192.168.155.21",
                    "site_id": "sea",
                    "agent_url": "http://192.168.155.21:9112",
                    "labels": {"role": "worker", "site": "sea"},
                }
            ],
            "controller_metrics_url": "http://192.168.155.10:9108/metrics",
            "etcd_endpoints": [
                "http://192.168.155.10:2379",
                "http://192.168.155.11:2379",
                "http://192.168.155.12:2379",
            ],
            "etcd_prefix": "k1s/lab/ha-control-plane",
            "nats_url": "nats://hub-controller:dev@192.168.155.10:4222",
            "hub_nodes": [{"name": "core-a", "monitor_url": "http://192.168.155.10:8222"}],
            "edge_core_sites": ["sea"],
            "edge_sites": [
                {
                    "site_id": "sea",
                    "monitor_url": "http://192.168.155.20:8223",
                    "expected_gateways": ["sea--sea-gw"],
                }
            ],
            "drills": {
                "leader_failover_command": "./scripts/lab/vm/ha_drill_actions.sh leader-failover",
                "etcd_restart_command": "./scripts/lab/vm/ha_drill_actions.sh etcd-restart",
                "transport_recovery_command": "./scripts/lab/vm/ha_drill_actions.sh transport-recovery --site sea",
            },
            "expected_version": "0.1.3.dev0",
        },
        timeout_s=900,
    )

    assert result["status"] == "failed"
    assert "ha_edge_runtime_ingress_smoke:sea" in result["detail"]
    assert "ha_drill_leader_failover" not in executed_checks
    assert "ha_drill_etcd_restart" not in executed_checks
    assert "ha_drill_transport_recovery" not in executed_checks
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["ha_drill_leader_failover"]["status"] == "skipped"
    assert checks["ha_drill_etcd_restart"]["status"] == "skipped"
    assert checks["ha_drill_transport_recovery"]["status"] == "skipped"
    assert checks["ha_drill_leader_failover"]["detail"] == "skipped after required HA failure"
    assert checks["ha_drill_etcd_restart"]["detail"] == "skipped after required HA failure"
    assert checks["ha_drill_transport_recovery"]["detail"] == "skipped after required HA failure"


def test_smoke_v2_skips_vm_managed_ha_infra_for_external_backends() -> None:
    variant = json.loads(
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(VARIANT_SCRIPT),
                "--variant",
                str(HA_VARIANT_FILE),
                "--print-json",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
    )
    variant["ha"]["etcd_endpoints"] = [
        "http://10.0.0.10:2379",
        "http://10.0.0.11:2379",
        "http://10.0.0.12:2379",
    ]
    assert smoke_v2.uses_vm_managed_ha_infra(variant, ["ha_control_plane"]) is False


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
