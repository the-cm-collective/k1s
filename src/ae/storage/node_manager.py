"""Node-side volume manager for NetFS-backed PVC mounts."""

from __future__ import annotations

import os
import socket

from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    PvcMountSpec,
    VolumeDeviceSpec,
    VolumeSpec,
    all_pvc_mounts,
)

from .netfs import NetFSManager, PvcNotReadyError
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
        self,
        manifest: AppManifest,
        *,
        node_id: str | None = None,
        replica_id: str | None = None,
    ) -> AppManifest:
        """Ensure PVC mounts are present and inject hostPath volumes into the manifest."""

        ordinal = self._replica_ordinal(replica_id)
        if ordinal is not None:
            manifest = self._resolve_claim_templates(manifest, ordinal)

        pvc_mounts = list(all_pvc_mounts(manifest) or [])
        if not pvc_mounts:
            return manifest
        if ordinal is None:
            pvc_mounts = [pm for pm in pvc_mounts if not getattr(pm, "claim_template", False)]
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
            try:
                mount = self._netfs.ensure_mount(
                    pvc,
                    node_id=node,
                    fs_group=fs_group,
                    selinux=selinux,
                    for_device=wants_device,
                )
            except PvcNotReadyError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = str(exc) or "PVC mount not ready"
                raise PvcNotReadyError(pvc, msg) from exc
            mounts_by_pvc[pvc] = mount

        volumes = list(getattr(manifest.spec, "volumes", []) or [])
        devices = list(getattr(manifest.spec, "volume_devices", []) or [])
        main_pvc_mounts = list(getattr(manifest.spec, "pvc_mounts", []) or [])
        self._extend_with_pvc_mounts(
            volumes, devices, main_pvc_mounts, mounts_by_pvc, namespace=ns
        )

        containers = []
        for c in getattr(manifest.spec, "containers", []) or []:
            containers.append(
                self._inject_container_pvcs(c, mounts_by_pvc, namespace=ns)
            )

        init_containers = []
        for c in getattr(manifest.spec, "init_containers", []) or []:
            init_containers.append(
                self._inject_container_pvcs(c, mounts_by_pvc, namespace=ns)
            )

        updated_spec = manifest.spec.model_copy(
            update={
                "volumes": volumes,
                "volume_devices": devices,
                "containers": containers,
                "init_containers": init_containers,
            }
        )
        return manifest.model_copy(update={"spec": updated_spec})

    @staticmethod
    def _pvc_ref(pm: PvcMountSpec, *, namespace: str) -> PvcRef:
        ns = getattr(pm, "namespace", None) or namespace
        return PvcRef(name=str(pm.claim_name), namespace=str(ns))

    @staticmethod
    def _replica_ordinal(replica_id: str | None) -> int | None:
        if not replica_id:
            return None
        raw = str(replica_id)
        if not raw:
            return None
        tail = raw.rsplit("-", 1)
        if len(tail) != 2:
            return None
        try:
            return int(tail[1])
        except Exception:
            return None

    def _resolve_claim_templates(self, manifest: AppManifest, ordinal: int) -> AppManifest:
        name = getattr(manifest.metadata, "name", None) or ""
        if not name:
            return manifest

        def _resolve_pm(pm):  # noqa: ANN001
            try:
                is_template = bool(getattr(pm, "claim_template", False))
            except Exception:
                is_template = False
            if not is_template:
                return pm
            base = None
            if isinstance(pm, dict):
                base = pm.get("claimName") or pm.get("claim_name")
            else:
                base = getattr(pm, "claim_name", None)
            if not base:
                return pm
            claim = f"{base}-{name}-{ordinal}"
            if isinstance(pm, dict):
                updated = dict(pm)
                updated["claimName"] = claim
                updated["claimTemplate"] = False
                return updated
            return pm.model_copy(update={"claim_name": claim, "claim_template": False})

        def _resolve_container(container):  # noqa: ANN001
            if not self._spec_has_field(container, "pvc_mounts", "pvcMounts"):
                return container
            pvc_mounts = list(getattr(container, "pvc_mounts", []) or [])
            if not pvc_mounts:
                return container
            resolved = [_resolve_pm(pm) for pm in pvc_mounts]
            if isinstance(container, dict):
                updated = dict(container)
                updated["pvc_mounts"] = resolved
                return updated
            return container.model_copy(update={"pvc_mounts": resolved})

        spec = manifest.spec
        updated_spec = spec
        root_mounts = list(getattr(spec, "pvc_mounts", []) or [])
        if root_mounts:
            updated_spec = updated_spec.model_copy(
                update={"pvc_mounts": [_resolve_pm(pm) for pm in root_mounts]}
            )
        containers = list(getattr(updated_spec, "containers", []) or [])
        if containers:
            updated_spec = updated_spec.model_copy(
                update={"containers": [_resolve_container(c) for c in containers]}
            )
        init_containers = list(getattr(updated_spec, "init_containers", []) or [])
        if init_containers:
            updated_spec = updated_spec.model_copy(
                update={"init_containers": [_resolve_container(c) for c in init_containers]}
            )
        if updated_spec is spec:
            return manifest
        return manifest.model_copy(update={"spec": updated_spec})

    @staticmethod
    def _spec_has_field(obj, name: str, alt: str | None = None) -> bool:  # noqa: ANN001
        if obj is None:
            return False
        if isinstance(obj, dict):
            return name in obj or (alt in obj if alt else False)
        fields = getattr(obj, "__pydantic_fields_set__", None)
        if fields is None:
            return False
        return name in fields or (alt in fields if alt else False)

    def _extend_with_pvc_mounts(
        self,
        volumes: list,
        devices: list,
        pvc_mounts: list,
        mounts_by_pvc: dict[PvcRef, NetFSMount],
        *,
        namespace: str,
    ) -> None:
        def _vol_key(v):  # noqa: ANN001
            if isinstance(v, dict):
                return (
                    v.get("hostPath") or v.get("host_path"),
                    v.get("mountPath") or v.get("mount_path"),
                    bool(v.get("readOnly", False)),
                )
            return (getattr(v, "host_path", None), getattr(v, "mount_path", None), bool(v.read_only))

        def _dev_key(d):  # noqa: ANN001
            if isinstance(d, dict):
                return (
                    d.get("hostPath") or d.get("host_path"),
                    d.get("devicePath") or d.get("device_path"),
                    bool(d.get("readOnly", False)),
                )
            return (getattr(d, "host_path", None), getattr(d, "device_path", None), bool(d.read_only))

        seen = {_vol_key(v) for v in volumes}
        seen_devices = {_dev_key(d) for d in devices}
        for pm in pvc_mounts or []:
            if not getattr(pm, "mount_path", None) and not getattr(pm, "device_path", None):
                continue
            pvc = self._pvc_ref(pm, namespace=namespace)
            mount = mounts_by_pvc.get(pvc)
            if not mount:
                continue
            host_path = mount.host_path
            sub_path = getattr(pm, "sub_path", None)
            if sub_path:
                host_path = os.path.join(str(host_path), str(sub_path).lstrip("/"))
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

    def _inject_container_pvcs(
        self, container, mounts_by_pvc: dict[PvcRef, NetFSMount], *, namespace: str
    ):  # noqa: ANN001
        if not self._spec_has_field(container, "pvc_mounts", "pvcMounts"):
            return container
        pvc_mounts = list(getattr(container, "pvc_mounts", []) or [])
        if not pvc_mounts:
            return container
        volume_mounts = list(getattr(container, "volume_mounts", []) or [])
        volume_devices = list(getattr(container, "volume_devices", []) or [])
        self._extend_with_pvc_mounts(
            volume_mounts, volume_devices, pvc_mounts, mounts_by_pvc, namespace=namespace
        )
        if isinstance(container, dict):
            updated = dict(container)
            updated["volume_mounts"] = volume_mounts
            updated["volume_devices"] = volume_devices
            return updated
        return container.model_copy(
            update={"volume_mounts": volume_mounts, "volume_devices": volume_devices}
        )
