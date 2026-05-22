from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "lab" / "vm" / "host_a_gpu_guest.py"
LABCTL = ROOT / "scripts" / "lab" / "vm" / "labctl.sh"
ENV_SAMPLE = ROOT / "ops" / "dev" / "host-a-gpu.env.sample"

_SPEC = spec_from_file_location("host_a_gpu_guest_script", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
host_a_gpu_guest = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = host_a_gpu_guest
_SPEC.loader.exec_module(host_a_gpu_guest)


@pytest.fixture(autouse=True)
def _isolate_default_local_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        host_a_gpu_guest,
        "DEFAULT_LOCAL_ENV_FILE",
        tmp_path / "missing-host-a-gpu.env",
    )


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[:3] == ["qemu-img", "info", "--output=json"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"virtual-size": 64 * 1024**3}),
                "",
            )
        if cmd[:2] == ["qemu-img", "create"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")


def _write_env(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _parse_args(args: list[str]) -> object:
    return host_a_gpu_guest.build_parser().parse_args(args)


def _make_base_paths(tmp_path: Path) -> dict[str, Path]:
    base_image = tmp_path / "artifacts" / "images" / "ubuntu-22.04-k1s-gpu.qcow2"
    base_image.parent.mkdir(parents=True, exist_ok=True)
    base_image.write_text("base", encoding="utf-8")
    ovmf_code = tmp_path / "OVMF_CODE.fd"
    ovmf_vars = tmp_path / "OVMF_VARS.fd"
    ovmf_code.write_text("code", encoding="utf-8")
    ovmf_vars.write_text("vars", encoding="utf-8")
    ssh_pub = tmp_path / "id_ed25519.pub"
    ssh_pub.write_text("ssh-ed25519 AAAATEST unit@test\n", encoding="utf-8")
    return {
        "base_image": base_image,
        "ovmf_code": ovmf_code,
        "ovmf_vars_template": ovmf_vars,
        "ssh_public_key_file": ssh_pub,
        "overlay_dir": tmp_path / "VMs",
        "state_root": tmp_path / "state" / "libvirt-host-a",
    }


def _make_config(tmp_path: Path, **overrides: object) -> host_a_gpu_guest.GuestConfig:
    paths = _make_base_paths(tmp_path)
    values: dict[str, object] = {
        "env_file_path": None,
        "connection_uri": "qemu:///system",
        "domain_name": "k1s-core-a-gpu",
        "guest_hostname": "k1s-core-a-gpu",
        "node_id": "core-a--hub",
        "guest_user": "ae",
        "guest_repo": "/mnt/host",
        "machine": "pc-q35-5.1",
        "memory_mib": 16 * 1024,
        "hard_limit_mib": 18 * 1024,
        "vcpus": 6,
        "cpu_sockets": 1,
        "cpu_cores": 3,
        "cpu_threads": 2,
        "iothreads": 1,
        "hugepages_kib": 2048,
        "overlay_size_gib": 80,
        "overlay_dir": paths["overlay_dir"],
        "base_image": paths["base_image"],
        "state_root": paths["state_root"],
        "ssh_public_key_file": paths["ssh_public_key_file"],
        "ssh_public_key": "ssh-ed25519 AAAATEST unit@test",
        "ovmf_code": paths["ovmf_code"],
        "ovmf_vars_template": paths["ovmf_vars_template"],
        "mgmt_network": "default",
        "mgmt_mac": "52:54:00:aa:bb:cc",
        "primary_nic_name": None,
        "primary_nic_bdf": "0000:05:00.0",
        "primary_nic_mac": "10:20:30:40:50:60",
        "gpu_bdf": "0000:65:00.0",
        "gpu_audio_bdf": "0000:65:00.1",
        "expected_primary_iommu_group": None,
    }
    values.update(overrides)
    return host_a_gpu_guest.GuestConfig(**values)


def _hostdev_bdfs(root: ET.Element) -> list[str]:
    values: list[str] = []
    for address in root.findall("./devices/hostdev/source/address"):
        domain = int(address.attrib["domain"], 16)
        bus = int(address.attrib["bus"], 16)
        slot = int(address.attrib["slot"], 16)
        function = int(address.attrib["function"], 16)
        values.append(f"{domain:04x}:{bus:02x}:{slot:02x}.{function:x}")
    return values


def _with_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key in list(os.environ):
        if key.startswith("HOST_A_GPU_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _set_sysfs_for_iface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sys_class_net = tmp_path / "sys" / "class" / "net"
    sys_bus_pci = tmp_path / "sys" / "bus" / "pci" / "devices"
    iface_dir = sys_class_net / "eno42"
    device_dir = sys_bus_pci / "0000:05:00.0"
    iface_dir.mkdir(parents=True, exist_ok=True)
    device_dir.mkdir(parents=True, exist_ok=True)
    (iface_dir / "address").write_text("10:20:30:40:50:60\n", encoding="utf-8")
    (device_dir / "net").mkdir(parents=True, exist_ok=True)
    (device_dir / "net" / "eno42").mkdir(parents=True, exist_ok=True)
    (device_dir / "net" / "eno42" / "address").write_text("10:20:30:40:50:60\n", encoding="utf-8")
    os.symlink(device_dir, iface_dir / "device")
    monkeypatch.setattr(host_a_gpu_guest, "SYS_CLASS_NET", sys_class_net)
    monkeypatch.setattr(host_a_gpu_guest, "SYS_BUS_PCI_DEVICES", sys_bus_pci)
    return iface_dir


def test_render_domain_xml_emits_required_profile_and_hostdevs(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    xml_text = host_a_gpu_guest.render_domain_xml(config)
    root = ET.fromstring(xml_text)

    os_type = root.find("./os/type")
    assert os_type is not None
    assert os_type.text == "hvm"
    assert os_type.attrib["machine"] == "pc-q35-5.1"

    cpu = root.find("./cpu")
    assert cpu is not None
    assert cpu.attrib["mode"] == "host-passthrough"
    topology = root.find("./cpu/topology")
    assert topology is not None
    assert topology.attrib == {"sockets": "1", "cores": "3", "threads": "2"}

    hard_limit = root.find("./memtune/hard_limit")
    assert hard_limit is not None
    assert hard_limit.text == str(18 * 1024)
    assert hard_limit.attrib["unit"] == "MiB"

    hugepage = root.find("./memoryBacking/hugepages/page")
    assert hugepage is not None
    assert hugepage.attrib == {"size": "2048", "unit": "KiB"}
    assert root.find("./memoryBacking/locked") is not None
    assert root.find("./memoryBacking/nosharepages") is not None
    allocation = root.find("./memoryBacking/allocation")
    assert allocation is not None
    assert allocation.attrib["mode"] == "immediate"

    disk_driver = root.find("./devices/disk[@device='disk']/driver")
    assert disk_driver is not None
    assert disk_driver.attrib["cache"] == "none"
    assert disk_driver.attrib["io"] == "native"
    assert disk_driver.attrib["iothread"] == "1"

    mgmt_interface = root.find("./devices/interface[@type='network']")
    assert mgmt_interface is not None
    mgmt_source = mgmt_interface.find("./source")
    assert mgmt_source is not None
    assert mgmt_source.attrib["network"] == "default"
    mgmt_model = mgmt_interface.find("./model")
    assert mgmt_model is not None
    assert mgmt_model.attrib["type"] == "virtio"

    assert sorted(_hostdev_bdfs(root)) == [
        "0000:05:00.0",
        "0000:65:00.0",
        "0000:65:00.1",
    ]

    qga = root.find("./devices/channel/target")
    assert qga is not None
    assert qga.attrib["name"] == "org.qemu.guest_agent.0"
    memballoon = root.find("./devices/memballoon")
    assert memballoon is not None
    assert memballoon.attrib["model"] == "none"


def test_render_domain_xml_excludes_windows_only_and_custom_audio_bits(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    xml_text = host_a_gpu_guest.render_domain_xml(config).lower()
    root = ET.fromstring(xml_text)
    assert "hyperv" not in xml_text
    assert "vendor_id" not in xml_text
    assert "hidden" not in xml_text
    assert "pulseaudio" not in xml_text
    assert "<sound" not in xml_text
    assert root.find("./os/firmware") is None


def test_render_network_config_uses_primary_and_management_macs(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    text = host_a_gpu_guest.render_network_config(config)
    assert "set-name: lan0" in text
    assert "set-name: mgmt0" in text
    assert "macaddress: '10:20:30:40:50:60'" in text
    assert "macaddress: '52:54:00:aa:bb:cc'" in text
    assert text.count("dhcp4: true") == 2


def test_inventory_payload_includes_guest_repo_and_user(tmp_path: Path) -> None:
    config = _make_config(tmp_path, guest_repo="/home/ae/k1s", guest_user="ae")

    payload = host_a_gpu_guest.inventory_payload(
        config,
        {
            "primary_ip": "192.168.29.148",
            "management_ip": "192.168.122.202",
            "interfaces": [],
        },
    )

    assert payload == [
        {
            "name": "k1s-core-a-gpu",
            "ip": "192.168.29.148",
            "primary_ip": "192.168.29.148",
            "management_ip": "192.168.122.202",
            "interfaces": [],
            "execution_model": "linux_guest_passthrough",
            "guest_repo": "/home/ae/k1s",
            "guest_user": "ae",
        }
    ]


def test_create_overlay_uses_repo_gpu_image_as_backing_file(tmp_path: Path) -> None:
    config = _make_config(tmp_path, primary_nic_bdf=None, primary_nic_mac=None, gpu_bdf=None, gpu_audio_bdf=None)
    runner = FakeRunner()

    payload = host_a_gpu_guest.create_overlay(config, runner=runner)

    assert payload["status"] == "created"
    assert payload["backing_image"] == str(config.base_image)
    create_cmd = next(cmd for cmd in runner.calls if cmd[:2] == ["qemu-img", "create"])
    assert create_cmd[6:10] == [
        "-b",
        str(config.base_image),
        str(config.overlay_path),
        "80G",
    ]


def test_make_config_loads_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _with_env(monkeypatch)
    env_file = _write_env(
        tmp_path / "host-a-gpu.env",
        [
            "HOST_A_GPU_PRIMARY_NIC_PCI=0000:09:00.0",
            "HOST_A_GPU_GPU_PCI=0000:67:00.0",
            "HOST_A_GPU_GPU_AUDIO_PCI=0000:67:00.1",
            "HOST_A_GPU_MGMT_NETWORK=mgmt-dev",
        ],
    )

    config = host_a_gpu_guest.make_config(_parse_args(["create-overlay", "--env-file", str(env_file)]))

    assert config.env_file_path == env_file
    assert config.primary_nic_bdf == "0000:09:00.0"
    assert config.gpu_bdf == "0000:67:00.0"
    assert config.gpu_audio_bdf == "0000:67:00.1"
    assert config.mgmt_network == "mgmt-dev"


def test_process_env_overrides_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "host-a-gpu.env",
        [
            "HOST_A_GPU_PRIMARY_NIC_PCI=0000:09:00.0",
            "HOST_A_GPU_GPU_PCI=0000:67:00.0",
        ],
    )
    _with_env(
        monkeypatch,
        HOST_A_GPU_PRIMARY_NIC_PCI="0000:0a:00.0",
        HOST_A_GPU_GPU_PCI="0000:68:00.0",
    )

    config = host_a_gpu_guest.make_config(_parse_args(["--env-file", str(env_file), "create-overlay"]))

    assert config.primary_nic_bdf == "0000:0a:00.0"
    assert config.gpu_bdf == "0000:68:00.0"


def test_cli_overrides_env_and_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "host-a-gpu.env",
        ["HOST_A_GPU_PRIMARY_NIC_PCI=0000:09:00.0"],
    )
    _with_env(monkeypatch, HOST_A_GPU_PRIMARY_NIC_PCI="0000:0a:00.0")

    config = host_a_gpu_guest.make_config(
        _parse_args(
            [
                "--env-file",
                str(env_file),
                "--primary-nic-pci",
                "0000:0b:00.0",
                "create-overlay",
            ]
        )
    )

    assert config.primary_nic_bdf == "0000:0b:00.0"


def test_make_config_autoloads_default_local_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = _write_env(
        tmp_path / "state" / "host-a-gpu.env",
        ["HOST_A_GPU_PRIMARY_NIC_PCI=0000:09:00.0"],
    )
    _with_env(monkeypatch)
    monkeypatch.setattr(host_a_gpu_guest, "DEFAULT_LOCAL_ENV_FILE", env_file)

    config = host_a_gpu_guest.make_config(_parse_args(["create-overlay"]))

    assert config.env_file_path == env_file
    assert config.primary_nic_bdf == "0000:09:00.0"


def test_resolve_hardware_config_from_primary_nic_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_sysfs_for_iface(tmp_path, monkeypatch)
    config = _make_config(
        tmp_path,
        primary_nic_name="eno42",
        primary_nic_bdf=None,
        primary_nic_mac=None,
        mgmt_mac=None,
    )

    resolved = host_a_gpu_guest.resolve_hardware_config(config)

    assert resolved.primary_nic_bdf == "0000:05:00.0"
    assert resolved.primary_nic_mac == "10:20:30:40:50:60"
    assert resolved.mgmt_mac is not None


def test_resolve_hardware_config_rejects_nic_name_and_pci_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_sysfs_for_iface(tmp_path, monkeypatch)
    config = _make_config(tmp_path, primary_nic_name="eno42", primary_nic_bdf="0000:09:00.0")

    with pytest.raises(host_a_gpu_guest.ConfigError, match="different devices"):
        host_a_gpu_guest.resolve_hardware_config(config)


def test_resolve_hardware_config_allows_missing_iface_when_pci_and_mac_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sys_class_net = tmp_path / "sys" / "class" / "net"
    sys_bus_pci = tmp_path / "sys" / "bus" / "pci" / "devices"
    sys_class_net.mkdir(parents=True, exist_ok=True)
    sys_bus_pci.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(host_a_gpu_guest, "SYS_CLASS_NET", sys_class_net)
    monkeypatch.setattr(host_a_gpu_guest, "SYS_BUS_PCI_DEVICES", sys_bus_pci)
    config = _make_config(
        tmp_path,
        primary_nic_name="eno42",
        primary_nic_bdf="0000:05:00.0",
        primary_nic_mac="10:20:30:40:50:60",
        mgmt_mac=None,
    )

    resolved = host_a_gpu_guest.resolve_hardware_config(config)

    assert resolved.primary_nic_bdf == "0000:05:00.0"
    assert resolved.primary_nic_mac == "10:20:30:40:50:60"
    assert resolved.mgmt_mac is not None


def test_prepare_config_requires_hardware_for_render(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _with_env(monkeypatch)
    config = host_a_gpu_guest.make_config(_parse_args(["render"]))

    with pytest.raises(host_a_gpu_guest.ConfigError, match="missing primary NIC config"):
        host_a_gpu_guest.prepare_config_for_action(config, "render")


def test_prepare_config_allows_overlay_without_hardware(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _with_env(monkeypatch)
    args = _parse_args(["create-overlay"])
    config = host_a_gpu_guest.make_config(args)

    prepared = host_a_gpu_guest.prepare_config_for_action(config, "create-overlay")

    assert prepared.primary_nic_bdf is None
    assert prepared.gpu_bdf is None
    assert prepared.gpu_audio_bdf is None


def test_preflight_omits_iommu_assertion_when_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(host_a_gpu_guest, "pci_driver_name", lambda bdf: "vfio-pci" if "65:00" in bdf else "igc")
    monkeypatch.setattr(host_a_gpu_guest, "iommu_group_number", lambda _bdf: 15)
    monkeypatch.setattr(
        host_a_gpu_guest,
        "read_meminfo",
        lambda: {"HugePages_Free": 8192, "Hugepagesize": 2048},
    )

    payload = host_a_gpu_guest.preflight_report(config)

    assert payload["status"] == "passed"
    assert "primary_nic_iommu_group_match" not in payload["assertions"]
    assert payload["primary_nic"]["iommu_group"] == 15


def test_preflight_checks_expected_iommu_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_config(tmp_path, expected_primary_iommu_group=14)
    monkeypatch.setattr(host_a_gpu_guest, "pci_driver_name", lambda bdf: "vfio-pci" if "65:00" in bdf else "igc")
    monkeypatch.setattr(host_a_gpu_guest, "iommu_group_number", lambda _bdf: 15)
    monkeypatch.setattr(
        host_a_gpu_guest,
        "read_meminfo",
        lambda: {"HugePages_Free": 8192, "Hugepagesize": 2048},
    )

    payload = host_a_gpu_guest.preflight_report(config)

    assert payload["status"] == "failed"
    assert payload["assertions"]["primary_nic_iommu_group_match"] is False


def test_virsh_command_targets_qemu_system_by_default(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    cmd = host_a_gpu_guest.virsh_command(config, "define", "/tmp/domain.xml")
    assert cmd[:3] == ["virsh", "-c", "qemu:///system"]


def test_env_sample_documents_required_local_keys() -> None:
    text = ENV_SAMPLE.read_text(encoding="utf-8")
    assert "HOST_A_GPU_PRIMARY_NIC_NAME" in text
    assert "HOST_A_GPU_PRIMARY_NIC_PCI" in text
    assert "HOST_A_GPU_GPU_PCI" in text
    assert "HOST_A_GPU_GPU_AUDIO_PCI" in text
    assert "state/host-a-gpu.env" in text
    assert "# HOST_A_GPU_VCPUS=6" in text
    assert "# HOST_A_GPU_CPU_CORES=3" in text


def test_default_guest_shape_matches_conservative_host_a_profile() -> None:
    assert host_a_gpu_guest.CONFIG_SPECS["memory_mib"].default == 16 * 1024
    assert host_a_gpu_guest.CONFIG_SPECS["hard_limit_mib"].default == 18 * 1024
    assert host_a_gpu_guest.CONFIG_SPECS["vcpus"].default == 6
    assert host_a_gpu_guest.CONFIG_SPECS["cpu_sockets"].default == 1
    assert host_a_gpu_guest.CONFIG_SPECS["cpu_cores"].default == 3
    assert host_a_gpu_guest.CONFIG_SPECS["cpu_threads"].default == 2


def test_labctl_exposes_host_a_gpu_entrypoint() -> None:
    text = LABCTL.read_text(encoding="utf-8")
    assert "$0 host-a-gpu <render|create-overlay|create-seed|define|start|stop|undefine|preflight|ips> [args]" in text
    assert 'host-a-gpu)\n    exec "$SCRIPT_DIR/host_a_gpu_guest.py" "$@"' in text


def test_json_flag_can_follow_action() -> None:
    args = _parse_args(["ips", "--json"])
    assert args.action == "ips"
    assert args.json is True
