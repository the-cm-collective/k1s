"""Storage state interfaces and in-memory implementation."""

from __future__ import annotations

import base64
import binascii
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Protocol

from ae._utc import UTC

from .types import NetFSMount, PvcRef, PvRef

CORE_GROUP = ""
CORE_VERSION = "v1"
PVC_RESOURCE = "persistentvolumeclaims"
PV_RESOURCE = "persistentvolumes"
SC_GROUP = "storage.k8s.io"
SC_VERSION = "v1"
SC_RESOURCE = "storageclasses"
VA_RESOURCE = "volumeattachments"
CSIDRIVER_RESOURCE = "csidrivers"
SECRETS_RESOURCE = "secrets"  # noqa: S105 - Kubernetes resource plural


class StorageState(Protocol):
    """Backend for tracking PVC/PV bindings and node mount records."""

    def get_pv_for_pvc(self, pvc: PvcRef) -> PvRef | None: ...

    def get_pv(self, pv: PvRef) -> Any | None: ...

    def get_storage_class(self, name: str) -> Any | None: ...

    def record_pvc_event(self, pvc: PvcRef, reason: str, message: str) -> None: ...

    def bind_pvc(self, pvc: PvcRef, pv: PvRef) -> None: ...

    def unbind_pvc(self, pvc: PvcRef) -> None: ...

    def get_mount(self, pvc: PvcRef, node_id: str) -> NetFSMount | None: ...

    def upsert_mount(self, mount: NetFSMount) -> None: ...

    def delete_mount(self, pvc: PvcRef, node_id: str) -> None: ...

    def list_mounts(self, node_id: str | None = None) -> list[NetFSMount]: ...

    def get_volume_attachment(self, pv: PvRef, node_id: str) -> Any | None: ...

    def get_csi_driver(self, name: str) -> Any | None: ...

    def get_secret(self, namespace: str, name: str) -> dict[str, str] | None: ...


class InMemoryStorageState:
    """Simple in-memory storage state for early NetFS scaffolding."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pvc_bindings: dict[tuple[str, str], PvRef] = {}
        self._mounts: dict[tuple[str, str, str], NetFSMount] = {}

    def get_pv_for_pvc(self, pvc: PvcRef) -> PvRef | None:
        with self._lock:
            return self._pvc_bindings.get(pvc.key())

    def get_pv(self, pv: PvRef) -> Any | None:
        _ = pv
        return None

    def get_storage_class(self, name: str) -> Any | None:
        _ = name
        return None

    def record_pvc_event(self, pvc: PvcRef, reason: str, message: str) -> None:
        _ = (pvc, reason, message)
        return None

    def bind_pvc(self, pvc: PvcRef, pv: PvRef) -> None:
        with self._lock:
            self._pvc_bindings[pvc.key()] = pv

    def unbind_pvc(self, pvc: PvcRef) -> None:
        with self._lock:
            self._pvc_bindings.pop(pvc.key(), None)

    def get_mount(self, pvc: PvcRef, node_id: str) -> NetFSMount | None:
        key = (pvc.namespace, pvc.name, node_id)
        with self._lock:
            return self._mounts.get(key)

    def upsert_mount(self, mount: NetFSMount) -> None:
        key = (mount.pvc.namespace, mount.pvc.name, mount.node_id)
        with self._lock:
            self._mounts[key] = mount

    def delete_mount(self, pvc: PvcRef, node_id: str) -> None:
        key = (pvc.namespace, pvc.name, node_id)
        with self._lock:
            self._mounts.pop(key, None)

    def list_mounts(self, node_id: str | None = None) -> list[NetFSMount]:
        with self._lock:
            mounts = list(self._mounts.values())
        if node_id is None:
            return sorted(mounts, key=lambda m: (m.node_id, m.pvc.namespace, m.pvc.name))
        return sorted(
            [m for m in mounts if m.node_id == node_id],
            key=lambda m: (m.pvc.namespace, m.pvc.name),
        )

    def get_volume_attachment(self, pv: PvRef, node_id: str) -> Any | None:
        _ = (pv, node_id)
        return None

    def get_csi_driver(self, name: str) -> Any | None:
        _ = name
        return None

    def get_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        _ = (namespace, name)
        return None


class ApishimStorageState(InMemoryStorageState):
    """Storage state backed by the apishim object store for PVC/PV lookups."""

    def __init__(self, store) -> None:
        super().__init__()
        self._store = store

    def get_pv_for_pvc(self, pvc: PvcRef) -> PvRef | None:
        obj = self._store.get(CORE_GROUP, CORE_VERSION, PVC_RESOURCE, pvc.namespace, pvc.name)
        if obj is None:
            return None
        spec = obj.spec or {}
        volume_name = spec.get("volumeName")
        if not volume_name:
            return None
        pv = self._store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, volume_name)
        if pv is None:
            return None
        driver = None
        try:
            if isinstance(pv.spec, dict):
                if isinstance(pv.spec.get("csi"), dict):
                    driver = pv.spec["csi"].get("driver")
                elif isinstance(pv.spec.get("nfs"), dict):
                    driver = "k1s.io/nfs"
        except Exception:
            driver = None
        uid = None
        try:
            uid = (pv.metadata or {}).get("uid")
        except Exception:
            uid = None
        return PvRef(name=str(volume_name), uid=uid, driver=driver)

    def get_pv(self, pv: PvRef) -> Any | None:
        return self._store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv.name)

    def get_storage_class(self, name: str) -> Any | None:
        if not name:
            return None
        return self._store.get(SC_GROUP, SC_VERSION, SC_RESOURCE, None, name)

    def get_volume_attachment(self, pv: PvRef, node_id: str) -> Any | None:
        try:
            attachments = self._store.list_all(SC_GROUP, SC_VERSION, VA_RESOURCE)
        except Exception:
            return None
        for att in attachments:
            spec = att.spec or {}
            if not isinstance(spec, dict):
                continue
            if spec.get("nodeName") != node_id:
                continue
            source = spec.get("source")
            if not isinstance(source, dict):
                continue
            if source.get("persistentVolumeName") == pv.name:
                return att
        return None

    def get_csi_driver(self, name: str) -> Any | None:
        if not name:
            return None
        try:
            return self._store.get(SC_GROUP, SC_VERSION, CSIDRIVER_RESOURCE, None, name)
        except Exception:
            return None

    def get_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        if not namespace or not name:
            return None
        try:
            secret = self._store.get(CORE_GROUP, CORE_VERSION, SECRETS_RESOURCE, namespace, name)
        except Exception:
            return None
        if secret is None:
            return None
        spec = secret.spec or {}
        if not isinstance(spec, dict):
            return None
        data = spec.get("data") if isinstance(spec.get("data"), dict) else spec
        if not isinstance(data, dict):
            return None
        decoded: dict[str, str] = {}
        for key, value in data.items():
            decoded[str(key)] = self._decode_secret_value(value)
        return decoded

    @staticmethod
    def _decode_secret_value(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            return str(value)
        raw = value.strip()
        if not raw:
            return ""
        try:
            payload = base64.b64decode(raw, validate=True)
            text = payload.decode("utf-8")
            # Only accept base64 decode when it round-trips.
            check = base64.b64encode(payload).decode("ascii").rstrip("=")
            if check == raw.rstrip("="):
                return text
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return raw
        return raw

    def record_pvc_event(self, pvc: PvcRef, reason: str, message: str) -> None:
        if not pvc.namespace or not pvc.name:
            return
        now = datetime.now(UTC)
        ts = now.isoformat().replace("+00:00", "Z")
        name = f"{pvc.name}.{int(time.time())}.{uuid.uuid4().hex[:6]}"
        involved = {"kind": "PersistentVolumeClaim", "name": pvc.name, "namespace": pvc.namespace}
        if pvc.uid:
            involved["uid"] = pvc.uid
        spec = {
            "involvedObject": involved,
            "reason": str(reason),
            "message": str(message),
            "type": "Warning",
            "firstTimestamp": ts,
            "lastTimestamp": ts,
            "eventTime": ts,
            "count": 1,
            "source": {"component": "netfs"},
        }
        metadata = {"name": name, "namespace": pvc.namespace}
        self._store.upsert("", "v1", "events", pvc.namespace, name, metadata, spec, status={})
