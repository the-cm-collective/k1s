"""Storage controller for StorageClass seeding and PVC/PV reconciliation."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ae.apishim.store import ObjectStore

from .config import (
    DEFAULT_CLASS_ANNOTATIONS,
    StorageClassConfig,
    StorageConfig,
    load_storage_classes,
    select_default_class,
)

LOGGER = logging.getLogger(__name__)

SC_GROUP = "storage.k8s.io"
SC_VERSION = "v1"
SC_RESOURCE = "storageclasses"

CORE_GROUP = ""
CORE_VERSION = "v1"
PVC_RESOURCE = "persistentvolumeclaims"
PV_RESOURCE = "persistentvolumes"
VA_RESOURCE = "volumeattachments"
EVENT_RESOURCE = "events"

NFS_PROVISIONER = "k1s.io/nfs"
LOCAL_PATH_PROVISIONER = "k1s.io/local-path"
WAIT_FOR_FIRST_CONSUMER = "WaitForFirstConsumer"
SELECTED_NODE_ANNOTATION = "volume.kubernetes.io/selected-node"
PROVISIONED_BY_ANNOTATION = "pv.kubernetes.io/provisioned-by"
STORAGE_PROVISIONER_ANNOTATION = "volume.kubernetes.io/storage-provisioner"
NFS_HOST_ROOT_ANNOTATION = "k1s.io/nfs-host-root"
NFS_HOST_PATH_ANNOTATION = "k1s.io/nfs-host-path"
LOCAL_HOST_ROOT_ANNOTATION = "k1s.io/local-host-root"
LOCAL_HOST_PATH_ANNOTATION = "k1s.io/local-host-path"
DEFAULT_LOCAL_ROOT = Path(os.getenv("AE_STORAGE_ROOT", "/var/lib/ae/storage"))


class StorageController:
    """Seed StorageClass objects from config and prepare for PVC/PV binding."""

    def __init__(self, store: ObjectStore, *, config: StorageConfig | None = None) -> None:
        self._store = store
        self._config = config or StorageConfig.from_env()
        self._storage_classes = load_storage_classes(self._config.provisioners_path)
        self._default_class = self._resolve_default(self._storage_classes)
        self._stop = threading.Event()
        self._pvc_thread: threading.Thread | None = None
        self._pv_thread: threading.Thread | None = None

    def sync(self) -> int:
        """Sync configured StorageClass objects into the apishim store."""
        if not self._storage_classes:
            return 0
        count = 0
        for sc in self._storage_classes:
            self._seed_storage_class(sc)
            count += 1
        return count

    def start(self) -> None:
        """Start background PVC/PV reconciliation."""
        if self._pvc_thread and self._pvc_thread.is_alive():
            return
        self._stop.clear()
        self._pvc_thread = threading.Thread(
            target=self._watch_pvcs, name="storage-pvc-watch", daemon=True
        )
        self._pv_thread = threading.Thread(
            target=self._watch_pvs, name="storage-pv-watch", daemon=True
        )
        self._pvc_thread.start()
        self._pv_thread.start()
        self.reconcile_once()

    def stop(self) -> None:
        self._stop.set()

    def reconcile_once(self) -> None:
        """Run a single PVC/PV binding pass."""
        self._reconcile_all()

    def _resolve_default(
        self, storage_classes: list[StorageClassConfig]
    ) -> StorageClassConfig | None:
        if not storage_classes:
            return None
        if self._config.default_class:
            for sc in storage_classes:
                if sc.name == self._config.default_class:
                    sc.is_default = True
                else:
                    sc.is_default = False
            return next(
                (sc for sc in storage_classes if sc.name == self._config.default_class), None
            )
        return select_default_class(storage_classes)

    def _seed_storage_class(self, sc: StorageClassConfig) -> None:
        annotations = {}
        if sc.is_default:
            annotations[DEFAULT_CLASS_ANNOTATIONS[0]] = "true"
        metadata = {"name": sc.name}
        if annotations:
            metadata["annotations"] = annotations
        spec = {
            "provisioner": sc.provisioner,
            "parameters": sc.parameters,
        }
        if sc.reclaim_policy:
            spec["reclaimPolicy"] = sc.reclaim_policy
        if sc.volume_binding_mode:
            spec["volumeBindingMode"] = sc.volume_binding_mode
        if sc.allow_volume_expansion is not None:
            spec["allowVolumeExpansion"] = bool(sc.allow_volume_expansion)
        if sc.mount_options:
            spec["mountOptions"] = list(sc.mount_options)
        self._store.upsert(
            SC_GROUP,
            SC_VERSION,
            SC_RESOURCE,
            None,
            sc.name,
            metadata,
            spec,
            status={},
        )

    def _watch_pvcs(self) -> None:
        gen = self._store.watch(
            CORE_GROUP, CORE_VERSION, PVC_RESOURCE, None, heartbeat_seconds=5, allow_bookmarks=True
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev == "DELETED":
                    self._handle_pvc_deleted(obj)
                    continue
                if ev in {"ADDED", "MODIFIED"}:
                    self._reconcile_pvc(obj)
        finally:
            with suppress(Exception):
                gen.close()  # type: ignore[attr-defined]

    def _watch_pvs(self) -> None:
        gen = self._store.watch(
            CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, heartbeat_seconds=5, allow_bookmarks=True
        )
        try:
            for _ev, _obj in gen:
                if self._stop.is_set():
                    break
                self._reconcile_pending()
        finally:
            with suppress(Exception):
                gen.close()  # type: ignore[attr-defined]

    def _reconcile_all(self) -> None:
        try:
            pvcs = self._store.list_all(CORE_GROUP, CORE_VERSION, PVC_RESOURCE)
        except Exception:
            pvcs = []
        for pvc in pvcs:
            self._reconcile_pvc(pvc)

    def _reconcile_pending(self) -> None:
        try:
            pvcs = self._store.list_all(CORE_GROUP, CORE_VERSION, PVC_RESOURCE)
        except Exception:
            pvcs = []
        for pvc in pvcs:
            if not self._pvc_is_bound(pvc):
                self._reconcile_pvc(pvc)

    def _reconcile_pvc(self, pvc) -> None:
        spec = pvc.spec or {}
        if self._pvc_is_bound(pvc):
            pv_name = spec.get("volumeName")
            if pv_name:
                pv = self._store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv_name)
                if pv is not None:
                    self._bind(pvc, pv)
                    self._reconcile_csi_attachment(pvc, pv)
            return

        pv = self._match_pv_for_pvc(pvc)
        if pv is None:
            pv = self._maybe_provision(pvc)
            if pv is None:
                self._ensure_pvc_phase(pvc, "Pending")
                return
        self._bind(pvc, pv)

    def _handle_pvc_deleted(self, pvc) -> None:
        spec = pvc.spec or {}
        pv_name = spec.get("volumeName")
        if not pv_name:
            return
        pv = self._store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv_name)
        if pv is None:
            return
        self._delete_volume_attachments(pv.name)
        policy = self._pv_reclaim_policy(pv)
        if policy == "Delete":
            self._delete_pv_and_backing(pv)
            return
        pv_status = dict(pv.status or {})
        if pv_status.get("phase") == "Released":
            return
        pv_status["phase"] = "Released"
        self._store.upsert(
            CORE_GROUP,
            CORE_VERSION,
            PV_RESOURCE,
            None,
            pv.name,
            pv.metadata,
            pv.spec,
            status=pv_status,
        )

    def _pvc_is_bound(self, pvc) -> bool:
        spec = pvc.spec or {}
        status = pvc.status or {}
        return bool(spec.get("volumeName")) or status.get("phase") == "Bound"

    def _match_pv_for_pvc(self, pvc):
        spec = pvc.spec or {}
        volume_name = spec.get("volumeName")
        if volume_name:
            pv = self._store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, volume_name)
            if pv and self._pv_claim_ref_conflicts(pv, pvc):
                return None
            return pv
        pv = self._find_prebound_pv(pvc)
        if pv is not None:
            return pv
        return self._find_available_pv(pvc)

    def _find_prebound_pv(self, pvc):
        try:
            pvs = self._store.list_all(CORE_GROUP, CORE_VERSION, PV_RESOURCE)
        except Exception:
            return None
        for pv in pvs:
            if self._pv_claim_ref_matches(pv, pvc):
                return pv
        return None

    def _find_available_pv(self, pvc):
        pvc_sc = self._pvc_storage_class(pvc)
        pvc_modes = set((pvc.spec or {}).get("accessModes") or [])
        pvc_vm = (pvc.spec or {}).get("volumeMode")
        try:
            pvs = self._store.list_all(CORE_GROUP, CORE_VERSION, PV_RESOURCE)
        except Exception:
            return None
        for pv in pvs:
            if self._pv_claim_ref_conflicts(pv, pvc):
                continue
            if not self._pv_is_available(pv):
                continue
            pv_sc = self._pv_storage_class(pv)
            if (pvc_sc or pv_sc) and pvc_sc != pv_sc:
                continue
            if pvc_modes and not self._modes_match(pvc_modes, pv):
                continue
            if pvc_vm and not self._volume_mode_match(pvc_vm, pv):
                continue
            return pv
        return None

    def _maybe_provision(self, pvc):
        sc = self._storage_class_for_pvc(pvc)
        if sc is None:
            return None
        sc_spec = sc.spec or {}
        binding_mode = str(sc_spec.get("volumeBindingMode") or "")
        selected_node = self._selected_node(pvc)
        if binding_mode == WAIT_FOR_FIRST_CONSUMER and not selected_node:
            self._record_pvc_event(
                pvc,
                "WaitForFirstConsumer",
                "waiting for selected node before provisioning",
            )
            return None
        provisioner = str(sc_spec.get("provisioner") or "")
        if provisioner == NFS_PROVISIONER:
            return self._provision_nfs(pvc, sc)
        if provisioner == LOCAL_PATH_PROVISIONER:
            return self._provision_local_path(pvc, sc, selected_node=selected_node)
        return None

    def _pv_is_available(self, pv) -> bool:
        phase = (pv.status or {}).get("phase")
        if phase in (None, "", "Available"):
            return True
        if phase == "Bound":
            return False
        return False

    def _pvc_storage_class(self, pvc) -> str | None:
        spec = pvc.spec or {}
        sc = spec.get("storageClassName")
        if sc:
            return str(sc)
        if self._default_class is not None:
            return self._default_class.name
        return None

    @staticmethod
    def _pv_storage_class(pv) -> str | None:
        spec = pv.spec or {}
        sc = spec.get("storageClassName")
        return str(sc) if sc else None

    @staticmethod
    def _modes_match(pvc_modes: set[str], pv) -> bool:
        pv_modes = set((pv.spec or {}).get("accessModes") or [])
        if not pv_modes:
            return True
        return pvc_modes.issubset(pv_modes)

    @staticmethod
    def _volume_mode_match(pvc_vm: str, pv) -> bool:
        pv_vm = (pv.spec or {}).get("volumeMode")
        if not pv_vm:
            return True
        return str(pv_vm) == str(pvc_vm)

    def _bind(self, pvc, pv) -> None:
        pvc_spec = dict(pvc.spec or {})
        pvc_status = dict(pvc.status or {})
        pv_spec = dict(pv.spec or {})
        pv_status = dict(pv.status or {})

        if pvc_spec.get("volumeName") != pv.name:
            pvc_spec["volumeName"] = pv.name
        if not pvc_spec.get("storageClassName") and self._default_class is not None:
            pvc_spec["storageClassName"] = self._default_class.name
        if pvc_status.get("phase") != "Bound":
            pvc_status["phase"] = "Bound"
        if "capacity" not in pvc_status and pv_spec.get("capacity"):
            pvc_status["capacity"] = pv_spec.get("capacity")
        if "accessModes" not in pvc_status and pvc_spec.get("accessModes"):
            pvc_status["accessModes"] = pvc_spec.get("accessModes")

        claim_ref = dict(pv_spec.get("claimRef") or {})
        if not self._pv_claim_ref_matches(pv, pvc):
            claim_ref["name"] = pvc.name
            claim_ref["namespace"] = pvc.namespace or ""
            uid = (pvc.metadata or {}).get("uid")
            if uid:
                claim_ref["uid"] = uid
            pv_spec["claimRef"] = claim_ref
        if pv_status.get("phase") != "Bound":
            pv_status["phase"] = "Bound"

        if not self._binding_up_to_date(pvc, pvc_spec, pvc_status):
            self._store.upsert(
                CORE_GROUP,
                CORE_VERSION,
                PVC_RESOURCE,
                pvc.namespace,
                pvc.name,
                pvc.metadata,
                pvc_spec,
                status=pvc_status,
            )
        if not self._binding_up_to_date(pv, pv_spec, pv_status):
            self._store.upsert(
                CORE_GROUP,
                CORE_VERSION,
                PV_RESOURCE,
                None,
                pv.name,
                pv.metadata,
                pv_spec,
                status=pv_status,
            )

    @staticmethod
    def _binding_up_to_date(obj, spec: dict[str, Any], status: dict[str, Any]) -> bool:
        try:
            return obj.spec == spec and obj.status == status
        except Exception:
            return False

    def _reconcile_csi_attachment(self, pvc, pv) -> None:
        pv_spec = pv.spec or {}
        csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
        if not isinstance(csi, dict):
            return
        node = self._selected_node(pvc)
        if not node:
            return
        driver = str(csi.get("driver") or "")
        handle = str(csi.get("volumeHandle") or "")
        if not driver or not handle:
            self._record_pvc_event(pvc, "InvalidVolume", "CSI PV missing driver/volumeHandle")
            return
        attachments = self._volume_attachments_for_pv(pv.name)
        single_writer = self._is_single_writer(pv_spec)
        conflict_nodes = sorted(
            {
                other
                for att in attachments
                if (other := self._attachment_node(att))
                and other != node
                and self._attachment_attached(att)
            }
        )
        if conflict_nodes and single_writer:
            nodes = ", ".join(conflict_nodes)
            self._record_pvc_event(
                pvc,
                "MultiAttachForbidden",
                f"volume {pv.name} already attached to node(s): {nodes}",
            )
            return

        name = self._volume_attachment_name(pv.name, node)
        annotations = {STORAGE_PROVISIONER_ANNOTATION: driver}
        meta = {"name": name, "annotations": annotations}
        spec = {
            "attacher": driver,
            "nodeName": node,
            "source": {"persistentVolumeName": pv.name},
        }
        status = {
            "attached": True,
            "attachmentMetadata": {"volumeHandle": handle},
        }
        self._store.upsert(
            SC_GROUP,
            SC_VERSION,
            VA_RESOURCE,
            None,
            name,
            meta,
            spec,
            status=status,
        )

        if not single_writer:
            return
        for att in attachments:
            other_node = self._attachment_node(att)
            if not other_node or other_node == node:
                continue
            if self._attachment_attached(att):
                continue
            with suppress(Exception):
                self._store.delete(SC_GROUP, SC_VERSION, VA_RESOURCE, None, att.name)

    def _storage_class_for_pvc(self, pvc):
        name = self._pvc_storage_class(pvc)
        if not name:
            return None
        return self._store.get(SC_GROUP, SC_VERSION, SC_RESOURCE, None, name)

    @staticmethod
    def _selected_node(pvc) -> str | None:
        meta = pvc.metadata or {}
        annotations = meta.get("annotations") if isinstance(meta, dict) else {}
        if not isinstance(annotations, dict):
            return None
        node = annotations.get(SELECTED_NODE_ANNOTATION)
        return str(node) if node else None

    @staticmethod
    def _pvc_uid(pvc) -> str | None:
        meta = pvc.metadata or {}
        if not isinstance(meta, dict):
            return None
        uid = meta.get("uid")
        return str(uid) if uid else None

    @staticmethod
    def _pvc_requested_capacity(pvc) -> dict[str, str]:
        spec = pvc.spec or {}
        resources = spec.get("resources") if isinstance(spec, dict) else {}
        requests = resources.get("requests") if isinstance(resources, dict) else {}
        storage = requests.get("storage") if isinstance(requests, dict) else None
        if storage:
            return {"storage": str(storage)}
        return {}

    def _provision_nfs(self, pvc, sc):
        uid = self._pvc_uid(pvc)
        if not uid:
            self._record_pvc_event(pvc, "ProvisioningFailed", "PVC uid missing")
            return None
        pv_name = f"pvc-{uid}"
        existing = self._store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv_name)
        if existing is not None:
            if self._pv_claim_ref_conflicts(existing, pvc):
                self._record_pvc_event(
                    pvc,
                    "ProvisioningConflict",
                    f"existing PV {pv_name} is claimed by another PVC",
                )
                return None
            return existing

        sc_spec = sc.spec or {}
        params = sc_spec.get("parameters") if isinstance(sc_spec, dict) else {}
        params = params if isinstance(params, dict) else {}
        server = params.get("server") or os.getenv("AE_STORAGE_NFS_SERVER")
        base_path = params.get("path") or os.getenv("AE_STORAGE_NFS_PATH")
        if not server or not base_path:
            self._record_pvc_event(
                pvc,
                "ProvisioningFailed",
                "NFS storage class requires parameters.server and parameters.path",
            )
            return None

        base_path = str(base_path).rstrip("/") or "/"
        host_root_raw = params.get("hostPath") or os.getenv("AE_STORAGE_NFS_HOSTPATH")
        host_root = Path(host_root_raw).expanduser() if host_root_raw else None
        nfs_path = base_path
        annotations = {
            PROVISIONED_BY_ANNOTATION: NFS_PROVISIONER,
            STORAGE_PROVISIONER_ANNOTATION: NFS_PROVISIONER,
        }
        if host_root is not None:
            host_path = host_root / uid
            if self._within_root(host_root, host_path):
                host_path.mkdir(parents=True, exist_ok=True)
                nfs_path = f"{base_path}/{uid}".replace("//", "/")
                annotations[NFS_HOST_ROOT_ANNOTATION] = str(host_root)
                annotations[NFS_HOST_PATH_ANNOTATION] = str(host_path)
            else:
                self._record_pvc_event(
                    pvc,
                    "ProvisioningFailed",
                    f"refusing to create NFS path outside host root: {host_path}",
                )
                return None

        pv_spec = {
            "capacity": self._pvc_requested_capacity(pvc),
            "accessModes": list((pvc.spec or {}).get("accessModes") or []),
            "volumeMode": (pvc.spec or {}).get("volumeMode") or "Filesystem",
            "storageClassName": sc.name,
            "persistentVolumeReclaimPolicy": sc_spec.get("reclaimPolicy") or "Retain",
            "mountOptions": list(sc_spec.get("mountOptions") or []),
            "claimRef": self._claim_ref_for(pvc),
            "nfs": {
                "server": str(server),
                "path": nfs_path,
                "readOnly": False,
            },
        }
        pv_meta = {"name": pv_name, "annotations": annotations}
        status = {"phase": "Available"}
        return self._store.upsert(
            CORE_GROUP,
            CORE_VERSION,
            PV_RESOURCE,
            None,
            pv_name,
            pv_meta,
            pv_spec,
            status=status,
        )

    def _provision_local_path(self, pvc, sc, *, selected_node: str | None):
        sc_spec = sc.spec or {}
        binding_mode = str(sc_spec.get("volumeBindingMode") or "")
        if binding_mode == WAIT_FOR_FIRST_CONSUMER and not selected_node:
            return None
        uid = self._pvc_uid(pvc)
        if not uid:
            self._record_pvc_event(pvc, "ProvisioningFailed", "PVC uid missing")
            return None
        pv_name = f"pvc-{uid}"
        existing = self._store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv_name)
        if existing is not None:
            if self._pv_claim_ref_conflicts(existing, pvc):
                self._record_pvc_event(
                    pvc,
                    "ProvisioningConflict",
                    f"existing PV {pv_name} is claimed by another PVC",
                )
                return None
            return existing

        params = sc_spec.get("parameters") if isinstance(sc_spec, dict) else {}
        params = params if isinstance(params, dict) else {}
        host_root_raw = params.get("hostPath") or os.getenv("AE_STORAGE_ROOT")
        host_root = Path(host_root_raw).expanduser() if host_root_raw else DEFAULT_LOCAL_ROOT
        node_seg = selected_node or "unbound"
        host_path = host_root / node_seg / uid
        if not self._within_root(host_root, host_path):
            self._record_pvc_event(
                pvc,
                "ProvisioningFailed",
                f"refusing to create local path outside host root: {host_path}",
            )
            return None
        host_path.mkdir(parents=True, exist_ok=True)

        annotations = {
            PROVISIONED_BY_ANNOTATION: LOCAL_PATH_PROVISIONER,
            STORAGE_PROVISIONER_ANNOTATION: LOCAL_PATH_PROVISIONER,
            LOCAL_HOST_ROOT_ANNOTATION: str(host_root),
            LOCAL_HOST_PATH_ANNOTATION: str(host_path),
        }
        pv_spec = {
            "capacity": self._pvc_requested_capacity(pvc),
            "accessModes": list((pvc.spec or {}).get("accessModes") or []),
            "volumeMode": (pvc.spec or {}).get("volumeMode") or "Filesystem",
            "storageClassName": sc.name,
            "persistentVolumeReclaimPolicy": sc_spec.get("reclaimPolicy") or "Delete",
            "mountOptions": list(sc_spec.get("mountOptions") or []),
            "claimRef": self._claim_ref_for(pvc),
            "hostPath": {"path": str(host_path)},
        }
        if selected_node:
            pv_spec["nodeAffinity"] = self._node_affinity(selected_node)
        pv_meta = {"name": pv_name, "annotations": annotations}
        status = {"phase": "Available"}
        return self._store.upsert(
            CORE_GROUP,
            CORE_VERSION,
            PV_RESOURCE,
            None,
            pv_name,
            pv_meta,
            pv_spec,
            status=status,
        )

    @staticmethod
    def _pv_claim_ref_matches(pv, pvc) -> bool:
        claim = (pv.spec or {}).get("claimRef") or {}
        return claim.get("name") == pvc.name and claim.get("namespace") == (pvc.namespace or "")

    @staticmethod
    def _pv_claim_ref_conflicts(pv, pvc) -> bool:
        claim = (pv.spec or {}).get("claimRef") or {}
        if not claim:
            return False
        if claim.get("name") == pvc.name and claim.get("namespace") == (pvc.namespace or ""):
            return False
        return True

    def _ensure_pvc_phase(self, pvc, phase: str) -> None:
        status = dict(pvc.status or {})
        if status.get("phase") == phase:
            return
        status["phase"] = phase
        self._store.upsert(
            CORE_GROUP,
            CORE_VERSION,
            PVC_RESOURCE,
            pvc.namespace,
            pvc.name,
            pvc.metadata,
            pvc.spec,
            status=status,
        )

    @staticmethod
    def _claim_ref_for(pvc) -> dict[str, Any]:
        claim_ref = {"name": pvc.name, "namespace": pvc.namespace or ""}
        uid = (pvc.metadata or {}).get("uid")
        if uid:
            claim_ref["uid"] = uid
        return claim_ref

    @staticmethod
    def _node_affinity(node_name: str) -> dict[str, Any]:
        return {
            "required": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [node_name],
                            }
                        ]
                    }
                ]
            }
        }

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

    @staticmethod
    def _pv_reclaim_policy(pv) -> str:
        spec = pv.spec or {}
        policy = spec.get("persistentVolumeReclaimPolicy") or spec.get("reclaimPolicy")
        if not policy:
            return "Retain"
        return str(policy)

    def _volume_attachments_for_pv(self, pv_name: str) -> list[Any]:
        try:
            attachments = self._store.list_all(SC_GROUP, SC_VERSION, VA_RESOURCE)
        except Exception:
            return []
        out: list[Any] = []
        for att in attachments:
            spec = att.spec or {}
            source = spec.get("source") if isinstance(spec, dict) else {}
            if not isinstance(source, dict):
                continue
            if source.get("persistentVolumeName") == pv_name:
                out.append(att)
        return out

    def _delete_volume_attachments(self, pv_name: str) -> None:
        for att in self._volume_attachments_for_pv(pv_name):
            with suppress(Exception):
                self._store.delete(SC_GROUP, SC_VERSION, VA_RESOURCE, None, att.name)

    @staticmethod
    def _is_single_writer(pv_spec: dict[str, Any]) -> bool:
        modes = set(pv_spec.get("accessModes") or []) if isinstance(pv_spec, dict) else set()
        if modes & {"ReadWriteMany", "ReadOnlyMany"}:
            return False
        return True

    @staticmethod
    def _attachment_node(att) -> str | None:
        spec = att.spec or {}
        if not isinstance(spec, dict):
            return None
        node = spec.get("nodeName")
        return str(node) if node else None

    @staticmethod
    def _attachment_attached(att) -> bool:
        status = att.status or {}
        if not isinstance(status, dict):
            return False
        return bool(status.get("attached"))

    def _volume_attachment_name(self, pv_name: str, node: str) -> str:
        token = uuid.uuid5(uuid.NAMESPACE_DNS, f"{pv_name}:{node}").hex[:8]
        raw = f"va-{pv_name}-{node}-{token}"
        return self._sanitize_name(raw)

    @staticmethod
    def _sanitize_name(value: str) -> str:
        safe = value.lower().replace("/", "-").replace("_", "-").replace(".", "-")
        safe = "".join(ch if (ch.isalnum() or ch == "-") else "-" for ch in safe).strip("-")
        if not safe:
            safe = "va"
        return safe[:253]

    def _delete_pv_and_backing(self, pv) -> None:
        self._delete_volume_attachments(pv.name)
        self._cleanup_backing_path(pv)
        try:
            self._store.delete(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv.name)
        except Exception:
            LOGGER.exception("failed to delete PV %s", pv.name)

    def _cleanup_backing_path(self, pv) -> None:
        meta = pv.metadata or {}
        annotations = meta.get("annotations") if isinstance(meta, dict) else {}
        if not isinstance(annotations, dict):
            return
        host_root = annotations.get(NFS_HOST_ROOT_ANNOTATION) or annotations.get(
            LOCAL_HOST_ROOT_ANNOTATION
        )
        host_path = annotations.get(NFS_HOST_PATH_ANNOTATION) or annotations.get(
            LOCAL_HOST_PATH_ANNOTATION
        )
        if not host_root or not host_path:
            return
        root = Path(str(host_root)).expanduser()
        path = Path(str(host_path)).expanduser()
        if not self._within_root(root, path):
            LOGGER.warning("skipping cleanup outside root: %s (root=%s)", path, root)
            return
        if not path.exists():
            return
        try:
            for child in sorted(path.glob("**/*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)  # type: ignore[call-arg]
            for child in sorted(path.glob("**/*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            path.rmdir()
        except Exception:
            LOGGER.exception("failed to clean up backing path %s", path)

    def _record_pvc_event(self, pvc, reason: str, message: str) -> None:
        if not pvc.namespace or not pvc.name:
            return
        now = datetime.now(UTC)
        ts = now.isoformat().replace("+00:00", "Z")
        name = f"{pvc.name}.{int(time.time())}.{uuid.uuid4().hex[:6]}"
        involved = {
            "kind": "PersistentVolumeClaim",
            "name": pvc.name,
            "namespace": pvc.namespace,
        }
        uid = (pvc.metadata or {}).get("uid")
        if uid:
            involved["uid"] = uid
        spec = {
            "involvedObject": involved,
            "reason": str(reason),
            "message": str(message),
            "type": "Warning",
            "firstTimestamp": ts,
            "lastTimestamp": ts,
            "eventTime": ts,
            "count": 1,
            "source": {"component": "storage-controller"},
        }
        metadata = {"name": name, "namespace": pvc.namespace}
        try:
            self._store.upsert(
                CORE_GROUP,
                CORE_VERSION,
                EVENT_RESOURCE,
                pvc.namespace,
                name,
                metadata,
                spec,
                status={},
            )
        except Exception:
            LOGGER.exception(
                "failed to record PVC event %s/%s: %s",
                pvc.namespace,
                pvc.name,
                reason,
            )


def seed_storage_classes(store: ObjectStore) -> int:
    """Helper to seed StorageClass definitions from config."""
    return StorageController(store).sync()
