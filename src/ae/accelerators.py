"""Typed accelerator capability helpers with gpu.* compatibility projection."""

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


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        item = _normalize_string(value)
        if item:
            out.append(item)
    return out


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
    kind = _normalize_string(item.get("kind")) or "discrete_gpu"
    device_count = _normalize_int(item.get("device_count"))
    if device_count is None:
        device_count = 1
    item["id"] = _normalize_string(item.get("id")) or default_id
    item["kind"] = kind
    item["vendor"] = _normalize_string(item.get("vendor"))
    item["family"] = _normalize_string(item.get("family")) or _normalize_string(item.get("model"))
    item["architecture"] = _normalize_string(item.get("architecture"))
    item["device_count"] = max(0, int(device_count))
    item["memory_model"] = _normalize_string(item.get("memory_model")) or _default_memory_model(kind)
    memory_bytes = _normalize_int(item.get("memory_bytes_per_device"))
    item["memory_bytes_per_device"] = max(0, int(memory_bytes)) if memory_bytes is not None else None
    item["runtime_handlers"] = _normalize_string_list(item.get("runtime_handlers"))
    item["partitioning_mode"] = (
        _normalize_string(item.get("partitioning_mode")) or _default_partitioning_mode(kind)
    )
    item["backing_device_id"] = _normalize_string(item.get("backing_device_id"))
    item["execution_role"] = _normalize_string(item.get("execution_role")) or "execution"
    return item


def normalize_capabilities(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    if "accelerators" in raw:
        accelerators = raw.get("accelerators")
        items: list[dict[str, Any]] = []
        if isinstance(accelerators, list):
            for index, accelerator in enumerate(accelerators):
                item = normalize_accelerator(accelerator, default_id=f"accelerator-{index}")
                if item is not None:
                    items.append(item)
        out["accelerators"] = items
    return out


def has_accelerator_inventory(capabilities: Any) -> bool:
    return isinstance(capabilities, dict) and isinstance(capabilities.get("accelerators"), list)


def accelerator_inventory(capabilities: Any) -> list[dict[str, Any]]:
    if not has_accelerator_inventory(capabilities):
        return []
    items = capabilities.get("accelerators") or []
    return [item for item in items if isinstance(item, dict)]


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
        except Exception:
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
