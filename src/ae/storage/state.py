"""Storage state interfaces and in-memory implementation."""

from __future__ import annotations

import base64
import binascii
import json
import os
import ssl
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from .types import NetFSMount, PvcRef, PvRef

CORE_GROUP = ""
CORE_VERSION = "v1"
PVC_RESOURCE = "persistentvolumeclaims"
PV_RESOURCE = "persistentvolumes"
SC_GROUP = "storage.k8s.io"
SC_VERSION = "v1"
SC_RESOURCE = "storageclasses"
VA_RESOURCE = "volumeattachments"
SECRETS_RESOURCE = "secrets"  # noqa: S105 - Kubernetes resource plural
CONFIGMAPS_RESOURCE = "configmaps"


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

    def get_secret(self, namespace: str, name: str) -> dict[str, str] | None: ...

    def get_config_map(self, namespace: str, name: str) -> dict[str, str] | None: ...


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

    def get_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        _ = (namespace, name)
        return None

    def get_config_map(self, namespace: str, name: str) -> dict[str, str] | None:
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

    def get_config_map(self, namespace: str, name: str) -> dict[str, str] | None:
        if not namespace or not name:
            return None
        try:
            cfg = self._store.get(
                CORE_GROUP, CORE_VERSION, CONFIGMAPS_RESOURCE, namespace, name
            )
        except Exception:
            return None
        if cfg is None:
            return None
        spec = cfg.spec or {}
        if not isinstance(spec, dict):
            return None
        data = spec.get("data") if isinstance(spec.get("data"), dict) else spec
        if not isinstance(data, dict):
            return None
        out: dict[str, str] = {}
        for key, value in data.items():
            out[str(key)] = "" if value is None else str(value)
        return out

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
    """Storage state backed by the apishim HTTP API (read-only for objects)."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 3.0,
        verify: bool | str = True,
    ) -> None:
        super().__init__()
        base = (base_url or "").strip()
        if base and "://" not in base:
            base = "http://" + base
        self._base = base.rstrip("/")
        self._token = token
        self._timeout = float(timeout) if timeout else 3.0
        self._verify = verify

    @classmethod
    def from_env(cls) -> "ApishimHttpStorageState | None":
        base = os.getenv("AE_APISHIM_URL") or os.getenv("AE_APISHIM_SERVER") or ""
        base = base.strip()
        if not base:
            return None
        token = os.getenv("AE_APISHIM_READ_TOKEN") or os.getenv("AE_APISHIM_TOKEN")
        if not token:
            token = os.getenv("AE_APISHIM_EXEC_TOKEN")
        if not token:
            token = os.getenv("AE_APISHIM_PORTFORWARD_TOKEN")
        if not token:
            token = os.getenv("AE_API_READ_TOKEN") or os.getenv("AE_API_ADMIN_TOKEN")
        insecure = os.getenv("AE_APISHIM_INSECURE") == "1"
        ca_path = (
            os.getenv("AE_APISHIM_CA_BUNDLE")
            or os.getenv("AE_APISHIM_CA")
            or os.getenv("AE_APISHIM_TLS_CA")
            or ""
        ).strip()
        verify: bool | str
        if insecure:
            verify = False
        elif ca_path:
            verify = ca_path if Path(ca_path).exists() else True
        else:
            verify = True
        raw_timeout = os.getenv("AE_APISHIM_HTTP_TIMEOUT") or os.getenv("AE_APISHIM_TIMEOUT") or ""
        try:
            timeout = float(raw_timeout) if raw_timeout else 3.0
        except Exception:
            timeout = 3.0
        return cls(base, token=token, timeout=timeout, verify=verify)

    def _request_json(self, path: str) -> dict | None:
        if not self._base:
            return None
        url = f"{self._base}{path}"
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            import requests as _req

            resp = _req.get(url, headers=headers, timeout=self._timeout, verify=self._verify)
            if resp.status_code >= 400:
                return None
            if not resp.content:
                return {}
            return resp.json()
        except Exception:
            pass
        ctx = None
        if self._verify is False:
            ctx = ssl._create_unverified_context()  # noqa: S323
        elif isinstance(self._verify, str):
            ctx = ssl.create_default_context(cafile=self._verify)
        try:
            req = urllib.request.Request(url, headers=headers)  # noqa: S310
            with urllib.request.urlopen(  # noqa: S310
                req, timeout=self._timeout, context=ctx
            ) as resp:
                if getattr(resp, "status", 200) >= 400:
                    return None
                body = resp.read()
                if not body:
                    return {}
                return json.loads(body)
        except Exception:
            return None

    @staticmethod
    def _name_path(value: str | None) -> str:
        return quote(str(value or ""), safe="") if value else ""

    def _get_obj(self, path: str) -> dict | None:
        obj = self._request_json(path)
        if not isinstance(obj, dict):
            return None
        return obj

    def get_pv_for_pvc(self, pvc: PvcRef) -> PvRef | None:
        if not pvc.namespace or not pvc.name:
            return None
        ns = self._name_path(pvc.namespace)
        name = self._name_path(pvc.name)
        pvc_obj = self._get_obj(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
        if not pvc_obj:
            return None
        spec = pvc_obj.get("spec") if isinstance(pvc_obj.get("spec"), dict) else {}
        volume_name = spec.get("volumeName")
        if not volume_name:
            return None
        pv_name = self._name_path(str(volume_name))
        pv_obj = self._get_obj(f"/api/v1/persistentvolumes/{pv_name}")
        if not pv_obj:
            return None
        pv_spec = pv_obj.get("spec") if isinstance(pv_obj.get("spec"), dict) else {}
        driver = None
        try:
            if isinstance(pv_spec.get("csi"), dict):
                driver = pv_spec["csi"].get("driver")
            elif isinstance(pv_spec.get("nfs"), dict):
                driver = "k1s.io/nfs"
        except Exception:
            driver = None
        uid = None
        try:
            meta = pv_obj.get("metadata") or {}
            uid = meta.get("uid")
        except Exception:
            uid = None
        return PvRef(name=str(volume_name), uid=uid, driver=driver)

    def get_pv(self, pv: PvRef) -> Any | None:
        if not pv.name:
            return None
        name = self._name_path(pv.name)
        return self._get_obj(f"/api/v1/persistentvolumes/{name}")

    def get_storage_class(self, name: str) -> Any | None:
        if not name:
            return None
        sc = self._name_path(name)
        return self._get_obj(f"/apis/storage.k8s.io/v1/storageclasses/{sc}")

    def get_volume_attachment(self, pv: PvRef, node_id: str) -> Any | None:
        if not pv.name or not node_id:
            return None
        payload = self._get_obj("/apis/storage.k8s.io/v1/volumeattachments")
        items = []
        if isinstance(payload, dict):
            items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for att in items:
            if not isinstance(att, dict):
                continue
            spec = att.get("spec")
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

    def get_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        if not namespace or not name:
            return None
        ns = self._name_path(namespace)
        sec_name = self._name_path(name)
        secret = self._get_obj(f"/api/v1/namespaces/{ns}/secrets/{sec_name}")
        if not secret:
            return None
        data = None
        if isinstance(secret.get("data"), dict):
            data = secret.get("data")
        elif isinstance(secret.get("spec"), dict):
            data = secret.get("spec")
        if not isinstance(data, dict):
            return None
        decoded: dict[str, str] = {}
        for key, value in data.items():
            decoded[str(key)] = self._decode_secret_value(value)
        return decoded

    def get_config_map(self, namespace: str, name: str) -> dict[str, str] | None:
        if not namespace or not name:
            return None
        ns = self._name_path(namespace)
        cfg_name = self._name_path(name)
        cfg = self._get_obj(f"/api/v1/namespaces/{ns}/configmaps/{cfg_name}")
        if not cfg:
            return None
        data = None
        if isinstance(cfg.get("data"), dict):
            data = cfg.get("data")
        elif isinstance(cfg.get("spec"), dict):
            data = cfg.get("spec")
        if not isinstance(data, dict):
            return None
        out: dict[str, str] = {}
        for key, value in data.items():
            out[str(key)] = "" if value is None else str(value)
        return out

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
            check = base64.b64encode(payload).decode("ascii").rstrip("=")
            if check == raw.rstrip("="):
                return text
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return raw
        return raw

    def record_pvc_event(self, pvc: PvcRef, reason: str, message: str) -> None:
        _ = (pvc, reason, message)
        # apishim events are read-only over HTTP; keep this as a best-effort no-op.
        return None
