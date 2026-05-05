#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[3]
SYS_CLASS_NET = Path("/sys/class/net")
SYS_BUS_PCI_DEVICES = Path("/sys/bus/pci/devices")
PROC_MEMINFO = Path("/proc/meminfo")

DEFAULT_CONNECTION_URI = "qemu:///system"
DEFAULT_DOMAIN_NAME = "k1s-core-a-gpu"
DEFAULT_GUEST_HOSTNAME = "k1s-core-a-gpu"
DEFAULT_NODE_ID = "core-a--hub"
DEFAULT_GUEST_USER = "ae"
DEFAULT_GUEST_REPO = "/mnt/host"
DEFAULT_MACHINE = "pc-q35-5.1"
DEFAULT_MEMORY_MIB = 16 * 1024
DEFAULT_HARD_LIMIT_MIB = 18 * 1024
DEFAULT_VCPUS = 8
DEFAULT_CPU_SOCKETS = 1
DEFAULT_CPU_CORES = 4
DEFAULT_CPU_THREADS = 2
DEFAULT_IOTHREADS = 1
DEFAULT_OVERLAY_SIZE_GIB = 80
DEFAULT_HUGEPAGES_KIB = 2048
DEFAULT_MGMT_NETWORK = "default"
DEFAULT_OVERLAY_DIR = Path.home() / "VMs"
DEFAULT_STATE_ROOT = ROOT / "state" / "libvirt-host-a"
DEFAULT_BASE_IMAGE = ROOT / "artifacts" / "images" / "ubuntu-22.04-k1s-gpu.qcow2"
DEFAULT_LOCAL_ENV_FILE = ROOT / "state" / "host-a-gpu.env"
ENV_TEMPLATE_PATH = ROOT / "ops" / "dev" / "host-a-gpu.env.sample"
DEFAULT_SSH_PUBLIC_KEY_FILE = (
    Path(os.environ.get("SSH_KEY_PATH", str(Path.home() / ".ssh" / "id_rsa")))
    .expanduser()
    .with_suffix(".pub")
)
OVMF_CODE_CANDIDATES = (
    Path("/run/libvirt/nix-ovmf/OVMF_CODE.fd"),
    Path("/usr/share/OVMF/OVMF_CODE.fd"),
    Path("/usr/share/edk2/x64/OVMF_CODE.fd"),
)
OVMF_VARS_CANDIDATES = (
    Path("/run/libvirt/nix-ovmf/OVMF_VARS.fd"),
    Path("/usr/share/OVMF/OVMF_VARS.fd"),
    Path("/usr/share/edk2/x64/OVMF_VARS.fd"),
)
QEMU_AGENT_NETWORK_COMMAND = {"execute": "guest-network-get-interfaces"}
HARDWARE_ACTIONS = frozenset({"render", "create-seed", "define", "start", "preflight", "ips"})


class ConfigError(RuntimeError):
    """Raised when helper configuration is incomplete or invalid."""


class CommandRunner(Protocol):
    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Run one command and return the completed process."""


@dataclass(frozen=True)
class GuestConfig:
    env_file_path: Path | None
    connection_uri: str
    domain_name: str
    guest_hostname: str
    node_id: str
    guest_user: str
    guest_repo: str
    machine: str
    memory_mib: int
    hard_limit_mib: int
    vcpus: int
    cpu_sockets: int
    cpu_cores: int
    cpu_threads: int
    iothreads: int
    hugepages_kib: int
    overlay_size_gib: int
    overlay_dir: Path
    base_image: Path
    state_root: Path
    ssh_public_key_file: Path
    ssh_public_key: str | None
    ovmf_code: Path | None
    ovmf_vars_template: Path | None
    mgmt_network: str
    mgmt_mac: str | None
    primary_nic_name: str | None
    primary_nic_bdf: str | None
    primary_nic_mac: str | None
    gpu_bdf: str | None
    gpu_audio_bdf: str | None
    expected_primary_iommu_group: int | None

    @property
    def overlay_path(self) -> Path:
        return self.overlay_dir / f"{self.domain_name}.qcow2"

    @property
    def seed_iso_path(self) -> Path:
        return self.overlay_dir / f"{self.domain_name}-seed.iso"

    @property
    def state_dir(self) -> Path:
        return self.state_root / self.domain_name

    @property
    def domain_xml_path(self) -> Path:
        return self.state_dir / f"{self.domain_name}.xml"

    @property
    def inventory_path(self) -> Path:
        return self.state_dir / "inventory.json"

    @property
    def user_data_path(self) -> Path:
        return self.state_dir / "user-data"

    @property
    def meta_data_path(self) -> Path:
        return self.state_dir / "meta-data"

    @property
    def network_config_path(self) -> Path:
        return self.state_dir / "network-config"

    @property
    def ovmf_vars_path(self) -> Path:
        return self.state_dir / f"{self.domain_name}-OVMF_VARS.fd"


@dataclass(frozen=True)
class ConfigSpec:
    env_name: str
    default: Any
    parser: Any


CONFIG_SPECS: dict[str, ConfigSpec] = {
    "connection_uri": ConfigSpec("HOST_A_GPU_CONNECTION_URI", DEFAULT_CONNECTION_URI, str),
    "domain_name": ConfigSpec("HOST_A_GPU_DOMAIN_NAME", DEFAULT_DOMAIN_NAME, str),
    "guest_hostname": ConfigSpec("HOST_A_GPU_GUEST_HOSTNAME", DEFAULT_GUEST_HOSTNAME, str),
    "node_id": ConfigSpec("HOST_A_GPU_NODE_ID", DEFAULT_NODE_ID, str),
    "guest_user": ConfigSpec("HOST_A_GPU_GUEST_USER", DEFAULT_GUEST_USER, str),
    "guest_repo": ConfigSpec("HOST_A_GPU_GUEST_REPO", DEFAULT_GUEST_REPO, str),
    "machine": ConfigSpec("HOST_A_GPU_MACHINE", DEFAULT_MACHINE, str),
    "memory_mib": ConfigSpec("HOST_A_GPU_MEMORY_MIB", DEFAULT_MEMORY_MIB, int),
    "hard_limit_mib": ConfigSpec("HOST_A_GPU_HARD_LIMIT_MIB", DEFAULT_HARD_LIMIT_MIB, int),
    "vcpus": ConfigSpec("HOST_A_GPU_VCPUS", DEFAULT_VCPUS, int),
    "cpu_sockets": ConfigSpec("HOST_A_GPU_CPU_SOCKETS", DEFAULT_CPU_SOCKETS, int),
    "cpu_cores": ConfigSpec("HOST_A_GPU_CPU_CORES", DEFAULT_CPU_CORES, int),
    "cpu_threads": ConfigSpec("HOST_A_GPU_CPU_THREADS", DEFAULT_CPU_THREADS, int),
    "iothreads": ConfigSpec("HOST_A_GPU_IOTHREADS", DEFAULT_IOTHREADS, int),
    "hugepages_kib": ConfigSpec("HOST_A_GPU_HUGEPAGES_KIB", DEFAULT_HUGEPAGES_KIB, int),
    "overlay_size_gib": ConfigSpec("HOST_A_GPU_OVERLAY_SIZE_GIB", DEFAULT_OVERLAY_SIZE_GIB, int),
    "overlay_dir": ConfigSpec("HOST_A_GPU_OVERLAY_DIR", DEFAULT_OVERLAY_DIR, "path"),
    "base_image": ConfigSpec("HOST_A_GPU_BASE_IMAGE", DEFAULT_BASE_IMAGE, "path"),
    "state_root": ConfigSpec("HOST_A_GPU_STATE_ROOT", DEFAULT_STATE_ROOT, "path"),
    "ssh_public_key_file": ConfigSpec(
        "HOST_A_GPU_SSH_PUBLIC_KEY_FILE",
        DEFAULT_SSH_PUBLIC_KEY_FILE,
        "path",
    ),
    "ovmf_code": ConfigSpec("HOST_A_GPU_OVMF_CODE", None, "optional_path"),
    "ovmf_vars_template": ConfigSpec("HOST_A_GPU_OVMF_VARS_TEMPLATE", None, "optional_path"),
    "mgmt_network": ConfigSpec("HOST_A_GPU_MGMT_NETWORK", DEFAULT_MGMT_NETWORK, str),
    "primary_nic_name": ConfigSpec("HOST_A_GPU_PRIMARY_NIC_NAME", None, "optional_str"),
    "primary_nic_bdf": ConfigSpec("HOST_A_GPU_PRIMARY_NIC_PCI", None, "optional_str"),
    "primary_nic_mac": ConfigSpec("HOST_A_GPU_PRIMARY_NIC_MAC", None, "optional_str"),
    "gpu_bdf": ConfigSpec("HOST_A_GPU_GPU_PCI", None, "optional_str"),
    "gpu_audio_bdf": ConfigSpec("HOST_A_GPU_GPU_AUDIO_PCI", None, "optional_str"),
    "expected_primary_iommu_group": ConfigSpec(
        "HOST_A_GPU_EXPECTED_PRIMARY_IOMMU_GROUP",
        None,
        "optional_int",
    ),
}


class SubprocessRunner:
    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )


def resolve_input_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def resolve_existing_path(
    label: str,
    path: Path | None = None,
    *,
    candidates: tuple[Path, ...] = (),
) -> Path:
    if path is not None:
        resolved = resolve_input_path(path)
        if not resolved.exists():
            raise ConfigError(f"{label} does not exist: {resolved}")
        return resolved
    for candidate in candidates:
        if candidate.exists():
            return candidate
    joined = ", ".join(str(candidate) for candidate in candidates)
    raise ConfigError(f"unable to resolve {label}; checked: {joined}")


def normalize_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_mac(value: str) -> str:
    parts = str(value or "").strip().lower().split(":")
    if len(parts) != 6 or any(len(part) != 2 for part in parts):
        raise ConfigError(f"invalid MAC address: {value!r}")
    for part in parts:
        int(part, 16)
    return ":".join(parts)


def normalize_pci_bdf(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise ConfigError("empty PCI BDF")
    if text.count(":") == 1:
        text = f"0000:{text}"
    parts = text.split(":")
    if len(parts) != 3 or "." not in parts[2]:
        raise ConfigError(f"invalid PCI BDF: {value!r}")
    domain, bus, slot_func = parts
    slot, function = slot_func.split(".", 1)
    if len(domain) != 4 or len(bus) != 2 or len(slot) != 2 or len(function) != 1:
        raise ConfigError(f"invalid PCI BDF: {value!r}")
    int(domain, 16)
    int(bus, 16)
    int(slot, 16)
    int(function, 16)
    return f"{domain}:{bus}:{slot}.{function}"


def pci_bdf_to_libvirt_address(value: str) -> dict[str, str]:
    domain, bus, slot_func = normalize_pci_bdf(value).split(":")
    slot, function = slot_func.split(".", 1)
    return {
        "domain": f"0x{domain}",
        "bus": f"0x{bus}",
        "slot": f"0x{slot}",
        "function": f"0x{function}",
    }


def stable_mgmt_mac(domain_name: str) -> str:
    digest = hashlib.md5(domain_name.encode("utf-8")).digest()  # noqa: S324
    return f"52:54:00:{digest[0]:02x}:{digest[1]:02x}:{digest[2]:02x}"


def read_public_key(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"SSH public key file does not exist: {path}") from exc


def parse_env_file(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    for idx, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ConfigError(f"invalid env assignment at {path}:{idx}: {raw_line!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"invalid env assignment at {path}:{idx}: {raw_line!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        payload[key] = value
    return payload


def resolve_env_file_path(cli_value: Any) -> Path | None:
    if cli_value is not None:
        path = resolve_input_path(cli_value)
        if not path.exists():
            raise ConfigError(f"--env-file does not exist: {path}")
        return path
    env_value = normalize_optional_string(os.environ.get("HOST_A_GPU_ENV_FILE"))
    if env_value:
        path = resolve_input_path(env_value)
        if not path.exists():
            raise ConfigError(f"HOST_A_GPU_ENV_FILE does not exist: {path}")
        return path
    if DEFAULT_LOCAL_ENV_FILE.exists():
        return DEFAULT_LOCAL_ENV_FILE
    return None


def _coerce_config_value(parser: Any, value: Any) -> Any:
    if parser == "path":
        return resolve_input_path(value)
    if parser == "optional_path":
        text = normalize_optional_string(value)
        return resolve_input_path(text) if text else None
    if parser == "optional_int":
        text = normalize_optional_string(value)
        return int(text) if text is not None else None
    if parser == "optional_str":
        return normalize_optional_string(value)
    return parser(value)


def _value_from_layers(
    *,
    field: str,
    cli_values: dict[str, Any],
    env_values: dict[str, str],
    file_values: dict[str, str],
) -> Any:
    spec = CONFIG_SPECS[field]
    if field in cli_values:
        return cli_values[field]
    if spec.env_name in env_values:
        return env_values[spec.env_name]
    if spec.env_name in file_values:
        return file_values[spec.env_name]
    return spec.default


def hardware_config_help() -> str:
    return (
        "set values with CLI flags, HOST_A_GPU_* environment variables, or "
        f"{DEFAULT_LOCAL_ENV_FILE} (copy {ENV_TEMPLATE_PATH})"
    )


def make_config(args: argparse.Namespace) -> GuestConfig:
    cli_values = vars(args).copy()
    env_file_path = resolve_env_file_path(cli_values.get("env_file"))
    file_values = parse_env_file(env_file_path) if env_file_path else {}
    env_values = {key: value for key, value in os.environ.items() if key.startswith("HOST_A_GPU_")}

    values: dict[str, Any] = {"env_file_path": env_file_path}
    for field, spec in CONFIG_SPECS.items():
        raw = _value_from_layers(
            field=field,
            cli_values=cli_values,
            env_values=env_values,
            file_values=file_values,
        )
        values[field] = _coerce_config_value(spec.parser, raw)

    mgmt_mac = normalize_optional_string(cli_values.get("mgmt_mac"))
    if mgmt_mac is not None:
        mgmt_mac = normalize_mac(mgmt_mac)
    values["mgmt_mac"] = mgmt_mac
    values["ssh_public_key"] = None
    return GuestConfig(**values)


def resolve_public_key_config(config: GuestConfig) -> GuestConfig:
    if config.ssh_public_key:
        return config
    return replace(config, ssh_public_key=read_public_key(config.ssh_public_key_file))


def resolve_firmware_config(config: GuestConfig) -> GuestConfig:
    return replace(
        config,
        ovmf_code=resolve_existing_path(
            "OVMF_CODE.fd",
            config.ovmf_code,
            candidates=OVMF_CODE_CANDIDATES,
        ),
        ovmf_vars_template=resolve_existing_path(
            "OVMF_VARS.fd",
            config.ovmf_vars_template,
            candidates=OVMF_VARS_CANDIDATES,
        ),
    )


def pci_device_net_mac(bdf: str) -> str | None:
    device_dir = SYS_BUS_PCI_DEVICES / normalize_pci_bdf(bdf)
    net_dir = device_dir / "net"
    if not net_dir.is_dir():
        return None
    for iface in sorted(net_dir.iterdir()):
        address_file = iface / "address"
        if address_file.is_file():
            return normalize_mac(address_file.read_text(encoding="utf-8").strip())
    return None


def resolve_primary_nic_from_name(config: GuestConfig) -> tuple[str | None, str | None]:
    if not config.primary_nic_name:
        return None, None
    iface_dir = SYS_CLASS_NET / config.primary_nic_name
    if not iface_dir.exists():
        if config.primary_nic_bdf is not None or config.primary_nic_mac is not None:
            return None, None
        raise ConfigError(
            f"primary NIC interface does not exist: {config.primary_nic_name}; {hardware_config_help()}"
        )
    device_link = iface_dir / "device"
    if not device_link.exists():
        if config.primary_nic_bdf is not None:
            return None, None
        raise ConfigError(
            f"primary NIC interface is not backed by a PCI device: {config.primary_nic_name}"
        )
    device_path = device_link.resolve()
    resolved_bdf = normalize_pci_bdf(device_path.name)
    mac = normalize_mac((iface_dir / "address").read_text(encoding="utf-8").strip())
    return resolved_bdf, mac


def resolve_hardware_config(config: GuestConfig) -> GuestConfig:
    primary_bdf_from_name, primary_mac_from_name = resolve_primary_nic_from_name(config)
    explicit_primary_bdf = (
        normalize_pci_bdf(config.primary_nic_bdf) if config.primary_nic_bdf is not None else None
    )
    if explicit_primary_bdf and primary_bdf_from_name and explicit_primary_bdf != primary_bdf_from_name:
        raise ConfigError(
            "primary NIC name and PCI address resolve to different devices; "
            f"name={config.primary_nic_name!r} -> {primary_bdf_from_name}, "
            f"pci={explicit_primary_bdf}"
        )
    primary_nic_bdf = explicit_primary_bdf or primary_bdf_from_name
    if primary_nic_bdf is None:
        raise ConfigError(
            "missing primary NIC config; set HOST_A_GPU_PRIMARY_NIC_NAME or "
            f"HOST_A_GPU_PRIMARY_NIC_PCI, or pass --primary-nic-name/--primary-nic-pci; {hardware_config_help()}"
        )

    explicit_primary_mac = (
        normalize_mac(config.primary_nic_mac) if config.primary_nic_mac is not None else None
    )
    discovered_primary_mac = primary_mac_from_name or pci_device_net_mac(primary_nic_bdf)
    if explicit_primary_mac and discovered_primary_mac and explicit_primary_mac != discovered_primary_mac:
        raise ConfigError(
            "primary NIC MAC does not match the selected device; "
            f"expected {explicit_primary_mac}, discovered {discovered_primary_mac}"
        )
    primary_nic_mac = explicit_primary_mac or discovered_primary_mac
    if primary_nic_mac is None:
        raise ConfigError(
            "unable to determine primary NIC MAC; set HOST_A_GPU_PRIMARY_NIC_MAC or "
            f"pass --primary-nic-mac; {hardware_config_help()}"
        )

    if config.gpu_bdf is None:
        raise ConfigError(
            "missing GPU PCI config; set HOST_A_GPU_GPU_PCI or pass --gpu-pci; "
            + hardware_config_help()
        )
    if config.gpu_audio_bdf is None:
        raise ConfigError(
            "missing GPU audio PCI config; set HOST_A_GPU_GPU_AUDIO_PCI or pass --gpu-audio-pci; "
            + hardware_config_help()
        )

    return replace(
        config,
        mgmt_mac=config.mgmt_mac or stable_mgmt_mac(config.domain_name),
        primary_nic_bdf=primary_nic_bdf,
        primary_nic_mac=primary_nic_mac,
        gpu_bdf=normalize_pci_bdf(config.gpu_bdf),
        gpu_audio_bdf=normalize_pci_bdf(config.gpu_audio_bdf),
    )


def prepare_config_for_action(config: GuestConfig, action: str) -> GuestConfig:
    prepared = config
    if action in HARDWARE_ACTIONS:
        prepared = resolve_hardware_config(prepared)
    if action in {"render", "create-seed", "define"}:
        prepared = resolve_public_key_config(prepared)
    if action in {"render", "define"}:
        prepared = resolve_firmware_config(prepared)
    return prepared


def config_summary(config: GuestConfig) -> dict[str, Any]:
    return {
        "env_file": str(config.env_file_path) if config.env_file_path else None,
        "connection_uri": config.connection_uri,
        "domain_name": config.domain_name,
        "guest_hostname": config.guest_hostname,
        "node_id": config.node_id,
        "overlay_path": str(config.overlay_path),
        "seed_iso_path": str(config.seed_iso_path),
        "state_dir": str(config.state_dir),
        "base_image": str(config.base_image),
        "mgmt_network": config.mgmt_network,
        "primary_nic_name": config.primary_nic_name,
        "primary_nic_bdf": config.primary_nic_bdf,
        "primary_nic_mac": config.primary_nic_mac,
        "gpu_bdf": config.gpu_bdf,
        "gpu_audio_bdf": config.gpu_audio_bdf,
    }


def render_user_data(config: GuestConfig) -> str:
    if config.ssh_public_key is None:
        raise ConfigError("SSH public key not resolved")
    return "\n".join(
        [
            "#cloud-config",
            f"hostname: {config.guest_hostname}",
            "manage_etc_hosts: true",
            f"users:\n  - name: {config.guest_user}",
            "    sudo: ALL=(ALL) NOPASSWD:ALL",
            "    shell: /bin/bash",
            "    lock_passwd: true",
            "    ssh_authorized_keys:",
            f"      - {config.ssh_public_key}",
            "package_update: false",
            "packages:",
            "  - qemu-guest-agent",
            "write_files:",
            "  - path: /etc/default/k1s-host-a-gpu",
            "    permissions: '0644'",
            "    content: |",
            f"      AE_NODE_ID={config.node_id}",
            "      AE_CRI_RUNTIME_HANDLER=nvidia",
            f"      K1S_GUEST_REPO={config.guest_repo}",
            "  - path: /etc/systemd/system/qemu-guest-agent.service.d/override.conf",
            "    permissions: '0644'",
            "    content: |",
            "      [Service]",
            "      Restart=always",
            "runcmd:",
            "  - [mkdir, -p, /mnt/host]",
            "  - [systemctl, daemon-reload]",
            "  - [systemctl, enable, --now, qemu-guest-agent.service]",
            "",
        ]
    )


def render_meta_data(config: GuestConfig) -> str:
    return "\n".join(
        [
            f"instance-id: {config.domain_name}",
            f"local-hostname: {config.guest_hostname}",
            "",
        ]
    )


def render_network_config(config: GuestConfig) -> str:
    if config.primary_nic_mac is None or config.mgmt_mac is None:
        raise ConfigError("hardware config not resolved for network-config rendering")
    return "\n".join(
        [
            "version: 2",
            "ethernets:",
            "  lan0:",
            f"    match:\n      macaddress: '{config.primary_nic_mac}'",
            "    set-name: lan0",
            "    dhcp4: true",
            "    dhcp6: true",
            "  mgmt0:",
            f"    match:\n      macaddress: '{config.mgmt_mac}'",
            "    set-name: mgmt0",
            "    dhcp4: true",
            "    dhcp6: true",
            "",
        ]
    )


def _add_text_element(parent: ET.Element, tag: str, text: str, **attrib: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrib)
    element.text = text
    return element


def _add_hostdev(parent: ET.Element, bdf: str) -> None:
    hostdev = ET.SubElement(
        parent,
        "hostdev",
        {"mode": "subsystem", "type": "pci", "managed": "yes"},
    )
    source = ET.SubElement(hostdev, "source")
    ET.SubElement(source, "address", pci_bdf_to_libvirt_address(bdf))


def render_domain_xml(config: GuestConfig) -> str:
    if config.ovmf_code is None or config.ovmf_vars_template is None:
        raise ConfigError("firmware paths not resolved")
    if config.primary_nic_bdf is None or config.gpu_bdf is None or config.gpu_audio_bdf is None:
        raise ConfigError("hardware config not resolved")
    if config.mgmt_mac is None:
        raise ConfigError("management MAC not resolved")

    domain = ET.Element("domain", {"type": "kvm"})
    _add_text_element(domain, "name", config.domain_name)
    _add_text_element(domain, "memory", str(config.memory_mib), unit="MiB")
    _add_text_element(domain, "currentMemory", str(config.memory_mib), unit="MiB")
    _add_text_element(domain, "vcpu", str(config.vcpus), placement="static")
    _add_text_element(domain, "iothreads", str(config.iothreads))

    os_elem = ET.SubElement(domain, "os")
    ET.SubElement(os_elem, "type", {"arch": "x86_64", "machine": config.machine}).text = "hvm"
    _add_text_element(
        os_elem,
        "loader",
        str(config.ovmf_code),
        readonly="yes",
        secure="no",
        type="pflash",
    )
    nvram = _add_text_element(os_elem, "nvram", str(config.ovmf_vars_path))
    nvram.set("template", str(config.ovmf_vars_template))

    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")

    cpu = ET.SubElement(domain, "cpu", {"mode": "host-passthrough", "check": "none"})
    ET.SubElement(
        cpu,
        "topology",
        {
            "sockets": str(config.cpu_sockets),
            "cores": str(config.cpu_cores),
            "threads": str(config.cpu_threads),
        },
    )

    memtune = ET.SubElement(domain, "memtune")
    _add_text_element(memtune, "hard_limit", str(config.hard_limit_mib), unit="MiB")

    memory_backing = ET.SubElement(domain, "memoryBacking")
    hugepages = ET.SubElement(memory_backing, "hugepages")
    ET.SubElement(hugepages, "page", {"size": str(config.hugepages_kib), "unit": "KiB"})
    ET.SubElement(memory_backing, "locked")
    ET.SubElement(memory_backing, "nosharepages")
    ET.SubElement(memory_backing, "allocation", {"mode": "immediate"})

    ET.SubElement(domain, "clock", {"offset": "utc"})

    devices = ET.SubElement(domain, "devices")

    disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(
        disk,
        "driver",
        {"name": "qemu", "type": "qcow2", "cache": "none", "io": "native", "iothread": "1"},
    )
    ET.SubElement(disk, "source", {"file": str(config.overlay_path)})
    ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})
    ET.SubElement(disk, "boot", {"order": "1"})

    ET.SubElement(devices, "controller", {"type": "sata", "index": "0"})
    seed_disk = ET.SubElement(devices, "disk", {"type": "file", "device": "cdrom"})
    ET.SubElement(seed_disk, "driver", {"name": "qemu", "type": "raw"})
    ET.SubElement(seed_disk, "source", {"file": str(config.seed_iso_path)})
    ET.SubElement(seed_disk, "target", {"dev": "sda", "bus": "sata"})
    ET.SubElement(seed_disk, "readonly")
    ET.SubElement(seed_disk, "boot", {"order": "2"})

    mgmt_nic = ET.SubElement(devices, "interface", {"type": "network"})
    ET.SubElement(mgmt_nic, "mac", {"address": config.mgmt_mac})
    ET.SubElement(mgmt_nic, "source", {"network": config.mgmt_network})
    ET.SubElement(mgmt_nic, "model", {"type": "virtio"})

    _add_hostdev(devices, config.primary_nic_bdf)
    _add_hostdev(devices, config.gpu_bdf)
    _add_hostdev(devices, config.gpu_audio_bdf)

    ET.SubElement(devices, "controller", {"type": "virtio-serial", "index": "0"})
    channel = ET.SubElement(devices, "channel", {"type": "unix"})
    ET.SubElement(channel, "target", {"type": "virtio", "name": "org.qemu.guest_agent.0"})
    serial = ET.SubElement(devices, "serial", {"type": "pty"})
    ET.SubElement(serial, "target", {"port": "0"})
    console = ET.SubElement(devices, "console", {"type": "pty"})
    ET.SubElement(console, "target", {"type": "serial", "port": "0"})
    ET.SubElement(devices, "memballoon", {"model": "none"})

    return ET.tostring(domain, encoding="unicode")


def ensure_parent_dirs(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)


def ensure_ovmf_vars(config: GuestConfig) -> None:
    if config.ovmf_vars_template is None:
        raise ConfigError("OVMF vars template not resolved")
    ensure_parent_dirs(config.ovmf_vars_path)
    if not config.ovmf_vars_path.exists():
        shutil.copyfile(config.ovmf_vars_template, config.ovmf_vars_path)


def render_cloud_init_files(config: GuestConfig) -> dict[str, str]:
    ensure_parent_dirs(config.user_data_path, config.meta_data_path, config.network_config_path)
    config.user_data_path.write_text(render_user_data(config), encoding="utf-8")
    config.meta_data_path.write_text(render_meta_data(config), encoding="utf-8")
    config.network_config_path.write_text(render_network_config(config), encoding="utf-8")
    return {
        "user_data": str(config.user_data_path),
        "meta_data": str(config.meta_data_path),
        "network_config": str(config.network_config_path),
    }


def render_files(config: GuestConfig) -> dict[str, str]:
    ensure_ovmf_vars(config)
    cloud_paths = render_cloud_init_files(config)
    ensure_parent_dirs(config.domain_xml_path)
    config.domain_xml_path.write_text(render_domain_xml(config), encoding="utf-8")
    payload = {
        "domain_xml": str(config.domain_xml_path),
        "ovmf_vars": str(config.ovmf_vars_path),
    }
    payload.update(cloud_paths)
    return payload


def read_qemu_img_virtual_size_bytes(path: Path, *, runner: CommandRunner | None = None) -> int:
    runner = runner or SubprocessRunner()
    result = runner.run(["qemu-img", "info", "--output=json", str(path)])
    if result.returncode != 0:
        raise SystemExit(result.stderr or f"qemu-img info failed: {path}")
    payload = json.loads(result.stdout or "{}")
    return int(payload["virtual-size"])


def read_qemu_img_virtual_size_gib(path: Path, *, runner: CommandRunner | None = None) -> int:
    size_bytes = read_qemu_img_virtual_size_bytes(path, runner=runner)
    gib = size_bytes // (1024**3)
    if size_bytes % (1024**3):
        gib += 1
    return gib


def create_overlay(
    config: GuestConfig,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    runner = runner or SubprocessRunner()
    base_image = resolve_existing_path("base image", config.base_image)
    config.overlay_path.parent.mkdir(parents=True, exist_ok=True)
    requested_size_gib = max(config.overlay_size_gib, read_qemu_img_virtual_size_gib(base_image, runner=runner))
    cmd = [
        "qemu-img",
        "create",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        str(base_image),
        str(config.overlay_path),
        f"{requested_size_gib}G",
    ]
    result = runner.run(cmd)
    if result.returncode != 0:
        raise SystemExit(result.stderr or f"qemu-img create failed: {config.overlay_path}")
    return {
        "status": "created",
        "domain_name": config.domain_name,
        "overlay_path": str(config.overlay_path),
        "backing_image": str(base_image),
        "virtual_size_gib": requested_size_gib,
    }


def create_seed_iso(config: GuestConfig, *, runner: CommandRunner | None = None) -> dict[str, Any]:
    runner = runner or SubprocessRunner()
    render_cloud_init_files(config)
    config.seed_iso_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "cloud-localds",
        "--network-config",
        str(config.network_config_path),
        str(config.seed_iso_path),
        str(config.user_data_path),
        str(config.meta_data_path),
    ]
    result = runner.run(cmd)
    if result.returncode != 0:
        raise SystemExit(result.stderr or f"cloud-localds failed: {config.seed_iso_path}")
    return {
        "status": "created",
        "seed_iso_path": str(config.seed_iso_path),
        "user_data": str(config.user_data_path),
        "meta_data": str(config.meta_data_path),
        "network_config": str(config.network_config_path),
    }


def virsh_command(config: GuestConfig, *args: str) -> list[str]:
    return ["virsh", "-c", config.connection_uri, *args]


def run_checked(
    cmd: list[str],
    *,
    runner: CommandRunner | None = None,
    stderr_label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    runner = runner or SubprocessRunner()
    result = runner.run(cmd)
    if result.returncode != 0:
        label = stderr_label or "command"
        detail = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"{label} failed: {' '.join(cmd)}\n{detail}")
    return result


def define_domain(config: GuestConfig, *, runner: CommandRunner | None = None) -> dict[str, Any]:
    render_files(config)
    run_checked(
        virsh_command(config, "define", str(config.domain_xml_path)),
        runner=runner,
        stderr_label="virsh define",
    )
    return {
        "status": "defined",
        "domain_name": config.domain_name,
        "domain_xml": str(config.domain_xml_path),
    }


def start_domain(config: GuestConfig, *, runner: CommandRunner | None = None) -> dict[str, Any]:
    run_checked(
        virsh_command(config, "start", config.domain_name),
        runner=runner,
        stderr_label="virsh start",
    )
    return {"status": "started", "domain_name": config.domain_name}


def stop_domain(
    config: GuestConfig,
    *,
    force: bool = False,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    action = "destroy" if force else "shutdown"
    run_checked(
        virsh_command(config, action, config.domain_name),
        runner=runner,
        stderr_label=f"virsh {action}",
    )
    return {"status": "stopped", "domain_name": config.domain_name, "force": force}


def undefine_domain(
    config: GuestConfig,
    *,
    purge_artifacts: bool = False,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    run_checked(
        virsh_command(config, "undefine", config.domain_name, "--nvram"),
        runner=runner,
        stderr_label="virsh undefine",
    )
    payload = {"status": "undefined", "domain_name": config.domain_name, "purged": purge_artifacts}
    if purge_artifacts:
        shutil.rmtree(config.state_dir, ignore_errors=True)
        for path in (config.overlay_path, config.seed_iso_path):
            if path.exists():
                path.unlink()
    return payload


def preferred_ip(addresses: list[dict[str, str]]) -> str | None:
    for candidate in addresses:
        ip = str(candidate.get("ip") or "")
        if candidate.get("type") == "ipv4" and not ip.startswith("127."):
            return ip
    for candidate in addresses:
        ip = str(candidate.get("ip") or "")
        if candidate.get("type") == "ipv6" and ip != "::1":
            return ip
    return None


def parse_qga_network_payload(raw_payload: str, config: GuestConfig) -> dict[str, Any]:
    payload = json.loads(raw_payload)
    interfaces: list[dict[str, Any]] = []
    primary_mac = normalize_mac(config.primary_nic_mac or "")
    mgmt_mac = normalize_mac(config.mgmt_mac or "")
    primary_ip = None
    management_ip = None
    for item in payload.get("return", []):
        mac = normalize_optional_string(item.get("hardware-address"))
        if mac is not None:
            mac = normalize_mac(mac)
        addresses: list[dict[str, str]] = []
        for address in item.get("ip-addresses", []):
            ip = normalize_optional_string(address.get("ip-address"))
            ip_type = normalize_optional_string(address.get("ip-address-type"))
            if not ip or not ip_type:
                continue
            if ip.startswith("127.") or ip == "::1":
                continue
            addresses.append({"ip": ip, "type": ip_type})
        entry = {
            "name": normalize_optional_string(item.get("name")),
            "mac": mac,
            "ips": [address["ip"] for address in addresses],
        }
        interfaces.append(entry)
        chosen_ip = preferred_ip(addresses)
        if mac == primary_mac and primary_ip is None:
            primary_ip = chosen_ip
        if mac == mgmt_mac and management_ip is None:
            management_ip = chosen_ip
    return {
        "primary_ip": primary_ip,
        "management_ip": management_ip,
        "interfaces": interfaces,
    }


def inventory_payload(config: GuestConfig, ip_report: dict[str, Any]) -> list[dict[str, Any]]:
    ip = ip_report.get("primary_ip") or ip_report.get("management_ip") or ""
    return [
        {
            "name": config.domain_name,
            "ip": ip,
            "primary_ip": ip_report.get("primary_ip"),
            "management_ip": ip_report.get("management_ip"),
            "interfaces": ip_report.get("interfaces", []),
            "execution_model": "linux_guest_passthrough",
            "guest_repo": config.guest_repo,
            "guest_user": config.guest_user,
        }
    ]


def query_guest_ips(config: GuestConfig, *, runner: CommandRunner | None = None) -> dict[str, Any]:
    runner = runner or SubprocessRunner()
    result = run_checked(
        virsh_command(
            config,
            "qemu-agent-command",
            config.domain_name,
            json.dumps(QEMU_AGENT_NETWORK_COMMAND),
        ),
        runner=runner,
        stderr_label="virsh qemu-agent-command",
    )
    report = parse_qga_network_payload(result.stdout or "{}", config)
    payload = inventory_payload(config, report)
    ensure_parent_dirs(config.inventory_path)
    config.inventory_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    report["inventory_path"] = str(config.inventory_path)
    report["domain_name"] = config.domain_name
    return report


def pci_driver_name(bdf: str) -> str | None:
    device_dir = SYS_BUS_PCI_DEVICES / normalize_pci_bdf(bdf)
    driver_link = device_dir / "driver"
    if not driver_link.exists():
        return None
    return driver_link.resolve().name


def iommu_group_number(bdf: str) -> int | None:
    device_dir = SYS_BUS_PCI_DEVICES / normalize_pci_bdf(bdf)
    group_link = device_dir / "iommu_group"
    if not group_link.exists():
        return None
    return int(group_link.resolve().name)


def read_meminfo() -> dict[str, int]:
    payload: dict[str, int] = {}
    for line in PROC_MEMINFO.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        raw_value = raw_value.strip()
        digits = []
        for char in raw_value:
            if char.isdigit():
                digits.append(char)
            elif digits:
                break
        if digits:
            payload[key] = int("".join(digits))
    return payload


def preflight_report(config: GuestConfig) -> dict[str, Any]:
    if config.primary_nic_bdf is None or config.gpu_bdf is None or config.gpu_audio_bdf is None:
        raise ConfigError("hardware config not resolved")
    meminfo = read_meminfo()
    hugepages_free = int(meminfo.get("HugePages_Free", 0))
    hugepage_size_kib = int(meminfo.get("Hugepagesize", config.hugepages_kib))
    required_pages = (config.memory_mib * 1024) // config.hugepages_kib
    primary_group = iommu_group_number(config.primary_nic_bdf)
    assertions: dict[str, bool] = {
        "gpu_bound_to_vfio": pci_driver_name(config.gpu_bdf) == "vfio-pci",
        "gpu_audio_bound_to_vfio": pci_driver_name(config.gpu_audio_bdf) == "vfio-pci",
        "hugepages_sufficient": hugepages_free >= required_pages,
        "hugepage_size_match": hugepage_size_kib == config.hugepages_kib,
    }
    if config.expected_primary_iommu_group is not None:
        assertions["primary_nic_iommu_group_match"] = primary_group == config.expected_primary_iommu_group

    return {
        "domain_name": config.domain_name,
        "primary_nic": {
            "name": config.primary_nic_name,
            "bdf": config.primary_nic_bdf,
            "mac": config.primary_nic_mac,
            "driver": pci_driver_name(config.primary_nic_bdf),
            "iommu_group": primary_group,
            "expected_iommu_group": config.expected_primary_iommu_group,
        },
        "gpu": {
            "bdf": config.gpu_bdf,
            "driver": pci_driver_name(config.gpu_bdf),
        },
        "gpu_audio": {
            "bdf": config.gpu_audio_bdf,
            "driver": pci_driver_name(config.gpu_audio_bdf),
        },
        "hugepages": {
            "free": hugepages_free,
            "size_kib": hugepage_size_kib,
            "required": required_pages,
        },
        "assertions": assertions,
        "status": "passed" if all(assertions.values()) else "failed",
    }


def build_parser() -> argparse.ArgumentParser:
    def add_common_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--env-file", type=Path, help="Optional shell-style env file.")
        parser.add_argument("--connection-uri")
        parser.add_argument("--domain-name")
        parser.add_argument("--guest-hostname")
        parser.add_argument("--node-id")
        parser.add_argument("--guest-user")
        parser.add_argument("--guest-repo")
        parser.add_argument("--machine")
        parser.add_argument("--memory-mib", type=int)
        parser.add_argument("--hard-limit-mib", type=int)
        parser.add_argument("--vcpus", type=int)
        parser.add_argument("--cpu-sockets", type=int)
        parser.add_argument("--cpu-cores", type=int)
        parser.add_argument("--cpu-threads", type=int)
        parser.add_argument("--iothreads", type=int)
        parser.add_argument("--hugepages-kib", type=int)
        parser.add_argument("--overlay-size-gib", type=int)
        parser.add_argument("--overlay-dir", type=Path)
        parser.add_argument("--base-image", type=Path)
        parser.add_argument("--state-root", type=Path)
        parser.add_argument("--ssh-public-key-file", type=Path)
        parser.add_argument("--ovmf-code", type=Path)
        parser.add_argument("--ovmf-vars-template", type=Path)
        parser.add_argument("--mgmt-network")
        parser.add_argument("--mgmt-mac")
        parser.add_argument("--primary-nic-name")
        parser.add_argument("--primary-nic-pci", dest="primary_nic_bdf")
        parser.add_argument("--primary-nic-mac")
        parser.add_argument("--gpu-pci", dest="gpu_bdf")
        parser.add_argument("--gpu-audio-pci", dest="gpu_audio_bdf")
        parser.add_argument("--expected-primary-iommu-group", type=int)
        parser.add_argument("--json", action="store_true", help="Emit JSON output.")

    parser = argparse.ArgumentParser(
        prog="host_a_gpu_guest.py",
        description="Manage the Host A libvirt GPU passthrough guest.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(parser)

    sub = parser.add_subparsers(dest="action", required=True)
    render = sub.add_parser(
        "render",
        help="Render libvirt XML and cloud-init files.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(render)
    create_overlay = sub.add_parser(
        "create-overlay",
        help="Create the qcow2 overlay.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(create_overlay)
    create_seed = sub.add_parser(
        "create-seed",
        help="Create the cloud-init seed ISO.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(create_seed)
    define = sub.add_parser(
        "define",
        help="Define the libvirt domain.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(define)
    start = sub.add_parser(
        "start",
        help="Start the libvirt domain.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(start)

    stop = sub.add_parser(
        "stop",
        help="Stop the libvirt domain.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(stop)
    stop.add_argument("--force", action="store_true", help="Use virsh destroy.")

    undefine = sub.add_parser(
        "undefine",
        help="Undefine the libvirt domain.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(undefine)
    undefine.add_argument(
        "--purge-artifacts",
        action="store_true",
        help="Delete overlay, seed ISO, and generated state after undefine.",
    )

    preflight = sub.add_parser(
        "preflight",
        help="Check the local host passthrough prerequisites.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(preflight)
    ips = sub.add_parser(
        "ips",
        help="Query guest IPs via qemu-guest-agent.",
        argument_default=argparse.SUPPRESS,
    )
    add_common_arguments(ips)
    return parser


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, indent=2)}")
        else:
            print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = make_config(args)
        action = str(args.action)
        prepared = prepare_config_for_action(config, action)
        if action == "render":
            payload = {"status": "rendered", **config_summary(prepared), **render_files(prepared)}
            emit(payload, as_json=bool(getattr(args, "json", False)))
            return 0
        if action == "create-overlay":
            payload = {"config": config_summary(prepared), **create_overlay(prepared)}
            emit(payload, as_json=bool(getattr(args, "json", False)))
            return 0
        if action == "create-seed":
            payload = {"config": config_summary(prepared), **create_seed_iso(prepared)}
            emit(payload, as_json=bool(getattr(args, "json", False)))
            return 0
        if action == "define":
            payload = {"config": config_summary(prepared), **define_domain(prepared)}
            emit(payload, as_json=bool(getattr(args, "json", False)))
            return 0
        if action == "start":
            payload = {"config": config_summary(prepared), **start_domain(prepared)}
            emit(payload, as_json=bool(getattr(args, "json", False)))
            return 0
        if action == "stop":
            payload = {"config": config_summary(prepared), **stop_domain(prepared, force=bool(getattr(args, "force", False)))}
            emit(payload, as_json=bool(getattr(args, "json", False)))
            return 0
        if action == "undefine":
            payload = {
                "config": config_summary(prepared),
                **undefine_domain(prepared, purge_artifacts=bool(getattr(args, "purge_artifacts", False))),
            }
            emit(payload, as_json=bool(getattr(args, "json", False)))
            return 0
        if action == "preflight":
            payload = preflight_report(prepared)
            emit(payload, as_json=bool(getattr(args, "json", False) or True))
            return 0
        if action == "ips":
            payload = query_guest_ips(prepared)
            emit(payload, as_json=bool(getattr(args, "json", False) or True))
            return 0
        raise SystemExit(f"unsupported action: {action}")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
