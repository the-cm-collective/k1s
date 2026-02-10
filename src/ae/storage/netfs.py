"""NetFS manager scaffolding for network-backed volumes."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterable
from logging import getLogger
from pathlib import Path
from typing import Any, Protocol

try:  # pragma: no cover - optional dependency
    import grpc
except Exception:  # pragma: no cover
    grpc = None

from .config import StorageConfig, StorageProvisionerRegistry, load_storage_registry
from .csi import CsiNodeClient, build_volume_capability
from .state import StorageState
from .types import NetFSMount, PvcRef, PvRef

LOGGER = getLogger(__name__)
NFS_HOST_ROOT_ANNOTATION = "k1s.io/nfs-host-root"
NFS_HOST_PATH_ANNOTATION = "k1s.io/nfs-host-path"


class PvcNotReadyError(RuntimeError):
    """Raised when PVC/PV binding is not ready for mount injection."""

    def __init__(self, pvc: PvcRef, message: str) -> None:
        super().__init__(message)
        self.pvc = pvc


class StorageDriver(Protocol):
    """CSI-aligned storage driver interface (controller + node)."""

    name: str

    # Controller-plane
    def create_volume(self, pvc: PvcRef) -> PvRef: ...

    def delete_volume(self, pv: PvRef) -> None: ...

    def controller_publish(self, pv: PvRef, node_id: str) -> None: ...

    def controller_unpublish(self, pv: PvRef, node_id: str) -> None: ...

    # Node-plane
    def node_stage(self, pv: PvRef, target_path: str) -> None: ...

    def node_publish(self, pv: PvRef, target_path: str, *, read_only: bool) -> None: ...

    def node_unpublish(self, pv: PvRef, target_path: str) -> None: ...

    def node_unstage(self, pv: PvRef, target_path: str) -> None: ...


class NetFSManager:
    """Tracks node mounts for network-backed PVCs."""

    def __init__(
        self,
        state: StorageState,
        *,
        root: str | Path | None = None,
        provisioners: StorageProvisionerRegistry | None = None,
    ) -> None:
        self._state = state
        if root is None:
            root = os.getenv("AE_NETFS_ROOT") or "/var/lib/ae/netfs"
        self._root = Path(root)
        self._nfs_tools_ok: bool | None = None
        config = StorageConfig.from_env()
        if provisioners is None:
            _classes, registry = load_storage_registry(config.provisioners_path)
            provisioners = registry
        self._provisioners = provisioners
        stage_root = os.getenv("AE_CSI_STAGE_ROOT") or "/var/lib/ae/csi"
        self._csi_stage_root = Path(stage_root)
        self._csi_timeout = float(os.getenv("AE_CSI_TIMEOUT_SECONDS", "10") or 10)
        self._csi_clients: dict[str, CsiNodeClient] = {}

    def ensure_mount(
        self,
        pvc: PvcRef,
        *,
        node_id: str,
        fs_group: int | None = None,
        selinux: dict[str, str] | None = None,
        for_device: bool = False,
    ) -> NetFSMount:
        """Ensure PV is attached (if needed) and mounted on the node."""

        existing = self._state.get_mount(pvc, node_id)
        if existing is not None:
            target = Path(existing.host_path)
            if fs_group is not None:
                self._apply_fs_group(pvc, target, fs_group)
            if selinux:
                pv_obj = self._state.get_pv(existing.pv)
                pv_spec = self._obj_spec(pv_obj)
                recursive = self._selinux_recursive(pv_spec, target)
                self._apply_selinux(pvc, target, selinux, recursive=recursive)
            return existing

        pv = self._state.get_pv_for_pvc(pvc)
        if pv is None:
            msg = f"PVC {pvc.namespace}/{pvc.name} is not bound to a PV"
            self._record_pvc_event(pvc, "PVCNotBound", msg)
            raise PvcNotReadyError(pvc, msg)

        pv_obj = self._state.get_pv(pv)
        if pv_obj is None:
            msg = f"PV {pv.name} not found for PVC {pvc.namespace}/{pvc.name}"
            self._record_pvc_event(pvc, "PVNotFound", msg)
            raise PvcNotReadyError(pvc, msg)

        pv_spec = self._obj_spec(pv_obj)
        volume_mode = str(pv_spec.get("volumeMode") or "Filesystem")
        if volume_mode.lower() == "block":
            if not for_device:
                msg = "block volumes require devicePath, not mountPath"
                self._record_pvc_event(pvc, "VolumeModeMismatch", msg)
                raise ValueError(msg)
            self._enforce_rwop(pvc, pv_spec, node_id)
            csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
            if isinstance(csi, dict):
                driver = str(csi.get("driver") or "")
                attach_required = self._csi_attach_required(driver)
                if attach_required:
                    attachment = self._state.get_volume_attachment(pv, node_id)
                    if attachment is None or not self._attachment_attached(attachment):
                        msg = f"PV {pv.name} is not attached to node {node_id}"
                        self._record_pvc_event(pvc, "VolumeNotAttached", msg)
                        raise RuntimeError(msg)
                self._resolve_csi_secret_ref(pvc, csi.get("nodeStageSecretRef"), "nodeStage")
                self._resolve_csi_secret_ref(
                    pvc, csi.get("nodePublishSecretRef"), "nodePublish"
                )
            block_path = self._block_device_path(pv_spec)
            if block_path is None:
                msg = "block volume requires hostPath path or CSI devicePath"
                self._record_pvc_event(pvc, "BlockDeviceMissing", msg)
                raise RuntimeError(msg)
            if not block_path.exists():
                msg = f"block device path missing: {block_path}"
                self._record_pvc_event(pvc, "BlockDeviceMissing", msg)
                raise RuntimeError(msg)
            if not self._is_block_or_file(block_path):
                msg = f"invalid block device path: {block_path}"
                self._record_pvc_event(pvc, "BlockDeviceInvalid", msg)
                raise RuntimeError(msg)
            if fs_group is not None:
                self._apply_fs_group(pvc, block_path, fs_group)
            if selinux:
                self._apply_selinux(pvc, block_path, selinux, recursive=False)
            read_only = bool(csi.get("readOnly", False)) if isinstance(csi, dict) else False
            mount = NetFSMount(
                pvc=pvc,
                pv=pv,
                node_id=node_id,
                host_path=str(block_path),
                read_only=read_only,
            )
            self._state.upsert_mount(mount)
            return mount
        if for_device:
            msg = "devicePath provided for filesystem volume"
            self._record_pvc_event(pvc, "VolumeModeMismatch", msg)
            raise ValueError(msg)
        self._enforce_rwop(pvc, pv_spec, node_id)
        nfs = pv_spec.get("nfs") if isinstance(pv_spec, dict) else None
        if isinstance(nfs, dict):
            return self._ensure_nfs_mount(
                pvc, pv, pv_spec, nfs, node_id=node_id, fs_group=fs_group, selinux=selinux
            )
        csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
        if isinstance(csi, dict):
            return self._ensure_csi_mount(
                pvc, pv, pv_spec, csi, node_id=node_id, fs_group=fs_group, selinux=selinux
            )
        msg = "NetFS supports NFS and CSI PVs in this phase"
        self._record_pvc_event(pvc, "UnsupportedVolume", msg)
        raise NotImplementedError(msg)

    def _enforce_rwop(self, pvc: PvcRef, pv_spec: dict[str, Any], node_id: str) -> None:
        if not self._is_rwop(pv_spec):
            return
        mounts = self._state.list_mounts()
        conflicts = [
            m for m in mounts if m.pvc == pvc and m.node_id and m.node_id != node_id
        ]
        if not conflicts:
            return
        nodes = ", ".join(sorted({m.node_id for m in conflicts}))
        msg = f"PVC {pvc.namespace}/{pvc.name} already mounted on node(s): {nodes}"
        self._record_pvc_event(pvc, "ReadWriteOncePodConflict", msg)
        raise RuntimeError(msg)

    def _apply_fs_group(self, pvc: PvcRef, target: Path, fs_group: int) -> None:
        try:
            gid = int(fs_group)
        except Exception:
            self._record_pvc_event(
                pvc, "FsGroupApplyFailed", f"invalid fsGroup value: {fs_group}"
            )
            return
        try:
            if not target.exists():
                return
            st = target.stat()
            if st.st_gid == gid:
                return
            os.chown(target, -1, gid)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) or "failed to apply fsGroup"
            self._record_pvc_event(pvc, "FsGroupApplyFailed", msg)

    def _apply_selinux(
        self, pvc: PvcRef, target: Path, opts: dict[str, str], *, recursive: bool = False
    ) -> None:
        if not target.exists():
            return
        if os.geteuid() != 0:
            self._record_pvc_event(
                pvc, "SelinuxRelabelSkipped", "insufficient privileges to apply SELinux label"
            )
            return
        if not shutil.which("chcon"):
            self._record_pvc_event(pvc, "SelinuxRelabelSkipped", "chcon not available")
            return
        args = ["chcon"]
        if recursive and target.is_dir():
            args.append("-R")
        user = opts.get("user")
        role = opts.get("role")
        typ = opts.get("type")
        level = opts.get("level")
        if user:
            args += ["-u", user]
        if role:
            args += ["-r", role]
        if typ:
            args += ["-t", typ]
        if level:
            args += ["-l", level]
        if len(args) == 1:
            return
        args.append(str(target))
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)  # noqa: S603,S607
        except subprocess.CalledProcessError as exc:
            msg = (exc.stderr or exc.stdout or "").strip() or "SELinux relabel failed"
            self._record_pvc_event(pvc, "SelinuxRelabelFailed", msg)

    @staticmethod
    def _selinux_recursive_enabled() -> bool:
        raw = os.getenv("AE_NETFS_SELINUX_RECURSIVE", "0")
        return str(raw).lower() in {"1", "true", "yes", "on"}

    def _selinux_recursive(self, pv_spec: dict[str, Any], target: Path) -> bool:
        if not target.exists() or not target.is_dir():
            return False
        if not self._selinux_recursive_enabled():
            return False
        if not isinstance(pv_spec, dict):
            return False
        modes = pv_spec.get("accessModes")
        if not isinstance(modes, list):
            return False
        shared = {"ReadWriteMany", "ReadOnlyMany"}
        return any(str(mode) in shared for mode in modes)

    def _ensure_nfs_mount(
        self,
        pvc: PvcRef,
        pv: PvRef,
        pv_spec: dict[str, Any],
        nfs: dict[str, Any],
        *,
        node_id: str,
        fs_group: int | None = None,
        selinux: dict[str, str] | None = None,
    ) -> NetFSMount:
        server = nfs.get("server")
        path = nfs.get("path")
        if not server or not path:
            msg = f"PV {pv.name} missing NFS server/path"
            self._record_pvc_event(pvc, "InvalidVolume", msg)
            raise ValueError(msg)

        pv_obj = self._state.get_pv(pv)
        if pv_obj is not None:
            self._ensure_nfs_export_path(pvc, pv_obj)

        target = self._mount_path(pvc)
        target.mkdir(parents=True, exist_ok=True)
        source = f"{server}:{path}"

        mount_opts = self._collect_mount_options(pv_spec)
        read_only = bool(nfs.get("readOnly", False))
        if not read_only and any(opt == "ro" for opt in mount_opts):
            read_only = True
        mount_opts = self._apply_read_only(mount_opts, read_only)

        info = self._mount_info(target)
        if info is None:
            try:
                self._ensure_nfs_tools()
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._record_pvc_event(pvc, "NfsPrereqFailed", msg)
                raise
            try:
                self._mount_nfs(source, target, mount_opts)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._record_pvc_event(pvc, "MountFailed", msg)
                raise
        else:
            current_source, fstype = info
            if current_source != source:
                msg = f"mountpoint {target} already used by {current_source}, expected {source}"
                self._record_pvc_event(pvc, "MountConflict", msg)
                raise RuntimeError(msg)
            if fstype not in {"nfs", "nfs4"}:
                msg = f"mountpoint {target} is {fstype}, expected nfs"
                self._record_pvc_event(pvc, "MountConflict", msg)
                raise RuntimeError(msg)

        if fs_group is not None:
            self._apply_fs_group(pvc, target, fs_group)
        if selinux:
            recursive = self._selinux_recursive(pv_spec, target)
            self._apply_selinux(pvc, target, selinux, recursive=recursive)
        self._maybe_resize_filesystem(pvc, target)

        mount = NetFSMount(
            pvc=pvc,
            pv=pv,
            node_id=node_id,
            host_path=str(target),
            read_only=read_only,
        )
        self._state.upsert_mount(mount)
        return mount

    def _ensure_nfs_export_path(self, pvc: PvcRef, pv_obj) -> None:  # noqa: ANN001
        try:
            meta = pv_obj.metadata or {}
        except Exception:
            return
        annotations = meta.get("annotations") if isinstance(meta, dict) else None
        if not isinstance(annotations, dict):
            return
        host_root = annotations.get(NFS_HOST_ROOT_ANNOTATION)
        host_path = annotations.get(NFS_HOST_PATH_ANNOTATION)
        if not host_root or not host_path:
            return
        root = Path(str(host_root)).expanduser()
        path = Path(str(host_path)).expanduser()
        if not self._within_root(root, path):
            msg = f"refusing to create NFS path outside root: {path}"
            self._record_pvc_event(pvc, "NfsExportInvalid", msg)
            return
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) or f"failed to create NFS export path {path}"
            self._record_pvc_event(pvc, "NfsExportCreateFailed", msg)

    @staticmethod
    def _within_root(root: Path, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(root.resolve())
        except AttributeError:  # pragma: no cover - py<3.9 fallback
            try:
                path.resolve().relative_to(root.resolve())
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _ensure_csi_mount(
        self,
        pvc: PvcRef,
        pv: PvRef,
        pv_spec: dict[str, Any],
        csi: dict[str, Any],
        *,
        node_id: str,
        fs_group: int | None = None,
        selinux: dict[str, str] | None = None,
    ) -> NetFSMount:
        driver = str(csi.get("driver") or "")
        handle = str(csi.get("volumeHandle") or "")
        if not driver or not handle:
            msg = f"PV {pv.name} missing CSI driver/volumeHandle"
            self._record_pvc_event(pvc, "InvalidVolume", msg)
            raise ValueError(msg)

        attach_required = self._csi_attach_required(driver)
        attachment = None
        publish_context: dict[str, str] = {}
        if attach_required:
            attachment = self._state.get_volume_attachment(pv, node_id)
            if attachment is None or not self._attachment_attached(attachment):
                msg = f"PV {pv.name} is not attached to node {node_id}"
                self._record_pvc_event(pvc, "VolumeNotAttached", msg)
                raise RuntimeError(msg)
            publish_context = self._attachment_publish_context(attachment)

        stage_secret = self._resolve_csi_secret_ref(pvc, csi.get("nodeStageSecretRef"), "nodeStage")
        publish_secret = self._resolve_csi_secret_ref(
            pvc, csi.get("nodePublishSecretRef"), "nodePublish"
        )

        target = self._mount_path(pvc)
        target.mkdir(parents=True, exist_ok=True)
        stage_path = self._csi_stage_path(driver, handle)
        stage_path.mkdir(parents=True, exist_ok=True)

        volume_context = self._csi_volume_context(csi)
        volume_mode = str(pv_spec.get("volumeMode") or "Filesystem")
        fs_type = str(csi.get("fsType") or "")
        mount_flags = self._csi_mount_flags(pv_spec)
        volume_capability = build_volume_capability(
            access_modes=pv_spec.get("accessModes") or [],
            volume_mode=volume_mode,
            fs_type=fs_type if fs_type else None,
            mount_flags=mount_flags,
        )

        try:
            client = self._csi_node_client(driver, pv_spec.get("storageClassName"))
        except RuntimeError as exc:
            reason = (
                "CsiGrpcUnavailable"
                if "grpc" in str(exc).lower()
                else "CsiEndpointMissing"
            )
            self._record_pvc_event(pvc, reason, str(exc))
            raise
        staged = True
        try:
            client.node_stage(
                volume_id=handle,
                staging_target_path=str(stage_path),
                volume_capability=volume_capability,
                secrets=stage_secret[2] if stage_secret else None,
                volume_context=volume_context,
                publish_context=publish_context,
            )
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                staged = False
            else:
                msg = f"CSI NodeStage failed: {exc.code().name}"
                self._record_pvc_event(pvc, "NodeStageFailed", msg)
                raise RuntimeError(msg) from exc

        try:
            client.node_publish(
                volume_id=handle,
                target_path=str(target),
                staging_target_path=str(stage_path) if staged else None,
                volume_capability=volume_capability,
                read_only=bool(csi.get("readOnly", False)),
                secrets=publish_secret[2] if publish_secret else None,
                volume_context=volume_context,
                publish_context=publish_context,
            )
        except grpc.RpcError as exc:
            msg = f"CSI NodePublish failed: {exc.code().name}"
            self._record_pvc_event(pvc, "NodePublishFailed", msg)
            raise RuntimeError(msg) from exc

        marker = target / ".csi-volume"
        try:
            lines = [f"driver={driver}", f"volumeHandle={handle}"]
            if fs_type:
                lines.append(f"fsType={fs_type}")
            lines.append(f"readOnly={str(bool(csi.get('readOnly', False))).lower()}")
            for key, value in volume_context.items():
                lines.append(f"volumeAttributes.{key}={value}")
            for key, value in publish_context.items():
                lines.append(f"publishContext.{key}={value}")
            secret_refs = (
                ("nodeStageSecretRef", stage_secret),
                ("nodePublishSecretRef", publish_secret),
            )
            for label, resolved in secret_refs:
                if resolved is None:
                    continue
                ns, name, data = resolved
                keys = ",".join(sorted(data.keys()))
                lines.append(f"{label}={ns}/{name}")
                if keys:
                    lines.append(f"{label}.keys={keys}")
            marker.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("failed to write CSI marker for %s: %s", pvc, exc)

        if fs_group is not None:
            self._apply_fs_group(pvc, target, fs_group)
        if selinux:
            recursive = self._selinux_recursive(pv_spec, target)
            self._apply_selinux(pvc, target, selinux, recursive=recursive)
        self._maybe_resize_filesystem(pvc, target)

        read_only = bool(csi.get("readOnly", False))
        mount = NetFSMount(
            pvc=pvc,
            pv=pv,
            node_id=node_id,
            host_path=str(target),
            read_only=read_only,
        )
        self._state.upsert_mount(mount)
        return mount

    def release_mount(self, pvc: PvcRef, *, node_id: str) -> None:
        """Unmount and detach if the PV is no longer referenced."""

        mount = self._state.get_mount(pvc, node_id)
        if mount is None:
            return
        target = Path(mount.host_path)
        pv_obj = self._state.get_pv(mount.pv)
        pv_spec = self._obj_spec(pv_obj)
        csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
        if isinstance(csi, dict):
            driver = str(csi.get("driver") or "")
            handle = str(csi.get("volumeHandle") or "")
            if driver and handle:
                if grpc is None:
                    self._record_pvc_event(
                        pvc, "CsiGrpcUnavailable", "grpc is required for CSI node operations"
                    )
                else:
                    stage_path = self._csi_stage_path(driver, handle)
                    try:
                        client = self._csi_node_client(driver, pv_spec.get("storageClassName"))
                        try:
                            client.node_unpublish(volume_id=handle, target_path=str(target))
                        except grpc.RpcError as exc:
                            msg = f"CSI NodeUnpublish failed: {exc.code().name}"
                            self._record_pvc_event(pvc, "NodeUnpublishFailed", msg)
                        try:
                            client.node_unstage(volume_id=handle, staging_target_path=str(stage_path))
                        except grpc.RpcError as exc:
                            if exc.code() != grpc.StatusCode.UNIMPLEMENTED:
                                msg = f"CSI NodeUnstage failed: {exc.code().name}"
                                self._record_pvc_event(pvc, "NodeUnstageFailed", msg)
                    except RuntimeError as exc:
                        self._record_pvc_event(pvc, "CsiEndpointMissing", str(exc))
        if self._mount_info(target) is not None:
            try:
                self._unmount(target)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._record_pvc_event(pvc, "UnmountFailed", msg)
                raise
        self._state.delete_mount(pvc, node_id)

    def list_mounts(self, *, node_id: str | None = None) -> list[NetFSMount]:
        return self._state.list_mounts(node_id=node_id)

    def _mount_path(self, pvc: PvcRef) -> Path:
        ns = self._sanitize(pvc.namespace)
        name = self._sanitize(pvc.name)
        return self._root / ns / name

    @staticmethod
    def _sanitize(value: str) -> str:
        # Avoid path traversal; PVC names are DNS labels but sanitize defensively.
        return value.replace("/", "_").replace("\\", "_")

    def _collect_mount_options(self, pv_spec: dict[str, Any]) -> list[str]:
        opts: list[str] = []
        sc_name = pv_spec.get("storageClassName") if isinstance(pv_spec, dict) else None
        if sc_name:
            sc_obj = self._state.get_storage_class(str(sc_name))
            if sc_obj is not None:
                opts.extend(self._normalize_mount_options(self._obj_spec(sc_obj).get("mountOptions")))
        if isinstance(pv_spec, dict):
            opts.extend(self._normalize_mount_options(pv_spec.get("mountOptions")))
        return self._dedupe(opts)

    @staticmethod
    def _normalize_mount_options(raw: Any) -> list[str]:
        if not raw:
            return []
        options: list[str] = []
        if isinstance(raw, list | tuple | set):
            items: Iterable[Any] = raw
        else:
            items = [raw]
        for item in items:
            if item is None:
                continue
            for opt in str(item).split(","):
                opt = opt.strip()
                if opt:
                    options.append(opt)
        return options

    @staticmethod
    def _apply_read_only(options: list[str], read_only: bool) -> list[str]:
        filtered = [opt for opt in options if opt not in {"ro", "rw"}]
        filtered.append("ro" if read_only else "rw")
        return filtered

    @staticmethod
    def _dedupe(options: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for opt in options:
            if opt in seen:
                continue
            seen.add(opt)
            out.append(opt)
        return out

    def _mount_nfs(self, source: str, target: Path, options: list[str]) -> None:
        cmd = ["mount", "-t", "nfs"]
        if options:
            cmd.extend(["-o", ",".join(options)])
        cmd.extend([source, str(target)])
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)  # noqa: S603,S607
        except subprocess.CalledProcessError as exc:  # pragma: no cover - integration path
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(f"failed to mount {source} on {target}: {stderr}") from exc

    def _unmount(self, target: Path) -> None:
        try:
            subprocess.run(
                ["umount", str(target)], check=True, capture_output=True, text=True  # noqa: S603,S607
            )
        except subprocess.CalledProcessError as exc:  # pragma: no cover - integration path
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(f"failed to unmount {target}: {stderr}") from exc

    def _ensure_nfs_tools(self) -> None:
        if self._nfs_tools_ok:
            return
        if not shutil.which("mount") or not shutil.which("umount"):
            raise RuntimeError("mount/umount utilities not found")
        if not self._has_nfs_helper():
            raise RuntimeError("NFS mount helper not found (mount.nfs/mount.nfs4)")
        self._nfs_tools_ok = True

    @staticmethod
    def _has_nfs_helper() -> bool:
        for name in ("mount.nfs", "mount.nfs4"):
            if shutil.which(name):
                return True
            for base in ("/sbin", "/usr/sbin", "/usr/local/sbin"):
                if Path(base, name).exists():
                    return True
        return False

    def _mount_info(self, target: Path) -> tuple[str, str] | None:
        target_path = os.path.abspath(str(target))
        mounts_path = "/proc/mounts"
        if not os.path.exists(mounts_path):
            mounts_path = "/etc/mtab"
        try:
            with open(mounts_path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    source = self._decode_mount_field(parts[0])
                    mnt = os.path.abspath(self._decode_mount_field(parts[1]))
                    fstype = parts[2]
                    if mnt == target_path:
                        return source, fstype
        except FileNotFoundError:
            return None
        return None

    def _maybe_resize_filesystem(self, pvc: PvcRef, target: Path) -> None:
        if not self._fs_resize_enabled():
            return
        info = self._mount_info(target)
        if info is None:
            return
        source, fstype = info
        if fstype in {"nfs", "nfs4", "cifs", "smbfs"}:
            return
        if fstype in {"xfs"}:
            tool = "xfs_growfs"
            cmd = [tool, str(target)]
        elif fstype in {"ext4", "ext3", "ext2"}:
            if not source.startswith("/dev/"):
                return
            tool = "resize2fs"
            cmd = [tool, source]
        else:
            return
        if not shutil.which(tool):
            self._record_pvc_event(pvc, "FileSystemResizeSkipped", f"{tool} not available")
            return
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)  # noqa: S603,S607
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "").strip()
            msg = err or f"{tool} failed"
            self._record_pvc_event(pvc, "FileSystemResizeFailed", msg)
            LOGGER.warning("filesystem resize failed for %s: %s", pvc, msg)
            return
        self._record_pvc_event(pvc, "FileSystemResized", f"filesystem resized via {tool}")

    @staticmethod
    def _fs_resize_enabled() -> bool:
        raw = os.getenv("AE_NETFS_FS_RESIZE", "0")
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _decode_mount_field(value: str) -> str:
        return (
            value.replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )

    def _record_pvc_event(self, pvc: PvcRef, reason: str, message: str) -> None:
        try:
            self._state.record_pvc_event(pvc, reason, message)
        except Exception:
            return

    def _resolve_csi_secret_ref(
        self, pvc: PvcRef, raw_ref: Any, purpose: str
    ) -> tuple[str, str, dict[str, str]] | None:
        if not isinstance(raw_ref, dict):
            return None
        name = str(raw_ref.get("name") or "").strip()
        namespace = str(raw_ref.get("namespace") or pvc.namespace).strip()
        if not name:
            msg = f"CSI {purpose} secretRef missing name"
            self._record_pvc_event(pvc, "InvalidSecretRef", msg)
            raise ValueError(msg)
        if not namespace:
            msg = f"CSI {purpose} secretRef missing namespace"
            self._record_pvc_event(pvc, "InvalidSecretRef", msg)
            raise ValueError(msg)
        secret = self._state.get_secret(namespace, name)
        if not secret:
            msg = f"CSI {purpose} secret {namespace}/{name} not found"
            self._record_pvc_event(pvc, "SecretNotFound", msg)
            raise KeyError(msg)
        return namespace, name, secret

    @staticmethod
    def _attachment_attached(attachment: Any) -> bool:
        status = getattr(attachment, "status", None)
        if not isinstance(status, dict):
            return False
        return bool(status.get("attached"))

    def _attachment_publish_context(self, attachment: Any) -> dict[str, str]:
        status = getattr(attachment, "status", None)
        if not isinstance(status, dict):
            return {}
        meta = status.get("attachmentMetadata") or status.get("attachment_metadata")
        if not isinstance(meta, dict):
            return {}
        raw = meta.get("publishContext") or meta.get("publish_context")
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if v is not None}

    def _csi_attach_required(self, driver: str) -> bool:
        if not driver:
            return True
        obj = None
        try:
            obj = self._state.get_csi_driver(driver)
        except Exception:
            obj = None
        if obj is None:
            return True
        spec = self._obj_spec(obj)
        raw = spec.get("attachRequired") if isinstance(spec, dict) else None
        if raw is None:
            return True
        return bool(raw)

    @staticmethod
    def _csi_volume_context(csi: dict[str, Any]) -> dict[str, str]:
        raw = csi.get("volumeAttributes")
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if v is not None}

    def _csi_mount_flags(self, pv_spec: dict[str, Any]) -> list[str]:
        opts = self._collect_mount_options(pv_spec)
        return [opt for opt in opts if opt not in {"ro", "rw"}]

    def _csi_stage_path(self, driver: str, handle: str) -> Path:
        driver_token = self._sanitize(driver)
        handle_token = self._sanitize(handle)
        return self._csi_stage_root / driver_token / handle_token

    def _csi_node_client(self, driver: str, sc_name: str | None) -> CsiNodeClient:
        if grpc is None:
            raise RuntimeError("grpc is required for CSI node operations")
        entry = self._provisioners.for_storage_class(sc_name) or self._provisioners.for_driver(
            driver
        )
        endpoint = entry.node_endpoint if entry else None
        if not endpoint:
            raise RuntimeError(f"CSI node endpoint missing for driver {driver}")
        client = self._csi_clients.get(driver)
        if client is not None and client.endpoint == endpoint:
            return client
        client = CsiNodeClient(endpoint, timeout=self._csi_timeout)
        self._csi_clients[driver] = client
        return client

    @staticmethod
    def _obj_spec(obj: Any) -> dict[str, Any]:
        if obj is None:
            return {}
        if isinstance(obj, dict):
            if "spec" in obj and isinstance(obj.get("spec"), dict):
                return obj.get("spec") or {}
            return obj
        spec = getattr(obj, "spec", None)
        if isinstance(spec, dict):
            return spec
        return {}

    @staticmethod
    def _is_rwop(pv_spec: dict[str, Any]) -> bool:
        if not isinstance(pv_spec, dict):
            return False
        modes = pv_spec.get("accessModes")
        if not isinstance(modes, list):
            return False
        return "ReadWriteOncePod" in {str(m) for m in modes}

    @staticmethod
    def _block_device_path(pv_spec: dict[str, Any]) -> Path | None:
        host = pv_spec.get("hostPath") if isinstance(pv_spec, dict) else None
        if not isinstance(host, dict):
            host = None
        if host:
            path = host.get("path")
            if path:
                return Path(str(path))
        csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
        if isinstance(csi, dict):
            raw_attrs = csi.get("volumeAttributes")
            attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
            device = attrs.get("devicePath") or attrs.get("device_path")
            if device:
                return Path(str(device))
        return None

    @staticmethod
    def _is_block_or_file(path: Path) -> bool:
        try:
            st = path.stat()
        except Exception:
            return False
        return stat.S_ISBLK(st.st_mode) or stat.S_ISREG(st.st_mode)
