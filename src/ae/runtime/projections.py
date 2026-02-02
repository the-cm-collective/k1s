"""Helpers for projecting K8s ConfigMap/Secret volumes into host paths."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ae.controller.spec import AppManifest, DEFAULT_NAMESPACE, VolumeSpec, app_key_for_manifest

LOGGER = logging.getLogger(__name__)


def ensure_k8s_volume_projections(
    manifest: AppManifest,
    revision: int,
    *,
    state: Any | None,
    logger: logging.Logger | None = None,
) -> AppManifest:
    sources = list(getattr(manifest.spec, "projection_sources", []) or [])
    if not sources:
        return manifest
    log = logger or LOGGER
    if state is None:
        log.warning("projection sources present but apishim storage state unavailable")
        return manifest

    app = app_key_for_manifest(manifest)
    mount_root = f"/var/run/ae/config/{app}"
    volumes = list(getattr(manifest.spec, "volumes", []) or [])
    host_root = _find_projection_root(volumes, mount_root)
    if host_root is None:
        base = Path(os.getenv("AE_PROJECTION_ROOT", "/var/lib/ae/projections"))
        host_root = base / f"{app}-rev{revision}"
        volumes.append(
            VolumeSpec(host_path=str(host_root), mount_path=mount_root, read_only=True)
        )
        updated_spec = manifest.spec.model_copy(update={"volumes": volumes})
        manifest = manifest.model_copy(update={"spec": updated_spec})
    try:
        host_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return manifest

    namespace = getattr(manifest.metadata, "namespace", None) or DEFAULT_NAMESPACE
    for src in sources:
        _write_projection_source(
            host_root, src, state, namespace=str(namespace), logger=log
        )
    return manifest


def _find_projection_root(volumes: list[Any], mount_root: str) -> Path | None:
    for v in volumes:
        mount_path = _spec_value(v, "mount_path", "mountPath")
        if not mount_path:
            continue
        if not str(mount_path).startswith(mount_root):
            continue
        host_path = _spec_value(v, "host_path", "hostPath")
        if not host_path:
            continue
        return Path(str(host_path))
    return None


def _write_projection_source(
    host_root: Path,
    src: Any,
    state: Any,
    *,
    namespace: str,
    logger: logging.Logger,
) -> None:
    vol_name = _spec_value(src, "name")
    src_type = _spec_value(src, "source_type", "type")
    src_name = _spec_value(src, "source_name", "sourceName")
    if not vol_name or not src_type or not src_name:
        return
    src_ns = _spec_value(src, "namespace") or namespace
    optional = bool(_spec_value(src, "optional") or False)
    default_mode = _parse_mode(_spec_value(src, "default_mode", "defaultMode"))
    base_dir = host_root / "k8s" / "volumes" / str(vol_name)
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    data = None
    if src_type == "configMap":
        data = state.get_config_map(str(src_ns), str(src_name))
    elif src_type == "secret":
        data = state.get_secret(str(src_ns), str(src_name))
    if data is None:
        if not optional:
            logger.warning(
                "projection source %s/%s not found", str(src_type), str(src_name)
            )
        return
    if not isinstance(data, dict):
        return

    items = list(_spec_value(src, "items") or [])
    if items:
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not key:
                continue
            if key not in data:
                if not optional:
                    logger.warning(
                        "projection key %s missing in %s/%s",
                        str(key),
                        str(src_type),
                        str(src_name),
                    )
                continue
            rel_path = item.get("path") or key
            mode = _parse_mode(item.get("mode"), default=default_mode)
            _write_projection_file(base_dir, rel_path, data.get(key), mode, logger)
        return

    for key, value in data.items():
        _write_projection_file(base_dir, str(key), value, default_mode, logger)


def _write_projection_file(
    base_dir: Path,
    rel_path: Any,
    value: Any,
    mode: int,
    logger: logging.Logger,
) -> None:
    rel = Path(str(rel_path))
    if rel.is_absolute() or ".." in rel.parts:
        logger.warning("projection path rejected: %s", rel)
        return
    dest = base_dir / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("" if value is None else str(value), encoding="utf-8")
        if mode is not None:
            os.chmod(dest, int(mode))
    except Exception:
        return


def _parse_mode(value: Any, default: int | None = 0o644) -> int:
    if value is None:
        return default if default is not None else 0o644
    if isinstance(value, int):
        return value
    try:
        raw = str(value).strip()
        if not raw:
            return default if default is not None else 0o644
        if raw.startswith("0"):
            return int(raw, 8)
        return int(raw)
    except Exception:
        return default if default is not None else 0o644


def _spec_value(obj: Any, *names: str) -> Any:
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None
