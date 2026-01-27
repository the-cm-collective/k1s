"""Node-side volume manager for NetFS-backed PVC mounts."""

from __future__ import annotations

import os
import socket
from typing import Iterable

from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    PvcMountSpec,
    VolumeDeviceSpec,
    VolumeSpec,
)

from .netfs import NetFSManager
from .types import NetFSMount, PvcRef


class NodeVolumeManager:
    """Resolve PVC mounts into hostPath volumes backed by NetFS."""

    def __init__(
        self,
        netfs: NetFSManager,
        *,
        node_id: str | None = None,
        default_namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        self._netfs = netfs
        self._node_id = node_id or os.getenv("AE_NODE_ID") or socket.gethostname()
        self._default_ns = default_namespace

    def inject_pvc_mounts(
        self, manifest: AppManifest, *, node_id: str | None = None
    ) -> AppManifest:
        """Ensure PVC mounts are present and inject hostPath volumes into the manifest."""

        pvc_mounts = list(getattr(manifest.spec, "pvc_mounts", []) or [])
        if not pvc_mounts:
            return manifest

        node = node_id or self._node_id
        ns = getattr(manifest.metadata, "namespace", None) or self._default_ns

        fs_group = None
        selinux = None
        if getattr(manifest.spec, "pod_security", None):
            fs_group = getattr(manifest.spec.pod_security, "fs_group", None)
            selinux = {}
            if getattr(manifest.spec.pod_security, "selinux_user", None):
                selinux["user"] = str(manifest.spec.pod_security.selinux_user)
            if getattr(manifest.spec.pod_security, "selinux_role", None):
                selinux["role"] = str(manifest.spec.pod_security.selinux_role)
            if getattr(manifest.spec.pod_security, "selinux_type", None):
                selinux["type"] = str(manifest.spec.pod_security.selinux_type)
            if getattr(manifest.spec.pod_security, "selinux_level", None):
                selinux["level"] = str(manifest.spec.pod_security.selinux_level)
            if not selinux:
                selinux = None

        mounts_by_pvc: dict[PvcRef, NetFSMount] = {}
        for pm in pvc_mounts:
            pvc = self._pvc_ref(pm, namespace=ns)
            if pvc in mounts_by_pvc:
                continue
            wants_device = bool(getattr(pm, "device_path", None))
            mount = self._netfs.ensure_mount(
                pvc,
                node_id=node,
                fs_group=fs_group,
                selinux=selinux,
                for_device=wants_device,
            )
            mounts_by_pvc[pvc] = mount

        volumes = list(getattr(manifest.spec, "volumes", []) or [])
        devices = list(getattr(manifest.spec, "volume_devices", []) or [])
        seen = {(v.host_path, v.mount_path, bool(v.read_only)) for v in volumes}
        seen_devices = {(d.host_path, d.device_path, bool(d.read_only)) for d in devices}
        for pm in pvc_mounts:
            if not getattr(pm, "mount_path", None) and not getattr(pm, "device_path", None):
                continue
            pvc = self._pvc_ref(pm, namespace=ns)
            mount = mounts_by_pvc.get(pvc)
            if not mount:
                continue
            host_path = mount.host_path
            read_only = bool(pm.read_only) or bool(mount.read_only)
            if getattr(pm, "device_path", None):
                key = (host_path, str(pm.device_path), bool(read_only))
                if key in seen_devices:
                    continue
                seen_devices.add(key)
                devices.append(
                    VolumeDeviceSpec(
                        host_path=host_path,
                        device_path=str(pm.device_path),
                        read_only=bool(read_only),
                    )
                )
                continue
            key = (host_path, str(pm.mount_path), bool(read_only))
            if key in seen:
                continue
            seen.add(key)
            volumes.append(
                VolumeSpec(
                    host_path=host_path,
                    mount_path=str(pm.mount_path),
                    read_only=bool(read_only),
                )
            )

        updated_spec = manifest.spec.model_copy(
            update={"volumes": volumes, "volume_devices": devices}
        )
        return manifest.model_copy(update={"spec": updated_spec})

    @staticmethod
    def _pvc_ref(pm: PvcMountSpec, *, namespace: str) -> PvcRef:
        ns = getattr(pm, "namespace", None) or namespace
        return PvcRef(name=str(pm.claim_name), namespace=str(ns))
