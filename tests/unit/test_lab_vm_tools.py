from __future__ import annotations

# ruff: noqa: S603
import json
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
IMAGE_BUILD_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_build.sh"
IMAGE_VERIFY_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_verify.sh"
RUN_PROFILE_SCRIPT = ROOT / "scripts" / "dev" / "run_profile.sh"
HA_CLOSEOUT_E2E_SCRIPT = ROOT / "scripts" / "dev" / "ha_closeout_e2e.sh"
CRI_IMAGE_MIRROR_SCRIPT = ROOT / "scripts" / "dev" / "cri_image_mirror.sh"
CRI_SEED_BUNDLE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_seed_bundle.sh"
HA_DASHBOARD_SMOKE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "ha_dashboard_smoke.sh"
HOST_PREPARE_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "host_prepare.sh"
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
HA_HUB_NODE_VARIANT_FILE = ROOT / "lab" / "variants" / "ha-control-plane-hub-node.yaml"
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


def test_checked_in_ha_variant_normalizes_for_closeout_lane() -> None:
    res = subprocess.run(  # noqa: S603
        [sys.executable, str(VARIANT_SCRIPT), "--variant", str(HA_VARIANT_FILE), "--print-json"],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(res.stdout)
    assert payload["name"] == "ha-control-plane-core"
    assert [host["role"] for host in payload["hosts"][:3]] == [
        "k1s-ha-core",
        "k1s-ha-core",
        "k1s-ha-core",
    ]
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
    assert payload["smoke"]["lanes"] == ["ha_control_plane"]


def test_checked_in_ha_hub_node_variant_normalizes_for_manual_smoke_lane() -> None:
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(VARIANT_SCRIPT),
            "--variant",
            str(HA_HUB_NODE_VARIANT_FILE),
            "--print-json",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(res.stdout)
    assert payload["name"] == "ha-control-plane-hub-node"
    assert [host["role"] for host in payload["hosts"][:3]] == [
        "k1s-ha-core",
        "k1s-ha-core",
        "k1s-ha-core",
    ]
    assert payload["hosts"][3]["role"] == "k1s-core-node"
    assert payload["hosts"][3]["node_id"] == "hub-1"
    assert payload["hosts"][3]["node_labels"] == "role=hub,site=hub"
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


def test_ha_drill_actions_require_guest_prereqs() -> None:
    text = HA_DRILL_ACTIONS_SCRIPT.read_text(encoding="utf-8")
    assert "source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh" in text
    assert "ensure_vm_bootstrap_prereqs" in text


def test_ha_drill_actions_restart_processes_without_profile_reentry() -> None:
    text = HA_DRILL_ACTIONS_SCRIPT.read_text(encoding="utf-8")
    assert "python3 -m ae.controller --loop --metrics-port" in text
    assert "python3 -m ae.gateway" in text
    assert "wait_for_local_tcp_port" in text
    assert "wait_for_local_process" in text
    assert "wait_for_local_etcd_health" in text
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


def test_variant_down_uses_run_inventory_fallback() -> None:
    text = VARIANT_DOWN_SCRIPT.read_text(encoding="utf-8")
    assert "[--purge] [--destroy-network] [--best-effort]" in text
    assert 'run_inventory="$(run_dir "$RUN_ID")/qemu_inventory.json"' in text
    assert 'log "using run inventory fallback for run_id=${RUN_ID}: $inventory"' in text
    assert 'log "continuing with best-effort cleanup derived from variant topology"' in text
    assert 'tap="k1s${i}"' in text
    assert 'pid_file="$state_dir/pids/${name}.pid"' in text
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
    assert "AE_CRI_CACHE_SEED_MODE" in text
    assert "AE_CRI_CACHE_SEED_BUNDLE" in text
    assert text.count("REGISTER_ONLY=1 SITE_ID") == 1

    run_profile_text = RUN_PROFILE_SCRIPT.read_text(encoding="utf-8")
    assert "STRICT_CRI_OWNERSHIP_HELPER_ARGS=()" in run_profile_text
    assert "strict_cri_explicit_target_configured()" in run_profile_text
    assert "AE_STRICT_CRI_TARGET_UID and AE_STRICT_CRI_TARGET_GID must be set together." in run_profile_text
    assert 'STRICT_CRI_OWNERSHIP_HELPER_ARGS=(--target-uid "$target_uid" --target-gid "$target_gid")' in run_profile_text


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
    assert "source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh" in text
    assert "ensure_vm_bootstrap_prereqs" in text
    assert "python3 /mnt/host/scripts/dev/cri_stack.py up-etcd \\" in text
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
    assert "/etc/crictl.yaml" in text
    assert "/opt/cni/bin" in text
    assert "10-k1s-bridge.conflist" in text
    assert "systemctl enable containerd" in text
    assert "systemctl restart containerd" in text
    assert "systemctl enable containerd qemu-guest-agent" not in text
    assert "systemctl restart containerd qemu-guest-agent" not in text
    assert '"vm_bootstrap_ready": true' in text
    assert '"python_alias": true' in text
    assert '"crictl_ready": true' in text
    assert '"cni_ready": true' in text


def test_image_build_writes_vm_bootstrap_metadata_flags() -> None:
    text = IMAGE_BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'build_dir="$OUTPUT_DIR/build-${variant}"' in text
    assert 'rm -rf "$build_dir"' in text
    assert "vm_bootstrap_ready:true" in text
    assert "python_alias:true" in text
    assert "crictl_ready:true" in text
    assert "cni_ready:true" in text


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
        [str(IMAGE_VERIFY_SCRIPT), "--image-dir", str(image_dir), "--variant", "base"],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr


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
        "  --variant lab/variants/ha-control-plane-hub-node.yaml \\\n"
        "  --apply"
    ) in bring_up
    assert (
        "scripts/lab/vm/labctl.sh host prepare \\\n"
        "  --variant lab/variants/ha-control-plane-hub-node.yaml \\\n"
        "  --apply"
    ) in runbook
    assert "lab/variants/ha-control-plane-hub-node.yaml" in bring_up
    assert "lab/variants/ha-control-plane-hub-node.yaml" in runbook
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


def test_retained_ha_dashboard_docs_use_make_helper_targets() -> None:
    bring_up = HA_BRING_UP_DOC.read_text(encoding="utf-8")
    runbook = VM_VARIANT_RUNBOOK_DOC.read_text(encoding="utf-8")
    ops = OPS_RUNBOOK_DOC.read_text(encoding="utf-8")
    for text in (bring_up, runbook, ops):
        assert "make lab-vm-ha-dashboard-up" in text
        assert "make lab-vm-ha-dashboard-status" in text
        assert "make lab-vm-ha-dashboard-refresh-all" in text
        assert "make lab-vm-ha-dashboard-down" in text
        assert "make lab-vm-ha-dashboard-purge" in text
        assert "make lab-vm-ha-dashboard-reset" in text
    assert 'LAB_VM_HA_DASHBOARD_ARGS="--purge"' not in bring_up
    assert 'LAB_VM_HA_DASHBOARD_ARGS="--purge"' not in runbook
    assert 'LAB_VM_HA_DASHBOARD_ARGS="--purge"' not in ops
    assert 'LAB_VM_HA_DASHBOARD_ARGS="--target all"' in bring_up
    assert 'LAB_VM_HA_DASHBOARD_ARGS="--rebuild-images --destroy-network"' in runbook
    assert 'retained-VM "rebuild and restart all" path' in ops


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


def test_make_lab_vm_ha_dashboard_targets_use_helper_wrapper() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    for target in (
        "lab-vm-ha-dashboard-up:",
        "lab-vm-ha-dashboard-status:",
        "lab-vm-ha-dashboard-down:",
        "lab-vm-ha-dashboard-purge:",
        "lab-vm-ha-dashboard-reseed-core:",
        "lab-vm-ha-dashboard-restart-core:",
        "lab-vm-ha-dashboard-restart-apishim:",
        "lab-vm-ha-dashboard-restart-hub-node:",
        "lab-vm-ha-dashboard-refresh-all:",
        "lab-vm-ha-dashboard-reset:",
    ):
        assert target in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh up" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh status" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh down" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh purge" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh reseed-core" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh restart-core" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh restart-apishim" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh restart-hub-node" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh refresh-all" in text
    assert "./scripts/lab/vm/ha_dashboard_smoke.sh reset" in text
    assert "RUN_ID=$${RUN_ID:-ha-dashboard-local}" in text
    assert "VARIANT=$${VARIANT:-lab/variants/ha-control-plane-hub-node.yaml}" in text
    assert "$${LAB_VM_HA_DASHBOARD_ARGS:-}" in text


def test_ha_dashboard_smoke_helper_wires_retained_refresh_and_reset_paths() -> None:
    text = HA_DASHBOARD_SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert 'DEFAULT_VARIANT="$ROOT_DIR/lab/variants/ha-control-plane-hub-node.yaml"' in text
    assert 'DEFAULT_RUN_ID="ha-dashboard-local"' in text
    assert 'source "$ROOT_DIR/scripts/lib/nixos_bridge.sh"' in text
    assert "reseed-core" in text
    assert "restart-core" in text
    assert "restart-apishim" in text
    assert "restart-hub-node" in text
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
    assert 'purge_retained_artifacts\n  cmd_up' in text
    assert 'rm -rf "$run_path"' in text
    assert "localhost:5001/k1s-apishim:dev" in text
    assert "docker.io/library/demo-shell:latest" in text
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
    assert 'sudo tail -n 80 /home/ae/k1s-ha-core.log' in text
    assert 'sudo crictl ps -a --name k1s-core-apishim -q' in text
    assert 'focus="${3:-controller}"' in text
    assert 'print_remote_ha_failure_context "$name" "$ip" controller' in text
    assert 'print_remote_ha_failure_context "$name" "$ip" apishim' in text


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
    assert "$ROOT_DIR/.venv/bin/python" in text
    assert "nix eval --raw nixpkgs#stdenv.cc.cc.lib.outPath" in text
    assert 'export LD_LIBRARY_PATH="$cc_lib/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"' in text
    assert "import grpc" in text
    assert "AE_E2E_HA_CLOSEOUT=1" in text
    assert "-m pytest -q tests/integration/test_ha_closeout_e2e.py" in text


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
    assert "quay.io/coreos/etcd:v3.5.13" in payload["images"]["core"]
    assert "docker.io/library/nats:2.10" in payload["images"]["edge"]
    assert "docker.io/library/demo-shell:latest" in payload["images"]["core"]
    assert "localhost:5001/k1s-apishim:dev" in payload["images"]["core"]


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
                    "name": "hub-1",
                    "node_id": "hub-1",
                    "ip": "192.168.155.20",
                    "agent_url": "http://192.168.155.20:9111",
                    "labels": {"role": "hub", "site": "hub"},
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
    assert captured_commands["ha_core_node_precheck"][:4] == [
        sys.executable,
        str(ROOT / "scripts" / "dev" / "ha_core_node_smoke.py"),
        "precheck",
        "--node-id",
    ]
    assert captured_commands["ha_core_node_precheck"][4] == "hub-1"
    assert "--label" in captured_commands["ha_core_node_precheck"]
    assert "role=hub" in captured_commands["ha_core_node_precheck"]
    assert "site=hub" in captured_commands["ha_core_node_precheck"]
    assert captured_commands["ha_core_node_workload_smoke"][:3] == [
        sys.executable,
        str(ROOT / "scripts" / "dev" / "ha_core_node_smoke.py"),
        "workload-smoke",
    ]
    assert "--manifest" in captured_commands["ha_core_node_workload_smoke"]
    assert (
        "shell-demo-node-hub.yaml"
        in captured_commands["ha_core_node_workload_smoke"][
            captured_commands["ha_core_node_workload_smoke"].index("--manifest") + 1
        ]
    )


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
