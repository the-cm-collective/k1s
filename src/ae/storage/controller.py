"""Storage controller for StorageClass seeding and PVC/PV reconciliation."""

from __future__ import annotations

import logging
import os
import shutil
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
    StorageQuotaConfig,
    load_storage_classes,
    load_storage_quotas,
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
CSC_RESOURCE = "csistoragecapacities"

SNAP_GROUP = "snapshot.storage.k8s.io"
SNAP_VERSION = "v1"
SNAPSHOT_RESOURCE = "volumesnapshots"
SNAPCLASS_RESOURCE = "volumesnapshotclasses"
SNAPCONTENT_RESOURCE = "volumesnapshotcontents"
SNAP_DEFAULT_CLASS_ANNOTATIONS = ("snapshot.storage.kubernetes.io/is-default-class",)

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
SNAP_HOST_ROOT_ANNOTATION = "k1s.io/snapshot-host-root"
SNAP_HOST_PATH_ANNOTATION = "k1s.io/snapshot-host-path"
SNAP_SOURCE_PV_ANNOTATION = "k1s.io/snapshot-source-pv"
SNAPSHOT_SOURCE_ANNOTATION = "k1s.io/snapshot-source"
SNAPSHOT_DIRNAME = ".snapshots"
DEFAULT_LOCAL_ROOT = Path(os.getenv("AE_STORAGE_ROOT", "/var/lib/ae/storage"))


class StorageController:
    """Seed StorageClass objects from config and prepare for PVC/PV binding."""

    def __init__(self, store: ObjectStore, *, config: StorageConfig | None = None) -> None:
        self._store = store
        self._config = config or StorageConfig.from_env()
        self._storage_classes = load_storage_classes(self._config.provisioners_path)
        self._default_class = self._resolve_default(self._storage_classes)
        self._default_snapshot_class: str | None = None
        self._volume_health: dict[str, bool] = {}
        self._storage_quotas = load_storage_quotas(self._config.quotas_path)
        self._capacity_namespace = os.getenv("AE_NETFS_CAPACITY_NAMESPACE", "default")
        self._stop = threading.Event()
        self._pvc_thread: threading.Thread | None = None
        self._pv_thread: threading.Thread | None = None
        self._snapshot_thread: threading.Thread | None = None

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
        self._snapshot_thread = threading.Thread(
            target=self._watch_snapshots, name="storage-snapshot-watch", daemon=True
        )
        self._pvc_thread.start()
        self._pv_thread.start()
        self._snapshot_thread.start()
        self.reconcile_once()

    def stop(self) -> None:
        self._stop.set()

    def reconcile_once(self) -> None:
        """Run a single PVC/PV binding pass."""
        self._reconcile_all()
        self._reconcile_snapshots()
        self._check_volume_health()
        self._reconcile_storage_capacity()

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
        if sc.allowed_topologies:
            spec["allowedTopologies"] = list(sc.allowed_topologies)
        if sc.topology_keys:
            spec["topologyKeys"] = list(sc.topology_keys)
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

    def _watch_snapshots(self) -> None:
        gen = self._store.watch(
            SNAP_GROUP,
            SNAP_VERSION,
            SNAPSHOT_RESOURCE,
            None,
            heartbeat_seconds=5,
            allow_bookmarks=True,
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev == "DELETED":
                    self._handle_snapshot_deleted(obj)
                    continue
                if ev in {"ADDED", "MODIFIED"}:
                    self._reconcile_snapshot(obj)
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

    def _reconcile_snapshots(self) -> None:
        try:
            snapshots = self._store.list_all(SNAP_GROUP, SNAP_VERSION, SNAPSHOT_RESOURCE)
        except Exception:
            snapshots = []
        for snapshot in snapshots:
            self._reconcile_snapshot(snapshot)

    def _check_volume_health(self) -> None:
        try:
            pvs = self._store.list_all(CORE_GROUP, CORE_VERSION, PV_RESOURCE)
        except Exception:
            return
        for pv in pvs:
            host_backing = self._pv_host_backing(pv)
            if host_backing is None:
                continue
            _root, path = host_backing
            healthy = path.exists()
            prev = self._volume_health.get(pv.name)
            if prev is not None and prev == healthy:
                continue
            self._volume_health[pv.name] = healthy
            claim_ref = (pv.spec or {}).get("claimRef") or {}
            if not isinstance(claim_ref, dict):
                continue
            pvc_ns = claim_ref.get("namespace")
            pvc_name = claim_ref.get("name")
            if not pvc_ns or not pvc_name:
                continue
            pvc = self._store.get(CORE_GROUP, CORE_VERSION, PVC_RESOURCE, pvc_ns, pvc_name)
            if pvc is None:
                continue
            if healthy:
                self._record_pvc_event(pvc, "VolumeHealthy", f"backing path is present: {path}")
            else:
                self._record_pvc_event(pvc, "VolumeUnhealthy", f"backing path missing: {path}")

    def _reconcile_storage_capacity(self) -> None:
        try:
            storage_classes = self._store.list_all(SC_GROUP, SC_VERSION, SC_RESOURCE)
        except Exception:
            storage_classes = []
        try:
            pvs = self._store.list_all(CORE_GROUP, CORE_VERSION, PV_RESOURCE)
        except Exception:
            pvs = []

        targets = self._capacity_targets(storage_classes, pvs)
        if not targets:
            return

        for target in targets:
            sc_name = target["storage_class"]
            host_root = target["host_root"]
            topology = target.get("node_topology")
            capacity = self._path_capacity(host_root)
            if capacity is None:
                continue
            name = self._capacity_name(sc_name, topology, host_root)
            spec: dict[str, Any] = {
                "storageClassName": sc_name,
                "capacity": str(capacity),
            }
            if topology:
                spec["nodeTopology"] = topology
            meta = {"name": name, "namespace": self._capacity_namespace}
            existing = self._store.get(
                SC_GROUP, SC_VERSION, CSC_RESOURCE, self._capacity_namespace, name
            )
            if existing is not None and existing.spec == spec:
                continue
            self._store.upsert(
                SC_GROUP,
                SC_VERSION,
                CSC_RESOURCE,
                self._capacity_namespace,
                name,
                meta,
                spec,
                status=existing.status if existing is not None else {},
            )

    def _capacity_targets(self, storage_classes: list[Any], pvs: list[Any]) -> list[dict[str, Any]]:
        targets: dict[str, dict[str, Any]] = {}
        pv_roots: set[tuple[str, Path]] = set()

        for pv in pvs:
            sc_name = self._pv_storage_class(pv)
            if not sc_name:
                continue
            backing = self._pv_host_backing(pv)
            if backing is None:
                continue
            host_root, _host_path = backing
            topology = self._pv_node_topology(pv)
            key = self._capacity_key(sc_name, topology, host_root)
            targets[key] = {
                "storage_class": sc_name,
                "host_root": host_root,
                "node_topology": topology,
            }
            pv_roots.add((sc_name, host_root))

        for sc in storage_classes:
            spec = sc.spec or {}
            if not isinstance(spec, dict):
                continue
            provisioner = str(spec.get("provisioner") or "")
            if provisioner not in {NFS_PROVISIONER, LOCAL_PATH_PROVISIONER}:
                continue
            host_root = self._storage_class_host_root(spec, provisioner)
            if host_root is None:
                continue
            if (sc.name, host_root) in pv_roots:
                continue
            topology = self._storage_class_topology(spec)
            key = self._capacity_key(sc.name, topology, host_root)
            targets.setdefault(
                key,
                {
                    "storage_class": sc.name,
                    "host_root": host_root,
                    "node_topology": topology,
                },
            )

        return list(targets.values())

    def _storage_class_host_root(
        self, spec: dict[str, Any], provisioner: str
    ) -> Path | None:
        params = spec.get("parameters") if isinstance(spec, dict) else {}
        params = params if isinstance(params, dict) else {}
        host_root_raw = params.get("hostPath")
        if host_root_raw:
            return Path(str(host_root_raw)).expanduser()
        if provisioner == LOCAL_PATH_PROVISIONER:
            return Path(os.getenv("AE_STORAGE_ROOT", str(DEFAULT_LOCAL_ROOT))).expanduser()
        return None

    @staticmethod
    def _storage_class_topology(spec: dict[str, Any]) -> dict[str, Any] | None:
        allowed = spec.get("allowedTopologies")
        if isinstance(allowed, list) and allowed:
            if len(allowed) == 1 and isinstance(allowed[0], dict):
                exprs = allowed[0].get("matchLabelExpressions")
                if isinstance(exprs, list) and exprs:
                    return {"matchExpressions": list(exprs)}
        topo_keys = spec.get("topologyKeys")
        if isinstance(topo_keys, list) and topo_keys:
            exprs = [{"key": str(k), "operator": "Exists"} for k in topo_keys if k]
            if exprs:
                return {"matchExpressions": exprs}
        return None

    @staticmethod
    def _pv_node_topology(pv) -> dict[str, Any] | None:
        spec = pv.spec or {}
        if not isinstance(spec, dict):
            return None
        affinity = spec.get("nodeAffinity")
        if not isinstance(affinity, dict):
            return None
        required = affinity.get("required")
        if not isinstance(required, dict):
            return None
        terms = required.get("nodeSelectorTerms")
        if not isinstance(terms, list) or not terms:
            return None
        term = terms[0] if isinstance(terms[0], dict) else None
        if not term:
            return None
        exprs = term.get("matchExpressions")
        if not isinstance(exprs, list) or not exprs:
            return None
        return {"matchExpressions": list(exprs)}

    @staticmethod
    def _capacity_key(
        sc_name: str, topology: dict[str, Any] | None, host_root: Path
    ) -> str:
        topo_sig = StorageController._topology_signature(topology)
        return f"{sc_name}:{host_root}:{topo_sig}"

    @staticmethod
    def _topology_signature(topology: dict[str, Any] | None) -> str:
        if not topology:
            return ""
        exprs = topology.get("matchExpressions") if isinstance(topology, dict) else []
        if not isinstance(exprs, list):
            return ""
        parts = []
        for expr in exprs:
            if not isinstance(expr, dict):
                continue
            key = str(expr.get("key") or "")
            op = str(expr.get("operator") or "")
            values = expr.get("values") if isinstance(expr.get("values"), list) else []
            vals = ",".join(sorted(str(v) for v in values if v is not None))
            parts.append(f"{key}:{op}:{vals}")
        return "|".join(sorted(parts))

    @staticmethod
    def _capacity_name(
        sc_name: str, topology: dict[str, Any] | None, host_root: Path
    ) -> str:
        topo_sig = StorageController._topology_signature(topology)
        token = uuid.uuid5(uuid.NAMESPACE_DNS, f"{sc_name}:{host_root}:{topo_sig}").hex[:8]
        raw = f"csc-{sc_name}-{token}"
        return StorageController._sanitize_name(raw)

    @staticmethod
    def _path_capacity(path: Path) -> int | None:
        try:
            if not path.exists():
                return None
            usage = shutil.disk_usage(path)
            return int(usage.free)
        except Exception:
            return None

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
                    self._maybe_expand_bound_volume(pvc, pv)
            return

        if not self._quota_allows_pvc(pvc):
            self._ensure_pvc_phase(pvc, "Pending")
            return

        pv = self._match_pv_for_pvc(pvc)
        if pv is None:
            pv = self._maybe_provision(pvc)
            if pv is None:
                self._ensure_pvc_phase(pvc, "Pending")
                return
        self._bind(pvc, pv)
        self._maybe_expand_bound_volume(pvc, pv)

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
        snapshot_info, blocked = self._snapshot_source_for_pvc(pvc)
        if blocked:
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
            return self._provision_nfs(pvc, sc, snapshot_info=snapshot_info)
        if provisioner == LOCAL_PATH_PROVISIONER:
            return self._provision_local_path(
                pvc, sc, selected_node=selected_node, snapshot_info=snapshot_info
            )
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

    def _quota_for_namespace(self, namespace: str | None) -> StorageQuotaConfig | None:
        if not namespace:
            return None
        for quota in self._storage_quotas:
            if quota.namespace == namespace:
                return quota
        return None

    def _quota_bytes(self, namespace: str | None) -> int | None:
        quota = self._quota_for_namespace(namespace)
        if quota is None:
            return None
        return self._quantity_bytes(quota.hard_storage)

    def _namespace_storage_usage(self, namespace: str, *, exclude: tuple[str, str | None] | None) -> int:
        try:
            pvcs = self._store.list_all(CORE_GROUP, CORE_VERSION, PVC_RESOURCE)
        except Exception:
            return 0
        total = 0
        for pvc in pvcs:
            if pvc.namespace != namespace:
                continue
            if exclude is not None:
                name, uid = exclude
                if pvc.name == name:
                    meta = pvc.metadata or {}
                    pvc_uid = meta.get("uid") if isinstance(meta, dict) else None
                    if uid is None or uid == pvc_uid:
                        continue
            requested = self._pvc_requested_storage(pvc)
            if not requested:
                continue
            req_bytes = self._quantity_bytes(requested)
            if req_bytes is None:
                continue
            total += req_bytes
        return total

    def _quota_allows_pvc(self, pvc) -> bool:
        quota_bytes = self._quota_bytes(pvc.namespace)
        if quota_bytes is None:
            return True
        requested_raw = self._pvc_requested_storage(pvc)
        if not requested_raw:
            return True
        requested = self._quantity_bytes(requested_raw)
        if requested is None:
            return True
        pvc_uid = (pvc.metadata or {}).get("uid") if isinstance(pvc.metadata, dict) else None
        usage = self._namespace_storage_usage(
            pvc.namespace or "", exclude=(pvc.name, pvc_uid)
        )
        if usage + requested > quota_bytes:
            self._record_pvc_event(
                pvc,
                "StorageQuotaExceeded",
                f"namespace storage quota exceeded: requested {requested_raw}",
            )
            return False
        return True

    def _quota_allows_expansion(self, pvc, requested_bytes: int) -> bool:
        quota_bytes = self._quota_bytes(pvc.namespace)
        if quota_bytes is None:
            return True
        pvc_uid = (pvc.metadata or {}).get("uid") if isinstance(pvc.metadata, dict) else None
        usage = self._namespace_storage_usage(
            pvc.namespace or "", exclude=(pvc.name, pvc_uid)
        )
        return usage + requested_bytes <= quota_bytes

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

    def _maybe_expand_bound_volume(self, pvc, pv) -> None:
        requested_raw = self._pvc_requested_storage(pvc)
        if not requested_raw:
            return
        pv_spec = dict(pv.spec or {})
        capacity = pv_spec.get("capacity") if isinstance(pv_spec.get("capacity"), dict) else {}
        current_raw = capacity.get("storage") if isinstance(capacity, dict) else None
        if not current_raw:
            return

        requested = self._quantity_bytes(requested_raw)
        current = self._quantity_bytes(str(current_raw))
        if requested is None or current is None:
            return
        if requested <= current:
            return
        if not self._quota_allows_expansion(pvc, requested):
            self._record_pvc_event(
                pvc,
                "StorageQuotaExceeded",
                "expansion would exceed namespace storage quota",
            )
            return

        if not self._storage_class_allows_expansion(pvc, pv):
            self._record_pvc_event(
                pvc,
                "VolumeExpansionForbidden",
                "storage class does not allow expansion",
            )
            return

        capacity["storage"] = str(requested_raw)
        pv_spec["capacity"] = capacity
        pv_status = dict(pv.status or {})
        pv_status["capacity"] = capacity
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

        pvc_spec = dict(pvc.spec or {})
        pvc_status = dict(pvc.status or {})
        pvc_status["capacity"] = capacity
        if pvc_status.get("phase") != "Bound":
            pvc_status["phase"] = "Bound"
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
        self._record_pvc_event(pvc, "VolumeExpanded", f"expanded to {requested_raw}")

    def _storage_class_allows_expansion(self, pvc, pv) -> bool:
        sc_name = self._pv_storage_class(pv) or self._pvc_storage_class(pvc)
        if not sc_name:
            return False
        sc = self._store.get(SC_GROUP, SC_VERSION, SC_RESOURCE, None, sc_name)
        if sc is None:
            return False
        sc_spec = sc.spec or {}
        if not isinstance(sc_spec, dict):
            return False
        return bool(sc_spec.get("allowVolumeExpansion"))

    @staticmethod
    def _pvc_requested_storage(pvc) -> str | None:
        spec = pvc.spec or {}
        resources = spec.get("resources") if isinstance(spec, dict) else {}
        requests = resources.get("requests") if isinstance(resources, dict) else {}
        storage = requests.get("storage") if isinstance(requests, dict) else None
        return str(storage) if storage else None

    @staticmethod
    def _quantity_bytes(raw: str | None) -> int | None:
        if raw is None:
            return None
        try:
            s = str(raw).strip()
            suffixes = {
                "b": 1,
                "k": 1024,
                "kb": 1024,
                "ki": 1024,
                "m": 1024**2,
                "mb": 1024**2,
                "mi": 1024**2,
                "g": 1024**3,
                "gb": 1024**3,
                "gi": 1024**3,
                "t": 1024**4,
                "tb": 1024**4,
                "ti": 1024**4,
            }
            if s.isdigit():
                return int(s)
            num = ""
            unit = ""
            for ch in s:
                if ch.isdigit() or ch == ".":
                    num += ch
                else:
                    unit += ch
            factor = suffixes.get(unit.lower())
            if factor is None:
                return None
            return int(float(num) * factor)
        except Exception:
            return None

    def _snapshot_source_for_pvc(self, pvc) -> tuple[dict[str, Any] | None, bool]:
        spec = pvc.spec or {}
        source = spec.get("dataSource") if isinstance(spec, dict) else None
        if not isinstance(source, dict):
            source = spec.get("dataSourceRef") if isinstance(spec, dict) else None
        if not isinstance(source, dict):
            return None, False
        kind = str(source.get("kind") or "")
        api_group = str(source.get("apiGroup") or "")
        if kind != "VolumeSnapshot":
            return None, False
        if api_group and api_group != SNAP_GROUP:
            return None, False
        snap_name = str(source.get("name") or "")
        snap_ns = str(source.get("namespace") or pvc.namespace or "")
        if not snap_name or not snap_ns:
            self._record_pvc_event(
                pvc,
                "SnapshotInvalid",
                "dataSource VolumeSnapshot requires name and namespace",
            )
            return None, True
        snapshot = self._store.get(
            SNAP_GROUP,
            SNAP_VERSION,
            SNAPSHOT_RESOURCE,
            snap_ns,
            snap_name,
        )
        if snapshot is None:
            self._record_pvc_event(
                pvc,
                "SnapshotNotFound",
                f"snapshot {snap_ns}/{snap_name} not found",
            )
            return None, True
        status = snapshot.status or {}
        if not bool(status.get("readyToUse")):
            self._record_pvc_event(
                pvc,
                "SnapshotNotReady",
                f"snapshot {snap_ns}/{snap_name} is not readyToUse",
            )
            return None, True
        content_name = status.get("boundVolumeSnapshotContentName")
        if not content_name:
            self._record_pvc_event(
                pvc,
                "SnapshotContentMissing",
                f"snapshot {snap_ns}/{snap_name} has no bound content",
            )
            return None, True
        content = self._store.get(
            SNAP_GROUP,
            SNAP_VERSION,
            SNAPCONTENT_RESOURCE,
            None,
            content_name,
        )
        if content is None:
            self._record_pvc_event(
                pvc,
                "SnapshotContentMissing",
                f"snapshot content {content_name} not found",
            )
            return None, True
        backing = self._snapshot_content_backing(content)
        if backing is None:
            self._record_pvc_event(
                pvc,
                "SnapshotRestoreUnavailable",
                f"snapshot {snap_ns}/{snap_name} has no restorable backing path",
            )
            return None, True
        host_root, host_path = backing
        if not host_path.exists():
            self._record_pvc_event(
                pvc,
                "SnapshotRestoreUnavailable",
                f"snapshot backing path missing: {host_path}",
            )
            return None, True
        return {
            "snapshot_ref": f"{snap_ns}/{snap_name}",
            "content_name": str(content_name),
            "host_root": host_root,
            "host_path": host_path,
        }, False

    def _reconcile_snapshot(self, snapshot) -> None:
        spec = dict(snapshot.spec or {})
        class_name = spec.get("volumeSnapshotClassName")
        if not class_name:
            default_class = self._default_snapshot_class_name()
            if default_class:
                spec["volumeSnapshotClassName"] = default_class
                snapshot = self._store.upsert(
                    SNAP_GROUP,
                    SNAP_VERSION,
                    SNAPSHOT_RESOURCE,
                    snapshot.namespace,
                    snapshot.name,
                    snapshot.metadata,
                    spec,
                    status=snapshot.status or {},
                )
                self._record_snapshot_event(
                    snapshot,
                    "SnapshotClassDefaulted",
                    f"defaulted volumeSnapshotClassName to {default_class}",
                )
        source = spec.get("source") if isinstance(spec, dict) else None
        source = source if isinstance(source, dict) else {}
        pvc_name = source.get("persistentVolumeClaimName")
        if not pvc_name:
            self._record_snapshot_event(snapshot, "SnapshotInvalid", "source PVC name is required")
            return
        snap_ns = snapshot.namespace or ""
        pvc = self._store.get(CORE_GROUP, CORE_VERSION, PVC_RESOURCE, snap_ns, pvc_name)
        if pvc is None:
            self._record_snapshot_event(
                snapshot,
                "SnapshotSourceMissing",
                f"PVC {snap_ns}/{pvc_name} not found",
            )
            return
        if not self._pvc_is_bound(pvc):
            self._record_snapshot_event(
                snapshot,
                "SnapshotSourceNotBound",
                f"PVC {snap_ns}/{pvc_name} is not bound",
            )
            return
        pv_name = (pvc.spec or {}).get("volumeName")
        if not pv_name:
            self._record_snapshot_event(
                snapshot,
                "SnapshotSourceNotBound",
                f"PVC {snap_ns}/{pvc_name} has no volumeName",
            )
            return
        pv = self._store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv_name)
        if pv is None:
            self._record_snapshot_event(
                snapshot,
                "SnapshotSourceMissing",
                f"PV {pv_name} not found",
            )
            return

        pv_spec = pv.spec or {}
        csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
        is_csi = isinstance(csi, dict)
        volume_handle = ""
        if is_csi:
            raw_handle = csi.get("volumeHandle")
            if raw_handle:
                volume_handle = str(raw_handle)

        snap_uid = self._snapshot_uid(snapshot)
        content_name = self._snapshot_content_name(snapshot, pv)
        existing_content = self._store.get(
            SNAP_GROUP, SNAP_VERSION, SNAPCONTENT_RESOURCE, None, content_name
        )
        driver, deletion_policy = self._snapshot_driver_and_policy(snapshot, pv)
        restore_size = ((pv.spec or {}).get("capacity") or {}).get("storage")
        existing_status = (
            dict(existing_content.status or {}) if existing_content is not None else {}
        )
        content_ready = bool(existing_status.get("readyToUse"))
        content_restore_size = existing_status.get("restoreSize") or existing_status.get("size")

        snapshot_path: Path | None = None
        host_backing = self._pv_host_backing(pv)
        existing_backing = self._snapshot_content_backing(existing_content)
        ready = False
        if existing_backing is not None and existing_backing[1].exists():
            snapshot_path = existing_backing[1]
            ready = True
        elif host_backing is not None:
            host_root, host_path = host_backing
            snapshot_path = host_root / SNAPSHOT_DIRNAME / snap_uid
            if not self._within_root(host_root, snapshot_path):
                self._record_snapshot_event(
                    snapshot,
                    "SnapshotFailed",
                    f"refusing to snapshot outside host root: {snapshot_path}",
                )
            else:
                snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                if not self._reset_dir(snapshot_path, root=host_root):
                    ready = False
                elif not self._copy_tree_contents(host_path, snapshot_path):
                    self._record_snapshot_event(
                        snapshot,
                        "SnapshotFailed",
                        f"failed to copy data from {host_path}",
                    )
                    ready = False
                else:
                    ready = True
        elif is_csi:
            if not volume_handle:
                self._record_snapshot_event(
                    snapshot,
                    "SnapshotInvalid",
                    f"PV {pv_name} missing CSI volumeHandle",
                )
            elif content_ready:
                ready = True
            else:
                self._record_snapshot_event(
                    snapshot,
                    "SnapshotPending",
                    f"waiting for CSI snapshotter for PV {pv_name}",
                )
        else:
            self._record_snapshot_event(
                snapshot,
                "SnapshotBackingMissing",
                f"PV {pv_name} has no hostPath backing annotations",
            )

        ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        content_annotations = {SNAP_SOURCE_PV_ANNOTATION: str(pv_name)}
        if snapshot_path and host_backing is not None:
            content_annotations[SNAP_HOST_ROOT_ANNOTATION] = str(host_backing[0])
            content_annotations[SNAP_HOST_PATH_ANNOTATION] = str(snapshot_path)
        content_meta = {"name": content_name, "annotations": content_annotations}
        if is_csi and volume_handle:
            content_source = {"volumeHandle": volume_handle}
        else:
            content_source = {"persistentVolumeName": str(pv_name)}
        content_spec = {
            "deletionPolicy": deletion_policy,
            "driver": driver,
            "volumeSnapshotRef": {
                "name": snapshot.name,
                "namespace": snap_ns,
                "uid": snap_uid,
            },
            "source": content_source,
        }
        if host_backing is not None or existing_backing is not None:
            content_status = {"readyToUse": ready, "creationTime": ts}
            if restore_size:
                content_status["restoreSize"] = str(restore_size)
        elif is_csi and existing_status:
            content_status = dict(existing_status)
            content_status.setdefault("creationTime", ts)
            if restore_size and "restoreSize" not in content_status:
                content_status["restoreSize"] = str(restore_size)
        else:
            content_status = {"readyToUse": ready, "creationTime": ts}
            if restore_size:
                content_status["restoreSize"] = str(restore_size)
        self._store.upsert(
            SNAP_GROUP,
            SNAP_VERSION,
            SNAPCONTENT_RESOURCE,
            None,
            content_name,
            content_meta,
            content_spec,
            status=content_status,
        )

        snap_status = dict(snapshot.status or {})
        snap_status["readyToUse"] = ready
        snap_status["boundVolumeSnapshotContentName"] = content_name
        effective_restore = content_restore_size or restore_size
        if effective_restore:
            snap_status["restoreSize"] = str(effective_restore)
        self._store.upsert(
            SNAP_GROUP,
            SNAP_VERSION,
            SNAPSHOT_RESOURCE,
            snap_ns,
            snapshot.name,
            snapshot.metadata,
            snapshot.spec,
            status=snap_status,
        )
        if ready:
            self._record_snapshot_event(
                snapshot,
                "SnapshotReady",
                f"snapshot ready via content {content_name}",
            )

    def _handle_snapshot_deleted(self, snapshot) -> None:
        status = snapshot.status or {}
        content_name = status.get("boundVolumeSnapshotContentName")
        if not content_name:
            return
        content = self._store.get(
            SNAP_GROUP,
            SNAP_VERSION,
            SNAPCONTENT_RESOURCE,
            None,
            content_name,
        )
        if content is None:
            return
        deletion_policy = str((content.spec or {}).get("deletionPolicy") or "Retain")
        if deletion_policy != "Delete":
            return
        self._delete_snapshot_content(content)

    def _snapshot_driver_and_policy(self, snapshot, pv) -> tuple[str, str]:
        spec = snapshot.spec or {}
        class_name = spec.get("volumeSnapshotClassName")
        if class_name:
            snap_class = self._store.get(
                SNAP_GROUP, SNAP_VERSION, SNAPCLASS_RESOURCE, None, class_name
            )
        else:
            snap_class = None
        class_spec = snap_class.spec if snap_class is not None else {}
        class_spec = class_spec if isinstance(class_spec, dict) else {}
        driver = str(class_spec.get("driver") or "")
        if not driver:
            pv_spec = pv.spec or {}
            csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
            if isinstance(csi, dict) and csi.get("driver"):
                driver = str(csi.get("driver"))
            elif isinstance(pv_spec.get("nfs"), dict):
                driver = NFS_PROVISIONER
            else:
                annotations = (pv.metadata or {}).get("annotations", {})
                if isinstance(annotations, dict):
                    driver = str(annotations.get(STORAGE_PROVISIONER_ANNOTATION) or "")
        deletion_policy = str(class_spec.get("deletionPolicy") or "Retain")
        return driver or "k1s.io/unknown", deletion_policy

    def _default_snapshot_class_name(self) -> str | None:
        if self._default_snapshot_class:
            return self._default_snapshot_class
        try:
            classes = self._store.list_all(SNAP_GROUP, SNAP_VERSION, SNAPCLASS_RESOURCE)
        except Exception:
            return None
        for snap_class in classes:
            meta = getattr(snap_class, "metadata", None)
            annotations = meta.get("annotations") if isinstance(meta, dict) else {}
            if not isinstance(annotations, dict):
                continue
            for key in SNAP_DEFAULT_CLASS_ANNOTATIONS:
                raw = annotations.get(key)
                if raw is not None and str(raw).lower() in {"true", "1", "yes"}:
                    self._default_snapshot_class = snap_class.name
                    return self._default_snapshot_class
        if classes:
            self._default_snapshot_class = classes[0].name
            return self._default_snapshot_class
        return None

    def _snapshot_content_backing(self, content) -> tuple[Path, Path] | None:
        if content is None:
            return None
        meta = content.metadata or {}
        annotations = meta.get("annotations") if isinstance(meta, dict) else {}
        if not isinstance(annotations, dict):
            return None
        host_root = annotations.get(SNAP_HOST_ROOT_ANNOTATION)
        host_path = annotations.get(SNAP_HOST_PATH_ANNOTATION)
        if not host_root or not host_path:
            return None
        root = Path(str(host_root)).expanduser()
        path = Path(str(host_path)).expanduser()
        if not self._within_root(root, path):
            return None
        return root, path

    def _pv_host_backing(self, pv) -> tuple[Path, Path] | None:
        meta = pv.metadata or {}
        annotations = meta.get("annotations") if isinstance(meta, dict) else {}
        if not isinstance(annotations, dict):
            return None
        host_root = annotations.get(NFS_HOST_ROOT_ANNOTATION) or annotations.get(
            LOCAL_HOST_ROOT_ANNOTATION
        )
        host_path = annotations.get(NFS_HOST_PATH_ANNOTATION) or annotations.get(
            LOCAL_HOST_PATH_ANNOTATION
        )
        if not host_root or not host_path:
            return None
        root = Path(str(host_root)).expanduser()
        path = Path(str(host_path)).expanduser()
        if not self._within_root(root, path):
            return None
        return root, path

    def _restore_snapshot_into(
        self, pvc, target_root: Path, target_path: Path, snapshot_info: dict[str, Any]
    ) -> bool:
        source_root = snapshot_info.get("host_root")
        source_path = snapshot_info.get("host_path")
        if not isinstance(source_root, Path) or not isinstance(source_path, Path):
            self._record_pvc_event(
                pvc,
                "SnapshotRestoreFailed",
                "snapshot backing path unavailable",
            )
            return False
        if not self._within_root(source_root, source_path):
            self._record_pvc_event(
                pvc,
                "SnapshotRestoreFailed",
                f"snapshot path outside source root: {source_path}",
            )
            return False
        if not self._within_root(target_root, target_path):
            self._record_pvc_event(
                pvc,
                "SnapshotRestoreFailed",
                f"target path outside host root: {target_path}",
            )
            return False
        if not source_path.exists():
            self._record_pvc_event(
                pvc,
                "SnapshotRestoreFailed",
                f"snapshot backing path missing: {source_path}",
            )
            return False
        if not self._reset_dir(target_path, root=target_root):
            self._record_pvc_event(pvc, "SnapshotRestoreFailed", f"failed to prepare {target_path}")
            return False
        if not self._copy_tree_contents(source_path, target_path):
            self._record_pvc_event(
                pvc,
                "SnapshotRestoreFailed",
                f"failed to copy data from snapshot {snapshot_info.get('snapshot_ref')}",
            )
            return False
        self._record_pvc_event(
            pvc,
            "SnapshotRestored",
            f"restored from snapshot {snapshot_info.get('snapshot_ref')}",
        )
        return True

    def _delete_snapshot_content(self, content) -> None:
        self._cleanup_snapshot_backing(content)
        with suppress(Exception):
            self._store.delete(SNAP_GROUP, SNAP_VERSION, SNAPCONTENT_RESOURCE, None, content.name)

    def _cleanup_snapshot_backing(self, content) -> None:
        backing = self._snapshot_content_backing(content)
        if backing is None:
            return
        root, path = backing
        if not path.exists():
            return
        if not self._within_root(root, path):
            LOGGER.warning("skipping snapshot cleanup outside root: %s (root=%s)", path, root)
            return
        try:
            shutil.rmtree(path)
        except Exception:
            LOGGER.exception("failed to clean snapshot backing path %s", path)

    def _reset_dir(self, path: Path, *, root: Path) -> bool:
        if not self._within_root(root, path):
            return False
        try:
            path.mkdir(parents=True, exist_ok=True)
            for child in path.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)  # type: ignore[call-arg]
        except Exception:
            LOGGER.exception("failed to reset directory %s", path)
            return False
        return True

    @staticmethod
    def _copy_tree_contents(source: Path, dest: Path) -> bool:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for child in source.iterdir():
                target = dest / child.name
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(child, target)
        except Exception:
            return False
        return True

    def _snapshot_content_name(self, snapshot, pv) -> str:
        snap_uid = self._snapshot_uid(snapshot)
        raw = f"vsc-{pv.name}-{snap_uid[:8]}"
        return self._sanitize_name(raw)

    def _snapshot_uid(self, snapshot) -> str:
        meta = snapshot.metadata or {}
        uid = meta.get("uid") if isinstance(meta, dict) else None
        if uid:
            return str(uid)
        token = uuid.uuid5(uuid.NAMESPACE_DNS, f"{snapshot.namespace}:{snapshot.name}")
        return token.hex

    def _record_snapshot_event(self, snapshot, reason: str, message: str) -> None:
        snap_ns = snapshot.namespace or ""
        snap_name = snapshot.name
        if not snap_ns or not snap_name:
            return
        now = datetime.now(UTC)
        ts = now.isoformat().replace("+00:00", "Z")
        name = f"{snap_name}.{int(time.time())}.{uuid.uuid4().hex[:6]}"
        involved = {"kind": "VolumeSnapshot", "name": snap_name, "namespace": snap_ns}
        uid = (snapshot.metadata or {}).get("uid") if isinstance(snapshot.metadata, dict) else None
        if uid:
            involved["uid"] = str(uid)
        spec = {
            "involvedObject": involved,
            "reason": str(reason),
            "message": str(message),
            "type": "Normal",
            "firstTimestamp": ts,
            "lastTimestamp": ts,
            "eventTime": ts,
            "count": 1,
            "source": {"component": "storage-controller"},
        }
        metadata = {"name": name, "namespace": snap_ns}
        with suppress(Exception):
            self._store.upsert(
                CORE_GROUP,
                CORE_VERSION,
                EVENT_RESOURCE,
                snap_ns,
                name,
                metadata,
                spec,
                status={},
            )

    def _provision_nfs(self, pvc, sc, *, snapshot_info: dict[str, Any] | None = None):
        volume_mode = (pvc.spec or {}).get("volumeMode")
        if str(volume_mode or "").lower() == "block":
            self._record_pvc_event(
                pvc,
                "VolumeModeUnsupported",
                "block volumeMode is not supported for NFS provisioner",
            )
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
        host_path: Path | None = None
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

        if snapshot_info and host_root is not None and host_path is not None:
            if not self._restore_snapshot_into(pvc, host_root, host_path, snapshot_info):
                return None
            annotations[SNAPSHOT_SOURCE_ANNOTATION] = snapshot_info.get("snapshot_ref", "")
        elif snapshot_info and host_root is None:
            self._record_pvc_event(
                pvc,
                "SnapshotRestoreSkipped",
                "snapshot restore requires parameters.hostPath for NFS provisioner",
            )

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

    def _provision_local_path(
        self,
        pvc,
        sc,
        *,
        selected_node: str | None,
        snapshot_info: dict[str, Any] | None = None,
    ):
        sc_spec = sc.spec or {}
        binding_mode = str(sc_spec.get("volumeBindingMode") or "")
        if binding_mode == WAIT_FOR_FIRST_CONSUMER and not selected_node:
            return None
        volume_mode = str((pvc.spec or {}).get("volumeMode") or "Filesystem")
        is_block = volume_mode.lower() == "block"
        if is_block:
            requested_raw = self._pvc_requested_storage(pvc)
            requested_bytes = self._quantity_bytes(requested_raw)
            if not requested_raw or requested_bytes is None or requested_bytes <= 0:
                self._record_pvc_event(
                    pvc,
                    "ProvisioningFailed",
                    "block volume requires a valid storage request",
                )
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
        if is_block:
            host_path.parent.mkdir(parents=True, exist_ok=True)
            if host_path.exists() and host_path.is_dir():
                self._record_pvc_event(
                    pvc,
                    "ProvisioningFailed",
                    f"block volume path exists as directory: {host_path}",
                )
                return None
            try:
                with open(host_path, "ab") as handle:
                    handle.truncate(requested_bytes)
            except Exception:
                self._record_pvc_event(
                    pvc,
                    "ProvisioningFailed",
                    f"failed to create block backing file: {host_path}",
                )
                return None
        else:
            host_path.mkdir(parents=True, exist_ok=True)

        if snapshot_info:
            if is_block:
                self._record_pvc_event(
                    pvc,
                    "SnapshotRestoreUnsupported",
                    "snapshot restore is not supported for block volumes",
                )
                return None
            if not self._restore_snapshot_into(pvc, host_root, host_path, snapshot_info):
                return None

        annotations = {
            PROVISIONED_BY_ANNOTATION: LOCAL_PATH_PROVISIONER,
            STORAGE_PROVISIONER_ANNOTATION: LOCAL_PATH_PROVISIONER,
            LOCAL_HOST_ROOT_ANNOTATION: str(host_root),
            LOCAL_HOST_PATH_ANNOTATION: str(host_path),
        }
        if snapshot_info:
            annotations[SNAPSHOT_SOURCE_ANNOTATION] = snapshot_info.get("snapshot_ref", "")
        pv_spec = {
            "capacity": self._pvc_requested_capacity(pvc),
            "accessModes": list((pvc.spec or {}).get("accessModes") or []),
            "volumeMode": volume_mode,
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
        if path.is_file() or path.is_symlink():
            with suppress(Exception):
                path.unlink(missing_ok=True)  # type: ignore[call-arg]
            return
        if not path.is_dir():
            LOGGER.warning("skipping cleanup for non-directory path: %s", path)
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
