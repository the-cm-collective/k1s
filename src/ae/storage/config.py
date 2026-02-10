"""Storage configuration helpers for NetFS and provisioner registry."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    quotas_path: Path | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> StorageConfig:
        use_env = env if env is not None else os.environ
        root = _env_path(use_env, "AE_NETFS_ROOT") or DEFAULT_NETFS_ROOT
        provisioners = _env_path(use_env, "AE_STORAGE_PROVISIONERS")
        default_class = use_env.get("AE_STORAGE_DEFAULT_CLASS") or None
        quotas = _env_path(use_env, "AE_STORAGE_QUOTAS")
        return cls(
            netfs_root=root,
            provisioners_path=provisioners,
            default_class=default_class,
            quotas_path=quotas,
        )


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


@dataclass(slots=True)
class StorageProvisionerConfig:
    """Storage provisioner registry entry (built-in or CSI)."""

    name: str
    provisioner: str
    type: str
    controller_endpoint: str | None = None
    node_endpoint: str | None = None
    parameters: dict[str, str] = field(default_factory=dict)
    access_modes: list[str] = field(default_factory=list)
    volume_binding_mode: str | None = None
    reclaim_policy: str | None = None
    allow_volume_expansion: bool | None = None
    mount_options: list[str] = field(default_factory=list)
    allowed_topologies: list[dict[str, Any]] = field(default_factory=list)
    topology_keys: list[str] = field(default_factory=list)
    is_default: bool = False


@dataclass(slots=True)
class StorageProvisionerRegistry:
    """Lookup registry for provisioner entries."""

    provisioners: list[StorageProvisionerConfig]
    _by_name: dict[str, StorageProvisionerConfig] = field(init=False, repr=False)
    _by_provisioner: dict[str, StorageProvisionerConfig] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_name = {}
        self._by_provisioner = {}
        for entry in self.provisioners:
            if entry.name and entry.name not in self._by_name:
                self._by_name[entry.name] = entry
            if entry.provisioner and entry.provisioner not in self._by_provisioner:
                self._by_provisioner[entry.provisioner] = entry

    def for_storage_class(self, name: str | None) -> StorageProvisionerConfig | None:
        if not name:
            return None
        return self._by_name.get(name)

    def for_driver(self, driver: str | None) -> StorageProvisionerConfig | None:
        if not driver:
            return None
        return self._by_provisioner.get(driver)

    def is_csi(self, *, storage_class: str | None = None, driver: str | None = None) -> bool:
        entry = self.for_storage_class(storage_class) or self.for_driver(driver)
        return bool(entry and entry.type == "csi")

@dataclass(slots=True)
class StorageQuotaConfig:
    """Namespace-scoped storage quota."""

    namespace: str
    hard_storage: str


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


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw if item is not None and str(item).strip()]
    return [str(raw)]


def _parse_provisioner_entry(raw: Mapping[str, Any]) -> StorageProvisionerConfig | None:
    if not raw:
        return None
    name = raw.get("name")
    provisioner = raw.get("provisioner")
    if not name or not provisioner:
        return None
    params = raw.get("parameters")
    parameters: dict[str, str] = {}
    if isinstance(params, dict):
        for key, value in params.items():
            if value is None:
                continue
            parameters[str(key)] = str(value)
    allowed_topologies = raw.get("allowedTopologies")
    if not isinstance(allowed_topologies, list):
        allowed_topologies = []
    topology_keys = _normalize_string_list(raw.get("topologyKeys"))
    mount_options = _normalize_string_list(raw.get("mountOptions"))
    access_modes = _normalize_string_list(raw.get("accessModes"))
    entry_type = str(raw.get("type") or "").strip().lower()
    if not entry_type:
        entry_type = "builtin" if str(provisioner).startswith("k1s.io/") else "csi"
    endpoint = raw.get("endpoint")
    controller_endpoint = raw.get("controllerEndpoint") or endpoint
    node_endpoint = raw.get("nodeEndpoint") or endpoint
    is_default = _parse_bool(
        raw.get("isDefault")
        or raw.get("default")
        or raw.get("defaultClass")
        or raw.get("is_default")
    )
    return StorageProvisionerConfig(
        name=str(name),
        provisioner=str(provisioner),
        type=entry_type,
        controller_endpoint=str(controller_endpoint) if controller_endpoint else None,
        node_endpoint=str(node_endpoint) if node_endpoint else None,
        parameters=parameters,
        access_modes=access_modes,
        volume_binding_mode=raw.get("volumeBindingMode"),
        reclaim_policy=raw.get("reclaimPolicy"),
        allow_volume_expansion=raw.get("allowVolumeExpansion"),
        mount_options=mount_options,
        allowed_topologies=allowed_topologies,
        topology_keys=topology_keys,
        is_default=is_default,
    )


def _storage_class_from_provisioner(
    entry: StorageProvisionerConfig,
) -> StorageClassConfig:
    return StorageClassConfig(
        name=entry.name,
        provisioner=entry.provisioner,
        parameters=entry.parameters,
        reclaim_policy=entry.reclaim_policy,
        volume_binding_mode=entry.volume_binding_mode,
        allow_volume_expansion=entry.allow_volume_expansion,
        mount_options=entry.mount_options,
        allowed_topologies=entry.allowed_topologies,
        topology_keys=entry.topology_keys,
        is_default=entry.is_default,
    )


def _parse_storage_quota(raw: Mapping[str, Any]) -> StorageQuotaConfig | None:
    if not raw:
        return None
    metadata = raw.get("metadata") if isinstance(raw, dict) else None
    spec = raw.get("spec") if isinstance(raw, dict) else None
    if not isinstance(spec, dict):
        spec = raw if isinstance(raw, dict) else {}
    namespace = (
        spec.get("namespace")
        or (metadata.get("namespace") if isinstance(metadata, dict) else None)
        or (metadata.get("name") if isinstance(metadata, dict) else None)
        or raw.get("namespace")
    )
    if not namespace:
        return None
    hard_storage = None
    hard = spec.get("hard") if isinstance(spec, dict) else None
    if isinstance(hard, dict):
        hard_storage = (
            hard.get("requests.storage")
            or hard.get("requests.storage")
            or hard.get("storage")
        )
    elif hard:
        hard_storage = hard
    if hard_storage is None:
        hard_storage = spec.get("storage") or spec.get("requests.storage")
    if hard_storage is None:
        return None
    return StorageQuotaConfig(namespace=str(namespace), hard_storage=str(hard_storage))


def _dedupe_storage_classes(items: list[StorageClassConfig]) -> list[StorageClassConfig]:
    seen: set[str] = set()
    out: list[StorageClassConfig] = []
    for sc in items:
        if sc.name in seen:
            continue
        seen.add(sc.name)
        out.append(sc)
    return out


def load_storage_registry(
    path: Path | None,
) -> tuple[list[StorageClassConfig], StorageProvisionerRegistry]:
    """Load StorageClasses and provisioner registry from YAML."""

    if path is None or not path.exists():
        return [], StorageProvisionerRegistry([])
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    if not docs:
        return [], StorageProvisionerRegistry([])

    classes: list[StorageClassConfig] = []
    provisioners: list[StorageProvisionerConfig] = []
    for data in docs:
        if not data:
            continue
        if isinstance(data, dict) and isinstance(data.get("provisioners"), list):
            for raw in data.get("provisioners") or []:
                if not isinstance(raw, dict):
                    continue
                entry = _parse_provisioner_entry(raw)
                if entry is None:
                    continue
                provisioners.append(entry)
                classes.append(_storage_class_from_provisioner(entry))
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
                classes.append(sc)
    return _dedupe_storage_classes(classes), StorageProvisionerRegistry(provisioners)


def load_storage_classes(path: Path | None) -> list[StorageClassConfig]:
    """Load StorageClass definitions from YAML."""

    classes, _registry = load_storage_registry(path)
    return classes


def load_storage_quotas(path: Path | None) -> list[StorageQuotaConfig]:
    """Load namespace storage quotas from YAML."""

    if path is None or not path.exists():
        return []
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    if not docs:
        return []
    out: list[StorageQuotaConfig] = []
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
            if raw.get("kind") and str(raw.get("kind")) not in {"StorageQuota", "ResourceQuota"}:
                continue
            quota = _parse_storage_quota(raw)
            if quota is not None:
                out.append(quota)
    return out


def select_default_class(storage_classes: list[StorageClassConfig]) -> StorageClassConfig | None:
    for sc in storage_classes:
        if sc.is_default:
            return sc
    return storage_classes[0] if storage_classes else None


def load_provisioners(path: Path | None) -> list[StorageClassConfig]:
    """Backward-compatible alias for storage class loading."""

    return load_storage_classes(path)


def load_storage_provisioner_registry(path: Path | None) -> StorageProvisionerRegistry:
    """Load provisioner registry entries (built-in + CSI)."""

    _classes, registry = load_storage_registry(path)
    return registry


def load_storage_provisioners(path: Path | None) -> list[StorageProvisionerConfig]:
    """Load provisioner entries as a list."""

    return load_storage_provisioner_registry(path).provisioners
