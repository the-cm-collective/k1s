from __future__ import annotations

import argparse
import subprocess
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


def _make_config(tmp_path: Path) -> host_a_netfs_lane.HostAConfig:
    return host_a_netfs_lane.HostAConfig(
        repo_root=tmp_path,
        env_file=tmp_path / "host-a-gpu.env",
        connection_uri="qemu:///system",
        domain_name="k1s-core-a-gpu",
        guest_user="ae",
        guest_repo="/home/ae/k1s",
        guest_key=tmp_path / "id_rsa",
        guest_port=22,
        node_id="core-a--hub",
        state_root=tmp_path / "state",
        overlay_dir=tmp_path / "VMs",
        base_image=tmp_path / "artifacts" / "images" / "ubuntu-22.04-k1s-gpu.qcow2",
        apishim_env=tmp_path / "apishim.env",
        controller_env=tmp_path / "controller.env",
        apishim_server="https://127.0.0.1:8445",
        lane_log_dir=tmp_path / "lane",
        nfs_export_root=tmp_path / "state" / "host-a-nfs-export",
        nfs_export_path=tmp_path / "state" / "host-a-nfs-export" / "netfs",
        nfs_container_name="ae-host-a-nfs",
        nfs_export_path_in_guest="/netfs",
        nfs_permitted=r"192.168.29.0\/24",
        smoke_script=tmp_path / "smoke.sh",
        labctl_script=tmp_path / "labctl.sh",
        gpu_validator_script=tmp_path / "gpu_validate.py",
        apishim_kubectl_script=tmp_path / "apishim_kubectl.sh",
    )


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

    assert "nohup sudo env -i " in command
    assert "sudo -E" not in command
    assert "HOME=/root" in command
    assert "LOGNAME=root" in command
    assert f"PATH={host_a_netfs_lane.shlex.quote(host_a_netfs_lane.ROOT_COMMAND_PATH)}" in command
    assert "USER=root" in command
    assert "AE_INFERENCE_EXPERIMENTAL=1" in command
    assert "AE_REMOTE_RUNTIME_ENSURE_TIMEOUT=180" in command
    assert "AE_STORAGE_NFS_SERVER=192.168.29.178" in command
    assert "AE_STORAGE_NFS_PATH=/netfs" in command
    assert f"AE_STORAGE_NFS_HOSTPATH={host_a_netfs_lane.shlex.quote(str(config.nfs_export_path))}" in command
    assert "bash ./scripts/dev/run_profile.sh k1s-core" in command


def test_build_guest_bootstrap_script_includes_expected_node_env(tmp_path: Path) -> None:
    args = host_a_netfs_lane.parse_args(["resume", "--env-file", str(tmp_path / "missing.env")])
    config = host_a_netfs_lane.load_config(args)

    script = host_a_netfs_lane.build_guest_bootstrap_script(
        config,
        "192.168.29.178",
        "192.168.29.104",
    )

    assert "run_apt_get update" in script
    assert "run_apt_get install -y nfs-common" in script
    assert "Could not get lock" in script
    assert "package_manager_busy()" in script
    assert "pgrep -x apt-get" in script
    assert "pgrep -x dpkg" in script
    assert "print_package_diagnostics" in script
    assert "systemctl --no-pager --full --lines=20 status apt-daily.service" in script
    assert "sudo fuser /var/lib/apt/lists/lock" in script
    assert "pgrep -f unattended-upgrade" not in script
    assert "pgrep -f unattended-upgrades" not in script
    assert "AE_ENABLE_NETFS=1" in script
    assert 'AE_NODE_ID=core-a--hub' in script
    assert 'AE_NODE_ADVERTISE_IP="${guest_ip}"' in script
    assert 'AE_AGENT_ENDPOINT="http://${guest_ip}:9111"' in script
    assert "make k1s-core-node > /home/ae/k1s-core-node.log" in script


def test_build_guest_presync_cleanup_script_targets_stale_inference_images() -> None:
    script = host_a_netfs_lane.build_guest_presync_cleanup_script()

    assert "rm -f /tmp/*-core-seed-cri-seed-images.oci.tar" in script
    assert "sudo ip link del ae0" in script
    assert 'sudo crictl ps -a -o json 2>/dev/null | python3 -c' in script
    assert 'sudo crictl pods -o json 2>/dev/null | python3 -c' in script
    assert 'labels.get("ae.app")' in script
    assert 'labels.get("app.kubernetes.io/managed-by")' in script
    assert 'sudo crictl stopp $managed_pod_ids' in script
    assert 'sudo crictl rmp $managed_pod_ids' in script
    assert 'sudo crictl ps -a --image "$image" -q' in script
    assert 'sudo crictl rm -f $ids' in script
    assert 'sudo ctr -n k8s.io images rm --sync "$image"' in script
    assert 'avail_kb="$(df -Pk / | awk \'NR==2 {print $4}\')"' in script
    assert 'sudo crictl ps -a -q 2>/dev/null || true' in script
    assert 'sudo ctr -n k8s.io images ls -q 2>/dev/null || true' in script
    assert "docker.io/vllm/vllm-openai:latest" in script
    assert "docker.io/vllm/vllm-openai:v0.6.2" in script
    assert "docker.io/rayproject/ray:latest" in script


def test_wait_for_guest_bootstrap_ready_checks_cloud_init_and_apt_idle(monkeypatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    captured: dict[str, str] = {}

    def fake_run_remote_script(_config, _guest_ip, script, *, dry_run):
        assert dry_run is False
        captured["script"] = script

    monkeypatch.setattr(host_a_netfs_lane, "run_remote_script", fake_run_remote_script)

    host_a_netfs_lane.wait_for_guest_bootstrap_ready(
        config,
        "192.168.29.105",
        timeout_s=180,
        dry_run=False,
    )

    script = captured["script"]
    assert "cloud-init status" in script
    assert "cloud-init status --long" in script
    assert "/var/lib/cloud/instance/boot-finished" in script
    assert 'status: error' in script
    assert "package_manager_busy()" in script
    assert "pgrep -x apt-get" in script
    assert "pgrep -x dpkg" in script
    assert "print_bootstrap_diagnostics" in script
    assert "systemctl is-system-running" in script
    assert "sudo fuser /var/lib/apt/lists/lock" in script
    assert "tail -n 40 /var/log/cloud-init.log /var/log/cloud-init-output.log /var/log/apt/term.log" in script
    assert "guest package manager still busy before bootstrap deadline" in script
    assert "pgrep -f unattended-upgrade" not in script
    assert "pgrep -f unattended-upgrades" not in script


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


def test_ssh_base_args_suppresses_known_host_noise(tmp_path: Path) -> None:
    args = host_a_netfs_lane.parse_args(
        [
            "resume",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--guest-key",
            str(tmp_path / "id_rsa"),
        ]
    )
    config = host_a_netfs_lane.load_config(args)

    ssh_args = host_a_netfs_lane.ssh_base_args(config)

    assert "StrictHostKeyChecking=no" in ssh_args
    assert "UserKnownHostsFile=/dev/null" in ssh_args
    assert "LogLevel=ERROR" in ssh_args


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


def test_wait_for_controller_shutdown_requires_lock_release_and_port_drain(monkeypatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    time_values = iter([0.0, 0.1, 2.1, 4.1])
    lock_states = iter([False, True])
    health_states = iter([True, False])
    postgres_states = iter([True, False])

    monkeypatch.setattr(host_a_netfs_lane.time, "monotonic", lambda: next(time_values))
    monkeypatch.setattr(host_a_netfs_lane.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(host_a_netfs_lane, "profile_lock_available", lambda _path: next(lock_states))
    monkeypatch.setattr(host_a_netfs_lane, "controller_healthy", lambda: next(health_states))
    monkeypatch.setattr(
        host_a_netfs_lane,
        "tcp_connectable",
        lambda host, port, timeout_s=1.0: next(postgres_states),
    )

    host_a_netfs_lane.wait_for_controller_shutdown(
        config,
        "192.168.29.178",
        timeout_s=10,
        dry_run=False,
    )


def test_wait_for_controller_health_requires_postgres_port(monkeypatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    time_values = iter([0.0, 0.1, 2.1, 4.1])
    postgres_states = iter([False, True])
    tcp_calls: list[tuple[str, int]] = []

    monkeypatch.setattr(host_a_netfs_lane.time, "monotonic", lambda: next(time_values))
    monkeypatch.setattr(host_a_netfs_lane.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(host_a_netfs_lane, "controller_start_failure", lambda _config: None)
    monkeypatch.setattr(host_a_netfs_lane, "http_json", lambda _url, headers=None: {"ok": True})

    def fake_tcp(host: str, port: int, *, timeout_s: float = 1.0) -> bool:
        tcp_calls.append((host, port))
        return next(postgres_states)

    monkeypatch.setattr(host_a_netfs_lane, "tcp_connectable", fake_tcp)

    host_a_netfs_lane.wait_for_controller_health(
        config,
        "192.168.29.178",
        timeout_s=10,
        dry_run=False,
    )

    assert tcp_calls == [("192.168.29.178", 55432), ("192.168.29.178", 55432)]


def test_do_resume_restart_controller_waits_for_shutdown_before_restart(monkeypatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    calls: list[str] = []

    def fake_run_cmd(cmd, **kwargs):
        if list(cmd) == ["bash", "-lc", "sudo -E make down || true"]:
            calls.append("down")
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(host_a_netfs_lane, "load_config", lambda _args: config)
    monkeypatch.setattr(host_a_netfs_lane, "require_cmd", lambda _name: None)
    monkeypatch.setattr(host_a_netfs_lane, "resolve_controller_host_ip", lambda _args: "192.168.29.178")
    monkeypatch.setattr(host_a_netfs_lane, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(host_a_netfs_lane, "start_vm", lambda *_args, **_kwargs: calls.append("start_vm"))
    monkeypatch.setattr(
        host_a_netfs_lane,
        "wait_for_guest_ip",
        lambda *_args, **_kwargs: calls.append("wait_guest_ip") or "192.168.29.105",
    )
    monkeypatch.setattr(host_a_netfs_lane, "ensure_nfs_export", lambda *_args, **_kwargs: calls.append("ensure_nfs"))
    monkeypatch.setattr(
        host_a_netfs_lane,
        "wait_for_controller_shutdown",
        lambda *_args, **_kwargs: calls.append("wait_shutdown"),
    )
    monkeypatch.setattr(host_a_netfs_lane, "start_controller", lambda *_args, **_kwargs: calls.append("start_controller"))
    monkeypatch.setattr(host_a_netfs_lane, "wait_for_controller_health", lambda *_args, **_kwargs: calls.append("wait_health"))

    rc = host_a_netfs_lane.do_resume(
        argparse.Namespace(
            env_file=tmp_path / "missing.env",
            guest_key=tmp_path / "id_rsa",
            guest_port=22,
            apishim_env=tmp_path / "apishim.env",
            controller_env=tmp_path / "controller.env",
            server="https://127.0.0.1:8445",
            restart_controller=True,
            skip_controller=False,
            skip_sync=True,
            skip_node=True,
            smoke=False,
            dry_run=False,
            guest_ip=None,
            guest_ip_timeout=150,
            controller_health_timeout=30,
            node_ready_timeout=30,
            overlay_timeout=30,
            controller_host_ip=None,
        )
    )

    assert rc == 0
    assert calls == [
        "down",
        "wait_shutdown",
        "start_vm",
        "wait_guest_ip",
        "ensure_nfs",
        "start_controller",
        "wait_health",
    ]


def test_do_resume_runs_presync_cleanup_before_rsync(monkeypatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(host_a_netfs_lane, "load_config", lambda _args: config)
    monkeypatch.setattr(host_a_netfs_lane, "ensure_lane_dirs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_a_netfs_lane, "require_cmd", lambda _name: None)
    monkeypatch.setattr(host_a_netfs_lane, "resolve_controller_host_ip", lambda _args: "192.168.29.178")
    monkeypatch.setattr(host_a_netfs_lane, "start_vm", lambda *_args, **_kwargs: calls.append("start_vm"))
    monkeypatch.setattr(
        host_a_netfs_lane,
        "wait_for_guest_ip",
        lambda *_args, **_kwargs: calls.append("wait_guest_ip") or "192.168.29.105",
    )
    monkeypatch.setattr(host_a_netfs_lane, "ensure_nfs_export", lambda *_args, **_kwargs: calls.append("ensure_nfs"))
    monkeypatch.setattr(host_a_netfs_lane, "controller_healthy", lambda: True)
    monkeypatch.setattr(host_a_netfs_lane, "wait_for_controller_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        host_a_netfs_lane,
        "presync_guest_cleanup",
        lambda *_args, **_kwargs: calls.append("presync_cleanup"),
    )
    monkeypatch.setattr(
        host_a_netfs_lane,
        "sync_repo",
        lambda *_args, **_kwargs: calls.append("sync_repo"),
    )

    rc = host_a_netfs_lane.do_resume(
        argparse.Namespace(
            env_file=tmp_path / "missing.env",
            guest_key=tmp_path / "id_rsa",
            guest_port=22,
            apishim_env=tmp_path / "apishim.env",
            controller_env=tmp_path / "controller.env",
            server="https://127.0.0.1:8445",
            restart_controller=False,
            skip_controller=False,
            skip_sync=False,
            skip_node=True,
            smoke=False,
            dry_run=False,
            guest_ip=None,
            guest_ip_timeout=150,
            controller_health_timeout=30,
            node_ready_timeout=30,
            overlay_timeout=30,
            controller_host_ip=None,
        )
    )

    assert rc == 0
    assert calls == [
        "start_vm",
        "wait_guest_ip",
        "ensure_nfs",
        "presync_cleanup",
        "sync_repo",
    ]


def test_do_rebuild_waits_for_guest_bootstrap_before_start_guest_node(monkeypatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    calls: list[str] = []
    timeouts: list[int] = []

    monkeypatch.setattr(host_a_netfs_lane, "load_config", lambda _args: config)
    monkeypatch.setattr(host_a_netfs_lane, "ensure_lane_dirs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_a_netfs_lane, "require_cmd", lambda _name: None)
    monkeypatch.setattr(host_a_netfs_lane, "resolve_controller_host_ip", lambda _args: "192.168.29.178")
    monkeypatch.setattr(
        host_a_netfs_lane,
        "rebuild_guest",
        lambda *_args, **_kwargs: calls.append("rebuild_guest") or "192.168.29.100",
    )
    monkeypatch.setattr(host_a_netfs_lane, "ensure_nfs_export", lambda *_args, **_kwargs: calls.append("ensure_nfs"))
    monkeypatch.setattr(host_a_netfs_lane, "start_controller", lambda *_args, **_kwargs: calls.append("start_controller"))
    monkeypatch.setattr(host_a_netfs_lane, "wait_for_controller_health", lambda *_args, **_kwargs: calls.append("wait_health"))
    monkeypatch.setattr(
        host_a_netfs_lane,
        "verify_controller_from_guest",
        lambda *_args, **_kwargs: calls.append("verify_controller"),
    )
    monkeypatch.setattr(
        host_a_netfs_lane,
        "wait_for_guest_bootstrap_ready",
        lambda *_args, **_kwargs: timeouts.append(int(_kwargs["timeout_s"])) or calls.append("wait_guest_bootstrap"),
    )
    monkeypatch.setattr(host_a_netfs_lane, "start_guest_node", lambda *_args, **_kwargs: calls.append("start_guest_node"))
    monkeypatch.setattr(host_a_netfs_lane, "wait_for_guest_node", lambda *_args, **_kwargs: calls.append("wait_guest_node"))

    rc = host_a_netfs_lane.do_rebuild(
        argparse.Namespace(
            env_file=tmp_path / "missing.env",
            guest_key=tmp_path / "id_rsa",
            guest_port=22,
            apishim_env=tmp_path / "apishim.env",
            controller_env=tmp_path / "controller.env",
            server="https://127.0.0.1:8445",
            guest_ip=None,
            guest_ip_timeout=150,
            controller_health_timeout=30,
            node_ready_timeout=120,
            overlay_timeout=30,
            controller_host_ip=None,
            skip_sync=True,
            skip_gpu_validate=True,
            skip_controller=False,
            skip_node=False,
            smoke=False,
            dry_run=False,
        )
    )

    assert rc == 0
    assert calls == [
        "rebuild_guest",
        "ensure_nfs",
        "start_controller",
        "wait_health",
        "verify_controller",
        "wait_guest_bootstrap",
        "start_guest_node",
        "wait_guest_node",
    ]
    assert timeouts == [600]


def test_do_down_treats_already_stopped_vm_as_success(monkeypatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        if calls[-1][-2:] == ["host-a-gpu", "stop"] or "stop" in calls[-1]:
            return subprocess.CompletedProcess(
                list(cmd),
                1,
                "",
                (
                    "virsh shutdown failed: virsh -c qemu:///system shutdown k1s-core-a-gpu\n"
                    "error: Requested operation is not valid: domain is not running\n"
                ),
            )
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(host_a_netfs_lane, "load_config", lambda _args: config)
    monkeypatch.setattr(host_a_netfs_lane, "run_cmd", fake_run_cmd)

    rc = host_a_netfs_lane.do_down(
        argparse.Namespace(
            env_file=tmp_path / "host-a-gpu.env",
            force_vm=False,
            purge_artifacts=False,
            dry_run=False,
        )
    )

    assert rc == 0
    assert any("make down || true" in " ".join(call) for call in calls)
    assert any(call[-2:] == ["host-a-gpu", "stop"] for call in calls)
