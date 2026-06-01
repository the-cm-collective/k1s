"""Typed node capability helpers with gpu.* compatibility projection."""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
from typing import Any

EXECUTION_ACCELERATOR_ROLES = frozenset({"execution", "mixed"})
GPU_COMPAT_LABELS = ("gpu.present", "gpu.count", "gpu.models")


def _truthy_env(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalize_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (float, int)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalize_string_list(values: Any) -> list[str]:
    if isinstance(values, str):
        item = _normalize_string(values)
        return [item] if item else []
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        item = _normalize_string(value)
        if item:
            out.append(item)
    return out


def _get_alias(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item:
            return item.get(key)
    return None


def _drop_aliases(item: dict[str, Any], *keys: str) -> None:
    for key in keys:
        item.pop(key, None)


def _default_memory_model(kind: str | None) -> str:
    if kind == "apu":
        return "unified"
    if kind == "virtual_gpu":
        return "partitioned"
    return "dedicated"


def _default_partitioning_mode(kind: str | None) -> str:
    if kind == "virtual_gpu":
        return "vgpu"
    return "none"


def normalize_accelerator(raw: Any, *, default_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = dict(raw)
    kind = _normalize_string(_get_alias(item, "kind")) or "discrete_gpu"
    device_count = _normalize_int(_get_alias(item, "device_count", "deviceCount"))
    if device_count is None:
        device_count = 1
    item["id"] = _normalize_string(_get_alias(item, "id")) or default_id
    item["kind"] = kind
    item["vendor"] = _normalize_string(_get_alias(item, "vendor"))
    item["family"] = _normalize_string(_get_alias(item, "family")) or _normalize_string(
        _get_alias(item, "model")
    )
    item["architecture"] = _normalize_string(_get_alias(item, "architecture"))
    item["device_count"] = max(0, int(device_count))
    item["memory_model"] = _normalize_string(
        _get_alias(item, "memory_model", "memoryModel")
    ) or _default_memory_model(kind)
    memory_bytes = _normalize_int(
        _get_alias(item, "memory_bytes_per_device", "memoryBytesPerDevice")
    )
    item["memory_bytes_per_device"] = (
        max(0, int(memory_bytes)) if memory_bytes is not None else None
    )
    item["runtime_handlers"] = _normalize_string_list(
        _get_alias(item, "runtime_handlers", "runtimeHandlers")
    )
    item["partitioning_mode"] = (
        _normalize_string(_get_alias(item, "partitioning_mode", "partitioningMode"))
        or _default_partitioning_mode(kind)
    )
    item["backing_device_id"] = _normalize_string(
        _get_alias(item, "backing_device_id", "backingDeviceId")
    )
    item["execution_role"] = (
        _normalize_string(_get_alias(item, "execution_role", "executionRole")) or "execution"
    )
    _drop_aliases(
        item,
        "deviceCount",
        "memoryModel",
        "memoryBytesPerDevice",
        "runtimeHandlers",
        "partitioningMode",
        "backingDeviceId",
        "executionRole",
    )
    return item


def normalize_storage_device(raw: Any, *, default_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = dict(raw)
    item["id"] = _normalize_string(_get_alias(item, "id", "name")) or default_id
    item["kind"] = _normalize_string(_get_alias(item, "kind", "type")) or "block"
    item["medium"] = _normalize_string(_get_alias(item, "medium", "media", "mediaType"))
    item["vendor"] = _normalize_string(_get_alias(item, "vendor"))
    item["model"] = _normalize_string(_get_alias(item, "model"))
    item["serial"] = _normalize_string(_get_alias(item, "serial"))
    size_bytes = _normalize_int(
        _get_alias(item, "size_bytes", "sizeBytes", "capacity_bytes", "capacityBytes")
    )
    item["size_bytes"] = max(0, int(size_bytes)) if size_bytes is not None else None
    item["transport"] = _normalize_string(_get_alias(item, "transport", "bus"))
    item["interface"] = _normalize_string(_get_alias(item, "interface"))
    item["device_path"] = _normalize_string(
        _get_alias(item, "device_path", "devicePath", "path")
    )
    item["mount_path"] = _normalize_string(_get_alias(item, "mount_path", "mountPath"))
    item["filesystem"] = _normalize_string(_get_alias(item, "filesystem", "fsType"))
    item["roles"] = _normalize_string_list(_get_alias(item, "roles", "role"))
    item["numa_node"] = _normalize_int(_get_alias(item, "numa_node", "numaNode"))
    _drop_aliases(
        item,
        "mediaType",
        "sizeBytes",
        "capacityBytes",
        "devicePath",
        "mountPath",
        "fsType",
        "numaNode",
    )
    return item


def normalize_link_metric(raw: Any, *, default_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = dict(raw)
    from_site = _normalize_string(_get_alias(item, "from_site", "fromSite"))
    to_site = _normalize_string(_get_alias(item, "to_site", "toSite"))
    rtt = _normalize_float(_get_alias(item, "rtt_p95_ms", "rttP95Ms"))
    if not from_site or not to_site or rtt is None:
        return None
    jitter = _normalize_float(_get_alias(item, "jitter_p95_ms", "jitterP95Ms"))
    loss = _normalize_float(_get_alias(item, "loss_pct", "lossPct"))
    item["id"] = _normalize_string(_get_alias(item, "id")) or default_id
    item["from_site"] = from_site
    item["to_site"] = to_site
    item["rtt_p95_ms"] = max(0.0, float(rtt))
    item["jitter_p95_ms"] = max(0.0, float(jitter)) if jitter is not None else 0.0
    item["loss_pct"] = max(0.0, float(loss)) if loss is not None else 0.0
    item["source"] = _normalize_string(_get_alias(item, "source")) or "node-capability"
    item["observed_at"] = _normalize_string(_get_alias(item, "observed_at", "observedAt"))
    _drop_aliases(
        item,
        "fromSite",
        "toSite",
        "rttP95Ms",
        "jitterP95Ms",
        "lossPct",
        "observedAt",
    )
    return item


def normalize_network_interface(raw: Any, *, default_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = dict(raw)
    item["id"] = _normalize_string(_get_alias(item, "id", "name")) or default_id
    item["name"] = _normalize_string(_get_alias(item, "name")) or item["id"]
    item["mac"] = _normalize_string(_get_alias(item, "mac", "macAddress"))
    addresses = _get_alias(item, "addresses", "ips", "ip_addresses", "ipAddresses")
    item["addresses"] = _normalize_string_list(addresses)
    item["mtu"] = _normalize_int(_get_alias(item, "mtu"))
    item["speed_mbps"] = _normalize_int(_get_alias(item, "speed_mbps", "speedMbps"))
    item["duplex"] = _normalize_string(_get_alias(item, "duplex"))
    item["driver"] = _normalize_string(_get_alias(item, "driver"))
    item["bus_id"] = _normalize_string(
        _get_alias(item, "bus_id", "busId", "pci_bus_id", "pciBusId")
    )
    item["roles"] = _normalize_string_list(_get_alias(item, "roles", "role"))
    item["site_id"] = _normalize_string(_get_alias(item, "site_id", "siteId"))
    item["fabric_id"] = _normalize_string(_get_alias(item, "fabric_id", "fabricId"))
    item["link_peer"] = _normalize_string(_get_alias(item, "link_peer", "linkPeer", "peer"))
    metrics = _get_alias(item, "link_metrics", "linkMetrics")
    link_metrics: list[dict[str, Any]] = []
    if isinstance(metrics, list):
        for index, metric in enumerate(metrics):
            normalized = normalize_link_metric(metric, default_id=f"{item['id']}-link-{index}")
            if normalized is not None:
                link_metrics.append(normalized)
    item["link_metrics"] = link_metrics
    _drop_aliases(
        item,
        "macAddress",
        "ipAddresses",
        "speedMbps",
        "busId",
        "pciBusId",
        "siteId",
        "fabricId",
        "linkPeer",
        "linkMetrics",
    )
    return item


def _normalize_pcie_state(raw: Any, item: dict[str, Any]) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    bus_id = _normalize_string(
        _get_alias(source, "bus_id", "busId", "pci_bus_id", "pciBusId")
    ) or _normalize_string(
        _get_alias(item, "pcie_bus_id", "pcieBusId", "pci_bus_id", "pciBusId")
    )
    width = _normalize_int(
        _get_alias(source, "link_width", "linkWidth")
    ) or _normalize_int(_get_alias(item, "pcie_link_width", "pcieLinkWidth"))
    speed = _normalize_float(
        _get_alias(source, "link_speed_gts", "linkSpeedGTs")
    ) or _normalize_float(_get_alias(item, "pcie_link_speed_gts", "pcieLinkSpeedGTs"))
    return {
        "bus_id": bus_id,
        "link_width": width,
        "link_speed_gts": speed,
        "numa_node": _normalize_int(_get_alias(source, "numa_node", "numaNode"))
        or _normalize_int(_get_alias(item, "numa_node", "numaNode")),
    }


def normalize_rdma_device(raw: Any, *, default_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = dict(raw)
    item["id"] = _normalize_string(_get_alias(item, "id", "name")) or default_id
    item["name"] = _normalize_string(_get_alias(item, "name")) or item["id"]
    item["kind"] = _normalize_string(_get_alias(item, "kind", "type")) or "rnic"
    item["vendor"] = _normalize_string(_get_alias(item, "vendor"))
    item["model"] = _normalize_string(_get_alias(item, "model"))
    item["driver"] = _normalize_string(_get_alias(item, "driver"))
    item["firmware"] = _normalize_string(_get_alias(item, "firmware", "fwVersion"))
    item["netdev"] = _normalize_string(_get_alias(item, "netdev", "netDevice", "interface"))
    item["state"] = _normalize_string(_get_alias(item, "state")) or "unknown"
    item["roles"] = _normalize_string_list(_get_alias(item, "roles", "role"))
    item["rdma_protocols"] = _normalize_string_list(
        _get_alias(item, "rdma_protocols", "rdmaProtocols", "protocols")
    )
    item["pcie"] = _normalize_pcie_state(_get_alias(item, "pcie", "pci"), item)
    _drop_aliases(
        item,
        "fwVersion",
        "netDevice",
        "rdmaProtocols",
        "pcieBusId",
        "pciBusId",
        "pcieLinkWidth",
        "pcieLinkSpeedGTs",
        "numaNode",
    )
    return item


def normalize_identity_roles(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for role, value in raw.items():
        role_name = _normalize_string(role)
        if not role_name:
            continue
        role_key = role_name.strip().lower().replace("-", "_")
        if isinstance(value, dict):
            item = dict(value)
            identity = (
                _normalize_string(_get_alias(item, "id"))
                or _normalize_string(_get_alias(item, "principal"))
                or _normalize_string(_get_alias(item, "subject"))
            )
            if not identity:
                continue
            item["id"] = identity
            item["kind"] = _normalize_string(_get_alias(item, "kind", "type")) or "identity"
            item["principal"] = _normalize_string(_get_alias(item, "principal")) or identity
            item["issuer"] = _normalize_string(_get_alias(item, "issuer"))
            item["scope"] = _normalize_string(_get_alias(item, "scope"))
            item["certificate_subject"] = _normalize_string(
                _get_alias(item, "certificate_subject", "certificateSubject")
            )
            _drop_aliases(item, "certificateSubject")
            out[role_key] = item
            continue
        identity = _normalize_string(value)
        if identity:
            out[role_key] = {
                "id": identity,
                "kind": "identity",
                "principal": identity,
                "issuer": None,
                "scope": None,
                "certificate_subject": None,
            }
    return out


def normalize_capabilities(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    accelerators = _get_alias(raw, "accelerators")
    if accelerators is not None:
        items: list[dict[str, Any]] = []
        if isinstance(accelerators, list):
            for index, accelerator in enumerate(accelerators):
                item = normalize_accelerator(accelerator, default_id=f"accelerator-{index}")
                if item is not None:
                    items.append(item)
        out["accelerators"] = items

    storage_devices = _get_alias(raw, "storage_devices", "storageDevices")
    if storage_devices is not None:
        out.pop("storageDevices", None)
        items = []
        if isinstance(storage_devices, list):
            for index, device in enumerate(storage_devices):
                item = normalize_storage_device(device, default_id=f"storage-{index}")
                if item is not None:
                    items.append(item)
        out["storage_devices"] = items

    network_interfaces = _get_alias(raw, "network_interfaces", "networkInterfaces")
    if network_interfaces is not None:
        out.pop("networkInterfaces", None)
        items = []
        if isinstance(network_interfaces, list):
            for index, interface in enumerate(network_interfaces):
                item = normalize_network_interface(interface, default_id=f"net-{index}")
                if item is not None:
                    items.append(item)
        out["network_interfaces"] = items

    rdma_devices = _get_alias(raw, "rdma_devices", "rdmaDevices")
    if rdma_devices is not None:
        out.pop("rdmaDevices", None)
        items = []
        if isinstance(rdma_devices, list):
            for index, device in enumerate(rdma_devices):
                item = normalize_rdma_device(device, default_id=f"rdma-{index}")
                if item is not None:
                    items.append(item)
        out["rdma_devices"] = items

    link_metrics = _get_alias(raw, "link_metrics", "linkMetrics")
    if link_metrics is not None:
        out.pop("linkMetrics", None)
        items = []
        if isinstance(link_metrics, list):
            for index, metric in enumerate(link_metrics):
                item = normalize_link_metric(metric, default_id=f"link-{index}")
                if item is not None:
                    items.append(item)
        out["link_metrics"] = items

    identity_roles = _get_alias(raw, "identity_roles", "identityRoles")
    if identity_roles is not None:
        out.pop("identityRoles", None)
        out["identity_roles"] = normalize_identity_roles(identity_roles)
    return out


def has_accelerator_inventory(capabilities: Any) -> bool:
    return isinstance(capabilities, dict) and isinstance(capabilities.get("accelerators"), list)


def accelerator_inventory(capabilities: Any) -> list[dict[str, Any]]:
    if not has_accelerator_inventory(capabilities):
        return []
    items = capabilities.get("accelerators") or []
    return [item for item in items if isinstance(item, dict)]


def storage_device_inventory(capabilities: Any) -> list[dict[str, Any]]:
    if not isinstance(capabilities, dict):
        return []
    items = capabilities.get("storage_devices") or []
    return [item for item in items if isinstance(item, dict)]


def network_interface_inventory(capabilities: Any) -> list[dict[str, Any]]:
    if not isinstance(capabilities, dict):
        return []
    items = capabilities.get("network_interfaces") or []
    return [item for item in items if isinstance(item, dict)]


def rdma_device_inventory(capabilities: Any) -> list[dict[str, Any]]:
    if not isinstance(capabilities, dict):
        return []
    items = capabilities.get("rdma_devices") or []
    return [item for item in items if isinstance(item, dict)]


def link_metric_inventory(capabilities: Any) -> list[dict[str, Any]]:
    if not isinstance(capabilities, dict):
        return []
    out = [item for item in capabilities.get("link_metrics") or [] if isinstance(item, dict)]
    for interface in network_interface_inventory(capabilities):
        out.extend(
            item for item in interface.get("link_metrics") or [] if isinstance(item, dict)
        )
    return out


def identity_role_inventory(capabilities: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(capabilities, dict):
        return {}
    roles = capabilities.get("identity_roles") or {}
    if not isinstance(roles, dict):
        return {}
    return {str(role): item for role, item in roles.items() if isinstance(item, dict)}


def has_typed_storage_media(capabilities: Any) -> bool:
    return bool(storage_device_inventory(capabilities))


def has_typed_link_topology(capabilities: Any) -> bool:
    return bool(network_interface_inventory(capabilities) or link_metric_inventory(capabilities))


def has_typed_rnic_rdma(capabilities: Any) -> bool:
    return bool(rdma_device_inventory(capabilities))


def has_identity_role_separation(capabilities: Any) -> bool:
    roles = identity_role_inventory(capabilities)
    required = ("management", "execution", "fabric")
    identities = []
    for role in required:
        item = roles.get(role)
        if not item:
            return False
        identity = _normalize_string(item.get("id")) or _normalize_string(item.get("principal"))
        if not identity:
            return False
        identities.append(identity)
    return len(set(identities)) == len(required)


def execution_accelerators(capabilities: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in accelerator_inventory(capabilities):
        role = str(item.get("execution_role") or "").strip().lower()
        if role not in EXECUTION_ACCELERATOR_ROLES:
            continue
        try:
            count = int(item.get("device_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        out.append(item)
    return out


def execution_accelerator_count(capabilities: Any) -> int:
    total = 0
    for item in execution_accelerators(capabilities):
        try:
            total += int(item.get("device_count") or 0)
        except (TypeError, ValueError):
            continue
    return total


def execution_accelerator_models(capabilities: Any) -> list[str]:
    models: set[str] = set()
    for item in execution_accelerators(capabilities):
        family = _normalize_string(item.get("family")) or _normalize_string(item.get("model"))
        if family:
            models.add(family)
    return sorted(models)


def project_gpu_labels(capabilities: Any) -> dict[str, str]:
    if not has_accelerator_inventory(capabilities):
        return {}
    gpu_count = execution_accelerator_count(capabilities)
    labels = {
        "gpu.present": "true" if gpu_count > 0 else "false",
        "gpu.count": str(gpu_count),
    }
    models = execution_accelerator_models(capabilities)
    if models:
        labels["gpu.models"] = ",".join(models)
    return labels


def merge_projected_gpu_labels(labels: dict[str, Any] | None, capabilities: Any) -> dict[str, str]:
    merged = {str(k): str(v) for k, v in dict(labels or {}).items()}
    if not has_accelerator_inventory(capabilities):
        return merged
    for key in GPU_COMPAT_LABELS:
        merged.pop(key, None)
    merged.update(project_gpu_labels(capabilities))
    return merged


def gpu_count_from_labels(labels: dict[str, Any] | None) -> int | None:
    if not labels:
        return None
    for key in ("gpu.count", "nvidia.gpu.count", "gpu_count"):
        raw = labels.get(key)
        if raw in (None, ""):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def preferred_gpu_count(labels: dict[str, Any] | None, capabilities: Any) -> int | None:
    if has_accelerator_inventory(capabilities):
        return execution_accelerator_count(capabilities)
    return gpu_count_from_labels(labels)


def preferred_gpu_models(labels: dict[str, Any] | None, capabilities: Any) -> str | None:
    if has_accelerator_inventory(capabilities):
        models = execution_accelerator_models(capabilities)
        return ",".join(models) if models else None
    if not labels:
        return None
    value = labels.get("gpu.models")
    if value in (None, ""):
        return None
    return str(value)


def _query_nvidia_smi(bin_name: str, fields: list[str]) -> list[list[str]]:
    output = subprocess.check_output(  # noqa: S603
        [
            bin_name,
            f"--query-gpu={','.join(fields)}",
            "--format=csv,noheader,nounits",
        ],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=3,
    )
    rows: list[list[str]] = []
    reader = csv.reader(io.StringIO(output))
    for row in reader:
        cleaned = [str(item or "").strip() for item in row]
        if any(cleaned):
            rows.append(cleaned)
    return rows


def _parse_memory_mebibytes(raw: str | None) -> int | None:
    text = _normalize_string(raw)
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def detect_nvidia_accelerator_capabilities(*, smi_bin: str | None = None) -> dict[str, Any]:
    if _truthy_env("AE_GPU_DISCOVERY_DISABLE"):
        return {}
    bin_name = str(smi_bin or os.getenv("AE_NVIDIA_SMI_BIN", "nvidia-smi")).strip() or "nvidia-smi"
    attempts = [
        ["index", "uuid", "name", "architecture", "memory.total"],
        ["index", "uuid", "name", "memory.total"],
        ["index", "name", "memory.total"],
    ]
    rows: list[list[str]] | None = None
    fields: list[str] = []
    for attempt in attempts:
        try:
            rows = _query_nvidia_smi(bin_name, attempt)
            fields = attempt
            break
        except Exception:  # noqa: S112
            continue
    if rows is None:
        return {}
    if not rows:
        return {"accelerators": []}

    accelerators: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        values = dict(zip(fields, row, strict=False))
        family = _normalize_string(values.get("name"))
        architecture = _normalize_string(values.get("architecture"))
        if architecture == "N/A":
            architecture = None
        memory_mib = _parse_memory_mebibytes(values.get("memory.total"))
        memory_bytes = memory_mib * 1024 * 1024 if memory_mib is not None else None
        gpu_index = _normalize_string(values.get("index")) or str(row_index)
        gpu_uuid = _normalize_string(values.get("uuid"))
        accelerators.append(
            normalize_accelerator(
                {
                    "id": gpu_uuid or f"nvidia-gpu-{gpu_index}",
                    "kind": "discrete_gpu",
                    "vendor": "nvidia",
                    "family": family,
                    "architecture": architecture,
                    "device_count": 1,
                    "memory_model": "dedicated",
                    "memory_bytes_per_device": memory_bytes,
                    "runtime_handlers": ["nvidia"],
                    "partitioning_mode": "none",
                    "backing_device_id": None,
                    "execution_role": "execution",
                },
                default_id=f"nvidia-gpu-{gpu_index}",
            )
            or {
                "id": gpu_uuid or f"nvidia-gpu-{gpu_index}",
                "kind": "discrete_gpu",
                "vendor": "nvidia",
                "family": family,
                "architecture": architecture,
                "device_count": 1,
                "memory_model": "dedicated",
                "memory_bytes_per_device": memory_bytes,
                "runtime_handlers": ["nvidia"],
                "partitioning_mode": "none",
                "backing_device_id": None,
                "execution_role": "execution",
            }
        )
    return {"accelerators": accelerators}
