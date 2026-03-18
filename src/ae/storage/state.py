"""Storage state interfaces and in-memory implementation."""

from __future__ import annotations

import base64
import binascii
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import requests

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

    def get_service_account(self, namespace: str, name: str) -> dict[str, Any] | None: ...


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

    def get_service_account(self, namespace: str, name: str) -> dict[str, Any] | None:
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

    def get_service_account(self, namespace: str, name: str) -> dict[str, Any] | None:
        if not namespace or not name:
            return None
        try:
            service_account = self._store.get(
                CORE_GROUP, CORE_VERSION, "serviceaccounts", namespace, name
            )
        except Exception:
            return None
        if service_account is None:
            return None
        spec = service_account.spec or {}
        return dict(spec) if isinstance(spec, dict) else None

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


class ApishimHttpStorageState(InMemoryStorageState):
    """Storage/passive-resource reads backed by the apishim HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        verify: bool | str = True,
        timeout_s: float = 5.0,
    ) -> None:
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self._token = token or None
        self._verify = verify
        self._timeout_s = float(timeout_s)

    @classmethod
    def from_env(cls) -> "ApishimHttpStorageState" | None:
        base = (os.getenv("AE_APISHIM_URL") or os.getenv("AE_APISHIM_SERVER") or "").strip()
        if not base:
            return None
        token = os.getenv("AE_APISHIM_READ_TOKEN") or os.getenv("AE_APISHIM_TOKEN")
        verify: bool | str = True
        ca_bundle = (
            os.getenv("AE_APISHIM_CA_BUNDLE")
            or os.getenv("AE_APISHIM_CA")
            or os.getenv("AE_APISHIM_TLS_CA")
        )
        if ca_bundle:
            verify = ca_bundle
        timeout_s = float(os.getenv("AE_APISHIM_HTTP_TIMEOUT_S", "5") or "5")
        return cls(base, token=token, verify=verify, timeout_s=timeout_s)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get_json(self, path: str) -> dict[str, Any] | None:
        resp = requests.get(
            f"{self._base_url}{path}",
            headers=self._headers(),
            timeout=self._timeout_s,
            verify=self._verify,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, dict) else None

    def _list_items(self, path: str) -> list[dict[str, Any]]:
        payload = self._get_json(path)
        if payload is None:
            return []
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def get_pv_for_pvc(self, pvc: PvcRef) -> PvRef | None:
        if not pvc.namespace or not pvc.name:
            return None
        payload = self._get_json(
            f"/api/v1/namespaces/{quote(pvc.namespace, safe='')}/persistentvolumeclaims/{quote(pvc.name, safe='')}"
        )
        if payload is None:
            return None
        spec = payload.get("spec")
        if not isinstance(spec, dict):
            return None
        volume_name = spec.get("volumeName")
        if not volume_name:
            return None
        pv = self.get_pv(PvRef(name=str(volume_name)))
        if pv is None:
            return None
        pv_spec = pv.get("spec") if isinstance(pv.get("spec"), dict) else {}
        driver = None
        try:
            if isinstance(pv_spec.get("csi"), dict):
                driver = pv_spec["csi"].get("driver")
            elif isinstance(pv_spec.get("nfs"), dict):
                driver = "k1s.io/nfs"
        except Exception:
            driver = None
        metadata = pv.get("metadata") if isinstance(pv.get("metadata"), dict) else {}
        uid = None
        if isinstance(metadata, dict):
            uid = metadata.get("uid")
        return PvRef(name=str(volume_name), uid=uid, driver=driver)

    def get_pv(self, pv: PvRef) -> Any | None:
        if not pv.name:
            return None
        return self._get_json(f"/api/v1/persistentvolumes/{quote(pv.name, safe='')}")

    def get_storage_class(self, name: str) -> Any | None:
        if not name:
            return None
        return self._get_json(f"/apis/storage.k8s.io/v1/storageclasses/{quote(name, safe='')}")

    def get_volume_attachment(self, pv: PvRef, node_id: str) -> Any | None:
        if not pv.name or not node_id:
            return None
        attachments = self._list_items("/apis/storage.k8s.io/v1/volumeattachments")
        for attachment in attachments:
            spec = attachment.get("spec")
            if not isinstance(spec, dict):
                continue
            if spec.get("nodeName") != node_id:
                continue
            source = spec.get("source")
            if not isinstance(source, dict):
                continue
            if source.get("persistentVolumeName") == pv.name:
                return attachment
        return None

    def get_csi_driver(self, name: str) -> Any | None:
        if not name:
            return None
        return self._get_json(f"/apis/storage.k8s.io/v1/csidrivers/{quote(name, safe='')}")

    def get_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        if not namespace or not name:
            return None
        payload = self._get_json(
            f"/api/v1/namespaces/{quote(namespace, safe='')}/secrets/{quote(name, safe='')}"
        )
        if payload is None:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        decoded: dict[str, str] = {}
        for key, value in data.items():
            decoded[str(key)] = ApishimStorageState._decode_secret_value(value)
        return decoded

    def get_service_account(self, namespace: str, name: str) -> dict[str, Any] | None:
        if not namespace or not name:
            return None
        payload = self._get_json(
            f"/api/v1/namespaces/{quote(namespace, safe='')}/serviceaccounts/{quote(name, safe='')}"
        )
        if payload is None:
            return None
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key in {"apiVersion", "kind", "metadata", "status"}:
                continue
            out[str(key)] = value
        return out
