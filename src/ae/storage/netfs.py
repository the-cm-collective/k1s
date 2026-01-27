"""NetFS manager scaffolding for network-backed volumes."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from logging import getLogger
from pathlib import Path
from typing import Any, Protocol

from .state import StorageState
from .types import NetFSMount, PvcRef, PvRef

LOGGER = getLogger(__name__)


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

    def __init__(self, state: StorageState, *, root: str | Path | None = None) -> None:
        self._state = state
        if root is None:
            root = os.getenv("AE_NETFS_ROOT") or "/var/lib/ae/netfs"
        self._root = Path(root)
        self._nfs_tools_ok: bool | None = None

    def ensure_mount(self, pvc: PvcRef, *, node_id: str) -> NetFSMount:
        """Ensure PV is attached (if needed) and mounted on the node."""

        pv = self._state.get_pv_for_pvc(pvc)
        if pv is None:
            msg = f"PVC {pvc.namespace}/{pvc.name} is not bound to a PV"
            self._record_pvc_event(pvc, "PVCNotBound", msg)
            raise KeyError(msg)

        pv_obj = self._state.get_pv(pv)
        if pv_obj is None:
            msg = f"PV {pv.name} not found for PVC {pvc.namespace}/{pvc.name}"
            self._record_pvc_event(pvc, "PVNotFound", msg)
            raise KeyError(msg)

        pv_spec = self._obj_spec(pv_obj)
        nfs = pv_spec.get("nfs") if isinstance(pv_spec, dict) else None
        if isinstance(nfs, dict):
            return self._ensure_nfs_mount(pvc, pv, pv_spec, nfs, node_id=node_id)
        csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
        if isinstance(csi, dict):
            return self._ensure_csi_mount(pvc, pv, pv_spec, csi, node_id=node_id)
        msg = "NetFS supports NFS and CSI PVs in this phase"
        self._record_pvc_event(pvc, "UnsupportedVolume", msg)
        raise NotImplementedError(msg)

    def _ensure_nfs_mount(
        self, pvc: PvcRef, pv: PvRef, pv_spec: dict[str, Any], nfs: dict[str, Any], *, node_id: str
    ) -> NetFSMount:
        server = nfs.get("server")
        path = nfs.get("path")
        if not server or not path:
            msg = f"PV {pv.name} missing NFS server/path"
            self._record_pvc_event(pvc, "InvalidVolume", msg)
            raise ValueError(msg)

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

    def _ensure_csi_mount(
        self, pvc: PvcRef, pv: PvRef, pv_spec: dict[str, Any], csi: dict[str, Any], *, node_id: str
    ) -> NetFSMount:
        _ = pv_spec
        driver = csi.get("driver")
        handle = csi.get("volumeHandle")
        if not driver or not handle:
            msg = f"PV {pv.name} missing CSI driver/volumeHandle"
            self._record_pvc_event(pvc, "InvalidVolume", msg)
            raise ValueError(msg)

        attachment = self._state.get_volume_attachment(pv, node_id)
        if attachment is None or not self._attachment_attached(attachment):
            msg = f"PV {pv.name} is not attached to node {node_id}"
            self._record_pvc_event(pvc, "VolumeNotAttached", msg)
            raise RuntimeError(msg)

        stage_secret = self._resolve_csi_secret_ref(pvc, csi.get("nodeStageSecretRef"), "nodeStage")
        publish_secret = self._resolve_csi_secret_ref(
            pvc, csi.get("nodePublishSecretRef"), "nodePublish"
        )

        target = self._mount_path(pvc)
        target.mkdir(parents=True, exist_ok=True)
        marker = target / ".csi-volume"
        try:
            lines = [f"driver={driver}", f"volumeHandle={handle}"]
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
        if self._mount_info(target) is not None:
            try:
                self._unmount(target)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._record_pvc_event(pvc, "UnmountFailed", msg)
                raise
        # TODO: Call node_unpublish/node_unstage and controller_unpublish as needed.
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
