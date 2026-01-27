"""Storage configuration helpers for NetFS and provisioner registry."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


DEFAULT_NETFS_ROOT = Path("/var/lib/ae/netfs")
DEFAULT_CLASS_ANNOTATIONS = (
    "storageclass.kubernetes.io/is-default-class",
    "storageclass.beta.kubernetes.io/is-default-class",
)


def _env_path(env: Mapping[str, str], key: str) -> Path | None:
    raw = env.get(key)
    if not raw:
        return None
    return Path(raw)


@dataclass(slots=True)
class StorageConfig:
    """Resolved storage configuration derived from environment variables."""

    netfs_root: Path
    provisioners_path: Path | None
    default_class: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "StorageConfig":
        use_env = env if env is not None else os.environ
        root = _env_path(use_env, "AE_NETFS_ROOT") or DEFAULT_NETFS_ROOT
        provisioners = _env_path(use_env, "AE_STORAGE_PROVISIONERS")
        default_class = use_env.get("AE_STORAGE_DEFAULT_CLASS") or None
        return cls(netfs_root=root, provisioners_path=provisioners, default_class=default_class)


@dataclass(slots=True)
class StorageClassConfig:
    """StorageClass definition loaded from configuration."""

    name: str
    provisioner: str
    parameters: dict[str, str] = field(default_factory=dict)
    reclaim_policy: str | None = None
    volume_binding_mode: str | None = None
    allow_volume_expansion: bool | None = None
    mount_options: list[str] = field(default_factory=list)
    allowed_topologies: list[dict[str, Any]] = field(default_factory=list)
    topology_keys: list[str] = field(default_factory=list)
    is_default: bool = False


def _parse_storage_class(raw: Mapping[str, Any]) -> StorageClassConfig | None:
    if not raw:
        return None
    metadata = raw.get("metadata") if isinstance(raw, dict) else None
    name = None
    if isinstance(metadata, dict):
        name = metadata.get("name")
    if not name:
        name = raw.get("name")
    if not name:
        return None
    provisioner = raw.get("provisioner")
    if not provisioner:
        return None
    params = raw.get("parameters")
    parameters: dict[str, str] = {}
    if isinstance(params, dict):
        for k, v in params.items():
            if v is None:
                continue
            parameters[str(k)] = str(v)
    allowed_topologies = raw.get("allowedTopologies")
    if not isinstance(allowed_topologies, list):
        allowed_topologies = []
    topology_keys_raw = raw.get("topologyKeys")
    topology_keys: list[str] = []
    if isinstance(topology_keys_raw, list):
        topology_keys = [str(k) for k in topology_keys_raw if k]
    annotations = {}
    if isinstance(metadata, dict):
        annotations = metadata.get("annotations") or {}
    is_default = False
    if isinstance(annotations, dict):
        for key in DEFAULT_CLASS_ANNOTATIONS:
            raw_val = annotations.get(key)
            if raw_val is not None and str(raw_val).lower() in {"true", "1", "yes"}:
                is_default = True
                break
    return StorageClassConfig(
        name=str(name),
        provisioner=str(provisioner),
        parameters=parameters,
        reclaim_policy=raw.get("reclaimPolicy"),
        volume_binding_mode=raw.get("volumeBindingMode"),
        allow_volume_expansion=raw.get("allowVolumeExpansion"),
        mount_options=list(raw.get("mountOptions") or []),
        allowed_topologies=allowed_topologies,
        topology_keys=topology_keys,
        is_default=is_default,
    )


def load_storage_classes(path: Path | None) -> list[StorageClassConfig]:
    """Load StorageClass definitions from YAML."""

    if path is None or not path.exists():
        return []
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    if not docs:
        return []
    out: list[StorageClassConfig] = []
    for data in docs:
        if not data:
            continue
        items: list[Mapping[str, Any]] = []
        if isinstance(data, list):
            items = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            if isinstance(data.get("items"), list):
                items = [d for d in data.get("items") if isinstance(d, dict)]
            else:
                items = [data]
        for raw in items:
            if raw.get("kind") and str(raw.get("kind")) != "StorageClass":
                continue
            sc = _parse_storage_class(raw)
            if sc is not None:
                out.append(sc)
    return out


def select_default_class(storage_classes: list[StorageClassConfig]) -> StorageClassConfig | None:
    for sc in storage_classes:
        if sc.is_default:
            return sc
    return storage_classes[0] if storage_classes else None


def load_provisioners(path: Path | None) -> list[StorageClassConfig]:
    """Backward-compatible alias for storage class loading."""

    return load_storage_classes(path)
