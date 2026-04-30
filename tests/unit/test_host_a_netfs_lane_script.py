from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "host_a_netfs_lane.py"

_SPEC = spec_from_file_location("host_a_netfs_lane_script", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
host_a_netfs_lane = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = host_a_netfs_lane
_SPEC.loader.exec_module(host_a_netfs_lane)


def test_parse_args_resume_defaults() -> None:
    args = host_a_netfs_lane.parse_args(["resume"])

    assert args.cmd == "resume"
    assert args.restart_controller is False
    assert args.smoke is False
    assert args.skip_controller is False
    assert args.skip_node is False
    assert args.guest_port == 22
    assert args.guest_ip_timeout == 150


def test_parse_args_rebuild_smoke_and_dry_run() -> None:
    args = host_a_netfs_lane.parse_args(["rebuild", "--smoke", "--dry-run", "--skip-gpu-validate"])

    assert args.cmd == "rebuild"
    assert args.smoke is True
    assert args.dry_run is True
    assert args.skip_gpu_validate is True


def test_load_simple_env_file_reads_host_a_values(tmp_path: Path) -> None:
    env_file = tmp_path / "host-a-gpu.env"
    env_file.write_text(
        "\n".join(
            [
                "HOST_A_GPU_DOMAIN_NAME=k1s-core-a-gpu",
                "HOST_A_GPU_GUEST_REPO=/home/ae/k1s",
                "HOST_A_GPU_OVERLAY_DIR=~/VMs",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    values = host_a_netfs_lane.load_simple_env_file(env_file)

    assert values["HOST_A_GPU_DOMAIN_NAME"] == "k1s-core-a-gpu"
    assert values["HOST_A_GPU_GUEST_REPO"] == "/home/ae/k1s"
    assert values["HOST_A_GPU_OVERLAY_DIR"] == "~/VMs"


def test_build_controller_start_command_includes_nfs_env(tmp_path: Path) -> None:
    args = host_a_netfs_lane.parse_args(["resume", "--env-file", str(tmp_path / "missing.env")])
    config = host_a_netfs_lane.load_config(args)

    command = host_a_netfs_lane.build_controller_start_command(config, "192.168.29.178")

    assert "AE_STORAGE_NFS_SERVER=192.168.29.178" in command
    assert "AE_STORAGE_NFS_PATH=/netfs" in command
    assert f"AE_STORAGE_NFS_HOSTPATH={host_a_netfs_lane.shlex.quote(str(config.nfs_export_path))}" in command
    assert "make k1s-core-cri" in command


def test_build_guest_bootstrap_script_includes_expected_node_env(tmp_path: Path) -> None:
    args = host_a_netfs_lane.parse_args(["resume", "--env-file", str(tmp_path / "missing.env")])
    config = host_a_netfs_lane.load_config(args)

    script = host_a_netfs_lane.build_guest_bootstrap_script(
        config,
        "192.168.29.178",
        "192.168.29.104",
    )

    assert "sudo apt-get install -y nfs-common" in script
    assert "AE_ENABLE_NETFS=1" in script
    assert 'AE_NODE_ID=core-a--hub' in script
    assert 'AE_AGENT_ENDPOINT="http://${guest_ip}:9111"' in script
    assert "make k1s-core-node > /home/ae/k1s-core-node.log" in script


def test_build_smoke_command_targets_guest_ip(tmp_path: Path) -> None:
    args = host_a_netfs_lane.parse_args(
        [
            "resume",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--guest-key",
            str(tmp_path / "id_rsa"),
            "--server",
            "https://127.0.0.1:8445",
        ]
    )
    config = host_a_netfs_lane.load_config(args)

    cmd = host_a_netfs_lane.build_smoke_command(config, "192.168.29.104")

    assert cmd[:2] == ["bash", str(config.smoke_script)]
    assert "--guest-ip" in cmd
    assert "192.168.29.104" in cmd
    assert "--server" in cmd
    assert "https://127.0.0.1:8445" in cmd


def test_rebuild_paths_follow_host_a_defaults(tmp_path: Path) -> None:
    env_file = tmp_path / "host-a-gpu.env"
    env_file.write_text(
        "\n".join(
            [
                "HOST_A_GPU_DOMAIN_NAME=k1s-core-a-gpu",
                f"HOST_A_GPU_STATE_ROOT={tmp_path / 'state' / 'libvirt-host-a'}",
                f"HOST_A_GPU_OVERLAY_DIR={tmp_path / 'VMs'}",
                f"HOST_A_GPU_BASE_IMAGE={tmp_path / 'artifacts' / 'images' / 'ubuntu-22.04-k1s-gpu.qcow2'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    args = host_a_netfs_lane.parse_args(["rebuild", "--env-file", str(env_file)])

    config = host_a_netfs_lane.load_config(args)

    assert config.state_dir == tmp_path / "state" / "libvirt-host-a" / "k1s-core-a-gpu"
    assert config.overlay_path == tmp_path / "VMs" / "k1s-core-a-gpu.qcow2"
    assert config.seed_path == tmp_path / "VMs" / "k1s-core-a-gpu-seed.iso"
    assert config.base_image_sha == tmp_path / "artifacts" / "images" / "ubuntu-22.04-k1s-gpu.qcow2.sha256"
