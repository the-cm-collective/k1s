"""Storage controller skeleton for NetFS and StorageClass seeding."""

from __future__ import annotations

import logging
import threading
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
            try:
                gen.close()  # type: ignore[attr-defined]
            except Exception:
                pass

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
            try:
                gen.close()  # type: ignore[attr-defined]
            except Exception:
                pass

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
            return

        pv = self._match_pv_for_pvc(pvc)
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
        status = pvc.status or {}
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
            if pvc_sc or pv_sc:
                if pvc_sc != pv_sc:
                    continue
            if pvc_modes and not self._modes_match(pvc_modes, pv):
                continue
            if pvc_vm and not self._volume_mode_match(pvc_vm, pv):
                continue
            return pv
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


def seed_storage_classes(store: ObjectStore) -> int:
    """Helper to seed StorageClass definitions from config."""
    return StorageController(store).sync()
